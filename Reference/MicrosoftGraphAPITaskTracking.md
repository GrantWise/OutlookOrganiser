# Microsoft Graph API can replace most of your custom tracking

**Your SQLite-based waiting-for tracker, follow-up system, and task delegation tracking could be largely replaced by native Microsoft 365 features — and doing so gives your CEO cross-device visibility, push notifications, and deep Outlook integration for free.** The Graph API's To Do tasks with `linkedResources`, email `followUpFlag`, and Calendar scheduling endpoints provide a rich, production-ready foundation. The optimal architecture is a hybrid: use native Microsoft To Do as the user-facing system of record, keep SQLite as a lightweight AI metadata cache, and leverage email flags and calendar blocks for immediate CEO visibility.

This report covers every relevant Graph API surface — endpoints, data models, JSON examples, permissions, and practical implementation patterns — organized around what matters most for your CEO AI assistant.

---

## To Do tasks with linked resources are the centerpiece

The Microsoft To Do API (`/me/todo/lists/{listId}/tasks`) is the single most valuable addition for your assistant. Its **`linkedResource` entity was explicitly designed to link tasks back to source emails**, making it the native replacement for your custom follow-up and waiting-for tracking.

**Core endpoints** (all under `https://graph.microsoft.com/v1.0`):

| Operation | Endpoint |
|---|---|
| List task lists | `GET /me/todo/lists` |
| Create task list | `POST /me/todo/lists` |
| CRUD tasks | `GET/POST/PATCH/DELETE /me/todo/lists/{listId}/tasks` |
| CRUD linked resources | `GET/POST/PATCH/DELETE /me/todo/lists/{listId}/tasks/{taskId}/linkedResources` |
| CRUD checklist items | `GET/POST /me/todo/lists/{listId}/tasks/{taskId}/checklistItems` |
| Delta sync | `GET /me/todo/lists/{listId}/tasks/delta` |

The `todoTask` data model maps directly to your tracking needs. **The `status` field supports five values**: `notStarted`, `inProgress`, `completed`, `waitingOnOthers`, and `deferred` — that `waitingOnOthers` status is a near-perfect match for your waiting-for tracker. Combined with `importance` (`low`/`normal`/`high`), `dueDateTime`, `reminderDateTime`, `isReminderOn`, `categories`, `checklistItems` (sub-steps), and `recurrence`, you get a richer data model than most custom implementations.

**Creating a task linked to an email** is a single POST that includes the linkedResource inline:

```json
POST /me/todo/lists/{listId}/tasks
{
  "title": "Reply to John re: Q1 Board Deck",
  "importance": "high",
  "status": "waitingOnOthers",
  "dueDateTime": {
    "dateTime": "2026-02-14T17:00:00",
    "timeZone": "UTC"
  },
  "isReminderOn": true,
  "reminderDateTime": {
    "dateTime": "2026-02-13T09:00:00",
    "timeZone": "UTC"
  },
  "body": {
    "content": "From: john@partner.com — Review and approve the Q1 board deck by Friday.",
    "contentType": "text"
  },
  "categories": ["Waiting-For"],
  "linkedResources": [
    {
      "webUrl": "https://outlook.office365.com/owa/?ItemID=AAMkADhNmAAA%3D&exvsurl=1&viewmodel=ReadMessageItem",
      "applicationName": "CEO AI Assistant",
      "displayName": "Email from John: Q1 Board Deck Review",
      "externalId": "AAMkADhNmAAA="
    }
  ]
}
```

The `linkedResource` renders as a clickable link in the To Do task detail view across all platforms. The `webUrl` should come from the `webLink` property on the Graph API message resource (`GET /me/messages/{id}?$select=webLink`), which returns a properly formatted Outlook deep link. Store the Graph message `id` in `externalId` for programmatic cross-referencing. Use the `Prefer: IdType="ImmutableId"` header when fetching messages so IDs survive folder moves.

**Where tasks appear**: Tasks created via Graph API surface in **Microsoft To Do** (Windows, Mac, iOS, Android, web), **Outlook's My Day sidebar**, the **Teams "Planner" app** under personal task lists, and on **mobile with push notifications** for reminders. This is the single biggest advantage over SQLite — your CEO sees tasks everywhere without any custom UI.

