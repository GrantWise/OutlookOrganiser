# Architecture Decisions: Native Microsoft 365 Task Integration (Phase 1.5)

**Version:** 2.0 | **Last Updated:** 2026-02-08

> **Purpose:** This document records the decision to integrate native Microsoft 365 task management features into the assistant's architecture *before* Phase 2 begins. Phase 1.5 establishes lean plumbing -- To Do tasks, category management, and immutable IDs. Phase 2 builds on this plumbing to add email flags, calendar awareness, task sync, and category growth. Claude Code should treat this as authoritative guidance that supersedes conflicting details in other spec documents for the affected areas.
>
> **Prerequisite reading:** `MicrosoftGraphAPITaskTracking.md` (research document in `Reference/` containing full API details, JSON examples, and endpoint references)

---

## 1. Decision Summary

### Phase 1.5 Decisions (This Phase)

| Question | Decision |
|----------|----------|
| Should we integrate native Microsoft To Do tasks? | **Yes -- To Do becomes the user-facing system of record for task/follow-up tracking** |
| Should we manage categories programmatically via Graph API? | **Yes -- bootstrap framework categories on first run, create taxonomy categories from config** |
| Should we clean up existing category debris? | **Yes -- interactive cleanup during category bootstrap on first run** |
| Should we replace the SQLite waiting_for table? | **No -- keep it as the AI metadata cache, but sync state to To Do** |
| Should we migrate to immutable message IDs? | **Yes -- prerequisite for stable linkedResources** |
| When should this be implemented? | **Phase 1.5 -- after Phase 1, before Phase 2** |
| Does this require re-implementing Phase 1 features? | **No -- this adds new plumbing alongside existing code, no Phase 1 rewrites** |

### Deferred to Phase 2 Decisions

| Question | Decision |
|----------|----------|
| Should we use email followUpFlags? | **Yes -- deferred to Phase 2 (Feature 2B: Waiting-For Tracker)** |
| Should we add calendar awareness? | **Yes -- deferred to Phase 2 (Feature 2C: Daily Digest)** |
| Should we add bidirectional task sync? | **Yes -- deferred to Phase 2 (Feature 2B: Waiting-For Tracker)** |
| Should we add a chat `manage_category` tool? | **Yes -- deferred to Phase 2 (Feature 2D: Learning from Corrections)** |
| Should we add AVAILABLE CATEGORIES to prompts? | **Yes -- deferred to Phase 2 (when learning system needs Claude to suggest categories)** |

### Implementation Deviations (Decided During Live Testing)

| Original Design | Implemented | Rationale |
|-----------------|-------------|-----------|
| Taxonomy categories for both projects AND areas | **Areas only** | Projects are temporary and would accumulate unboundedly in the master category list. The folder hierarchy already conveys the project. Areas are permanent cross-cutting concerns. |
| `derive_taxonomy_name()` checks projects first, then areas | Checks areas only (projects parameter removed) | Simplification from the areas-only decision. |
| APScheduler `next_run_time=None` (defer first cycle) | `next_run_time=datetime.now() + timedelta(seconds=60)` | `next_run_time=None` permanently pauses the job in APScheduler v3.x. Changed to 60s startup delay. |

> **Note:** Several sections below still reference "project categories" as part of the original design. The implementation only creates taxonomy categories for **areas**. Project names do not get Outlook categories.

---

## 2. Why Phase 1.5 (Not After Phase 2)

Phase 2 items 1 and 2 -- the waiting-for tracker and daily digest -- are the features most directly affected by this integration. The waiting-for tracker as currently specified in `03-agent-behaviors.md` Section 5 is designed entirely around a `waiting_for` SQLite table with custom nudge/escalation logic and a custom `/waiting` page in the Review UI. The daily digest in Section 4 aggregates data exclusively from SQLite.

**If Phase 2 builds these on pure SQLite first**, retrofitting native Microsoft integration later means:

1. Rewriting the waiting-for data flow to sync bidirectionally with To Do
2. Reworking the digest to read task completion status from Graph API
3. Adding a sync/reconciliation layer after the fact
4. Re-testing all affected code paths

**If Phase 1.5 establishes the plumbing first**, Phase 2 builds on it from the start. The waiting-for tracker writes to both SQLite (for AI metadata) and To Do (for user visibility). The digest reads from both sources natively. No rework.

**Phase 2 items NOT affected by this decision** (items 2A, 2D-2M in `PHASE_2_INTELLIGENCE.md`): delta queries, learning from corrections, confidence calibration, sender affinity auto-rules, auto-rules hygiene, suggestion queue management, stats dashboard, sender management, graceful degradation. These proceed exactly as specified.

---

## 3. The Hybrid Architecture Pattern

The core architectural principle is **dual system of record with clear ownership**:

### Phase 1.5 (This Phase)

| Concern | System of Record | Rationale |
|---------|-----------------|-----------|
| User-facing tasks and follow-ups | **Microsoft To Do** (via Graph API) | Visible across all devices, push notifications, Outlook sidebar, mobile To Do app |
| AI classification results | **SQLite** (existing) | Processing history, confidence scores, sender patterns, prompt context |
| Escalation logic and thresholds | **SQLite** (existing) | Custom time-based logic (nudge after 48h, escalate after 96h) has no native equivalent |
| Sync state between systems | **SQLite** (`task_sync` table) | Maps Graph API task IDs to email IDs |
| Category taxonomy | **Outlook master categories** (via Graph API) | Unified color-coded labels across email, To Do, and calendar |

### Phase 2 Additions

| Concern | System of Record | Rationale |
|---------|-----------------|-----------|
| Email flags and due dates | **Outlook followUpFlag** (via Graph API) | Native Outlook experience, feeds into To Do's "Flagged email" list |
| Calendar availability | **Outlook Calendar** (via Graph API, read-only) | Schedule-aware digest timing, "time to respond" suggestions |

**Phase 1.5 data flow (one-directional):**

