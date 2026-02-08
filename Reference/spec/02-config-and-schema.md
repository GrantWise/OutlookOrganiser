# Outlook AI Assistant Ã¢â‚¬â€ Config & Data Schema

> **Parent doc:** `01-overview.md` | **Read when:** Working on config loading, database setup, Pydantic schema, or folder/category taxonomy.

---

## 1. Folder Taxonomy (PARA-based)

The agent uses folders exclusively for organizational taxonomy. Action state (needs reply, waiting for, etc.) is handled via Outlook categories, not folders Ã¢â‚¬â€ since an email can only live in one folder, folders represent *what* the email is about, while categories represent *what to do* about it.

```
Inbox/                          # Untriaged incoming mail (standard)
Projects/                       # Active projects with defined outcomes
  Ã¢â€Å"Ã¢â€â‚¬Ã¢â€â‚¬ {project-name}/           # One subfolder per active project
  Ã¢â€â€Ã¢â€â‚¬Ã¢â€â‚¬ .../
Areas/                          # Ongoing responsibilities (no end date)
  Ã¢â€Å"Ã¢â€â‚¬Ã¢â€â‚¬ {area-name}/              # One subfolder per area
  Ã¢â€â€Ã¢â€â‚¬Ã¢â€â‚¬ .../
Reference/                      # Useful information, no action needed
  Ã¢â€Å"Ã¢â€â‚¬Ã¢â€â‚¬ Newsletters/
  Ã¢â€Å"Ã¢â€â‚¬Ã¢â€â‚¬ Industry/
  Ã¢â€â€Ã¢â€â‚¬Ã¢â€â‚¬ Vendor Updates/
Archive/                        # Completed projects, old threads
  Ã¢â€Å"Ã¢â€â‚¬Ã¢â€â‚¬ {completed-project-name}/
  Ã¢â€â€Ã¢â€â‚¬Ã¢â€â‚¬ .../
```

**Design rationale:** Earlier versions included `_Action Required` and `_Waiting For` as top-level folders. These were removed because an email can only exist in one Outlook folder Ã¢â‚¬â€ forcing a choice between "this is about the Tradecore project" and "this needs a reply" breaks one organizational axis. Outlook categories solve this cleanly since multiple categories can be applied to a single email simultaneously.

**Folder creation rules:**
- The agent proposes folder creation during bootstrap; user approves
- In ongoing operation, the agent suggests new project folders when it detects a new project pattern
- The agent never deletes folders, only proposes archival moves

**Note on existing Outlook rules:** The user may have server-side Outlook rules that automatically move emails out of Inbox. These emails will be invisible to the triage engine since it only monitors Inbox. Before going live, the user should review existing rules and either disable conflicting ones or configure the triage engine to also scan affected folders (via the `triage.watch_folders` config option).

---

## 2. Category Labels (Outlook Categories)

Outlook categories provide cross-cutting classification that overlays the folder structure. Multiple categories can be applied to a single email simultaneously.

**Priority (Eisenhower matrix):**

| Category | Color | Meaning |
|----------|-------|---------|
| `P1 - Urgent Important` | Red | Act now Ã¢â‚¬â€ client escalation, deadline today, blocker |
| `P2 - Important` | Orange | Schedule time Ã¢â‚¬â€ strategic work, key decisions, planning |
| `P3 - Urgent Low` | Blue | Delegate or batch Ã¢â‚¬â€ quick replies, routine requests |
| `P4 - Low` | Grey | Archive or defer Ã¢â‚¬â€ FYI, informational, nice-to-have |

**Action type:**

| Category | Meaning |
|----------|---------|
| `Needs Reply` | User needs to respond to this email |
| `Waiting For` | User is waiting for an external response in this thread |
| `Delegated` | User has forwarded or assigned to someone else |
| `FYI Only` | Informational, no action required |
| `Scheduled` | Action planned for a specific date |
| `Review` | Needs user review/approval (document, PR, proposal) |

