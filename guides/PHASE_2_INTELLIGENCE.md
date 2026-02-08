# Phase 2 — Intelligence

> **Prerequisites:** Phase 1 (Foundation/MVP) and Phase 1.5 (Native M365 Integration) fully implemented and tested against a live mailbox.
> **Theme:** Make the system smarter, faster, and self-improving.
> **Builds on:** All existing modules — extends, never replaces.
>
> **Phase 1.5 plumbing available:** Phase 1.5 establishes native Microsoft 365 integration — To Do task creation via `graph_tasks.py`, category management (framework + taxonomy categories in the Outlook master category list), immutable message IDs, the `task_sync` table, and the triage engine hook that creates tasks on suggestion approval. Phase 2 features 2B, 2C, and 2D build directly on this plumbing to add bidirectional sync, email flags, calendar awareness, and category growth through learning. See `Reference/spec/10-native-task-integration.md` for full architecture details.

---

## Overview

Phase 1 delivers the core loop: scan, classify, suggest, review. Phase 1.5 adds native M365 integration: To Do tasks, categories, and immutable IDs. Phase 2 makes the loop *intelligent*. The system learns from user corrections, detects patterns in sender behavior, reduces API costs through delta queries, generates daily digests to surface what matters, and provides dashboards for accuracy tracking and sender management.

Phase 2 also absorbs several items originally scoped for Phase 1.5 but deferred to keep Phase 1.5 lean: email `followUpFlag` operations (Feature 2B), calendar awareness with `Calendars.Read` (Feature 2C), bidirectional task sync (Feature 2B), the `manage_category` chat tool (Feature 2D), `AVAILABLE CATEGORIES` in prompts (Feature 2D), and category growth through learning (Feature 2D).

Every feature in this phase extends the existing foundation. No existing interfaces change. New config fields are additive with sensible defaults so existing `config.yaml` files continue to work without modification.

---

## Feature Summary

| # | Feature | New Files | Extended Files |
|---|---------|-----------|----------------|
| 2A | Delta Queries + Fast Polling | — | `graph/client.py`, `engine/triage.py` |
| 2B | Waiting-For Tracker + To Do Sync + Email Flags | `engine/waiting_for.py` | `engine/triage.py`, `web/routes.py`, `graph/tasks.py`, `db/store.py` |
| 2C | Daily Digest + Calendar Awareness | `engine/digest.py` | `engine/triage.py`, `cli.py`, `web/routes.py`, `classifier/prompts.py`, `graph/tasks.py` |
| 2D | Learning from Corrections + Category Growth | `classifier/preference_learner.py` | `web/routes.py`, `classifier/prompts.py`, `chat/tools.py`, `graph/tasks.py` |
| 2E | Sender Affinity Auto-Rules | — | `web/routes.py`, `classifier/auto_rules.py`, `config.py` |
| 2F | Auto-Rules Hygiene | — | `classifier/auto_rules.py`, `cli.py` |
| 2G | Suggestion Queue Management | — | `engine/triage.py`, `db/store.py` |
| 2H | Stats & Accuracy Dashboard | `web/templates/stats.html` | `web/routes.py`, `db/store.py` |
| 2I | Sender Management Page | `web/templates/senders.html` | `web/routes.py`, `db/store.py` |
| 2K | Confidence Calibration | — | `db/store.py`, stats dashboard |
| 2M | Enhanced Graceful Degradation | — | `engine/triage.py`, `web/routes.py` |

> **Removed features:** 2J (Webhook + Delta Hybrid) was removed per architecture decision — webhooks require public HTTPS infrastructure that is unjustified for a single-user Docker-local deployment. Delta query polling at 5-minute intervals achieves near-real-time without the complexity. 2L (Token Cache Encryption) was removed — file permissions (mode 600) provide sufficient protection for the Docker-local deployment model.

---

## Recommended Implementation Order

Features are ordered by dependency chain and diminishing returns. Sub-phases can be committed independently — each is a self-contained increment.

```
Priority 1 — Foundation:
  2A  Delta Queries + Fast Polling  (replaces timestamp polling, enables 5-min cycles)

Priority 2 — High Value, Independent:
  2D  Learning from Corrections     (high ROI, no dependencies)
  2G  Suggestion Queue Management   (small scope, quick win)
  2E  Sender Affinity Auto-Rules    (quick win)

Priority 3 — Active Tracking (builds on Phase 1.5 graph_tasks.py):
  2B  Waiting-For Tracker + To Do Sync + Email Flags  (builds on Phase 1.5, foundation for 2C)
  2C  Daily Digest + Calendar Awareness               (builds on Phase 1.5 + 2B, adds Calendars.Read)
  2F  Auto-Rules Hygiene                              (pairs with 2E)

Priority 4 — Dashboards:
  2H  Stats & Accuracy Dashboard    (depends on correction data from 2D)
  2K  Confidence Calibration        (integrates into 2H)
  2I  Sender Management Page        (independent, UI-only)

Priority 5 — Hardening:
  2M  Enhanced Graceful Degradation (independent, reliability)
```

---

## 2A: Delta Queries + Fast Polling

### What It Does

Replaces timestamp-based polling (`receivedDateTime > last_processed_timestamp`) with Microsoft Graph delta queries and reduces the polling interval from 15 minutes to 5 minutes. Delta queries return only messages that have changed since the last sync, reducing API calls from O(all-recent-emails) to O(new-changes) per cycle. This is the primary near-real-time mechanism — webhooks were removed per architecture decision (see `Reference/spec/09-architecture-decisions.md`).

### User-Facing Behavior

Emails are processed within ~5 minutes of arrival instead of ~15 minutes. Triage cycles become faster and use fewer Graph API calls. The system silently falls back to timestamp polling if the delta token expires. The default `triage.interval_minutes` changes from 15 to 5.

### How It Builds on Existing Code

**`graph/client.py`** — Add a new method to `GraphClient`:

```python
async def get_delta_messages(
    self,
    folder: str,
    delta_token: str | None,
    select_fields: str,
    max_items: int,
) -> tuple[list[dict], str | None]:
    """Fetch messages using delta query. Returns (messages, new_delta_token).

    If delta_token is None, performs initial full sync.
    If delta token has expired (410 Gone), raises DeltaTokenExpiredError.
    """
```

**`engine/triage.py`** — Modify `_fetch_new_emails()`:
- Try delta query first using stored token from `agent_state`
- On success: use returned messages, store new delta token
- On `DeltaTokenExpiredError` (410 Gone): log WARNING, fall back to timestamp-based fetch, clear stored token
- On next cycle: initiate fresh delta sync (no token = full initial sync)

### Database Changes

No schema changes. Uses existing `agent_state` key-value store:
- Key: `delta_token` — already anticipated in the schema comments
- Key: `delta_token_folder` — which folder the token belongs to (for multi-folder support)

### Config Changes