**Critical permission limitation**: The To Do API supports only **delegated permissions** (`Tasks.ReadWrite`), not application permissions. This means the CEO must authenticate once and you store the refresh token. Since you already do this for `Mail.ReadWrite`, your existing auth flow is compatible.

---

## Email flags provide instant lightweight tracking

The `followUpFlag` property on messages is the simplest way to surface email-based reminders in the CEO's native Outlook experience. Flagging an email via the Graph API is functionally identical to flagging manually — **flagged emails automatically appear in To Do's "Flagged email" smart list** (if enabled in To Do settings).

**Flag structure** (the property on a message resource is called `flag`, typed as `followUpFlag`):

| Property | Type | Description |
|---|---|---|
| `flagStatus` | Enum | `notFlagged`, `flagged`, `complete` |
| `startDateTime` | dateTimeTimeZone | When follow-up begins |
| `dueDateTime` | dateTimeTimeZone | When follow-up is due |
| `completedDateTime` | dateTimeTimeZone | When completed |

**Key constraints**: You must provide both `startDateTime` and `dueDateTime` together (sending only `dueDateTime` returns `400 Bad Request`). There is **no dedicated reminder time property** on flags — Outlook generates reminders based on the user's default settings when `dueDateTime` is set. There is also **no `flagType` in Graph API** (the `followUp`/`reply`/`forward` distinction exists only in legacy EWS/COM). The Graph API simplifies to just status + dates.

**Practical PATCH examples**:

```json
// Flag with due date
PATCH /me/messages/{id}
{
  "flag": {
    "flagStatus": "flagged",
    "startDateTime": { "dateTime": "2026-02-10T08:00:00", "timeZone": "Eastern Standard Time" },
    "dueDateTime": { "dateTime": "2026-02-14T17:00:00", "timeZone": "Eastern Standard Time" }
  }
}

// Mark complete
PATCH /me/messages/{id}
{
  "flag": {
    "flagStatus": "complete",
    "completedDateTime": { "dateTime": "2026-02-12T14:30:00", "timeZone": "UTC" }
  }
}

// Query all flagged messages
GET /me/messages?$filter=flag/flagStatus eq 'flagged'
```

**The strategic choice between flags and To Do tasks**: Use flags for simple "come back to this email" tracking — it's one API call with no task list management. Use To Do tasks with `linkedResources` for richer tracking that needs custom titles, checklist items, status like `waitingOnOthers`, or AI-generated context in the body. The two approaches are complementary: flags feed the "Flagged email" list in To Do, while explicit tasks feed custom task lists. For your waiting-for tracker specifically, **To Do tasks with `status: "waitingOnOthers"` are the better fit** because they support richer metadata and the `waitingOnOthers` status semantics align perfectly.

---

## Categories are a unified system across the entire Microsoft 365 surface

Your existing use of categories for email classification is more powerful than you may realize. **Outlook categories sync across email, calendar events, contacts, To Do tasks, and group posts — they are the same master category list.** This means a category you apply to an email (like "Waiting-For" or "Urgent-Investor") can be applied to a To Do task created from that email, creating a consistent visual taxonomy across the CEO's apps.

The master category list is managed at `/me/outlook/masterCategories` with full CRUD. Categories have a `displayName` (immutable after creation) and a `color` from **25 preset values** (`preset0` through `preset24`, mapping to colors from Red to DarkCranberry). Applying a category to any resource uses the `categories` string array property with displayName matching:

```json
// Apply to a To Do task
PATCH /me/todo/lists/{listId}/tasks/{taskId}
{ "categories": ["Waiting-For", "Board-Related"] }

// Same categories work on calendar events
PATCH /me/events/{eventId}
{ "categories": ["Email Follow-up"] }
```

Categories on flagged emails sync bidirectionally between the email and its corresponding To Do task. **Permission**: `MailboxSettings.Read` for reading categories (you already have this), `MailboxSettings.ReadWrite` for creating new categories.

---

## Calendar API enables schedule-aware email management

The Calendar API adds two capabilities your assistant lacks: **schedule-aware timing** (suggesting when the CEO should process emails) and **time-blocking** (creating calendar events linked to specific emails).

**Reading the CEO's availability** uses `getSchedule` or `calendarView`:

```json
// Quick availability bitmap (30-min slots)
POST /me/calendar/getSchedule
{
  "schedules": ["ceo@company.com"],
  "startTime": { "dateTime": "2026-02-10T09:00:00", "timeZone": "Eastern Standard Time" },
  "endTime": { "dateTime": "2026-02-10T18:00:00", "timeZone": "Eastern Standard Time" },
  "availabilityViewInterval": 30
}
```

This returns an `availabilityView` string where each character represents a slot (`0`=free, `1`=tentative, `2`=busy, `3`=out-of-office). Your AI can use this to suggest optimal email processing windows in the daily digest or to time reminder delivery.

**Creating "time to respond" calendar blocks** with email deep links:

```json
POST /me/events
{
  "subject": "📧 Reply to investor re: Series B terms",
  "body": {
    "contentType": "HTML",
    "content": "<p>From: sarah@vcfirm.com</p><p><a href='https://outlook.office365.com/owa/?ItemID=AAMkADh...'>Open email in Outlook</a></p><p>Key ask: confirm valuation cap by EOD Friday.</p>"
  },
  "start": { "dateTime": "2026-02-10T14:00:00", "timeZone": "Eastern Standard Time" },
  "end": { "dateTime": "2026-02-10T14:30:00", "timeZone": "Eastern Standard Time" },
  "categories": ["Email Follow-up"],
  "isReminderOn": true,
  "reminderMinutesBeforeStart": 5,
  "showAs": "tentative"
}
```

The `findMeetingTimes` endpoint (`POST /me/findMeetingTimes`) can suggest free slots considering working hours, but only supports delegated permissions and requires `Calendars.Read.Shared` at minimum. For finding the CEO's own free time, `calendarView` with gap analysis in your code is simpler and more reliable.

**Permissions**: `Calendars.Read` for reading schedule/events (read-only is sufficient if you only suggest times), `Calendars.ReadWrite` if you want to create calendar blocks programmatically.

---

## Planner is overkill for a single-user assistant

Microsoft Planner provides rich team task management (plans → buckets → tasks with assignments, checklists, references, labels, priority) but **it requires either a Microsoft 365 Group or a Roster container**, adding unnecessary complexity for a single-user scenario. Planner tasks can link to emails via `plannerExternalReferences` on the `plannerTaskDetails` resource, but this is clunkier than To Do's `linkedResource` — reference URLs must be URL-encoded as property names with special character escaping.

Planner's value proposition is team visibility: task boards, assignments to multiple people, and organizational views. For a CEO's personal assistant, **To Do is the correct choice**. One relevant integration: Planner tasks assigned to the CEO from other systems automatically appear in their To Do "Assigned to me" list, so your assistant could read those to incorporate team-assigned work into the daily digest.

If the assistant eventually needs to create tasks for the CEO's direct reports (delegation tracking), Planner becomes relevant. The permission model for Planner includes `Tasks.ReadWrite` (delegated) and the recently added `Tasks.ReadWrite.All` (application, tenant-wide). Planner does **not** support webhooks — you would need to poll for changes.

---

## Recent Microsoft Graph additions worth tracking

**Copilot APIs (2025)**: Microsoft launched three API categories at Build 2025 under `graph.microsoft.com/beta/copilot`. The **Retrieval API** (`POST /beta/copilot/retrieval`) enables semantic search over Microsoft 365 data — potentially useful for grounding your AI in the CEO's SharePoint/OneDrive documents. The **Chat API** allows programmatic conversations with Copilot. Both require a Microsoft 365 Copilot license per user.

**Agent Identity APIs (November 2025, preview)**: Microsoft Entra now supports managing identities for AI agents with the same IAM capabilities as human users — agent registrations, conditional access, and risk detection. This is directly relevant if your assistant needs a formal identity in the tenant.

**Focused Inbox API** (already GA): The `inferenceClassification` property on messages (`Focused` vs `Other`) and the `/me/inferenceClassification/overrides` endpoint let you read and configure Microsoft's own email priority signals. You can use this as an input signal to your Claude-based classification — messages Microsoft marks `Focused` may correlate with CEO importance. Permission: covered by `Mail.ReadWrite`.

