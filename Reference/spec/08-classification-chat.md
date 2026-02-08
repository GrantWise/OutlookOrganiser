# Outlook AI Assistant -- Classification Chat Assistant

> **Parent doc:** `01-overview.md` | **Read when:** Implementing the contextual chat assistant for reclassification, config refinement, or rule creation from the Review UI.

---

## 1. Problem Statement

When the triage engine misclassifies an email or thread, the user currently has two options: correct the individual suggestion (one-off fix) or manually edit `config.yaml` to fix the underlying pattern (prevents future misclassifications). The first doesn't prevent recurrence. The second requires YAML knowledge, understanding the config schema, and is impractical for a CEO workflow.

**Example scenario:** An email about a new client engagement lands in `Areas/Sales` but should be a new project. The user needs to:
1. Reclassify this specific email
2. Create a new project entry in config with the right signals
3. Possibly create an auto-rule for the sender
4. Possibly reclassify other emails in the same thread

Today this requires editing YAML by hand. The classification chat assistant makes this conversational.

---

## 2. Design Principles

- **Scoped, not general-purpose.** The chat assistant is anchored to a specific email/suggestion. It is not a general inbox chatbot.
- **Conversational config editing.** The assistant translates natural language instructions ("all emails from this sender should go to Sales") into validated config changes.
- **No restart required.** Config changes use the existing hot-reload mechanism (mtime check on each triage cycle). Changes take effect on the next cycle automatically.
- **Safe writes.** All config modifications go through Pydantic validation before writing. Invalid changes are rejected with an explanation in the chat.
- **Auditable.** Every change made via chat is logged to `action_log` with `triggered_by = 'chat_assistant'`.
- **Ephemeral sessions.** Each chat session is tied to one email context. No persistent chat history is stored. Closing the panel ends the session.

---

## 3. User Experience

### Entry Point

The Review UI (`/review`) gains a **"Discuss"** button on each suggestion card, alongside the existing Approve/Correct/Reject/Open in Outlook actions.

### Chat Panel

Clicking "Discuss" opens a slide-out panel on the right side of the review page. The panel contains:

1. **Context header** -- Email subject, sender, current suggested classification, confidence score (read-only summary)
2. **Chat area** -- Scrollable message history for the current session
3. **Input field** -- Text input with send button
4. **Action log** -- Collapsible section at the bottom showing config changes made during this session

The panel is dismissed by clicking a close button or clicking outside it. Dismissing the panel discards the chat session (no persistence).

### Example Conversations

**Reclassifying a single email:**
```
User: This should be in Projects/Tradecore Steel, not Areas/Support
Bot:  Done -- I've updated the classification to Projects/Tradecore Steel
      and approved the suggestion. The email will be moved on the next
      triage cycle.

      The original classification matched on "issue" in the subject line,
      which triggered the Areas/Support signals. Would you like me to add
      "tradecore" as a higher-priority signal for the Tradecore project to
      prevent this in future?
User: Yes please
Bot:  Added "tradecore" to the subject signals for Projects/Tradecore Steel
      in your config. This will take effect on the next triage cycle.
```

**Creating a new project:**
```
User: This is a brand new project -- Acme Corp onboarding. We need a folder for it.
Bot:  I'll create a new project entry. Based on this email and the thread,
      here's what I'd suggest:

      Name: Acme Corp Onboarding
      Folder: Projects/Acme Corp
      Subject signals: ["acme", "acme corp", "onboarding"]
      Sender signals: ["*@acmecorp.com"]
      Default priority: P2 - Important

      Should I go ahead with this, or would you like to adjust anything?
User: Change the folder to Projects/Acme Onboarding and add "implementation" to the subjects
Bot:  Done. Created project "Acme Corp Onboarding" with folder
      Projects/Acme Onboarding. I've also reclassified this email
      to the new project. Config changes take effect next triage cycle.
```

**Creating an auto-rule from context:**
```
User: Every email from this sender is a newsletter. Just auto-route them all.
Bot:  I'll create an auto-rule for notifications@industrydigest.com:

      Folder: Reference/Newsletters
      Category: FYI Only
      Priority: P4 - Low

      This will skip AI classification entirely for this sender. Go ahead?
User: Yes
Bot:  Auto-rule created. I also found 3 other pending suggestions from this
      sender -- want me to approve them all with the same classification?
User: Do it
Bot:  Done -- approved 3 additional suggestions as Reference/Newsletters / P4 / FYI Only.
```

---

## 4. Architecture

### API Endpoint

A single new endpoint handles the chat interaction:

```
POST /api/chat
Content-Type: application/json

{
  "email_id": "AAMkAGI2...",
  "messages": [
    {"role": "user", "content": "This should go to Projects/Tradecore Steel"}
  ]
}

Response:
{
  "reply": "Done -- I've updated the classification...",
  "actions_taken": [
    {
      "type": "reclassify_email",
      "details": {"email_id": "AAMkAGI2...", "new_folder": "Projects/Tradecore Steel"}
    }
  ]
}
```

The frontend maintains the message history client-side and sends the full conversation on each request. The backend is stateless -- no server-side session storage.

### Model Selection

The chat assistant uses **Sonnet 4.5** (`claude-sonnet-4-5-20250929`). Rationale:
- These are infrequent, high-value interactions (a few per day at most)
- The assistant needs to reason about config structure, suggest changes, and explain trade-offs
- Cost impact is negligible -- even 20 chat interactions per day at Sonnet rates is < $0.50/day
- Add to `config.yaml` models section as `chat: "claude-sonnet-4-5-20250929"`

### Request Flow

```
Frontend (Review Page)
  |
  +-- User clicks "Discuss" on suggestion card
  |
  +-- Frontend opens chat panel, builds initial context from suggestion data
  |   already available on the page (email_id, subject, sender, classification, etc.)
  |
  +-- User types message
  |
  +-- POST /api/chat with email_id + full message history
  |
  +-- Backend:
  |   1. Load email record from emails table (snippet, metadata, thread info)
  |   2. Load current suggestion from suggestions table
  |   3. Fetch thread emails from emails table (same conversation_id)
  |   4. Load sender history (classification distribution)
  |   5. Load sender profile from sender_profiles table
  |   6. Load current config (projects, areas, auto_rules, key_contacts)
  |   7. Build system prompt with all context
  |   8. Call Claude Sonnet with tools + message history
  |   9. Execute any tool calls (config writes, suggestion updates)
  |   10. Return assistant reply + list of actions taken
  |
  +-- Frontend appends reply to chat, updates suggestion card if changed
```

---

## 5. System Prompt

```
You are a classification assistant for the Outlook AI email triage system.
The user is reviewing a specific email and wants help with classification,
config changes, or rule creation.

You have tools to make changes. Always confirm before making config changes
(adding projects, areas, or auto-rules). For reclassifications, you can act
immediately -- and always reclassify the entire conversation thread by default,
not just the single email. Only use scope "single" if the user explicitly asks
to change just one email in a thread.

Be concise and action-oriented. The user is a busy CEO -- don't over-explain.
After making changes, briefly confirm what you did and mention when it takes
effect.

CURRENT EMAIL:
From: {sender_name} <{sender_email}>
Subject: {subject}
Received: {received_datetime}
Snippet: {snippet}

Current classification: {suggested_folder} | {suggested_priority} | {suggested_action_type}
Confidence: {confidence}
Reasoning: {reasoning}

THREAD EMAILS ({thread_count} messages in this conversation):
{thread_emails_summary}

SENDER HISTORY:
{sender_history || "No prior emails from this sender."}

SENDER PROFILE:
{sender_profile || "No sender profile available."}

AVAILABLE FOLDERS:
Projects: {project_folders}
Areas: {area_folders}
Reference: {reference_folders}

CURRENT AUTO-RULES ({rule_count} rules):
{auto_rules_summary}

CURRENT KEY CONTACTS:
{key_contacts_summary}
```

---

## 6. Tool Definitions

### 6.1 reclassify_email

Reclassifies the current email **and all other emails in the same conversation thread**. This is the default behaviour because if a classification is wrong for one email in a thread, it's wrong for the entire thread -- they belong to the same conversation. The `scope` parameter allows restricting to just the current email in the rare case where only one message in a thread needs different treatment.

```json
{
  "name": "reclassify_email",
  "description": "Reclassify the current email and, by default, all other emails in the same conversation thread. Updates suggestion records and approves them for execution.",
  "input_schema": {
    "type": "object",
    "properties": {
      "folder": {
        "type": "string",
        "description": "New folder path (e.g., 'Projects/Tradecore Steel'). Use current value if unchanged."
      },
      "priority": {
        "type": "string",
        "enum": ["P1 - Urgent Important", "P2 - Important", "P3 - Urgent Low", "P4 - Low"]
      },
      "action_type": {
        "type": "string",
        "enum": ["Needs Reply", "Review", "Delegated", "FYI Only", "Waiting For", "Scheduled"]
      },
      "scope": {
        "type": "string",
        "enum": ["thread", "single"],
        "description": "Default 'thread': update all emails in the conversation. Use 'single' only if the user explicitly asks to reclassify just this one email."
      },
      "approve": {
        "type": "boolean",
        "description": "If true, mark suggestions as approved so moves execute on next triage cycle. Default true."
      }
    },
    "required": ["folder", "priority", "action_type"]
  }
}
```