The default polling interval changes from 15 minutes to 5 minutes. No new config fields are needed — delta queries are used automatically when a delta token is available in `agent_state`, falling back to timestamp polling otherwise.

### Error Handling

| Error | Response |
|-------|----------|
| 410 Gone (expired token) | Log WARNING: "Delta token expired for folder '{folder}'. Performing full sync this cycle." Fall back to timestamp polling. Clear stored token. Next cycle starts fresh delta sync. |
| Pagination timeout | Follow `@odata.nextLink` with existing retry/backoff. If total pages exceed a safety limit (100), log WARNING and process what we have. |
| Duplicate messages in delta | Deduplicate by Graph API `id` before processing. For messages already in `emails` table, check if `current_folder` changed — update folder field but do not re-classify. |

### New Exception

```python
class DeltaTokenExpiredError(GraphAPIError):
    """Delta sync token has expired (410 Gone). Must perform full sync."""
```

Add to `core/errors.py`, inheriting from existing `GraphAPIError`.

### Testing Strategy

**Unit tests:**
- Delta query returns messages + new token
- 410 Gone triggers `DeltaTokenExpiredError`
- Fallback to timestamp polling on expired token
- Deduplication of messages with same `id`
- Folder-change detection for moved messages

**Integration tests:**
- Full delta lifecycle: initial sync -> delta token -> incremental sync -> 410 Gone -> recovery

### Verification Checklist

- [ ] Delta query fetches only changed messages
- [ ] Delta token stored in `agent_state` after each cycle
- [ ] 410 Gone handled gracefully with WARNING log and timestamp fallback
- [ ] Duplicate messages deduplicated before processing
- [ ] Moved messages update `current_folder` without re-classification
- [ ] No user-visible behavior change

---

## 2B: Waiting-For Tracker + To Do Sync + Email Flags

### What It Does

Transforms the existing passive `waiting_for` table into an active tracking system with native Microsoft 365 integration. Each triage cycle checks tracked conversations for new replies. Items are automatically resolved when a reply arrives, and escalated in the daily digest when overdue. Builds on Phase 1.5 plumbing to write waiting-for items to both SQLite and To Do, add bidirectional task sync, and optionally set email `followUpFlag` for Outlook visibility.

### Phase 1.5 Dependencies

This feature builds on Phase 1.5 infrastructure:
- **`graph_tasks.py`** — To Do task CRUD with linkedResources (already implemented)
- **`task_sync` table** — maps email IDs to To Do task IDs (already implemented)
- **Triage engine hook** — creates tasks on approval (already implemented)

Phase 2 adds: bidirectional sync (reading completion status back), email flags, and active monitoring logic.

### User-Facing Behavior

- Waiting-for items auto-resolve when a reply is detected in the tracked conversation
- Overdue items (past `nudge_after_hours`) appear in the daily digest with age indicators
- Critical items (past `escalate_after_hours`) are highlighted prominently
- The `/waiting` page shows aging indicators and status transitions

### How It Builds on Existing Code

**New file: `engine/waiting_for.py`**

```python
class WaitingForTracker:
    """Active monitoring of waiting-for items.

    Called each triage cycle to check for resolved items and flag overdue ones.
    Writes to both SQLite (AI metadata) and To Do (user visibility) via Phase 1.5 plumbing.
    """

    def __init__(self, store: Store, message_manager: MessageManager, task_manager: TaskManager) -> None: ...

    async def check_all(self, cycle_id: str) -> WaitingForCheckResult:
        """Check all active waiting-for items for resolution or escalation.

        Returns:
            WaitingForCheckResult with counts of resolved, nudged, escalated items.
        """

    async def _check_for_reply(self, item: WaitingForItem) -> bool:
        """Check if a reply has arrived in the tracked conversation.

        Uses SentItemsCache first, then falls back to Graph API query
        for replies from expected_from.
        """

    async def _check_escalation(self, item: WaitingForItem) -> EscalationLevel:
        """Determine escalation level based on age vs config thresholds."""
```

**`engine/triage.py`** — Add `WaitingForTracker.check_all()` call after main classification loop in each triage cycle. Log results in cycle summary.

**`web/routes.py`** — Enhance `/waiting` page:
- Add aging indicator (hours/days since waiting started)
- Color-code by escalation level (normal → warning → critical)
- Add "Extend deadline" action
- Add "Escalate" manual action

### Result Dataclass

```python
@dataclass(frozen=True)
class WaitingForCheckResult:
    """Result of checking all waiting-for items in a triage cycle."""
    resolved: int       # Auto-resolved (reply detected)
    nudged: int         # Past nudge threshold, included in digest
    escalated: int      # Past escalate threshold, flagged critical
    unchanged: int      # Still waiting, within thresholds
    errors: int         # Failed to check (Graph API error)
```

### Database Changes

No schema changes. The `waiting_for` table already has all required columns:
- `status` ('waiting', 'received', 'expired')
- `nudge_after_hours` (default 48)
- `conversation_id` (for monitoring)
- `expected_from` (for reply detection)

New `agent_state` key:
- `last_waiting_for_check` — timestamp of last check (avoid redundant checks if cycle runs quickly)

### Config Changes

These fields are defined in the spec (`02-config-and-schema.md`) but may not yet exist in the user's `config.yaml`. They are additive with defaults, so existing configs work unchanged:

```yaml
aging:
  waiting_for_nudge_hours: 48       # Flag waiting-for items for digest after N hours
  waiting_for_escalate_hours: 96    # Flag as critical after N hours
  needs_reply_warning_hours: 24     # Used by digest for overdue reply detection
  needs_reply_critical_hours: 48    # Used by digest for critical overdue detection
```

### Error Handling

- Graph API failures when checking replies: log WARNING, skip item, count as `errors` in result
- Reply detection is best-effort — false negatives (missed reply) are acceptable since user can manually resolve
- Never auto-expire waiting-for items due to API failures

### Testing Strategy

**Unit tests:**
- Reply detected → status changes to 'received'
- No reply, within threshold → status unchanged
- No reply, past nudge threshold → flagged for nudge
- No reply, past escalate threshold → flagged as critical
- Graph API failure → item unchanged, error counted

**Integration tests:**
- Full lifecycle: create waiting-for → detect reply → auto-resolve
- Aging lifecycle: create → nudge threshold → escalate threshold

### Verification Checklist

- [ ] Triage cycle includes waiting-for check with results in cycle summary
- [ ] Reply from `expected_from` auto-resolves waiting-for item
- [ ] Items past `nudge_after_hours` flagged for digest
- [ ] Items past `escalate_after_hours` flagged as critical
- [ ] `/waiting` page shows aging indicators
- [ ] Graph API failures handled gracefully (item not affected)

---

## 2C: Daily Digest + Calendar Awareness

### What It Does

Generates a morning summary highlighting what needs attention: overdue replies, aging waiting-for items, yesterday's processing stats, pending reviews, and failed classifications. Optionally reads the CEO's calendar via `getSchedule` to pick optimal delivery timing (avoid interrupting meetings).