**Category management (Phase 1.5):**

Categories are no longer a static list defined only in documentation. The agent programmatically manages categories in the Outlook master category list via `/me/outlook/masterCategories` (requires `MailboxSettings.ReadWrite`).

Categories are organized in three tiers:
- **Framework categories** (the 4 priority + 6 action type categories listed above) -- created automatically on first run, never deleted
- **Taxonomy categories** -- one category per project and area in `config.yaml`, created during bootstrap and when new projects/areas are added. Applied to emails and To Do tasks alongside priority categories for cross-app visibility.
- **User categories** (Phase 2) -- custom categories created through the classification chat or detected from user behavior. Proposed by the agent, confirmed by the user.

The same categories are applied consistently to emails and To Do tasks. This provides unified color-coded labeling across the CEO's Microsoft 365 experience.

**Category-to-Color mapping:**

| Category | Preset | Color |
|----------|--------|-------|
| `P1 - Urgent Important` | preset0 | Red |
| `P2 - Important` | preset1 | Orange |
| `P3 - Urgent Low` | preset7 | Blue |
| `P4 - Low` | preset14 | DarkSteel |
| `Needs Reply` | preset3 | Yellow |
| `Waiting For` | preset8 | Teal |
| `Delegated` | preset9 | Olive |
| `Review` | preset5 | Purple |
| `FYI Only` | preset15 | Steel |
| `Scheduled` | preset10 | Green |

Taxonomy categories (projects/areas) use presets in a rotating sequence from the remaining presets. The agent only sets colors on categories it creates fresh -- if a category already exists in the master list (even from a prior attempt), its existing color is preserved.

---

## 3. SQLite Schema

### Database Initialization

On first startup, enable WAL (Write-Ahead Logging) mode for SQLite. This is critical because the triage engine and FastAPI review UI may write to the database concurrently. WAL mode allows concurrent readers and a single writer without blocking.

> **Ref:** SQLite WAL mode documentation: https://www.sqlite.org/wal.html
> WAL provides better concurrency than the default rollback journal mode. Multiple readers can operate while a write is in progress.

```sql
-- MUST be set before creating tables. Persists across connections.
PRAGMA journal_mode=WAL;
```

### Tables