**Implementation:**

1. **Determine target emails:**
   - If `scope` is `"thread"` (default): query `emails` table for all records with the same `conversation_id` as the current email
   - If `scope` is `"single"`: target only the current email
2. **For each target email:**
   - Look up the corresponding `suggestions` row
   - If a suggestion exists (any status -- pending, approved, or partial): update `approved_folder`, `approved_priority`, `approved_action_type`
   - If `approve` is true: set `status = 'approved'`, `resolved_at = now()`
   - If no suggestion exists (email was auto-ruled or has no suggestion record): create a new suggestion record with the given classification and mark it approved
3. **Update the `emails` table:** set `inherited_folder` to the new folder for all thread emails, so future thread inheritance uses the corrected classification
4. **Log each update** to `action_log` with `triggered_by = 'chat_assistant'`
5. **Return summary** to Claude: "Updated N emails in thread (M were pending, K were previously approved and have been re-approved with new classification)"

**Why thread-wide by default:** Thread inheritance means future emails in this conversation will inherit the folder from the most recent classified email. If only the current email is corrected but earlier thread emails still point to the wrong folder, the thread inheritance lookup could return stale data depending on query ordering. Correcting the whole thread ensures consistency.

**Re-approved emails:** If a thread email was previously approved with a different classification but hasn't been moved yet (still in the triage queue), the new classification overwrites it. If the email was already moved to the wrong folder, the new approved suggestion will trigger a corrective move on the next triage cycle.

### 6.2 add_auto_rule

Adds a new auto-routing rule to `config.yaml`.

```json
{
  "name": "add_auto_rule",
  "description": "Add an auto-routing rule that skips AI classification for matching emails.",
  "input_schema": {
    "type": "object",
    "properties": {
      "name": {
        "type": "string",
        "description": "Human-readable rule name (e.g., 'Industry Digest Newsletter')"
      },
      "match_senders": {
        "type": "array",
        "items": { "type": "string" },
        "description": "Sender email patterns (e.g., ['notifications@github.com', '*@acme.com'])"
      },
      "match_subjects": {
        "type": "array",
        "items": { "type": "string" },
        "description": "Subject keyword patterns (optional)"
      },
      "folder": { "type": "string" },
      "category": {
        "type": "string",
        "enum": ["Needs Reply", "Review", "Delegated", "FYI Only", "Waiting For", "Scheduled"]
      },
      "priority": {
        "type": "string",
        "enum": ["P1 - Urgent Important", "P2 - Important", "P3 - Urgent Low", "P4 - Low"]
      }
    },
    "required": ["name", "folder", "category", "priority"]
  }
}
```

**Implementation:**
1. Load current config via Pydantic model
2. Check for conflicts with existing auto-rules (overlapping sender/subject patterns)
3. If conflict detected: return warning to Claude, do not write
4. Append new rule to `auto_rules` list
5. Validate full config via Pydantic
6. Write atomically (backup current file, write new, verify)
7. Hot-reload picks up changes on next triage cycle
8. Log to `action_log`

### 6.3 update_project_signals

Adds signal keywords or sender patterns to an existing project or area.

```json
{
  "name": "update_project_signals",
  "description": "Add signal keywords or sender patterns to an existing project or area in config.",
  "input_schema": {
    "type": "object",
    "properties": {
      "target_name": {
        "type": "string",
        "description": "Exact name of the project or area to update (e.g., 'Tradecore Steel Implementation')"
      },
      "add_subjects": {
        "type": "array",
        "items": { "type": "string" },
        "description": "Subject keywords to add (appended to existing list)"
      },
      "add_senders": {
        "type": "array",
        "items": { "type": "string" },
        "description": "Sender patterns to add (e.g., '*@newdomain.com')"
      },
      "add_body_keywords": {
        "type": "array",
        "items": { "type": "string" },
        "description": "Body keywords to add"
      }
    },
    "required": ["target_name"]
  }
}
```

**Implementation:**
1. Load current config
2. Find project or area by `name` (exact match)
3. If not found: return error to Claude
4. Deduplicate: skip keywords already present in the existing signals
5. Append new keywords to the appropriate signal lists
6. Validate and write atomically
7. Log to `action_log`

### 6.4 create_project_or_area

Creates a new project or area entry in `config.yaml`.