### Phase 1.5 Dependencies

This feature builds on Phase 1.5 infrastructure:
- **`graph_tasks.py`** — To Do task CRUD and category management (already implemented)
- **`task_sync` table** — maps email IDs to To Do task IDs (already implemented)

Phase 2 adds: reading task completion status from Graph API (via `task_sync` cross-reference) for digest content, and calendar awareness for delivery timing.

### User-Facing Behavior

A formatted digest delivered at the configured time (default 08:00) via the configured channel (stdout, file, or email). When calendar awareness is enabled, digest delivery is shifted to avoid interrupting meetings (finds the nearest free slot within a configurable window). Also accessible via CLI: `python -m assistant digest`.

### Digest Content Sections

```
1. OVERDUE REPLIES — Emails classified as "Needs Reply" older than warning/critical thresholds
2. WAITING FOR    — Overdue waiting-for items (past nudge threshold) with age
3. TASK STATUS    — To Do tasks completed/modified by user since last digest (via task_sync cross-reference)
4. ACTIVITY       — Yesterday's processing stats (emails processed, auto-ruled, classified, failed)
5. PENDING REVIEW — Count of pending suggestions awaiting user input
6. FAILED         — Classifications that failed after 3 attempts
```

### How It Builds on Existing Code

**New file: `engine/digest.py`**

```python
class DigestGenerator:
    """Generate daily email digest summaries.

    Uses Claude Haiku to format structured data into a scannable digest.
    """

    def __init__(
        self,
        store: Store,
        anthropic_client: anthropic.AsyncAnthropic,
        task_manager: TaskManager,
        config: AppConfig,
    ) -> None: ...

    async def generate(self) -> DigestResult:
        """Gather data and generate formatted digest.

        Steps:
        1. Query DB for overdue replies (emails with action_type='Needs Reply',
           age > needs_reply_warning_hours, no user reply)
        2. Query DB for overdue waiting-for items (past nudge_after_hours)
        3. Query task_sync + Graph API for task status changes since last digest
        4. Query DB for yesterday's processing stats from action_log
        5. Query DB for pending suggestion count
        6. Query DB for failed classifications
        7. Send structured data to Claude Haiku with digest prompt
        8. Return formatted digest text
        """

    async def deliver(self, digest: DigestResult) -> None:
        """Deliver digest via configured channel (stdout/file/email).

        If integrations.calendar.enabled and digest_schedule_aware,
        check CEO's calendar via getSchedule before delivering —
        shift to nearest free slot if currently in a meeting.
        """
```

**`classifier/prompts.py`** — Add digest prompt constants (following the existing pattern where triage prompts are constants in this file):

```python
# === Digest Prompts ===

DIGEST_SYSTEM_PROMPT = """You are generating a daily email digest for a busy CEO.
Be concise and action-oriented. Highlight what needs attention most urgently.
Use the generate_digest tool to produce the formatted output."""

GENERATE_DIGEST_TOOL = {
    "name": "generate_digest",
    "description": "Generate a formatted daily digest summary",
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": "Complete formatted digest text"
            }
        },
        "required": ["summary"]
    }
}
```

**`engine/triage.py`** — Register digest as APScheduler job:
```python
scheduler.add_job(
    self._run_digest,
    CronTrigger(hour=digest_hour, minute=digest_minute),
    id="daily_digest",
)
```

**`cli.py`** — Add `digest` command for manual generation:
```bash
python -m assistant digest              # Generate and deliver now
python -m assistant digest --stdout     # Override delivery to stdout
```

### Calendar Awareness (requires `Calendars.Read`)

When `integrations.calendar.enabled` and `integrations.calendar.digest_schedule_aware` are both true, the digest delivery checks the CEO's calendar before sending:

```python
async def _find_delivery_slot(self) -> datetime:
    """Find optimal digest delivery time by checking calendar availability.

    Uses Graph API getSchedule to check a ±30 minute window around
    the configured delivery time. If the user is busy (availability
    char '2' or '3'), shifts delivery to the next free 30-min slot.

    The getSchedule response returns an availabilityView string where
    each character represents a 30-minute slot:
      0 = free, 1 = tentative, 2 = busy, 3 = out of office, 4 = working elsewhere

    If no free slot within the window, delivers at the original time
    (better to interrupt than skip entirely).
    """
```

This requires adding `Calendars.Read` to the permission set (the 6th permission, deferred from Phase 1.5). See `Reference/spec/10-native-task-integration.md` Section 9.8 for the updated permissions table.

### Result Dataclass

```python
@dataclass(frozen=True)
class DigestResult:
    """Result of digest generation."""
    text: str                    # Formatted digest content
    overdue_replies: int         # Count of overdue reply items
    overdue_waiting: int         # Count of overdue waiting-for items
    tasks_completed: int         # Count of To Do tasks completed since last digest
    tasks_modified: int          # Count of To Do tasks modified since last digest
    pending_suggestions: int     # Count of pending suggestions
    failed_classifications: int  # Count of failed classifications
    generated_at: datetime
```

### Database Changes

No schema changes. New `agent_state` key:
- `last_digest_run` — already anticipated in schema comments

### Config Changes

Uses existing config fields (already defined in `config.yaml`):
- `digest.enabled` (default true)
- `digest.schedule` (default "08:00")
- `digest.delivery` (default "stdout")
- `digest.include_sections` (list of section names)
- `models.digest` (default Haiku 4.5)

New config for calendar awareness (see Config Additions Summary below):
- `integrations.calendar.enabled` (default true)
- `integrations.calendar.digest_schedule_aware` (default true)

### New DB Store Methods

```python
async def get_overdue_replies(
    self, warning_hours: int, critical_hours: int
) -> list[dict]:
    """Get emails needing reply that are past the warning threshold."""

async def get_task_status_changes(
    self, since: datetime
) -> dict:
    """Get To Do task status changes since last digest via task_sync.

    Cross-references task_sync with Graph API task status to detect
    tasks completed or modified by the user since the given timestamp.

    Returns: {completed: [...], modified: [...]}
    """

async def get_processing_stats(
    self, since: datetime
) -> dict:
    """Get processing stats since a given timestamp.

    Returns: {processed, auto_ruled, classified, inherited, failed}
    """
```

### Error Handling

- Claude API failure during digest formatting: fall back to plain-text template (no AI formatting)
- Email delivery failure (Graph API): log ERROR, fall back to stdout delivery, include in next digest
- Empty digest (no overdue items, no pending): still generate with "All clear" message — confirms the system is running
- Calendar API failure (getSchedule): log WARNING, deliver at configured time (calendar awareness is best-effort)
- Task status read failure (Graph API): log WARNING, omit task status section from digest, note "task status unavailable"

### Testing Strategy