```sql
-- Track every email the agent has processed
CREATE TABLE emails (
    id TEXT PRIMARY KEY,                    -- Graph API message ID
    conversation_id TEXT,                   -- Graph API conversation thread ID
    conversation_index TEXT,                -- Graph API conversationIndex (base64, encodes thread depth)
    subject TEXT,
    sender_email TEXT,
    sender_name TEXT,
    received_at DATETIME,
    snippet TEXT,                           -- First 1000 chars of cleaned body (see snippet processing)
    current_folder TEXT,                    -- Current Outlook folder path
    web_link TEXT,                          -- OWA deep link URL from Graph API webLink field
    importance TEXT DEFAULT 'normal',       -- Sender-set importance: 'low', 'normal', 'high'
    is_read INTEGER DEFAULT 0,             -- 1 if user has read the email
    flag_status TEXT DEFAULT 'notFlagged',  -- 'notFlagged', 'flagged', 'complete'
    has_user_reply INTEGER DEFAULT 0,       -- 1 if user has replied in this thread
    inherited_folder TEXT,                  -- If set, folder was inherited from prior thread classification
    processed_at DATETIME,
    classification_json TEXT,              -- Full Claude classification result
    classification_attempts INTEGER DEFAULT 0,  -- Retry counter for failed classifications
    classification_status TEXT DEFAULT 'pending' -- 'pending', 'classified', 'failed'
);

-- Index for efficient thread inheritance lookups
CREATE INDEX idx_emails_conversation_id ON emails(conversation_id);

-- Index for efficient sender history lookups
CREATE INDEX idx_emails_sender ON emails(sender_email);

-- Track compound suggestions (one row per classification, covers folder + priority + action)
CREATE TABLE suggestions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email_id TEXT REFERENCES emails(id),
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,

    -- Suggested classification (compound Ã¢â‚¬â€ all fields from one classification event)
    suggested_folder TEXT,                  -- e.g., 'Projects/Tradecore Steel'
    suggested_priority TEXT,                -- e.g., 'P2 - Important'
    suggested_action_type TEXT,             -- e.g., 'Needs Reply'
    confidence REAL,                        -- 0.0-1.0 from Claude
    reasoning TEXT,                         -- Claude's one-sentence explanation

    -- User decision (per-field approval/correction)
    status TEXT DEFAULT 'pending',          -- 'pending', 'approved', 'rejected', 'partial'
    approved_folder TEXT,                   -- NULL = pending, or user's chosen folder
    approved_priority TEXT,                 -- NULL = pending, or user's chosen priority
    approved_action_type TEXT,              -- NULL = pending, or user's chosen action type
    resolved_at DATETIME
);

-- Track "Waiting For" threads
CREATE TABLE waiting_for (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email_id TEXT REFERENCES emails(id),
    conversation_id TEXT,                   -- For monitoring thread replies
    waiting_since DATETIME,
    expected_from TEXT,                     -- Email address we're waiting on
    description TEXT,                       -- What we're waiting for
    status TEXT DEFAULT 'waiting',          -- 'waiting', 'received', 'expired'
    nudge_after_hours INTEGER DEFAULT 48,
    resolved_at DATETIME
);

-- Map emails to To Do tasks (Phase 1.5)
CREATE TABLE task_sync (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email_id TEXT REFERENCES emails(id),
    todo_task_id TEXT NOT NULL,             -- Graph API task ID
    todo_list_id TEXT NOT NULL,             -- Graph API task list ID
    task_type TEXT NOT NULL,                -- e.g., 'waiting_for', 'needs_reply', 'review', 'delegated'
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    synced_at DATETIME,                    -- Last time sync status was checked
    status TEXT DEFAULT 'active'           -- 'active', 'completed', 'deleted'
);

CREATE INDEX idx_task_sync_email ON task_sync(email_id);
CREATE INDEX idx_task_sync_todo ON task_sync(todo_task_id);

-- Agent state persistence (cursors, tokens, counters)
CREATE TABLE agent_state (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
-- Keys: 'last_processed_timestamp', 'delta_token', 'last_bootstrap_run',
--        'last_digest_run', 'authenticated_user_email',
--        'classification_preferences', 'config_schema_version',
--        'todo_list_id', 'todo_enabled', 'categories_bootstrapped',
--        'immutable_ids_migrated'
--
-- 'classification_preferences': Natural language text describing learned
--   classification patterns derived from user corrections. Updated after
--   each correction batch. Included in the Claude triage prompt as context.
--   Inspired by LangChain's "triage preferences" memory pattern.
--   Ref: https://github.com/langchain-ai/agents-from-scratch
--        (Module 4: HITL memory updates from user feedback)
--
-- 'config_schema_version': Integer version number for config.yaml schema.
--   Incremented when new required fields are added. On startup, if the
--   stored version is older than the current code version, run migration
--   logic to add new fields with defaults. Prevents config breakage on
--   upgrade.

-- Persistent sender profiles for faster classification routing
-- Populated during bootstrap, updated incrementally during triage.
-- Ref: Inbox Zero's sender-level categorization approach
--   https://github.com/elie222/inbox-zero (sender categorization feature)
--   Categorizing senders (not just individual emails) enables faster routing
--   decisions and a "manage senders" UI page.
CREATE TABLE sender_profiles (
    email TEXT PRIMARY KEY,
    display_name TEXT,
    domain TEXT,
    category TEXT DEFAULT 'unknown',    -- 'key_contact', 'newsletter', 'automated',
                                        -- 'internal', 'client', 'vendor', 'unknown'
    default_folder TEXT,                -- Most common approved folder for this sender
    email_count INTEGER DEFAULT 0,      -- Total emails processed from this sender
    last_seen DATETIME,
    auto_rule_candidate INTEGER DEFAULT 0,  -- 1 if >90% to single folder with 10+ emails
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_sender_profiles_domain ON sender_profiles(domain);
CREATE INDEX idx_sender_profiles_category ON sender_profiles(category);

-- LLM request/response log for debugging classification issues
-- Stores full prompt and response for every Claude API call.
-- Ref: gmail-llm-labeler's metrics and logging approach
--   https://github.com/ColeMurray/gmail-llm-labeler
--   (Blog: https://www.colemurray.com/blog/automate-email-labeling-gmail-llm)
--   Logging every LLM interaction enables prompt iteration, accuracy debugging,
--   and cost tracking. Retained for 30 days by default.
CREATE TABLE llm_request_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
    task_type TEXT,                      -- 'triage', 'bootstrap', 'digest', 'waiting_for'
    model TEXT,                          -- Model string used (e.g., 'claude-haiku-4-5-20251001')
    email_id TEXT,                       -- NULL for non-email tasks (digest, bootstrap)
    triage_cycle_id TEXT,                -- Correlation ID for the triage cycle
    prompt_json TEXT,                    -- Full prompt sent to Claude (messages array)
    response_json TEXT,                  -- Full response from Claude
    tool_call_json TEXT,                 -- Extracted tool call result (if applicable)
    input_tokens INTEGER,
    output_tokens INTEGER,
    duration_ms INTEGER,
    error TEXT                           -- NULL on success, error message on failure
);

CREATE INDEX idx_llm_log_timestamp ON llm_request_log(timestamp);
CREATE INDEX idx_llm_log_email ON llm_request_log(email_id);

-- Audit log of all agent actions
CREATE TABLE action_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
    action_type TEXT,                       -- 'classify', 'move', 'categorize', 'suggest', 'bootstrap'
    email_id TEXT,
    details_json TEXT,                      -- Full action details
    triggered_by TEXT                       -- 'auto', 'user_approved', 'bootstrap'
);
```