```json
{
  "name": "create_project_or_area",
  "description": "Create a new project or area in the config with folder and signal definitions.",
  "input_schema": {
    "type": "object",
    "properties": {
      "type": {
        "type": "string",
        "enum": ["project", "area"],
        "description": "Whether this is a project (has an end date) or area (ongoing responsibility)"
      },
      "name": {
        "type": "string",
        "description": "Display name (e.g., 'Acme Corp Onboarding')"
      },
      "folder": {
        "type": "string",
        "description": "Folder path (e.g., 'Projects/Acme Onboarding'). Must start with 'Projects/' or 'Areas/'."
      },
      "subjects": {
        "type": "array",
        "items": { "type": "string" },
        "description": "Subject signal keywords"
      },
      "senders": {
        "type": "array",
        "items": { "type": "string" },
        "description": "Sender patterns (e.g., ['*@acmecorp.com'])"
      },
      "body_keywords": {
        "type": "array",
        "items": { "type": "string" },
        "description": "Body signal keywords"
      },
      "priority_default": {
        "type": "string",
        "enum": ["P1 - Urgent Important", "P2 - Important", "P3 - Urgent Low", "P4 - Low"],
        "description": "Default priority for emails in this project/area"
      }
    },
    "required": ["type", "name", "folder", "subjects", "priority_default"]
  }
}
```