**Unit tests:**
- Digest data gathering queries return correct results
- Task status changes detected via task_sync cross-reference
- Claude formatting produces expected structure
- Fallback to plain-text on Claude failure
- Delivery routing (stdout vs file vs email)
- Empty digest generates "all clear" message
- Calendar awareness shifts delivery to free slot
- Calendar API failure falls back to configured time

**Integration tests:**
- Full digest lifecycle: seed data → generate → deliver
- APScheduler trigger fires at configured time
- Task completion detection: complete task in To Do → reflected in next digest

### Verification Checklist

- [ ] Digest generates at configured schedule
- [ ] All 6 sections populated with correct data
- [ ] Task status section shows completed/modified tasks from To Do
- [ ] `python -m assistant digest` generates on demand
- [ ] Delivery to stdout works
- [ ] Delivery to file writes to configured path
- [ ] Claude API failure falls back to plain-text
- [ ] Empty digest shows "all clear" message
- [ ] `last_digest_run` updated in `agent_state`
- [ ] Calendar awareness defers delivery when user is busy
- [ ] Calendar API failure falls back to configured time gracefully

---

## 2D: Learning from Corrections + Category Growth

### What It Does

Analyzes user corrections from the review UI to discover classification patterns, then stores those patterns as natural language preferences that are included in every future triage prompt. Also provides category growth through learning (detecting user-applied categories and proposing formalization), a `manage_category` chat tool for manual category management, and `AVAILABLE CATEGORIES` context in triage/chat prompts.

### Phase 1.5 Dependencies

This feature builds on Phase 1.5 infrastructure:
- **`graph_tasks.py`** — Category management: read/create/delete master categories (already implemented)
- **Category bootstrap** — Framework and taxonomy categories established (already implemented)

Phase 2 adds: category growth from user behavior, `manage_category` chat tool, and `AVAILABLE CATEGORIES` in prompts.

### User-Facing Behavior

After the user corrects several suggestions, the system automatically learns patterns like:
- "Emails about infrastructure monitoring should go to Areas/Development even when they mention 'security'"
- "The user prefers P2 over P3 for emails from SYSPRO regardless of content"
- "Emails from legal@translution.com are always P2 - Important, never P3"

Additionally:
- When the user manually applies a category in Outlook that the agent hasn't seen, after 3+ occurrences the agent proposes formalizing it as a taxonomy or user category via the chat interface
- The `manage_category` chat tool allows the user to create, rename, or delete categories through natural language
- Triage and chat system prompts include `AVAILABLE CATEGORIES` context showing all active categories

These preferences and category information appear in the `/stats` page and influence future classifications.

### How It Builds on Existing Code

The infrastructure is already wired:
- `agent_state.classification_preferences` — storage key exists
- `PromptAssembler.build_system_prompt()` — already includes `LEARNED PREFERENCES` section
- `suggestions` table — stores `approved_folder`, `approved_priority`, `approved_action_type` alongside suggested values

**New file: `classifier/preference_learner.py`**

```python
class PreferenceLearner:
    """Learn classification preferences from user corrections.

    After each batch of corrections, analyzes patterns and updates
    the natural language preferences stored in agent_state.
    """

    def __init__(
        self,
        store: Store,
        anthropic_client: anthropic.AsyncAnthropic,
        config: AppConfig,
    ) -> None: ...

    async def update_preferences(self) -> PreferenceUpdateResult:
        """Analyze recent corrections and update classification preferences.

        Steps:
        1. Fetch corrections from last 7 days (status='approved' or 'partial'
           where approved_* differs from suggested_*)
        2. Fetch current preferences from agent_state
        3. Send corrections + current preferences to Claude:
           "Given these user corrections, update the classification preferences"
        4. Validate response (non-empty, reasonable length)
        5. Store updated preferences in agent_state
        6. Return result with change summary
        """
```

**`web/routes.py`** — Trigger preference update after corrections:
- After approving/correcting a suggestion, check if correction count in last 24 hours exceeds threshold (e.g., 3 corrections)
- If so, schedule preference update (async, non-blocking)
- Alternatively: run preference update at end of each triage cycle if new corrections exist

### Category Growth Through Learning

Each triage cycle, the preference learner also checks for user-applied categories:

```python
async def detect_new_user_categories(self) -> list[str]:
    """Detect categories applied by the user in Outlook that the agent doesn't manage.

    Steps:
    1. Read master category list via graph_tasks.py
    2. Compare against known framework + taxonomy + user categories
    3. For unknown categories seen on 3+ emails, propose formalization
    4. Return list of category names to propose to the user via chat
    """
```

When a user-applied category reaches the threshold (3+ occurrences), the agent surfaces a proposal in the chat interface:
- "I noticed you've applied the category 'Board Meetings' to 5 emails. Would you like me to formalize this as a taxonomy category?"
- On approval: creates the category in the master list (if not already there) and records it as a managed category

### `manage_category` Chat Tool (deferred from Phase 1.5)

**`chat/tools.py`** — Add the `manage_category` tool:

```python
MANAGE_CATEGORY_TOOL = {
    "name": "manage_category",
    "description": "Create, rename, or delete a custom category in the Outlook master category list.",
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["create", "delete"],
                "description": "Action to perform on the category"
            },
            "category_name": {
                "type": "string",
                "description": "Name of the category to create or delete"
            },
            "color_preset": {
                "type": "string",
                "description": "Optional color preset (preset0-preset24) for new categories"
            }
        },
        "required": ["action", "category_name"]
    }
}
```

Safety rules:
- **Cannot delete framework categories** (P1–P4, action types) — tool returns an error
- **Cannot delete taxonomy categories** — must use `remove_project_or_area` tool instead
- **User-tier categories only** — this tool manages the third tier of categories
- Rename is not supported by Graph API (`displayName` is immutable after creation) — tool suggests delete + create instead

### AVAILABLE CATEGORIES in Prompts (deferred from Phase 1.5)

**`classifier/prompts.py`** — Add dynamic `AVAILABLE CATEGORIES` section to the triage and chat system prompts:

```python
def build_available_categories_section(categories: list[str]) -> str:
    """Build the AVAILABLE CATEGORIES section for system prompts.

    Groups categories by tier (framework, taxonomy, user) for clarity.
    Only included when the learning system is active (Phase 2).
    """
```

This gives Claude visibility into the full category taxonomy so it can:
- Suggest existing categories when relevant (instead of inventing new ones)
- Detect when a user correction implies a new category should exist
- Provide accurate category information in chat responses

### Preference Update Prompt

```
You are analyzing user corrections to an email classification system.
The user has corrected the following classifications in the last 7 days:

{corrections_formatted}

Current learned preferences:
{current_preferences || "No preferences learned yet."}

Based on these corrections, write updated classification preferences.
Rules:
- State preferences as clear, actionable rules
- Preserve existing preferences that are NOT contradicted by new corrections
- Remove preferences that are contradicted by new corrections
- Keep the total under 500 words (these are included in every classification prompt)
- Be specific: name senders, folders, priority levels
- Do not include obvious rules (e.g., "newsletters go to newsletters folder")
```