```
AI Decision (Claude classifies email, suggestion approved)
    |
    ├── Move email to folder (existing Phase 1 behavior)
    |
    ├── Apply compound categories to email: priority + action type + taxonomy
    |
    ├── Create To Do task via Graph API (user-facing, with linkedResource to email)
    |   Task categories: priority + taxonomy (action type conveyed via task status field)
    |
    └── Record mapping in task_sync table
```

**Phase 2 adds bidirectional sync:**

```
User completes task in To Do (on phone, Outlook sidebar, etc.)
    |
    ├── Next sync cycle: agent reads task status from To Do via Graph API
    |
    └── Updates SQLite waiting_for status to "received"
```

---

## 4. New Graph API Permissions

### 4.1 Required Additions (Phase 1.5)

Add these delegated permissions to the app registration:

| Permission | Purpose | Phase |
|------------|---------|-------|
| `Tasks.ReadWrite` | Create, read, update, delete To Do tasks, task lists, linked resources, checklist items | Phase 1.5 |
| `MailboxSettings.ReadWrite` | Create and manage Outlook categories programmatically (upgrades existing `MailboxSettings.Read`) | Phase 1.5 |

**Note on `MailboxSettings.ReadWrite`:** This *replaces* the existing `MailboxSettings.Read` permission -- `ReadWrite` is a superset that includes all read capabilities. The app registration should have `MailboxSettings.ReadWrite` instead of `MailboxSettings.Read`, not both.

### 4.2 Phase 2 Addition

| Permission | Purpose | Phase |
|------------|---------|-------|
| `Calendars.Read` | Read calendar events and free/busy schedule for schedule-aware features | Phase 2 (Feature 2C: Daily Digest) |

### 4.3 Updated Permission Set

After Phase 1.5, the full permission set is:

```
Microsoft Graph (5)
├── Mail.ReadWrite             Delegated    ✅ Granted   (existing)
├── Mail.Send                  Delegated    ✅ Granted   (existing)
├── MailboxSettings.ReadWrite  Delegated    ✅ Granted   (UPGRADED from Read -- Phase 1.5)
├── User.Read                  Delegated    ✅ Granted   (existing)
└── Tasks.ReadWrite            Delegated    ✅ Granted   (NEW -- Phase 1.5)
```

After Phase 2 (Feature 2C), the set grows to 6:

```
Microsoft Graph (6)
├── ...all Phase 1.5 permissions...
└── Calendars.Read             Delegated    ✅ Granted   (NEW -- Phase 2)
```

### 4.4 Auth Scope Update

The MSAL `scopes` list in `config.yaml` and the device code flow must include the new and upgraded permissions. **Existing cached tokens will need to be deleted** (remove `data/token_cache.json`) so the next authentication cycle requests the expanded scope. The device code flow will prompt the user to consent to the additional permissions.

```yaml
auth:
  scopes:
    - "Mail.ReadWrite"
    - "Mail.Send"
    - "MailboxSettings.ReadWrite"   # UPGRADED from MailboxSettings.Read
    - "User.Read"
    - "Tasks.ReadWrite"             # NEW
    # Calendars.Read added in Phase 2
```

---

## 5. New Module: `graph_tasks.py`

Create a new module `graph_tasks.py` alongside the existing `graph_client.py`. This module handles all To Do and category operations. **Do not add task operations to `graph_client.py`** -- keep mail operations and task operations in separate modules for clarity.

### 5.1 Task List Management

On first run (or when no task list ID is stored), the agent must discover or create its working task list:

```python
# Discovery: look for existing list named "AI Assistant"
# GET /me/todo/lists?$filter=displayName eq 'AI Assistant'

# If not found, create it:
# POST /me/todo/lists
# { "displayName": "AI Assistant" }

# Store the list ID in agent_state:
# key: 'todo_list_id', value: '{listId}'
```

**Task list naming convention:**

| List Name | Purpose |
|-----------|---------|
| `AI Assistant` | Primary task list for all agent-created tasks (waiting-for items, follow-up reminders, flagged-for-action items) |

The agent only manages tasks in its own list. It never reads or modifies tasks in the user's personal "Tasks" list or any other list.

### 5.2 Core Operations

The module must implement these operations:

**Create task with linked email:**
```
POST /me/todo/lists/{listId}/tasks
```
With body containing: `title`, `status` (mapped from action type), `importance` (mapped from priority), `dueDateTime`, `isReminderOn`, `reminderDateTime`, `body` (AI-generated context), `categories`, and inline `linkedResources` array with the email's `webLink` and Graph message `id`.

**Read tasks (for sync -- Phase 2):**
```
GET /me/todo/lists/{listId}/tasks?$filter=status ne 'completed'
```
Used during Phase 2 sync cycles to detect tasks the user completed outside the assistant. Implemented in Phase 1.5 as infrastructure but not called until Phase 2.

**Update task status:**
```
PATCH /me/todo/lists/{listId}/tasks/{taskId}
```
Used when a reply arrives for a waiting-for item (mark as completed) or when escalation thresholds are hit (update importance, add reminder).

**Delete task:**
```
DELETE /me/todo/lists/{listId}/tasks/{taskId}
```
Used when a waiting-for item is manually dismissed by the user via the Review UI.

### 5.3 Field Mapping

When creating To Do tasks from email classifications, map fields as follows:

| Agent Concept | To Do Field | Mapping |
|---------------|-------------|---------|
| Action type: "Waiting For" | `status` | `"waitingOnOthers"` |
| Action type: "Needs Reply" | `status` | `"notStarted"` |
| Action type: "Review" | `status` | `"notStarted"` |
| Action type: "Delegated" | `status` | `"inProgress"` |
| Priority: "P1 - Urgent Important" | `importance` | `"high"` |
| Priority: "P2 - Important" | `importance` | `"high"` |
| Priority: "P3 - Urgent Low" | `importance` | `"normal"` |
| Priority: "P4 - Low" | `importance` | `"low"` |
| Nudge threshold (48h) | `dueDateTime` | `received_at + nudge_after_hours` |
| Escalation threshold (96h) | `reminderDateTime` | `received_at + escalate_after_hours` |
| Email subject + sender | `title` | `"Reply to {sender_name} re: {subject}"` (truncated to 255 chars) |
| AI reasoning + context | `body.content` | `"From: {sender}\n{snippet_first_200_chars}\n\nClassified: {folder} | {priority}"` |
| Email webLink | `linkedResources[0].webUrl` | Direct from Graph API message `webLink` field |
| Graph message ID | `linkedResources[0].externalId` | Graph API message `id` (use immutable IDs) |
| Agent identifier | `linkedResources[0].applicationName` | `"Outlook AI Assistant"` |
| Categories on task | `categories` | Priority + taxonomy categories only (e.g., `["P2 - Important", "Tradecore Steel Implementation"]`). Action type is conveyed via the task `status` field, not duplicated as a category. |