**Implementation:**
1. Load current config
2. Validate folder path prefix matches type (`Projects/` for project, `Areas/` for area)
3. Check for duplicate names in existing projects/areas
4. Create new entry and append to the appropriate list
5. Validate and write atomically
6. Optionally trigger folder creation via Graph API (create the Outlook folder if it doesn't exist)
7. **Create taxonomy category** via `graph_tasks.py` -- calls `create_category()` with the project/area name and the appropriate color preset for taxonomy-tier categories. This ensures the new project/area is immediately available for compound category application during triage. (Phase 1.5+)
8. Log to `action_log`

### 6.5 fetch_thread_emails

Retrieves all emails in the current conversation thread for the assistant to reference.

```json
{
  "name": "fetch_thread_emails",
  "description": "Fetch all emails in the current conversation thread from the local database.",
  "input_schema": {
    "type": "object",
    "properties": {},
    "required": []
  }
}
```

**Implementation:**
1. Query `emails` table: `WHERE conversation_id = '{current_conversation_id}' ORDER BY received_at ASC`
2. For each email, include: sender, subject, received_at, snippet, current classification (from suggestions table), status
3. Return as structured list

Note: The `conversation_id` is known from the current email context -- the tool needs no parameters.

### 6.6 fetch_sender_history

Retrieves classification history and profile for the current email's sender.

```json
{
  "name": "fetch_sender_history",
  "description": "Fetch classification history and sender profile for the current email's sender.",
  "input_schema": {
    "type": "object",
    "properties": {},
    "required": []
  }
}
```

**Implementation:**
1. Query `sender_profiles` table for the sender email
2. Query classification distribution: folder counts from `suggestions` joined with `emails` for this sender
3. Return: sender category, default folder, email count, classification distribution, auto-rule candidate flag

---

## 7. Config Write Safety

All tools that modify `config.yaml` follow the same write protocol:

```
1. Read current config.yaml into memory
2. Parse into Pydantic model
3. Apply the modification in memory
4. Validate the full modified config via Pydantic
   - If validation fails: return error message to Claude, do NOT write
5. Write backup: config.yaml.bak.{timestamp}
6. Write new config atomically (write to temp file, rename)
7. Verify the written file parses correctly
8. Log the change to action_log with full before/after details
```

The existing hot-reload mechanism (mtime check on each triage cycle) picks up the change automatically. No restart needed.

**Conflict handling:** If two config writes happen in quick succession (e.g., user makes two changes in the same chat session), each write reads the current file state first. Since the chat is single-user and sequential, race conditions are not a practical concern. The backup chain provides recovery if anything goes wrong.

---

## 8. Changes to Existing Specs

### 8.1 Spec index (`01-overview.md`)

Add row to the Spec Index table:

| Document | Contents | Read when... |
|----------|----------|-------------|
| `08-classification-chat.md` | Chat assistant for reclassification, config refinement, rule creation from Review UI | Implementing the chat panel, chat tools, or config write logic |

### 8.2 config.yaml additions (`02-config-and-schema.md`)

Add to the `models` section:

```yaml
models:
  # ... existing entries ...
  chat: "claude-sonnet-4-5-20250929"      # Classification chat assistant
```

### 8.3 Project structure additions (`01-overview.md`)

```
src/assistant/
  ├── chat/
  │   ├── __init__.py
  │   ├── assistant.py         # Chat session handler (builds prompt, calls Claude, executes tools)
  │   ├── tools.py             # Tool implementations (reclassify, add_rule, update_signals, etc.)
  │   └── prompts.py           # System prompt template for chat assistant
  ├── web/
  │   ├── routes.py            # Add POST /api/chat endpoint
  │   └── templates/
  │       └── review.html      # Add chat panel UI
```

### 8.4 Model tiering table (`01-overview.md`)

Add row:

| Task | Model | Rationale |
|------|-------|-----------|
| Classification Chat | `claude-sonnet-4-5-20250929` (Sonnet 4.5) | Infrequent, high-value interactions requiring config reasoning. |

### 8.5 Safety boundaries (`06-safety-and-testing.md`)

Add to "Agent MAY do autonomously":
- Reclassify suggestions via chat assistant (user-initiated)

Add to "Agent MUST get user approval for":
- Config modifications via chat assistant (Claude proposes, user confirms in chat before tool executes)

### 8.6 Review UI page updates (`03-agent-behaviors.md` Section 3)

Add "Discuss" button to the Review Queue suggestion card actions:

```
Actions per suggestion:
  - ✅ Approve All
  - ✏️ Correct
  - ❌ Reject
  - 🔗 Open in Outlook
  - 📝 Create Rule
  - 💬 Discuss        <- NEW: opens chat panel for this email
```

### 8.7 Build phase placement

This feature is part of **Phase 1** -- it addresses a critical usability gap in the core review workflow. Without it, config refinement requires YAML editing, which contradicts the target user profile.

Add to Phase 1 item list in `01-overview.md`:

```
26. Classification chat assistant: contextual Sonnet-powered chat panel on
    review page for reclassification, config refinement, and rule creation
```

---

## 9. Testing

### Unit Tests
- Tool implementations: verify each tool modifies config/suggestions correctly
- Config write safety: verify Pydantic validation rejects invalid changes
- Config write safety: verify backup file is created before writing
- Config write safety: verify atomic write (temp file + rename)
- Prompt template rendering with full email context
- Conflict detection: verify auto-rule conflicts are caught and reported
- Duplicate detection: verify duplicate project names are rejected
- Signal deduplication: verify existing keywords are not re-added

### Integration Tests
- Full chat round-trip: send message -> Claude responds with tool call -> tool executes -> response returned
- Reclassify flow: verify suggestion record is updated and status changes to approved
- Thread reclassify: verify all pending suggestions in thread are updated
- Config modification flow: verify config.yaml is updated and validates after change
- Error handling: verify invalid folder path returns error in chat, config unchanged

### Manual Testing
- Chat panel UX: open, send messages, close, reopen on different email
- Multi-turn conversation: reclassify -> add signals -> create rule in one session
- Config hot-reload verification: make change via chat, trigger triage cycle, verify new classification uses updated config

---

## 10. Estimated Cost Impact

The chat assistant uses Sonnet 4.5, which is more expensive per token than Haiku but is used infrequently.

| Scenario | Estimated cost |
|----------|---------------|
| Single reclassification (2-3 turns) | ~$0.01-0.03 |
| Config change conversation (4-6 turns) | ~$0.03-0.08 |
| 10 chat sessions per day (heavy use) | ~$0.10-0.50/day |
| Typical usage (3-5 sessions per day) | ~$0.05-0.20/day |

This is negligible relative to the daily triage cost (~$0.10-0.30/day for Haiku classification).

---

## 11. Phase 1.5 and Phase 2 Integration

### Phase 1.5 Changes

- **`create_project_or_area` tool (Section 6.4):** Step 7 added -- when creating a new project or area, the tool also creates a taxonomy category via `graph_tasks.py`. This ensures compound categories (priority + action type + taxonomy) can be applied to emails classified into the new project/area.
- **Config hot-reload:** When a new project/area is added via chat and the config hot-reload detects it, missing taxonomy categories are automatically created.

### Phase 2 Additions (Not in Phase 1.5)

The following chat features are deferred to Phase 2:

- **`manage_category` tool:** A new chat tool allowing users to create, rename, or delete user-tier categories conversationally. Full tool schema in `10-native-task-integration.md` Phase 2 Additions section.
- **AVAILABLE CATEGORIES in system prompt:** The chat system prompt (Section 5) will be extended with an `AVAILABLE CATEGORIES` block listing all framework, taxonomy, and user-tier categories. This helps the assistant suggest correct categories during reclassification.
- **Category growth through learning:** When the learning-from-corrections system (Feature 2D) detects recurring user-applied categories, it can propose formalizing them via the chat assistant.