### Result Dataclass

```python
@dataclass(frozen=True)
class PreferenceUpdateResult:
    """Result of preference learning cycle."""
    corrections_analyzed: int
    preferences_before: str
    preferences_after: str
    changed: bool                # True if preferences text changed
```

### Database Changes

No schema changes. Uses existing:
- `agent_state` key `classification_preferences`
- `suggestions` table with `suggested_*` and `approved_*` fields

### Config Changes

New optional config section:

```yaml
learning:
  enabled: true                    # Enable learning from corrections
  min_corrections_to_update: 3     # Min corrections before triggering update
  lookback_days: 7                 # How far back to look for corrections
  max_preferences_words: 500       # Cap on preferences text length
```

### Error Handling

- Claude API failure during preference update: log WARNING, keep existing preferences unchanged
- No corrections in lookback window: skip update, no error
- Preferences text exceeds max length: truncate with WARNING log
- Contradictory corrections: Claude resolves conflicts based on recency (newer corrections win)
- Category growth detection failure (Graph API): log WARNING, skip detection for this cycle
- `manage_category` tool: attempting to delete framework/taxonomy categories returns clear error message to user
- Category rename attempted: tool explains `displayName` is immutable, suggests delete + create workflow

### Testing Strategy

**Unit tests:**
- Correction detection (approved_folder != suggested_folder)
- Preference update prompt assembly
- Preference storage and retrieval
- Min correction threshold respected
- Existing preferences preserved when not contradicted
- User-applied category detection (3+ occurrences threshold)
- `manage_category` tool: create, delete, reject framework deletion
- `AVAILABLE CATEGORIES` prompt section assembly with grouped tiers

**Integration tests:**
- Full cycle: seed corrections → update preferences → verify prompt includes new preferences
- Contradictory corrections resolved
- Category growth: manually apply category to 3+ emails → agent proposes formalization
- `manage_category` tool: create category → verify exists in master list

### Verification Checklist

- [ ] Corrections detected from suggestions table
- [ ] Preference update triggers after threshold corrections
- [ ] Claude generates clear, actionable preferences
- [ ] Preferences stored in `agent_state.classification_preferences`
- [ ] Preferences appear in triage classification prompts
- [ ] Claude API failure does not corrupt existing preferences
- [ ] Preferences visible in `/stats` page
- [ ] User-applied categories detected after 3+ occurrences
- [ ] Category formalization proposed via chat interface
- [ ] `manage_category` tool creates user-tier categories
- [ ] `manage_category` tool rejects deletion of framework/taxonomy categories
- [ ] `AVAILABLE CATEGORIES` section appears in triage and chat prompts
- [ ] Category groups shown by tier (framework, taxonomy, user)

---

## 2E: Sender Affinity Auto-Rules

### What It Does

When a sender's classification history shows strong affinity for a single folder (>90% of emails, 10+ total), the system surfaces an auto-rule suggestion in the review UI. On acceptance, a new auto-rule is created in `config.yaml` — future emails from that sender bypass Claude classification entirely.

### User-Facing Behavior

In the review UI, when correcting or approving a suggestion from a sender with high affinity:
- Banner: "Emails from john@tradecore.co.za go to Projects/Tradecore Steel 94% of the time (47/50 emails). Create an auto-rule?"
- Buttons: "Create Rule" / "Not Now"
- On "Create Rule": rule added to config, config reloaded, toast notification

Also visible in the sender management page (Feature 2I).

### How It Builds on Existing Code

The `sender_profiles` table already tracks `auto_rule_candidate` and `default_folder`. The `AutoRulesEngine` already matches sender patterns with `fnmatch`.

**`web/routes.py`** — Extend approval endpoints:
- After approving/correcting, check if sender has `auto_rule_candidate = 1`
- If so, include rule suggestion in the response
- New endpoint: `POST /api/auto-rules/create-from-sender`

**`classifier/auto_rules.py`** — Add method:
```python
def create_rule_from_sender(
    self,
    sender_email: str,
    folder: str,
    priority: str,
    action_type: str,
    rule_name: str,
) -> AutoRuleConfig:
    """Generate an auto-rule config entry from sender affinity data."""
```

**`config.py`** — Add a standalone config writer function (separate from the singleton reader pattern). This is a write-path function, not part of the `get_config()` / `reload_config_if_changed()` read-path:

```python
def append_auto_rule(config_path: Path, rule: AutoRuleConfig) -> None:
    """Append an auto-rule to config.yaml and trigger reload.

    Reads current YAML file, appends to auto_rules list, validates,
    writes back with backup (config.yaml.bak.{timestamp}).
    Calls reload_config_if_changed() after successful write.

    Raises ConfigValidationError if the modified config is invalid.
    """
```

Note: This same write-path pattern is reused in Phase 3 Feature 3C (new project creation) and Feature 3D (project archiving). All config modifications follow the same read-modify-validate-backup-write-reload pattern.

### Database Changes

No schema changes. Uses existing `sender_profiles.auto_rule_candidate`.

### Config Changes

No new config fields. Auto-rules are appended to the existing `auto_rules` list in `config.yaml`.

### Error Handling

- Config write failure: log ERROR, keep existing config, inform user via UI
- Duplicate rule detection: check if sender pattern already exists in auto_rules before creating
- Config validation failure after append: roll back to backup, log ERROR

### Testing Strategy

**Unit tests:**
- Rule generation from sender affinity data
- Duplicate rule detection
- Config append and reload
- Config backup before modification

**Integration tests:**
- Full lifecycle: approve suggestion → rule suggestion appears → create rule → future email auto-routed

### Verification Checklist

- [ ] Auto-rule candidates surfaced in review UI
- [ ] Rule creation writes to `config.yaml`
- [ ] Config backup created before modification
- [ ] Duplicate rules prevented
- [ ] Config reload triggers after rule creation
- [ ] Future emails from sender auto-routed (bypass Claude)

---

## 2F: Auto-Rules Hygiene

### What It Does

Monitors auto-rules for quality: detects conflicts (overlapping patterns), identifies stale rules (zero matches recently), and warns when rule count is excessive.

### User-Facing Behavior

- Dashboard warning when auto_rules count exceeds `max_rules`
- Config editor highlights conflicting rules
- `python -m assistant rules --audit` CLI command outputs rule health report
- Stale rules flagged in config editor

### How It Builds on Existing Code

**`classifier/auto_rules.py`** — Add audit methods:

```python
class AutoRulesEngine:
    # ... existing match logic ...

    def detect_conflicts(self, rules: list[AutoRuleConfig]) -> list[RuleConflict]:
        """Find rules with overlapping sender/subject patterns.

        Two rules conflict if the same email could match both.
        Uses fnmatch to test each rule's patterns against each other.
        """

    def detect_stale_rules(
        self,
        rules: list[AutoRuleConfig],
        match_counts: dict[str, int],
        threshold_days: int,
    ) -> list[str]:
        """Find rules with zero matches in the threshold period."""

    def audit_report(self, rules: list[AutoRuleConfig]) -> RulesAuditReport:
        """Generate complete audit report: conflicts, stale, count warning."""
```