---

## 4. Config Validation

The config.yaml is the single source of truth for the entire system. It must be validated on every load.

**Validation approach:**
- Define a Pydantic model (`ConfigSchema`) that mirrors the config.yaml structure
- On startup and on every hot-reload, parse YAML then validate against the Pydantic model
- Report specific, actionable errors: `"Project 'Tradecore Steel' is missing required key 'folder'"`
- If validation fails on hot-reload, keep the previous valid config and log a WARNING
- If validation fails on startup, exit with a clear error message

### Config Schema Versioning

The config.yaml includes a `schema_version` integer at the top level. When the code expects a newer schema version than the file contains:

1. On startup, compare `schema_version` in the file against `CURRENT_SCHEMA_VERSION` in code
2. If the file version is older, apply migrations sequentially (v1->v2, v2->v3, etc.)
3. Migrations add new fields with sensible defaults -- never remove or rename existing fields
4. Write the migrated config back to disk (with backup: `config.yaml.bak.{timestamp}`)
5. Log the migration at INFO level

**Design rationale:** As the project evolves through build phases, new config fields will be added (e.g., `digest.delivery` in Phase 2, `auto_mode.confidence_threshold` in Phase 3). Without versioning, users upgrading the agent would face opaque Pydantic validation errors. All new fields should be added as optional with defaults so that an un-migrated config still loads -- the migration just makes the defaults explicit in the file.

**CLI validation command:**
```bash
python -m assistant validate-config
```

---

## 5. config.yaml Structure

This is the primary configuration file. It is generated by the bootstrap scanner and then edited by the user. The agent reads this file on every triage cycle so changes take effect immediately (subject to validation).