**No native AI classification/summarization API** exists beyond Focused Inbox. Your Claude-based classification and summarization remain the differentiator. Microsoft's "Prioritize My Inbox" (Copilot-powered, rolling out 2025) is a client-side feature with no API exposure.

**Webhooks for To Do tasks** are supported (subscribe to `/me/todo/lists/{listId}/tasks` for `created`, `updated`, `deleted` change types). This enables real-time sync: when the CEO completes a task in To Do, your system receives a notification and can update SQLite state or trigger downstream actions like sending a confirmation email.

---

## The permission set you need to add

Your current permissions (`Mail.ReadWrite`, `Mail.Send`, `MailboxSettings.Read`, `User.Read`) cover email operations and flagging. To leverage tasks and calendar, add exactly two delegated scopes:

| New Permission | Purpose |
|---|---|
| **`Tasks.ReadWrite`** | Create, read, update, delete To Do tasks, task lists, linked resources, checklist items |
| **`Calendars.Read`** | Read calendar events and free/busy schedule (upgrade to `Calendars.ReadWrite` only if creating calendar blocks) |

**Optional additions** depending on features you build:

| Permission | Purpose |
|---|---|
| `Calendars.ReadWrite` | Create "time to respond" calendar events |
| `MailboxSettings.ReadWrite` | Create new categories programmatically (you can read with your existing `MailboxSettings.Read`) |

All permissions should be **delegated** (not application), consistent with your existing auth flow. The CEO authenticates once, you store the refresh token with `offline_access` scope, and use it for ongoing operations.

---

## What to replace and what to keep in SQLite

The practical decision matrix for your current and planned features:

**Replace with native Microsoft features:**

Your **waiting-for tracker** maps to To Do tasks with `status: "waitingOnOthers"`, `dueDateTime`, and `linkedResources` pointing to the original email. Native advantages: CEO sees waiting-for items in To Do across all devices, gets push notification reminders, can mark complete from mobile. Your **follow-up flagging** maps to `followUpFlag` on messages for simple cases or To Do tasks for rich tracking. Both surface in the CEO's Outlook and To Do.

**Keep in SQLite but sync to native features (hybrid):**

Your **nudge/escalation thresholds** require custom time-based logic (e.g., "if no reply in 48 hours, escalate") that doesn't exist natively. Keep the escalation engine in SQLite, but write the resulting nudge tasks to To Do so the CEO sees them. Your **daily digest** aggregates data from multiple sources (email patterns, task status, calendar) — keep the generation logic in SQLite, but read task completion status from Graph API rather than tracking it independently. Your **email classification** with Claude has no native equivalent — keep it entirely custom, but write classification results as categories on messages and tasks for cross-app visibility.

**Keep entirely in SQLite:**

AI processing history, conversation context, Claude inference logs, analytics/metrics, and any custom scoring models. These are internal AI state that has no user-facing representation.

**The implementation pattern** is: Graph API is the system of record for user-facing state (tasks, flags, calendar), SQLite is the system of record for AI state (processing history, scores, escalation logic). Sync between them using webhooks (To Do task changes → SQLite) and your processing pipeline (AI decisions → Graph API writes). Use delta queries for periodic reconciliation. Use `Prefer: IdType="ImmutableId"` on all message fetches so your SQLite foreign keys to email IDs remain stable when the CEO moves messages between folders.

---

## Conclusion

The Microsoft Graph API provides a production-ready task management surface that directly replaces the user-facing components of your custom tracking system. **Three additions deliver the highest impact**: To Do tasks with `linkedResources` for rich follow-up and waiting-for tracking (visible everywhere the CEO works), email `followUpFlag` for lightweight flagging (one-line PATCH, instant Outlook integration), and `Calendars.Read` for schedule-aware email management. The total permission footprint increase is just two scopes: `Tasks.ReadWrite` and `Calendars.Read`.

The key architectural insight is that **native features and custom tracking are not mutually exclusive**. Your Claude-based intelligence — classification, prioritization, escalation logic, digest generation — remains entirely custom and is your core differentiator. But the output of that intelligence should be written to native Microsoft surfaces (To Do tasks, email flags, calendar blocks, categories) rather than trapped in SQLite where the CEO can't see it. This gives your assistant the best of both worlds: sophisticated AI reasoning backed by the CEO's familiar, cross-device Microsoft 365 experience.