**`cli.py`** — Add `rules --audit` command:
```bash
python -m assistant rules --audit
```

Output:
```
=== Auto-Rules Audit Report ===

Rules: 47 / 100 (OK)

Conflicts (2):
  "GitHub Notifications" and "Dev Alerts" both match *@github.com
  "Calendar" and "Meeting Reminders" both match subjects containing "invitation"

Stale Rules (3 with 0 matches in last 30 days):
  "Old Vendor Alerts" — last match: 45 days ago
  "Temp Project Alpha" — last match: never
  "Legacy Notifications" — last match: 62 days ago
```

### Database Changes

New table for tracking rule match counts. This table is created fresh in Phase 2 — no data migration needed. It starts empty and populates from future triage cycles as auto-rules match emails.

```sql
CREATE TABLE IF NOT EXISTS auto_rule_matches (
    rule_name TEXT PRIMARY KEY,
    match_count INTEGER DEFAULT 0,
    last_match_at DATETIME,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

Updated each triage cycle when an auto-rule matches.

### Config Changes

Uses existing config fields:
- `auto_rules_hygiene.max_rules` (default 100)
- `auto_rules_hygiene.warn_on_conflicts` (default true)
- `auto_rules_hygiene.consolidation_check_days` (default 30)

### Error Handling

- Conflict detection is advisory only — does not block rule loading
- Stale rule detection is advisory only — rules are never auto-deleted
- Audit command works even if database is empty (reports zero matches)

### Testing Strategy

**Unit tests:**
- Conflict detection between overlapping sender patterns
- Conflict detection between overlapping subject patterns
- Stale rule detection with mock match counts
- Rule count warning threshold
- Audit report formatting

### Verification Checklist

- [ ] Conflicting rules detected and reported
- [ ] Stale rules detected based on match history
- [ ] Rule count warning at configured threshold
- [ ] `rules --audit` CLI command outputs report
- [ ] Match counts updated each triage cycle
- [ ] Dashboard shows warning for excessive rule count

---

## 2G: Suggestion Queue Management

### What It Does

Automates suggestion lifecycle: old pending suggestions are expired, and high-confidence suggestions are auto-approved after a configurable delay.

### User-Facing Behavior

- Pending suggestions older than 14 days automatically expire (status → 'expired')
- Suggestions with confidence >= 0.95 auto-approve after 48 hours if the user hasn't acted
- Dashboard shows auto-approved vs expired counts

### How It Builds on Existing Code

The triage engine already has a maintenance step that runs after each cycle. The suggestion expiry mechanism already exists. This feature extends it with auto-approve logic.

**`engine/triage.py`** — Extend `_run_maintenance()`:

```python
async def _run_maintenance(self) -> None:
    # Existing: expire old suggestions
    expired = await self._store.expire_old_suggestions(
        days=config.suggestion_queue.expire_after_days
    )

    # New: auto-approve high-confidence suggestions after delay
    if config.suggestion_queue.auto_approve_confidence:
        auto_approved = await self._auto_approve_high_confidence()

    # Existing: prune LLM logs
    pruned = await self._store.prune_llm_logs(
        days=config.llm_logging.retention_days
    )
```

**`db/store.py`** — Add auto-approve query:

```python
async def auto_approve_high_confidence(
    self,
    min_confidence: float,
    min_age_hours: int,
) -> int:
    """Auto-approve pending suggestions above confidence threshold
    that have been pending for at least min_age_hours.

    Sets status='auto_approved', copies suggested_* to approved_* fields.
    Returns count of auto-approved suggestions.
    """
```

**`engine/triage.py`** — After auto-approving, execute the moves:
- For each auto-approved suggestion, call Graph API to move the email
- Log each action to `action_log` with `triggered_by='auto_approved'`
- If Graph API move fails, revert suggestion to 'pending'

### Database Changes

Add new status value to suggestions: `'auto_approved'`. No schema change needed — `status` is a TEXT column.

Also add a new status `'expired'` for explicitly expired suggestions.

### Config Changes

Uses existing config fields:
- `suggestion_queue.expire_after_days` (default 14)
- `suggestion_queue.auto_approve_confidence` (default 0.95)
- `suggestion_queue.auto_approve_delay_hours` (default 48)

### Error Handling

- Graph API failure during auto-approved move: revert suggestion to 'pending', log WARNING
- Never auto-approve if `triage.mode` is "suggest" and auto-approve is not explicitly enabled — the user must opt in via `auto_approve_confidence` config
- Never auto-approve P1 emails (always require human review regardless of confidence)

### Testing Strategy

**Unit tests:**
- Suggestions older than threshold expire correctly
- High-confidence suggestions auto-approve after delay
- P1 suggestions never auto-approved
- Graph API failure reverts to pending

**Integration tests:**
- Full lifecycle: create suggestion → wait → auto-approve → move email

### Verification Checklist

- [ ] Old suggestions expire after `expire_after_days`
- [ ] High-confidence suggestions auto-approve after delay
- [ ] P1 emails never auto-approved
- [ ] Auto-approved moves execute via Graph API
- [ ] Failed moves revert suggestion to pending
- [ ] Dashboard shows auto-approved counts
- [ ] `action_log` records auto-approved actions

---

## 2H: Stats & Accuracy Dashboard

### What It Does

New `/stats` page showing classification accuracy, confidence calibration, correction patterns, and API cost tracking.

### User-Facing Behavior

Dashboard page with sections:

1. **Approval Rate** — Overall and per-folder: what percentage of suggestions are approved vs corrected vs rejected
2. **Correction Heatmap** — Which folder/priority/action combinations are most often corrected and what they're corrected to
3. **Confidence Calibration** — Chart showing predicted confidence vs actual approval rate (are 0.9 predictions actually approved 90% of the time?)
4. **Cost Tracking** — Daily/weekly/monthly token usage and estimated cost from `llm_request_log`
5. **Learned Preferences** — Current classification preferences text (from Feature 2D)

### How It Builds on Existing Code

**New template: `web/templates/stats.html`**

**`web/routes.py`** — Add routes:

```python
@router.get("/stats")
async def stats_page(request: Request) -> HTMLResponse:
    """Stats & accuracy dashboard."""

@router.get("/api/stats")
async def stats_api(request: Request) -> JSONResponse:
    """JSON stats data for dashboard charts."""
```

**`db/store.py`** — Add query methods:

```python
async def get_approval_stats(
    self, days: int = 30
) -> dict:
    """Get approval/correction/rejection rates, overall and per folder."""

async def get_correction_heatmap(
    self, days: int = 30
) -> list[dict]:
    """Get most common corrections (suggested_X -> approved_X transitions)."""

