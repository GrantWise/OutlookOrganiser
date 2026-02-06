# Outlook AI Assistant Ã¢â‚¬â€ Microsoft Graph API Integration

> **Parent doc:** `01-overview.md` | **Read when:** Implementing or debugging Microsoft Graph API integration, email operations, folder management, or authentication.

---

## 1. Key Endpoints

| Operation | Endpoint | Method |
|-----------|----------|--------|
| List inbox messages | `/me/mailFolders/inbox/messages` | GET |
| List sent messages | `/me/mailFolders/sentitems/messages` | GET |
| Get message details | `/me/messages/{id}` | GET |
| Move message to folder | `/me/messages/{id}/move` | POST |
| Create mail folder | `/me/mailFolders` | POST |
| Create subfolder | `/me/mailFolders/{id}/childFolders` | POST |
| Set categories | `/me/messages/{id}` (PATCH) | PATCH |
| List folders | `/me/mailFolders?$expand=childFolders` | GET |
| Search messages | `/me/messages?$search=""` | GET |
| Get user profile | `/me` | GET |
| Delta query (inbox) | `/me/mailFolders/inbox/messages/delta` | GET |
| List thread messages | `/me/messages?$filter=conversationId eq '{id}'` | GET |

---

## 2. Required Select Fields

When fetching messages, always request these fields to avoid additional API calls:

```
$select=id,conversationId,conversationIndex,subject,from,receivedDateTime,
        bodyPreview,parentFolderId,categories,webLink,flag,isRead,importance
```

**Field notes:**
- `webLink`: Provides the OWA deep link URL stored in the emails table (see `03-agent-behaviors.md` Section 8).
- `conversationIndex`: Base64-encoded binary indicating thread position and depth. The first 22 bytes (decoded) identify the thread root; each additional 5 bytes represent a reply level. Use decoded length to determine thread depth: `depth = (len(base64.b64decode(conversationIndex)) - 22) / 5`. A depth of 0 means this is the first message in the thread.
- `importance`: Sender-set message importance (`low`, `normal`, `high`). Useful as a classification signal for priority.
- `flag`: Contains `flagStatus` (`notFlagged`, `flagged`, `complete`). If the user has manually flagged an email, it likely needs action.
- `isRead`: Whether the user has read the email. Combined with age, an unread email from 3 days ago is a stronger signal for "Needs Reply" than a read one.

---

## 3. Pagination

Graph API returns max 50 messages per page. The agent must handle `@odata.nextLink` for pagination during bootstrap scanning.

---

## 4. Rate Limits

Microsoft Graph API limits:
- 10,000 requests per 10 minutes per app per mailbox
- The agent should implement exponential backoff on 429 responses
- Bootstrap scanner should pace requests (e.g., 100ms delay between pages)

---

## 5. Reply State Detection

To determine if the user has replied to a conversation thread:

```
GET /me/mailFolders/sentitems/messages
    ?$filter=conversationId eq '{conversation_id}'
    &$orderby=receivedDateTime desc
    &$top=1
    &$select=receivedDateTime
```

**Optimization:** On each triage cycle, batch-fetch recent sent items (last N hours) and cache conversation IDs locally. Then check the cache instead of making per-thread API calls. This reduces API calls from O(emails) to O(1) per cycle.

---

## 6. Thread Context Queries

To fetch recent messages in a conversation thread for classification context:

```
GET /me/messages
    ?$filter=conversationId eq '{conversation_id}'
    &$orderby=receivedDateTime desc
    &$top=4
    &$select=id,subject,from,receivedDateTime,bodyPreview
```

This returns the most recent 4 messages in the thread (the current email plus up to 3 prior messages). The current email is excluded client-side. Prior message `bodyPreview` values are cleaned through the same snippet pipeline but truncated to 500 characters each (shorter than the primary email snippet) to keep prompt size manageable.