**Why action type categories are on emails but not tasks:** The To Do task `status` field (`waitingOnOthers`, `notStarted`, `inProgress`) already conveys the action type semantically. Duplicating "Waiting For" as both a `status` and a `category` on the same task is redundant. However, action type categories on *emails* are valuable because they enable filtering in Outlook (e.g., filter folder view by "Needs Reply" to see all emails awaiting response). The email and the task serve complementary roles: the email shows all category dimensions, the task uses native status semantics.

### 5.4 Immutable Message IDs

**Critical -- This is build step 0 and must be tested independently before any task features.**

When creating `linkedResources` that reference emails, the Graph API message `id` can change when a message is moved between folders. To prevent broken links:

1. Add the `Prefer: IdType="ImmutableId"` header to **all** message fetch requests in `graph_client.py`
2. Store immutable IDs in the `emails.id` column
3. Use immutable IDs in `linkedResources[0].externalId`

**Migration note:** Phase 1 was implemented using standard (mutable) IDs. A one-time migration is needed:
1. On first startup after Phase 1.5 upgrade, check `agent_state['immutable_ids_migrated']`
2. If not set, fetch all stored message IDs with the `Prefer: IdType="ImmutableId"` header
3. Update the `emails` table with the immutable IDs
4. If messages have already been moved and their mutable IDs return 404 -- log a warning and skip them (do not delete records)
5. Set `immutable_ids_migrated = "true"` in `agent_state`

**Testing:** This migration must be tested against the live mailbox before any task features are built on top. Verify:
- IDs returned with the `Prefer` header are in the expected immutable format
- Moving a message between folders does not change the immutable ID
- Existing mutable IDs can be translated to immutable IDs via the API

### 5.5 Category Management

Categories are a unified system across Microsoft 365 -- the same master category list applies to email, To Do tasks, calendar events, contacts, and group posts. This makes categories a powerful cross-app organizational axis: when the agent applies "P2 - Important" and "Needs Reply" to an email, and then creates a To Do task from that email with matching priority and taxonomy categories, the CEO sees consistent color-coded labels everywhere.

The `graph_tasks.py` module must implement these category operations:

**Read master category list:**
```
GET /me/outlook/masterCategories
```
Returns all categories with `id`, `displayName`, and `color` (preset0-preset24).

**Create category:**
```
POST /me/outlook/masterCategories
{
  "displayName": "P1 - Urgent Important",
  "color": "preset0"
}
```
**Important:** `displayName` is immutable after creation. If a category name needs to change, the old one must be deleted and a new one created. The `color` can be updated via PATCH.

**Delete category:**
```
DELETE /me/outlook/masterCategories/{id}
```
Removing a category from the master list does not remove it from resources that already have it applied -- those resources retain the category name as a string but it becomes "uncategorized" (no color) in the UI.

**Apply categories to a resource:**
```
PATCH /me/messages/{id}
{ "categories": ["P2 - Important", "Needs Reply", "Tradecore Steel Implementation"] }

PATCH /me/todo/lists/{listId}/tasks/{taskId}
{ "categories": ["P2 - Important", "Tradecore Steel Implementation"] }
```
The `categories` property is a string array. Setting it replaces all existing categories on that resource. To add a category without removing existing ones, read the current list first, append, and write back.

Note: emails get the full compound set (priority + action type + taxonomy). Tasks get priority + taxonomy only (action type is conveyed via task `status`).

### 5.6 Category Bootstrap

On first run (or when `categories_bootstrapped` is not set in `agent_state`), the agent ensures its framework categories exist in the master category list with the correct colors. This is similar to the folder bootstrap but deterministic -- no Claude analysis needed.

**Category tiers:**

| Tier | Categories | Created By | Lifecycle |
|------|-----------|------------|-----------|
| **Framework** | 4 priority categories + 6 action type categories | Phase 1.5 bootstrap (automatic) | Permanent -- never deleted by the agent |
| **Taxonomy** | Project and area categories matching the folder taxonomy | Bootstrap + config changes | Created when projects/areas are added; archived when projects complete |
| **User** | Custom categories created through classification chat or detected from user behavior | Phase 2 chat tool + learning from corrections | User-managed -- agent proposes, user confirms |

**Framework categories (created on first run):**

| Category | Color Preset | Color Name | Tier |
|----------|-------------|------------|------|
| `P1 - Urgent Important` | `preset0` | Red | Framework |
| `P2 - Important` | `preset1` | Orange | Framework |
| `P3 - Urgent Low` | `preset7` | Blue | Framework |
| `P4 - Low` | `preset14` | Steel | Framework |
| `Needs Reply` | `preset3` | Yellow | Framework |
| `Waiting For` | `preset8` | Purple | Framework |
| `Delegated` | `preset5` | Green | Framework |
| `FYI Only` | `preset14` | Steel | Framework |
| `Scheduled` | `preset9` | Teal | Framework |
| `Review` | `preset2` | Brown | Framework |

**Taxonomy categories (created from config):**

After framework categories are bootstrapped, the agent reads the `projects` and `areas` lists from `config.yaml` and creates a category for each:

```python
# For each project in config:
#   Create category "{project.name}" with color preset11 (Mango) for projects
#   e.g., "Tradecore Steel Implementation", "SOC 2 Compliance", ".NET 9 Migration"

# For each area in config:
#   Create category "{area.name}" with color preset10 (Lavender) for areas
#   e.g., "Sales & Prospects", "Development Team", "Client Support"
```