async def get_confidence_calibration(
    self, days: int = 30
) -> list[dict]:
    """Get predicted confidence vs actual approval rate by bucket."""

async def get_cost_tracking(
    self, days: int = 30
) -> dict:
    """Get token usage and estimated cost from llm_request_log."""
```

### Database Changes

No schema changes. All data comes from existing tables:
- `suggestions` — approval rates, correction patterns
- `llm_request_log` — token usage, cost tracking

### Config Changes

New optional config section:

```yaml
stats:
  default_lookback_days: 30    # Default time window for stats
```

### Testing Strategy

**Unit tests:**
- Approval rate calculation with various scenarios
- Correction heatmap with known correction data
- Confidence calibration bucketing
- Cost estimation from token counts

### Verification Checklist

- [ ] `/stats` page loads with all sections
- [ ] Approval rates accurate per-folder and overall
- [ ] Correction heatmap shows top corrections
- [ ] Confidence calibration shows predicted vs actual
- [ ] Cost tracking shows daily/weekly/monthly usage
- [ ] Learned preferences displayed

---

## 2I: Sender Management Page

### What It Does

New `/senders` page for managing sender profiles — view all known senders, their categories, default folders, and email counts. Quick actions to change categories, set default folders, and create auto-rules.

### User-Facing Behavior

Table view with columns:
- Sender email, display name, domain
- Category (key_contact, newsletter, automated, internal, client, vendor, unknown)
- Default folder (most common approved folder)
- Email count, last seen
- Auto-rule candidate indicator

Actions:
- Change category (dropdown)
- Set default folder (dropdown)
- Create auto-rule (one-click)
- Filter by category, sort by any column

### How It Builds on Existing Code

**New template: `web/templates/senders.html`**

**`web/routes.py`** — Add routes:

```python
@router.get("/senders")
async def senders_page(request: Request) -> HTMLResponse:
    """Sender management page."""

@router.post("/api/senders/{email}/category")
async def update_sender_category(
    email: str, category: str, store: Store = Depends(get_store)
) -> JSONResponse:
    """Update sender category."""

@router.post("/api/senders/{email}/default-folder")
async def update_sender_default_folder(
    email: str, folder: str, store: Store = Depends(get_store)
) -> JSONResponse:
    """Update sender default folder."""
```

**`db/store.py`** — Add query methods:

```python
async def list_sender_profiles(
    self,
    category: str | None = None,
    sort_by: str = "email_count",
    sort_order: str = "desc",
    limit: int = 100,
    offset: int = 0,
) -> list[dict]:
    """List sender profiles with pagination and filtering."""

async def update_sender_category(
    self, email: str, category: str
) -> None:
    """Update a sender's category."""

async def update_sender_default_folder(
    self, email: str, folder: str
) -> None:
    """Update a sender's default folder."""
```

### Database Changes

No schema changes. Uses existing `sender_profiles` table.

### Config Changes

None.

### Testing Strategy

**Unit tests:**
- Sender list query with pagination and filtering
- Category update
- Default folder update
- Sort by different columns

### Verification Checklist

- [ ] `/senders` page lists all sender profiles
- [ ] Filtering by category works
- [ ] Sorting by email count, last seen, name works
- [ ] Category update persists
- [ ] Default folder update persists
- [ ] Auto-rule candidates highlighted
- [ ] Create auto-rule action works (ties to Feature 2E)

---

## 2K: Confidence Calibration

### What It Does

Analyzes the relationship between the classifier's predicted confidence scores and actual user approval rates. Identifies miscalibration (e.g., classifier says 0.9 but only 70% are approved) and recommends model upgrades when accuracy is below acceptable thresholds.

### User-Facing Behavior

Section in the `/stats` dashboard:
- Calibration chart: buckets (0.5-0.6, 0.6-0.7, ..., 0.9-1.0) showing predicted vs actual approval rate
- Alert if calibration is significantly off: "Classifier predictions are over-confident. Consider upgrading triage model from Haiku to Sonnet."
- Recommendation engine: based on correction rate and cost, suggest model tier changes

### How It Builds on Existing Code

Integrated into the stats dashboard (Feature 2H). All data comes from existing `suggestions` table.

**`db/store.py`** — The `get_confidence_calibration()` method (defined in 2H) provides the data:

```python
async def get_confidence_calibration(self, days: int = 30) -> list[dict]:
    """Get predicted confidence vs actual approval rate by bucket.

    Returns list of:
    {
        "bucket": "0.8-0.9",
        "count": 45,
        "approved": 38,
        "approval_rate": 0.844,
        "avg_confidence": 0.856,
    }
    """
```

### Dashboard Presentation

```
Confidence Calibration (last 30 days):

  Predicted   | Actual     | Count | Status
  0.50 - 0.60 | 48% approved |  12  | OK (within 15%)
  0.60 - 0.70 | 61% approved |  23  | OK
  0.70 - 0.80 | 72% approved |  34  | OK
  0.80 - 0.90 | 84% approved |  45  | OK
  0.90 - 1.00 | 71% approved |  67  | WARNING: over-confident by 22%
```

### Alert Logic

```python
def check_calibration_alerts(calibration_data: list[dict]) -> list[str]:
    """Generate alerts for miscalibrated confidence buckets.

    Alert if any bucket's actual approval rate differs from
    predicted confidence by more than 15 percentage points.
    """
```

### Database Changes

None.

### Config Changes

None. Calibration thresholds are constants (15% tolerance), not user-configurable — this follows YAGNI.

### Testing Strategy

**Unit tests:**
- Calibration bucketing with known data
- Alert generation for over-confident and under-confident scenarios
- Edge cases: empty buckets, single data point

### Verification Checklist

- [ ] Calibration chart appears in stats dashboard
- [ ] Buckets calculated correctly
- [ ] Alerts generated for miscalibrated buckets
- [ ] Model upgrade recommendation shown when appropriate

---

## 2M: Enhanced Graceful Degradation

### What It Does

Extends the existing degraded mode (3-consecutive-fail → auto-rules-only) with backlog recovery, dashboard indicators, and separate Graph API outage detection.

### User-Facing Behavior

- Dashboard shows a visible "Degraded Mode" banner when active, with reason and duration
- On recovery, pending backlog is processed in FIFO order with rate limiting
- Separate detection for Claude API vs Graph API outages

### How It Builds on Existing Code

**`engine/triage.py`** — Extend degraded mode logic:

```python
class DegradationState:
    """Track degradation state for both Claude and Graph APIs."""

    claude_consecutive_failures: int = 0
    graph_consecutive_failures: int = 0
    degraded_since: datetime | None = None
    degraded_reason: str | None = None

    @property
    def is_degraded(self) -> bool:
        return self.claude_consecutive_failures >= 3 or self.graph_consecutive_failures >= 3

    def record_claude_success(self) -> bool:
        """Record Claude API success. Returns True if recovering from degraded mode."""

    def record_graph_success(self) -> bool:
        """Record Graph API success. Returns True if recovering from degraded mode."""