**Optimization:** Before calling the Graph API, check the local `emails` table for messages with the same `conversation_id`. If prior messages are already stored locally (with their cleaned snippets), use those instead of fetching from the API. Only query Graph API for thread messages not yet in the local database.

**Cost:** Adds 1 Graph API call per email requiring Claude classification. Since thread inheritance (see `03-agent-behaviors.md`) handles ~60-70% of thread replies without any API call, the net impact is modest.

---

## 7. Delta Queries (Optimization)

For ongoing triage, the agent should use delta queries where possible:
- `GET /me/mailFolders/inbox/messages/delta`
- This returns only changes since last sync, reducing API calls significantly
- Store the delta token in the `agent_state` SQLite table between runs

### Delta Query Error Handling

> **Ref:** Microsoft Graph delta query documentation:
> https://learn.microsoft.com/en-us/graph/delta-query-messages
> Community reports confirm delta tokens can silently expire, items appear in unexpected
> order, and the same item can appear multiple times across @odata.nextLink pages.

**410 Gone handling:** Delta tokens expire after an unspecified period (typically days to weeks). When the API returns `410 Gone`:
1. Log WARNING: "Delta token expired. Performing full sync."
2. Delete the stored delta token from `agent_state`
3. Fall back to timestamp-based polling for this cycle: `receivedDateTime > last_processed_timestamp`
4. On the next cycle, initiate a fresh delta query (no token = full initial sync)
5. Store the new delta token from the `@odata.deltaLink` response

**Deduplication:** Delta query results can include the same message multiple times (e.g., when a message is modified during pagination). Before processing:
1. Collect all message IDs from the delta response pages
2. Deduplicate by `id` before processing
3. For messages already in the `emails` table, check if `current_folder` changed (message was moved) — update the folder field but do not re-classify

**Pagination:** Follow `@odata.nextLink` until `@odata.deltaLink` is returned. Important: changes can occur while following nextLinks. When the first deltaLink is returned, follow it once to check for additional changes before storing it.

> **Ref:** Microsoft Graph best practices for delta queries:
> https://learn.microsoft.com/en-us/graph/best-practices-concept#use-delta-query-to-track-changes

---

## 8. Webhook + Delta Query Hybrid (Phase 2)

For Phase 2, combine Microsoft Graph webhooks with delta queries for near-real-time processing:

1. **Subscribe to inbox changes:** `POST /subscriptions` with `changeType: "created,updated"` and `resource: "/me/mailFolders/inbox/messages"`
2. **Webhook fires** when new email arrives → triggers a delta query cycle immediately
3. **Delta query** returns only the new/changed messages (same logic as Section 7)
4. **APScheduler still runs** as a fallback safety net (catches anything webhooks miss, e.g., during subscription renewal gaps)

This reduces latency from "up to 15 minutes" (polling interval) to "seconds" while maintaining the reliability of periodic polling.

> **Ref:** Microsoft Graph change notifications documentation:
> https://learn.microsoft.com/en-us/graph/change-notifications-overview
> Microsoft recommends combining webhooks with delta queries for best results.

**Webhook subscription management:**
- Subscriptions expire after max 3 days (Outlook messages). The agent must renew before expiry.
- Store subscription ID and expiry in `agent_state`
- On startup, check if subscription exists and is valid; create/renew if needed
- Handle webhook validation requests (Microsoft sends a validation token on subscription creation)

---

## 9. Authentication

## 8. Authentication

The agent uses Microsoft's **device code flow** for OAuth2 authentication via MSAL.

Required Microsoft Graph API permissions (delegated):
- `Mail.ReadWrite` Ã¢â‚¬â€ read email (Inbox + Sent Items) and move between folders
- `Mail.Send` Ã¢â‚¬â€ future: send digest emails
- `MailboxSettings.Read` Ã¢â‚¬â€ read user timezone, auto-replies status
- `User.Read` Ã¢â‚¬â€ read basic user profile (name, email) for identity auto-detection

For full Azure AD setup instructions, MSAL implementation reference, and troubleshooting, see `07-setup-guide.md`.