This means when Claude classifies an email to `Projects/Tradecore Steel`, the agent applies both the priority/action categories *and* the `Tradecore Steel Implementation` category. The email shows up with color-coded project/area tags in Outlook, and when a To Do task is created from that email, it inherits the priority and taxonomy tags -- visible in the To Do app across all devices.

**Bootstrap flow:**

```
On startup, check agent_state['categories_bootstrapped']:
  |
  +-- Not set or "false":
  |   |
  |   +-- GET /me/outlook/masterCategories
  |   |
  |   +-- For each framework category:
  |   |   +-- Exists (any color)? -> Skip (preserve user's existing color)
  |   |   +-- Missing? -> POST to create with specified color
  |   |
  |   +-- For each project/area in config:
  |   |   +-- Category with matching displayName exists? -> Skip
  |   |   +-- Missing? -> POST to create with appropriate color
  |   |
  |   +-- Run interactive category cleanup (see below)
  |   |
  |   +-- Set agent_state['categories_bootstrapped'] = "true"
  |   +-- Log INFO: "Bootstrapped {N} categories ({M} created, {K} already existed)"
  |
  +-- Set to "true": -> Skip bootstrap, proceed normally
```

**Critical rule on existing category colors:** The agent **never** changes the color of an existing category via PATCH. If a framework category already exists (even from a previous abandoned attempt), its current color is preserved. Colors are only set on categories the agent creates fresh. This respects any manual color choices the user has made.

> **Future enhancement:** Color grouping by tier -- all priority categories in warm tones (red/orange), action types in cool tones (blue/purple/teal), projects in one hue (mango), areas in another (lavender). This is noted for a future phase but not implemented now.

**Re-bootstrap trigger:** When the config hot-reload detects new projects or areas added to `config.yaml`, check if corresponding categories exist and create any that are missing. This ensures categories stay in sync with the folder taxonomy without requiring a full re-bootstrap.

### 5.7 Category Cleanup

The user's mailbox may contain orphaned categories from previous manual attempts at organization. During the first category bootstrap (Phase 1.5 first run), the agent runs an interactive cleanup:

```
After framework + taxonomy category bootstrap:
  |
  +-- Read full master category list from Graph API
  |
  +-- Identify orphans: categories NOT matching any:
  |   - Framework category name (P1-P4, action types)
  |   - Taxonomy category name (project/area names from config)
  |
  +-- If orphans found:
  |   +-- Display list to user:
  |   |   "Found {N} categories not managed by the assistant:
  |   |    - 'Old Category 1'
  |   |    - 'Previous Attempt Category'
  |   |    - 'Manual Label'
  |   |   Delete these categories? (y/N/select individually)"
  |   |
  |   +-- User confirms which to delete
  |   +-- DELETE confirmed orphans via Graph API
  |   +-- Log INFO: "Cleaned up {N} orphaned categories"
  |
  +-- If no orphans: skip cleanup silently
```