```

**Recovery logic:**

```python
async def _process_backlog(self) -> int:
    """Process pending emails accumulated during degraded mode.

    Processes in FIFO order, rate-limited to avoid burst API usage.
    Returns count of processed emails.
    """
    pending = await self._store.get_pending_emails(
        order_by="received_at ASC",
        limit=self._config.triage.batch_size,
    )
    # Process batch, remaining handled in subsequent cycles
```

**`web/routes.py`** — Add degradation state to dashboard context:

```python
# In dashboard route:
context["degraded_mode"] = engine.degradation_state.is_degraded
context["degraded_since"] = engine.degradation_state.degraded_since
context["degraded_reason"] = engine.degradation_state.degraded_reason
context["backlog_count"] = await store.count_pending_emails()
```

### Database Changes

None. Uses existing `classification_status = 'pending'` for backlog tracking.

### Config Changes

None. Degradation thresholds are constants (3 consecutive failures), not user-configurable — this keeps the system simple and avoids misconfiguration.

### Error Handling

- Backlog processing errors: if a batch partially fails, process what we can, leave rest for next cycle
- Recovery detection: first successful API call clears degraded state, logs INFO recovery event

### Testing Strategy

**Unit tests:**
- Degradation state transitions (normal → degraded → recovery)
- Claude vs Graph outage tracking
- Backlog processing in FIFO order
- Rate limiting during backlog processing

**Integration tests:**
- Simulate Claude outage → degraded mode → recovery → backlog processing

### Verification Checklist

- [ ] Degraded mode indicator on dashboard
- [ ] Separate tracking for Claude vs Graph outages
- [ ] Backlog processed in FIFO order on recovery
- [ ] Rate limiting during backlog processing
- [ ] Recovery logged at INFO level

---

## Principles Alignment

### Toyota Five Pillars

| Pillar | Application in Phase 2 |
|--------|----------------------|
| Not Over-Engineered | Each feature is minimal — delta queries don't change the processing pipeline, just the fetch strategy. Preference learning uses existing prompt infrastructure. |
| Sophisticated Where Needed | Delta query error recovery (410 Gone handling), degradation state tracking — complexity justified by reliability requirements. |
| Robust Error Handling | Every feature specifies error scenarios with specific responses. No silent failures. Graceful fallbacks everywhere (delta → timestamp, AI digest → plain text). |
| Complete Observability | All new operations log with correlation IDs. Stats dashboard provides system-wide visibility. Preference learning is transparent (preferences visible in stats page). |
| Proven Patterns | FastAPI routes, APScheduler jobs, aiosqlite queries — all standard Python patterns. No custom frameworks. |

### Unix Philosophy

| Rule | Application in Phase 2 |
|------|----------------------|
| Representation | Auto-rules in config YAML, classification preferences in natural language, confidence thresholds in config — knowledge in data, not code. |
| Least Surprise | Delta queries are invisible to the user. Digest format matches the spec exactly. Auto-rules work identically whether manually created or auto-generated. |
| Modularity | Each feature is a self-contained module: `waiting_for.py`, `digest.py`, `preference_learner.py`. Clear interfaces, no tangled dependencies. |
| Separation | Thresholds (policy) in config; checking logic (mechanism) in code. Digest content (data) separate from formatting (Claude prompt). |
| Composition | Features compose: delta queries feed triage cycles, waiting-for feeds digest, corrections feed preference learning. Standard interfaces throughout. |
| Silence | No debug spam. Structured logging for significant events only. Digest is concise and actionable. |
| Repair | Every error includes what failed, where, why, and how to fix. Degraded mode tells the dashboard exactly what's wrong. |

---

## Database Migration Strategy

Phase 2 adds one new table (`auto_rule_matches`), extends the Phase 1.5 `task_sync` table with two new columns, and adds new `agent_state` keys.

```python
# In db/models.py, add to schema creation:
PHASE_2_MIGRATIONS = [
    # New table for auto-rule match tracking (Feature 2F)
    """CREATE TABLE IF NOT EXISTS auto_rule_matches (
        rule_name TEXT PRIMARY KEY,
        match_count INTEGER DEFAULT 0,
        last_match_at DATETIME,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )""",

    # Extend Phase 1.5 task_sync table for email flags and conversation tracking (Feature 2B)
    """ALTER TABLE task_sync ADD COLUMN flag_set INTEGER DEFAULT 0""",
    """ALTER TABLE task_sync ADD COLUMN conversation_id TEXT""",

    # Index for conversation-based lookups (Feature 2B waiting-for tracker)
    """CREATE INDEX IF NOT EXISTS idx_task_sync_conversation
       ON task_sync(conversation_id)""",
]
```

New `agent_state` keys:
- `calendar_enabled` — whether `Calendars.Read` permission was successfully granted (Feature 2C)
- `last_waiting_for_check` — timestamp of last waiting-for check (Feature 2B)
- `last_digest_run` — timestamp of last digest generation (Feature 2C)

Migration runs automatically on startup. `CREATE TABLE IF NOT EXISTS` and `CREATE INDEX IF NOT EXISTS` are idempotent. `ALTER TABLE ADD COLUMN` migrations should check if the column exists first (SQLite raises an error on duplicate column addition).

---

## Config Additions Summary

All new config fields are optional with defaults. Existing configs continue to work unchanged. Phase 2 extends the `integrations` section established by Phase 1.5.

```yaml
# New in Phase 2 (all optional):

learning:
  enabled: true
  min_corrections_to_update: 3
  lookback_days: 7
  max_preferences_words: 500

stats:
  default_lookback_days: 30

# Extensions to Phase 1.5 integrations section:
integrations:
  todo:
    # ...Phase 1.5 fields (enabled, list_name, create_for_action_types)...
    sync_interval_minutes: 5         # How often to check for user-completed tasks (Feature 2B)

  email_flags:                        # Deferred from Phase 1.5 → Feature 2B
    enabled: true                    # Set followUpFlag on actionable emails
    flag_action_types:               # Which action types get flagged
      - "Needs Reply"
      - "Waiting For"
    only_after_approval: true        # Only flag emails after suggestion is approved

  calendar:                           # Deferred from Phase 1.5 → Feature 2C
    enabled: true                    # Read calendar for schedule awareness
    digest_schedule_aware: true      # Use calendar to pick optimal digest delivery time

# New permission (Phase 2):
auth:
  scopes:
    # ...Phase 1.5 scopes (Mail.ReadWrite, Mail.Send, MailboxSettings.ReadWrite, User.Read, Tasks.ReadWrite)...
    - "Calendars.Read"               # Phase 2 — schedule-aware features (Feature 2C)
```

> **Note:** The `webhooks` and `auth.encrypt_token_cache` config sections from earlier drafts have been removed per architecture decisions. See `Reference/spec/09-architecture-decisions.md`.