```yaml
# config.yaml Ã¢â‚¬â€ Outlook AI Assistant Configuration
schema_version: 1                # Increment when new required fields are added (see Section 4)


# -- Authentication (see 07-setup-guide.md for Azure AD setup) --
auth:
  client_id: ""                 # Azure AD Application (client) ID
  tenant_id: ""                 # Azure AD Directory (tenant) ID Ã¢â‚¬â€ or "common" for personal accounts
  scopes:
    - "Mail.ReadWrite"
    - "Mail.Send"
    - "MailboxSettings.ReadWrite"    # UPGRADED from Read -- Phase 1.5
    - "User.Read"
    - "Tasks.ReadWrite"              # NEW -- Phase 1.5
    # Calendars.Read added in Phase 2
  token_cache_path: "/app/data/token_cache.json"

# -- Identity --
# user_email is auto-detected from the Graph API /me endpoint on first auth.
# Override here only if auto-detection fails.
# user_email: "grant@translution.com"
timezone: "America/New_York"  # Used for scheduling and digest times

# -- Triage Settings --
triage:
  interval_minutes: 15          # How often to check for new mail
  lookback_hours: 2             # On restart, re-check emails from this window
  batch_size: 20                # Max emails to process per triage cycle
  mode: "suggest"               # "suggest" or "auto" (future)
  watch_folders: ["Inbox"]      # Folders to monitor (add others if Outlook rules move mail)

# -- Model Selection (per task, upgrade individual tasks if accuracy is low) --
models:
  bootstrap: "claude-sonnet-4-5-20250929"
  bootstrap_merge: "claude-sonnet-4-5-20250929"
  triage: "claude-haiku-4-5-20251001"
  dry_run: "claude-haiku-4-5-20251001"
  digest: "claude-haiku-4-5-20251001"
  waiting_for: "claude-haiku-4-5-20251001"

# -- Snippet Processing --
snippet:
  max_length: 1000              # Characters to extract from email body
  strip_signatures: true        # Remove signature blocks (-- or _____ separators)
  strip_disclaimers: true       # Remove CONFIDENTIAL/legal disclaimer blocks
  strip_forwarded_headers: true # Remove forwarded message headers

# -- Projects (active, have defined outcomes) --
projects:
  - name: "Tradecore Steel Implementation"
    folder: "Projects/Tradecore Steel"
    signals:
      subjects: ["tradecore", "outbound process", "steel scanning", "proof of delivery"]
      senders: ["*@tradecoresteel.co.za"]
      body_keywords: ["weight validation", "android scanning"]
    priority_default: "P2 - Important"

  - name: "SOC 2 Compliance"
    folder: "Projects/SOC 2"
    signals:
      subjects: ["soc 2", "security audit", "compliance", "penetration test"]
      senders: []
      body_keywords: ["security controls", "audit evidence"]
    priority_default: "P2 - Important"

  - name: ".NET 9 Migration"
    folder: "Projects/NET9 Migration"
    signals:
      subjects: [".net 9", "modernization", "framework migration"]
      senders: []
      body_keywords: [".net 9", "target framework"]
    priority_default: "P2 - Important"

# -- Areas (ongoing responsibilities, no end date) --
areas:
  - name: "Sales & Prospects"
    folder: "Areas/Sales"
    signals:
      subjects: ["demo", "pricing", "proposal", "quote", "rfp", "trial"]
      senders: []
      body_keywords: ["interested in", "pricing", "demo request"]
    priority_default: "P2 - Important"

  - name: "Development Team"
    folder: "Areas/Development"
    signals:
      subjects: ["pull request", "deployment", "release", "bug", "sprint"]
      senders: []
      body_keywords: ["merge request", "code review"]
    priority_default: "P3 - Urgent Low"

  - name: "Client Support"
    folder: "Areas/Support"
    signals:
      subjects: ["support ticket", "issue", "error", "not working", "urgent"]
      senders: []
      body_keywords: ["experiencing issues", "error message"]
    priority_default: "P1 - Urgent Important"

  - name: "SYSPRO Relationship"
    folder: "Areas/SYSPRO"
    signals:
      subjects: ["syspro", "partner", "integration"]
      senders: ["*@syspro.com"]
      body_keywords: []
    priority_default: "P2 - Important"

  - name: "Immigration & Relocation"
    folder: "Areas/Immigration"
    signals:
      subjects: ["visa", "green card", "embassy", "immigration", "relocation"]
      senders: []
      body_keywords: ["petition", "i-140", "consular"]
    priority_default: "P2 - Important"

# -- Auto-routing rules (high confidence, skip Claude classification) --
auto_rules:
  - name: "GitHub Notifications"
    match:
      senders: ["notifications@github.com"]
    action:
      folder: "Reference/Dev Notifications"
      category: "FYI Only"
      priority: "P4 - Low"

  - name: "Calendar Notifications"
    match:
      senders: ["*@calendar.google.com", "*@outlook.com"]
      subjects: ["accepted", "declined", "invitation", "canceled"]
    action:
      folder: "Reference/Calendar"
      category: "FYI Only"
      priority: "P4 - Low"

  - name: "Newsletters"
    match:
      senders: []  # Populated during bootstrap via sender pattern analysis
    action:
      folder: "Reference/Newsletters"
      category: "FYI Only"
      priority: "P4 - Low"

# -- Key contacts (for context in classification) --
key_contacts:
  - email: "ceo@syspro.com"
    role: "SYSPRO CEO"
    priority_boost: 1  # Bump priority by 1 level

  - email: "cfo@translution.com"
    role: "CFO"
    priority_boost: 1

# -- Aging thresholds --
aging:
  needs_reply_warning_hours: 24
  needs_reply_critical_hours: 48
  waiting_for_nudge_hours: 48
  waiting_for_escalate_hours: 96

# -- Digest settings --
digest:
  enabled: true
  schedule: "08:00"              # Local time, daily
  delivery: "stdout"             # "stdout", "email", or "file"
  include_sections:
    - "overdue_replies"
    - "aging_waiting_for"
    - "new_high_priority"
    - "classification_summary"

# -- Auto-rules hygiene --
# Ref: Inbox Zero reports problems with auto-generated rules accumulating over time
#   https://github.com/elie222/inbox-zero (ARCHITECTURE.md -- rule management lessons)
#   Without limits, hundreds of rules can accumulate with conflicts.
auto_rules_hygiene:
  max_rules: 100                 # Warn user when auto_rules exceeds this count
  warn_on_conflicts: true        # Detect overlapping sender/subject patterns across rules
  consolidation_check_days: 30   # Suggest consolidation for rules with <5 matches in N days

# -- Suggestion queue management --
suggestion_queue:
  expire_after_days: 14          # Auto-expire pending suggestions older than N days
  auto_approve_confidence: 0.95  # Auto-approve suggestions above this threshold after 48h (Phase 2)
  auto_approve_delay_hours: 48   # Wait this long before auto-approving high-confidence suggestions

# -- Integrations (Phase 1.5+) --
integrations:
  todo:
    enabled: true                    # Create To Do tasks for actionable emails
    list_name: "AI Assistant"        # Name of the To Do list to use (created if missing)
    create_for_action_types:         # Which action types generate tasks
      - "Waiting For"
      - "Needs Reply"
      - "Review"
      - "Delegated"
  # email_flags and calendar sections added in Phase 2 -- see PHASE_2_INTELLIGENCE.md

# -- LLM request logging --
llm_logging:
  enabled: true
  retention_days: 30             # Delete LLM request logs older than N days
  log_prompts: true              # Store full prompts (disable to save disk space)
  log_responses: true            # Store full responses
```

---

## 6. Configuration Hot-Reload

The agent checks `config.yaml` mtime on each triage cycle. If changed:
1. Parse YAML
2. Validate against Pydantic schema
3. If valid: swap in new config, log INFO event
4. If invalid: keep previous config, log WARNING with specific errors

No restart required for valid config changes.