**Safety rules:**
- Never auto-delete categories -- always require user confirmation
- Framework categories are never candidates for cleanup (they're managed by the agent)
- Categories applied to existing emails/tasks are not removed from those resources when the master category is deleted -- they just lose their color in the UI
- The cleanup runs only on first bootstrap, not on subsequent startups

### 5.8 Category Application During Triage

When the triage engine processes an email and a suggestion is approved, the classification includes category application as part of the compound action:

```
Suggestion = {
    folder: "Projects/Tradecore Steel",
    priority: "P2 - Important",          -> Outlook category: "P2 - Important"
    action_type: "Needs Reply",           -> Outlook category: "Needs Reply"
    taxonomy_category: derived from       -> Outlook category: "Tradecore Steel Implementation"
      folder -> config project/area name
}
```

**Taxonomy category derivation:** The taxonomy category is derived deterministically by the triage engine from the folder mapping -- it is NOT returned by Claude in the tool call. The engine maps `suggested_folder` -> config project/area -> `name` -> category. This avoids prompt complexity, saves tokens, and ensures consistency.

When the suggestion is approved and executed:
1. **Move email** to the suggested folder (existing behavior)
2. **Apply categories to email**: `["P2 - Important", "Needs Reply", "Tradecore Steel Implementation"]` -- all three tiers in a single PATCH call (enhanced behavior)
3. **Create To Do task** (if `integrations.todo.enabled` and the action type is in `create_for_action_types`) with categories `["P2 - Important", "Tradecore Steel Implementation"]` and `status: "notStarted"` (new Phase 1.5 behavior)
4. **Record task mapping** in `task_sync` table (new Phase 1.5 behavior)

**Important:** The current Phase 1 code in `web/routes.py` already applies priority and action type categories to emails via `set_categories()`. Phase 1.5 extends this to also apply taxonomy categories (project/area names) alongside the existing categories, and creates the To Do task.

### 5.9 Taxonomy Category Sync with Config

When the `add_project_or_area` chat tool creates a new project or area (writing to `config.yaml` and creating a folder via Graph API), it should also create the corresponding taxonomy category:

```
Step 6 (existing): Optionally trigger folder creation via Graph API
Step 7 (NEW):      Create taxonomy category via graph_tasks.py with project/area color
Step 8 (existing): Log to action_log
```

This is a small extension to the existing tool -- one additional Graph API call. It prevents a gap where new projects have folders but no matching category.

Similarly, when config hot-reload detects new projects or areas, it checks for and creates any missing taxonomy categories.

---

## 6. New SQLite Table: `task_sync`

Add a new table to track the mapping between To Do tasks and emails. This table is managed exclusively through `store.py`.

```sql
CREATE TABLE task_sync (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email_id TEXT REFERENCES emails(id),          -- Graph message ID (immutable)
    todo_task_id TEXT NOT NULL,                    -- Graph To Do task ID
    todo_list_id TEXT NOT NULL,                    -- Graph To Do list ID
    task_type TEXT NOT NULL,                       -- 'waiting_for', 'needs_reply', 'review', 'delegated'
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    synced_at DATETIME,                            -- Last time status was synced from Graph API
    status TEXT DEFAULT 'active'                    -- 'active', 'completed', 'deleted'
);

CREATE INDEX idx_task_sync_email ON task_sync(email_id);
CREATE INDEX idx_task_sync_todo ON task_sync(todo_task_id);
```

**Phase 2 additions to this table:** When Features 2B (waiting-for tracker with bidirectional sync) and email flags are implemented, add:
- `conversation_id TEXT` column (for cross-referencing with `waiting_for` table)
- `flag_set INTEGER DEFAULT 0` column (tracks whether `followUpFlag` was set on the email)
- `idx_task_sync_conversation` index

**Relationship to existing `waiting_for` table:** The `waiting_for` table continues to own the AI-specific metadata (nudge thresholds, escalation state, expected sender). The `task_sync` table maps those entries to their Graph API counterparts. When the Phase 2 waiting-for tracker is built, it writes to both tables: `waiting_for` for AI logic, `task_sync` for the To Do task reference.

---

## 7. New `agent_state` Keys

Store these additional values in the `agent_state` table:

| Key | Value | Purpose |
|-----|-------|---------|
| `todo_list_id` | Graph API task list ID | Cached reference to the "AI Assistant" task list |
| `todo_enabled` | `"true"` or `"false"` | Feature flag -- allows disabling To Do integration without removing permissions |
| `categories_bootstrapped` | `"true"` or `"false"` | Tracks whether the category bootstrap has run |
| `immutable_ids_migrated` | `"true"` or `"false"` | Tracks whether the one-time mutable->immutable ID migration has run |

**Phase 2 addition:** `calendar_enabled` key (when `Calendars.Read` is added in Feature 2C).

---

## 8. New Config Values

Add to `config.yaml`:

```yaml
# -- Microsoft 365 Native Integration (Phase 1.5) --
integrations:
  todo:
    enabled: true                    # Create To Do tasks for actionable emails
    list_name: "AI Assistant"        # Name of the To Do list to use (created if missing)
    create_for_action_types:         # Which action types generate tasks
      - "Waiting For"
      - "Needs Reply"
      - "Review"
      - "Delegated"
```

**Pydantic schema:** Add a new `IntegrationsConfig` model with nested `TodoConfig`. All fields have defaults, so existing `config.yaml` files remain valid without this section (integrations default to disabled if the section is missing, preserving backward compatibility with Phase 1 configs).

**Phase 2 config additions** (documented in `PHASE_2_INTELLIGENCE.md`):

```yaml
integrations:
  todo:
    # ...Phase 1.5 fields above...
    sync_interval_minutes: 5         # How often to check for user-completed tasks

  email_flags:
    enabled: true                    # Set followUpFlag on actionable emails
    flag_action_types:               # Which action types get flagged
      - "Needs Reply"
      - "Waiting For"
    only_after_approval: true        # Only flag emails after suggestion is approved

  calendar:
    enabled: true                    # Read calendar for schedule awareness
    digest_schedule_aware: true      # Use calendar to pick optimal digest delivery time
```

---

## 9. Changes to Existing Spec Documents

### 9.1 Changes to `01-overview.md`

**Spec Index table:** Add row:

| Document | Contents | Read when... |
|----------|----------|-------------|
| `10-native-task-integration.md` | Phase 1.5 To Do integration, category management, hybrid architecture | Implementing task creation, category bootstrap, or native M365 integration features |

**Build Phases section:** Insert Phase 1.5 between Phase 1 and Phase 2:

```
### Phase 1.5 -- Native Microsoft 365 Integration

**Goal:** Establish lean plumbing for To Do tasks, category management, and immutable message IDs

1. Add Tasks.ReadWrite permission; upgrade MailboxSettings.Read to ReadWrite
2. Implement immutable message ID migration (Prefer: IdType="ImmutableId" header)
3. graph_tasks.py module: To Do task list discovery/creation, task CRUD with linkedResources
4. graph_tasks.py module: master category list management (read/create/delete)
5. Category bootstrap: ensure framework categories (priorities + action types) exist with
   correct colors; create taxonomy categories for each project/area in config;
   interactive cleanup of orphaned categories from previous attempts
6. task_sync SQLite table and store.py CRUD operations
7. Config schema update: integrations section with todo config
8. Triage engine hooks: apply compound categories (priority + action + taxonomy) to emails
   and create To Do tasks on suggestion approval
9. Chat tool extension: add_project_or_area also creates taxonomy category
10. Config hot-reload: create categories for newly added projects/areas
11. Tests: category bootstrap, task creation, immutable IDs, category application
12. Token cache migration: delete existing token cache to force re-auth with expanded scopes
```

**Phase 2 item updates:** Amend items to note Phase 1.5 dependencies:

```
2B. Waiting-for tracker -- writes to both SQLite and To Do via Phase 1.5 graph_tasks module;
    adds bidirectional task sync, email followUpFlag operations
2C. Daily digest generation -- reads task status from Graph API for completion detection;
    adds Calendars.Read permission and calendar awareness for delivery timing
2D. Learning from corrections -- adds category growth through learning, manage_category
    chat tool, AVAILABLE CATEGORIES in triage/chat prompts
```

### 9.2 Changes to `02-config-and-schema.md`

**Section 2 (Category Labels):** This section currently defines categories as a static list of 10 labels applied to emails. Phase 1.5 elevates categories to a managed, growing taxonomy. Add after the existing tables:

> **Category management (Phase 1.5):**
>
> Categories are no longer a static list defined only in documentation. The agent programmatically manages categories in the Outlook master category list via `/me/outlook/masterCategories` (requires `MailboxSettings.ReadWrite`).
>
> Categories are organized in three tiers:
> - **Framework categories** (the 4 priority + 6 action type categories listed above) -- created automatically on first run, never deleted
> - **Taxonomy categories** -- one category per project and area in `config.yaml`, created during bootstrap and when new projects/areas are added. Applied to emails and To Do tasks alongside priority categories for cross-app visibility.
> - **User categories** (Phase 2) -- custom categories created through the classification chat or detected from user behavior. Proposed by the agent, confirmed by the user.
>
> The same categories are applied consistently to emails and To Do tasks. This provides unified color-coded labeling across the CEO's Microsoft 365 experience.

**Section 2, new subsection -- Category-to-Color mapping:** Add the color preset mapping table from Section 5.6 of this document.

**Section 3 (SQLite Schema):** Add the `task_sync` table definition from Section 6 of this document (simplified, without Phase 2 columns). Add the new `agent_state` keys from Section 7.

**Section 5 (config.yaml):** Add the `integrations.todo` section from Section 8 of this document. Update `auth.scopes`:

```yaml
auth:
  scopes:
    - "Mail.ReadWrite"
    - "Mail.Send"
    - "MailboxSettings.ReadWrite"    # UPGRADED from Read -- Phase 1.5
    - "User.Read"
    - "Tasks.ReadWrite"              # NEW -- Phase 1.5
    # Calendars.Read added in Phase 2
```

### 9.3 Changes to `03-agent-behaviors.md`

**Section 2 (Triage Engine) -- After suggestion approval:** Add:

> **Phase 1.5 integration:** When a suggestion with an actionable `action_type` (Needs Reply, Waiting For, Review, Delegated) is approved, the triage engine:
> 1. Creates a To Do task via `graph_tasks.py` (if `integrations.todo.enabled` and the action type is in `create_for_action_types`)
> 2. Applies the full category set (priority + action type + taxonomy) to the email in a single PATCH call
> 3. Applies priority + taxonomy categories to the To Do task (action type is conveyed via task `status`)
> 4. Records the task mapping in `task_sync`
>
> In autonomous mode (Phase 3), tasks and categories are applied immediately upon classification for high-confidence results, without waiting for approval.

**Section 5 (Waiting For Tracker):** Add:

> **Phase 1.5 plumbing:** Phase 1.5 establishes To Do task creation and the `task_sync` table. When Phase 2 builds the active waiting-for tracker, it uses this plumbing to create To Do tasks with `status: "waitingOnOthers"` and `linkedResources` pointing to the email. Phase 2 also adds bidirectional sync (detecting when the user completes a task in To Do) and email `followUpFlag` operations.

**Section 4 (Daily Digest):** Add:

> **Phase 1.5/2 integration:** Phase 2 digest generation reads task completion status from the Graph API (via `task_sync` cross-reference) in addition to SQLite state. Calendar awareness for delivery timing requires `Calendars.Read` (Phase 2 Feature 2C).

### 9.4 Changes to `05-graph-api.md`

**Section 1 (Key Endpoints):** Add these rows to the endpoint table:

| Operation | Endpoint | Method |
|-----------|----------|--------|
| List To Do task lists | `/me/todo/lists` | GET |
| Create To Do task list | `/me/todo/lists` | POST |
| Create task (with linkedResources) | `/me/todo/lists/{listId}/tasks` | POST |
| Update task | `/me/todo/lists/{listId}/tasks/{taskId}` | PATCH |
| Delete task | `/me/todo/lists/{listId}/tasks/{taskId}` | DELETE |
| List tasks (for sync) | `/me/todo/lists/{listId}/tasks` | GET |
| Create linked resource on task | `/me/todo/lists/{listId}/tasks/{taskId}/linkedResources` | POST |
| List master categories | `/me/outlook/masterCategories` | GET |
| Create category | `/me/outlook/masterCategories` | POST |
| Update category color | `/me/outlook/masterCategories/{id}` | PATCH |
| Delete category | `/me/outlook/masterCategories/{id}` | DELETE |

Phase 2 additions (not in Phase 1.5):

| Operation | Endpoint | Method |
|-----------|----------|--------|
| Set email follow-up flag | `/me/messages/{id}` (PATCH flag property) | PATCH |
| Get calendar availability | `/me/calendar/getSchedule` | POST |
| List calendar events | `/me/calendarView` | GET |

**New Section 2b. Required Select Fields -- To Do Tasks:**

> When fetching tasks for status sync, request:
> ```
> $select=id,title,status,importance,dueDateTime,completedDateTime,
>         lastModifiedDateTime,categories
> ```
> The `linkedResources` navigation property must be expanded separately if needed:
> ```
> $expand=linkedResources
> ```

**Section 9 (Authentication):** Update the permissions list:

```
Required Microsoft Graph API permissions (delegated):
- Mail.ReadWrite -- read email and move between folders
- Mail.Send -- future: send digest emails
- MailboxSettings.ReadWrite -- read/write user timezone, mailbox config, and master categories (upgraded from Read in Phase 1.5)
- User.Read -- read basic user profile (name, email) for identity auto-detection
- Tasks.ReadWrite -- create and manage To Do tasks linked to emails (Phase 1.5)
- Calendars.Read -- read calendar for schedule-aware features (Phase 2)
```

### 9.5 Changes to `06-safety-and-testing.md`

**Section 1 (Autonomy Boundaries):** Add to "Agent MAY do autonomously":

```
- Create and manage categories in the Outlook master category list
- Create To Do tasks in the "AI Assistant" task list (after suggestion approval)
- Read To Do task status for sync purposes
```

Add to "Agent MUST get user approval for":

```
- Deleting orphaned categories during cleanup (interactive confirmation)
- Creating user-tier categories (Phase 2 -- proposed by agent, confirmed by user via chat)
```

Add to "Agent MUST NEVER":

```
- Delete framework categories (P1-P4, action types)
- Modify or delete tasks in any To Do list other than "AI Assistant"
- Change the color of an existing category (only set colors on newly created categories)
- Read other users' task lists or calendars
```

**Section 5 (Testing Strategy):** Add to unit tests:

```
- Task field mapping: verify priority->importance, action_type->status mapping
- Task title generation: verify truncation to 255 chars, proper formatting
- Immutable ID migration: verify mutable->immutable ID conversion logic
- task_sync CRUD: verify create, read, update, delete through store.py
- Config backward compatibility: verify missing integrations section defaults to disabled
- Category bootstrap: verify all 10 framework categories are created with correct colors
- Category bootstrap: verify taxonomy categories are created for each project/area in config
- Category bootstrap: verify existing categories are not duplicated
- Category bootstrap: verify existing category colors are never changed
- Category cleanup: verify orphaned categories are identified correctly
- Category application: verify compound category array (priority + action + taxonomy) is applied to emails
- Category application: verify tasks get priority + taxonomy only (not action type)
- Category growth: verify new project in config triggers category creation on hot-reload
```

Add to integration tests:

```
- Graph API To Do: create task list, create task with linkedResource, read task, update status, delete task
- Graph API categories: read master list, create category, delete category
- Category bootstrap end-to-end: clean slate -> bootstrap -> verify all categories exist with correct colors
- Category sync with config: add new project to config -> hot-reload -> verify category created
- End-to-end: classify email -> approve -> verify categories on email, task created with matching categories
- Immutable ID: fetch message with Prefer header -> verify ID format -> move message -> verify ID unchanged
```

### 9.6 Changes to `04-prompts.md`

**Section 3 (Triage Classification):** Add note:

> **Phase 1.5 note:** The taxonomy category (project/area name to apply as an Outlook category) is derived deterministically by the triage engine from the folder mapping: `suggested_folder` -> config project/area -> `name` -> category. Claude does not return a `taxonomy_category` field -- the classification tool schema is unchanged. No prompt changes in Phase 1.5.
>
> **Phase 2 note:** `AVAILABLE CATEGORIES` will be added to the system prompt when the learning system (Feature 2D) needs Claude to suggest new categories based on user behavior.

### 9.7 Changes to `08-classification-chat.md`

**Section 6 (add_project_or_area):** Update the implementation to also create the corresponding taxonomy category:

```
Step 6 (existing): Optionally trigger folder creation via Graph API
Step 7 (NEW):      Create taxonomy category via graph_tasks.py with project/area color
Step 8 (existing): Log to action_log
```

**Phase 2 additions** (not in Phase 1.5):
- `manage_category` tool for user-requested custom categories
- `AVAILABLE CATEGORIES` in the system prompt

### 9.8 Changes to `07-setup-guide.md`

**Section 1.5 (Configure API Permissions):** Update the permissions table:

| Permission | Purpose | Required for |
|------------|---------|-------------|
| `Mail.ReadWrite` | Read emails and move between folders | All phases -- core functionality |
| `Mail.Send` | Send emails on behalf of the user | Phase 2+ -- digest delivery via email |
| `MailboxSettings.ReadWrite` | Read/write user timezone, mailbox config, and manage master categories | All phases -- upgraded from Read in Phase 1.5 |
| `User.Read` | Read basic user profile (name, email) | All phases -- identity auto-detection |
| `Tasks.ReadWrite` | Create and manage To Do tasks linked to emails | Phase 1.5+ -- task tracking and follow-ups |
| `Calendars.Read` | Read calendar events and free/busy schedule | Phase 2+ -- schedule-aware features |

**Section 1.6 (Verify Permission Status):** Update the expected output:

```
Microsoft Graph (5)                              <- 6 after Phase 2 adds Calendars.Read
├── Mail.ReadWrite             Delegated    ✅ Granted
├── Mail.Send                  Delegated    ✅ Granted
├── MailboxSettings.ReadWrite  Delegated    ✅ Granted
├── User.Read                  Delegated    ✅ Granted
└── Tasks.ReadWrite            Delegated    ✅ Granted
```

**Section 1.7 (Add Credentials to Config):** Update the scopes list:

```yaml
auth:
  scopes:
    - "Mail.ReadWrite"
    - "Mail.Send"
    - "MailboxSettings.ReadWrite"
    - "User.Read"
    - "Tasks.ReadWrite"
    # Calendars.Read added in Phase 2
```

**Add new section after Section 2 (First-Time Authentication Flow):**

> **Section 2.1 -- Upgrading from Phase 1 (Re-authentication)**
>
> If the assistant was previously running with Phase 1 permissions (4 scopes), upgrading to Phase 1.5 requires re-authentication to consent to the new and upgraded permissions (`Tasks.ReadWrite` and `MailboxSettings.ReadWrite` replacing `MailboxSettings.Read`):
>
> 1. Stop the assistant
> 2. Delete the cached token: `rm data/token_cache.json`
> 3. Update `config.yaml` to add the new scopes and `integrations` section
> 4. Start the assistant -- it will initiate a new device code flow
> 5. Authenticate and consent to the expanded permissions
> 6. The assistant will run the category bootstrap (create framework + taxonomy categories, interactive cleanup)
> 7. The assistant will detect `immutable_ids_migrated` is not set and run the one-time ID migration

### 9.9 Changes to `09-architecture-decisions.md`

**Section 9 (Summary of Spec Document Impact):** Add a row noting this document:

| Document | Impact | Details |
|----------|--------|---------|
| `10-native-task-integration.md` | New document | Phase 1.5 architecture: To Do tasks, category management, immutable IDs, hybrid sync pattern. Phase 2 additions: email flags, calendar awareness, bidirectional sync. |

---

## 10. Phase 1.5 Build Order

These items should be implemented in sequence. Each builds on the previous.

1. **Permissions and auth** -- Update `config.yaml` scopes (add `Tasks.ReadWrite`, upgrade `MailboxSettings.Read` to `MailboxSettings.ReadWrite`), delete token cache, re-authenticate. Verify new permissions are granted in Azure portal.

2. **Immutable ID migration** -- Add `Prefer: IdType="ImmutableId"` header to all message fetch requests in `graph_client.py`. Implement one-time migration in `store.py` to re-fetch and update stored message IDs. Set `immutable_ids_migrated` in `agent_state`. **Test independently against live mailbox before proceeding.**

3. **`graph_tasks.py` module** -- Implement To Do task list discovery/creation, task CRUD with linkedResources, and **category management** (read master list, create/delete categories). Follow the same error handling patterns as `graph_client.py` (retry with exponential backoff, structured logging, 429 handling).

4. **Category bootstrap + cleanup** -- On first run, ensure all framework categories (4 priority + 6 action type) exist in the master category list with correct colors (never override existing colors). Then create taxonomy categories for each project and area in `config.yaml`. Run interactive cleanup of orphaned categories. Set `categories_bootstrapped` in `agent_state`.

5. **`task_sync` table** -- Add the table via database migration in `store.py`. Implement CRUD operations in `store.py` (do not import `aiosqlite` in `graph_tasks.py`).

6. **Config schema update** -- Add `IntegrationsConfig` Pydantic model with nested `TodoConfig`. All defaults to preserve backward compatibility. Add to config hot-reload.

7. **Triage engine integration point** -- After a suggestion is approved in the Review UI, call `graph_tasks.py` to create the To Do task. Apply compound categories (priority + action type + taxonomy) to the email and (priority + taxonomy) to the To Do task. Record in `task_sync`. This is a hook into the existing approval flow, not a rewrite of it.

8. **Chat tool extension** -- Update `add_project_or_area` tool to also create taxonomy categories via `graph_tasks.py`.

9. **Config hot-reload** -- When hot-reload detects new projects or areas, check for and create missing taxonomy categories.

10. **Tests** -- Unit and integration tests per Section 9.5.

11. **Documentation** -- Update all spec documents per Section 9 of this document.

---

## 11. What Phase 1.5 Does NOT Include

To keep the scope bounded, these are explicitly deferred to Phase 2. Full specifications for each are preserved in `PHASE_2_INTELLIGENCE.md` under the relevant feature.

| Deferred Item | Phase 2 Feature | Reason |
|---------------|----------------|--------|
| Email `followUpFlag` operations | 2B (Waiting-For Tracker) | To Do tasks provide the same visibility with more context; let tasks prove value first |
| Calendar awareness (`Calendars.Read`) | 2C (Daily Digest) | YAGNI -- the digest (its primary consumer) doesn't exist until Phase 2 |
| Task sync cycle (bidirectional status reading from To Do) | 2B (Waiting-For Tracker) | One-directional flow is sufficient until the waiting-for tracker needs to detect task completions |
| Chat `manage_category` tool | 2D (Learning from Corrections) | Category management through chat is a convenience, not plumbing |
| AVAILABLE CATEGORIES in triage/chat prompts | 2D (Learning from Corrections) | Categories are applied deterministically in Phase 1.5; Claude doesn't need visibility until the learning system can suggest new categories |
| Category growth through learning from corrections | 2D (Learning from Corrections) | Detecting manually-applied categories and proposing formalization requires the Phase 2 corrections analysis pipeline |
| Waiting-for auto-detection (Claude analyzing reply state) | 2B (Waiting-For Tracker) | This is Claude intelligence logic, not plumbing |
| Daily digest generation | 2C (Daily Digest) | Uses the plumbing established here, but the digest itself is a Phase 2 feature |
| Calendar event creation ("time to respond" blocks) | Future | Requires `Calendars.ReadWrite` and is a user-facing feature, not plumbing |
| To Do task webhooks (real-time sync) | Future | Polling is sufficient; webhooks require public endpoint infrastructure |
| Planner integration | Phase 4 | Planner is team-oriented; To Do is correct for single-user |

---

## 12. Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| User has no Microsoft To Do license | Low | Agent can't create tasks | Check on first run; if To Do API returns 403, set `todo_enabled = false` in `agent_state` and log WARNING. Classification and categories continue working. |
| Rate limiting on To Do API | Low | Task creation fails intermittently | To Do API shares the 10,000 req/10min Graph API limit. At ~100 emails/day with ~30% actionable, that's ~30 task operations/day -- negligible. Use same retry/backoff as mail operations. |
| Immutable ID migration breaks existing data | Medium | Broken email references in SQLite | Run migration carefully: fetch with immutable header, match by subject+sender+date for any IDs that return 404, log warnings for unmatchable records. Do not delete unmatched records. Test independently before building on top. |
| User confused by AI-created tasks in To Do | Medium | User deletes tasks or ignores the list | Isolate to a dedicated "AI Assistant" list. Include clear context in task body. Agent detects deletions gracefully (task missing = treat as dismissed). |
| Existing category debris causes confusion | Medium | Cluttered category picker, inconsistent colors | Interactive cleanup during first bootstrap -- user approves which orphans to delete. Never auto-delete. |
| Category namespace collision with existing user categories | Medium | Agent skips creation, user sees unexpected behavior | On bootstrap, read existing master categories first. If a name already exists, skip creation and preserve the existing category's color. Log INFO noting the existing category was preserved. |
| Too many categories overwhelm the user | Low | Category picker in Outlook becomes unwieldy | Framework categories are fixed at 10. Taxonomy categories grow with projects/areas (typically 10-20). Total should stay under 40 -- well within Outlook's handling capacity. Cleanup removes debris. |
| Token cache deletion loses auth | Low | User must re-authenticate | This is expected and documented. The device code flow takes <1 minute. |

---

## 13. Decision Rationale Summary

The core insight is that **the output of AI intelligence should be surfaced where the user already works**. A CEO checking email on their phone should see follow-up reminders in To Do without opening a custom web UI. An overdue waiting-for item should trigger a push notification, not sit silently in a SQLite table until the next time the CEO opens `localhost:8080/waiting`. A project tag on an email should show the same color in Outlook, To Do, and the calendar -- providing a unified visual language across the entire Microsoft 365 surface.

Native Microsoft 365 features provide the user-facing surface. Claude provides the intelligence that decides *what* to surface and *when*. SQLite provides the AI's memory. Categories provide the shared visual vocabulary that ties it all together. The four systems serve complementary roles with minimal overlap.

**Phase 1.5** establishes the lean plumbing: To Do task creation, category management, immutable IDs, and the triage engine hook. **Phase 2** builds intelligence on top: bidirectional sync, email flags, calendar awareness, and category growth through learning. This two-phase approach delivers immediate value (tasks visible across all devices, color-coded email categories) while deferring complexity until its consumers exist.

The permission cost is one new scope plus one upgrade. The implementation cost is one new module, one new table, category bootstrap logic, and hooks into the existing approval flow. The user experience improvement is substantial: cross-device task visibility, consistent color-coded labeling across Microsoft 365 apps, and a clean category taxonomy -- all without building that infrastructure ourselves.
