# Phase 3 — Autonomy

> **Prerequisites:** Phase 2 (Intelligence) fully implemented. Specifically: learning from corrections (2D), stats dashboard (2H), suggestion queue auto-approve (2G), and daily digest (2C) must be operational.
> **Theme:** Graduate from suggest-only to autonomous mode for high-confidence actions.
> **Builds on:** Phase 1 triage engine, Phase 2 learning + confidence calibration.

---

## Overview

Phase 1 builds the core triage loop. Phase 2 makes it intelligent — the system learns from corrections, tracks accuracy, and surfaces patterns. Phase 3 uses that intelligence to act autonomously: when the system is confident enough and has proven itself through user correction history, it can move emails without waiting for approval.

This is the most safety-critical phase. Every feature includes explicit guardrails, undo mechanisms, and audit trails. The autonomous mode is opt-in, progressive, and always transparent about what it did and why.

### The Trust Ladder

```
Phase 1: Agent suggests → User reviews everything
Phase 2: Agent suggests → User reviews, agent learns from corrections
Phase 3: Agent acts on high-confidence items → User reviews the rest
         Agent acts on more as trust builds → User reviews less
```

The system never removes the ability to review. It just moves the review from "before execution" to "after execution" for items where it has earned trust through demonstrated accuracy.

---

## Feature Summary

| # | Feature | New Files | Extended Files |
|---|---------|-----------|----------------|
| 3A | Autonomous Mode Config | — | `config.py`, `engine/triage.py` |
| 3B | Auto-Execution Engine | — | `engine/triage.py`, `db/store.py`, `web/routes.py` |
| 3C | New Project Detection | — | `web/routes.py`, `graph/folders.py`, `config.py` |
| 3D | Auto-Archive Completed Projects | — | `engine/triage.py`, `web/routes.py` |
| 3E | Weekly Review Report | `engine/weekly_review.py` | `engine/triage.py`, `cli.py` |
| 3F | Email Delivery for Digests | — | `engine/digest.py`, `graph/messages.py` |

---

## Recommended Implementation Order

```
3A  Autonomous Mode Config       (foundation for 3B)
3B  Auto-Execution Engine        (depends on 3A, core feature)
3F  Email Delivery for Digests   (no dependencies, small scope)
3C  New Project Detection        (independent, extends existing tool schema)
3D  Auto-Archive Completed       (independent, project lifecycle)
3E  Weekly Review Report         (depends on Phase 2 stats infrastructure)
```

---

## 3A: Autonomous Mode Configuration

### What It Does

Adds configuration for autonomous mode — the ability for the agent to execute email moves without waiting for user approval, gated by confidence thresholds, folder whitelists, and safety rules.

### User-Facing Behavior

User adds to `config.yaml`:

```yaml
auto_mode:
  enabled: true
  confidence_threshold: 0.90
  auto_folders:
    - "Reference/Newsletters"
    - "Reference/Dev Notifications"
    - "Reference/Calendar"
    - "Areas/Support"
  auto_actions:
    - "FYI Only"
    - "Review"
  excluded_priorities:
    - "P1 - Urgent Important"
  require_sender_history: true     # Only auto-execute if sender has 5+ prior classifications
  daily_auto_limit: 50             # Max auto-executions per day (safety cap)
```

The system starts in suggest mode by default. The user progressively enables auto-mode for low-risk folders first (newsletters, notifications), then expands as trust builds.

### How It Builds on Existing Code

**`config.py`** — Add new Pydantic model:

```python
class AutoModeConfig(BaseModel):
    """Configuration for autonomous email classification mode."""
    enabled: bool = False
    confidence_threshold: float = Field(default=0.90, ge=0.70, le=1.0)
    auto_folders: list[str] = Field(default_factory=list)
    auto_actions: list[str] = Field(default_factory=list)
    excluded_priorities: list[str] = Field(
        default_factory=lambda: ["P1 - Urgent Important"]
    )
    require_sender_history: bool = True
    daily_auto_limit: int = Field(default=50, ge=1, le=500)
```

**`engine/triage.py`** — The existing `triage.mode` field currently accepts "suggest" or "auto". Phase 3 changes "auto" to use the new `auto_mode` config section. The mode field is kept for backward compatibility:
- `mode: "suggest"` — Phase 1 behavior (all suggestions need review)
- `mode: "auto"` — Phase 3 behavior (auto-execute when conditions met)

### Validation Rules

On config load, validate:
- `auto_folders` must all exist in the configured projects/areas folder list
- `auto_actions` must be valid action types (from the enum)
- `excluded_priorities` must be valid priority levels
- `confidence_threshold` must be >= 0.70 (never allow auto-execution below 70%)

Log WARNING at startup if auto_mode is enabled:
```
"Autonomous mode enabled. Auto-executing classifications with confidence >= 0.90 "
"for folders: Reference/Newsletters, Reference/Dev Notifications, Reference/Calendar, Areas/Support. "
"Excluded priorities: P1 - Urgent Important. Daily limit: 50."
```

### Error Handling

- Invalid `auto_folders` (folder not in taxonomy): fail fast on config load with specific error
- `confidence_threshold` below 0.70: reject with "Confidence threshold must be >= 0.70 for safety"
- `daily_auto_limit` exceeded: switch to suggest-only for remainder of day, log WARNING

### Testing Strategy

**Unit tests:**
- Config validation: valid auto_mode config loads correctly
- Config validation: invalid folder in auto_folders rejected
- Config validation: threshold below 0.70 rejected
- Config validation: default config (auto_mode disabled) loads correctly

### Verification Checklist

- [ ] `auto_mode` config section parses correctly
- [ ] Validation catches invalid folders, actions, priorities
- [ ] Threshold floor of 0.70 enforced
- [ ] Startup log message lists auto-mode configuration
- [ ] Default config (disabled) works without the section present

---

## 3B: Auto-Execution Engine

### What It Does

When autonomous mode is enabled, the triage engine automatically executes email moves for classifications that meet all conditions: confidence above threshold, folder in whitelist, action type in whitelist, priority not excluded, and sender has sufficient history (if required).

### User-Facing Behavior

- High-confidence emails matching auto-mode criteria are moved immediately
- Suggestion record created with `status='auto_executed'`
- Dashboard shows auto-executed items separately from manually approved
- Activity log records every auto-execution with full context
- User can undo any auto-execution via CLI or (future) UI

### The Decision Gate

```
Classification result received
  |
  +-- auto_mode.enabled == false? --> Create pending suggestion (Phase 1 behavior)
  |
  +-- confidence < threshold? --> Create pending suggestion
  |
  +-- folder NOT in auto_folders? --> Create pending suggestion
  |
  +-- action_type NOT in auto_actions? --> Create pending suggestion
  |
  +-- priority in excluded_priorities? --> Create pending suggestion
  |
  +-- require_sender_history AND sender has < 5 prior classifications?
  |   --> Create pending suggestion
  |
  +-- daily_auto_limit reached? --> Create pending suggestion, log WARNING
  |
  +-- ALL conditions met --> AUTO-EXECUTE:
      1. Create suggestion with status='auto_executed'
      2. Move email via Graph API
      3. Set categories via Graph API
      4. Log to action_log with triggered_by='auto_executed'
      5. Increment daily auto-execution counter
```

### How It Builds on Existing Code

**`engine/triage.py`** — Add auto-execution decision after classification:

```python
async def _handle_classification(
    self,
    email: dict,
    result: ClassificationResult,
    cycle_id: str,
) -> None:
    """Handle a classification result — suggest or auto-execute.

    Checks auto_mode conditions. If all pass, execute immediately.
    Otherwise, create pending suggestion for manual review.
    """
    if self._should_auto_execute(result):
        await self._auto_execute(email, result, cycle_id)
    else:
        await self._create_suggestion(email, result)
```

```python
def _should_auto_execute(self, result: ClassificationResult) -> bool:
    """Check if a classification result qualifies for auto-execution.

    All conditions must be met. Returns False if any condition fails.
    Each failing condition is logged at DEBUG level for transparency.
    """
    config = self._config.auto_mode
    if not config.enabled:
        return False
    if result.confidence < config.confidence_threshold:
        return False
    if result.folder not in config.auto_folders:
        return False
    if result.action_type not in config.auto_actions:
        return False
    if result.priority in config.excluded_priorities:
        return False
    if config.require_sender_history:
        # Check sender has sufficient history (5+ prior classifications)
        # Uses sender_profiles table
        ...
    if self._daily_auto_count >= config.daily_auto_limit:
        return False
    return True
```

**`db/store.py`** — Add methods:

```python
async def create_auto_executed_suggestion(
    self,
    email_id: str,
    result: ClassificationResult,
) -> int:
    """Create a suggestion record for an auto-executed classification.

    Sets status='auto_executed', copies classification to both
    suggested_* and approved_* fields.
    """

async def get_auto_execution_count(self, since: datetime) -> int:
    """Count auto-executed suggestions since a given timestamp.

    Used for daily limit enforcement.
    """

async def get_auto_execution_stats(self, days: int = 30) -> dict:
    """Get auto-execution statistics for dashboard.

    Returns: {total, by_folder, by_action_type, undo_count}
    """
```

**`web/routes.py`** — Extend dashboard:
- Show auto-executed count alongside pending/approved/rejected
- New section: "Recent auto-executions" with folder, confidence, timestamp
- Each auto-execution shows "Undo" button

### Undo Support

Auto-executed moves are fully reversible:

```python
async def undo_auto_execution(self, suggestion_id: int) -> None:
    """Undo an auto-executed email move.

    1. Look up original folder from action_log
    2. Move email back via Graph API
    3. Update suggestion status to 'undone'
    4. Log undo to action_log
    """
```

The existing `undo --last N` CLI command works for auto-executed actions — it queries `action_log` where `triggered_by = 'auto_executed'`.

### Database Changes

New suggestion status value: `'auto_executed'`. No schema change needed — `status` is a TEXT column.

New `agent_state` key:
- `daily_auto_count_date` — date for daily counter reset
- `daily_auto_count` — counter for daily limit enforcement

### Config Changes

New config section (see 3A above):
```yaml
auto_mode:
  enabled: false
  confidence_threshold: 0.90
  auto_folders: []
  auto_actions: []
  excluded_priorities: ["P1 - Urgent Important"]
  require_sender_history: true
  daily_auto_limit: 50
```

### Structured Logging

Every auto-execution logs a structured event:

```json
{
    "event": "email_auto_executed",
    "triage_cycle_id": "...",
    "email_id": "...",
    "folder": "Reference/Newsletters",
    "priority": "P4 - Low",
    "action_type": "FYI Only",
    "confidence": 0.97,
    "method": "claude_tool_use",
    "sender": "news@example.com",
    "subject": "Weekly Newsletter #47",
    "daily_auto_count": 12
}
```

### Error Handling

| Error | Response |
|-------|----------|
| Graph API move failure | Revert suggestion to 'pending', log ERROR, count as manual review needed |
| Graph API category set failure | Log WARNING, email still moved (categories are non-critical) |
| Daily limit reached | Switch to suggest-only, log WARNING: "Daily auto-execution limit of {N} reached. Remaining emails queued for manual review." |
| Undo move failure | Log ERROR with original and current folder, suggest manual move |

### Safety Invariants

These are non-negotiable rules enforced in code, not just config:

1. **P1 emails are NEVER auto-executed** — even if the user misconfigures `excluded_priorities`
2. **Auto-execution always creates a suggestion record** — full audit trail
3. **Auto-execution always logs to action_log** — immutable audit
4. **Daily limit exists** — prevents runaway auto-execution
5. **Undo is always available** — every auto-execution can be reversed
6. **The user can disable auto-mode at any time** — set `auto_mode.enabled: false`, takes effect next cycle

```python
# SAFETY: Hardcoded, not configurable
NEVER_AUTO_EXECUTE_PRIORITIES = ["P1 - Urgent Important"]

def _should_auto_execute(self, result: ClassificationResult) -> bool:
    # ...
    # SAFETY: P1 never auto-executed, regardless of config
    if result.priority in NEVER_AUTO_EXECUTE_PRIORITIES:
        return False
    # ...
```

### Testing Strategy

**Unit tests:**
- Decision gate: all conditions pass → auto-execute
- Decision gate: any condition fails → suggest
- P1 never auto-executed (even with misconfigured excluded_priorities)
- Daily limit enforcement
- Daily counter reset at midnight
- Undo reverses move and updates status

**Integration tests:**
- Full lifecycle: classify → auto-execute → verify email moved → undo → verify moved back
- Daily limit: auto-execute up to limit → next classification queued as pending

### Verification Checklist

- [ ] Auto-execution only when ALL conditions met
- [ ] P1 emails never auto-executed (hardcoded safety)
- [ ] Suggestion record created for every auto-execution
- [ ] Action log records every auto-execution
- [ ] Dashboard shows auto-executed counts
- [ ] Daily limit enforced
- [ ] Undo works via CLI and (future) UI
- [ ] Graph API failure reverts to pending
- [ ] Startup log confirms auto-mode configuration

---

## 3C: New Project Detection

### What It Does

When Claude classifies an email and returns `suggested_new_project` (a field already in the tool schema), the system surfaces a project creation suggestion in the review UI.

### User-Facing Behavior

In the review UI, a special card appears:
```
NEW PROJECT DETECTED
Claude suggests creating: "Azure Migration"
Based on: 3 recent emails about Azure cloud migration
Action: [Create Project] [Dismiss] [Snooze 7 days]
```

On "Create Project":
1. Create folder in Outlook via Graph API
2. Add project to `config.yaml` projects section
3. Reclassify the triggering email(s) to the new folder
4. Reload config

### How It Builds on Existing Code

The `classify_email` tool already has an optional `suggested_new_project` field. Currently this field is captured in `ClassificationResult` but not acted upon.

**`web/routes.py`** — Add new project management:

```python
@router.get("/api/project-suggestions")
async def list_project_suggestions(store: Store = Depends(get_store)) -> JSONResponse:
    """List pending project creation suggestions."""

@router.post("/api/project-suggestions/{id}/create")
async def create_project(
    id: int,
    request: Request,
    store: Store = Depends(get_store),
) -> JSONResponse:
    """Accept a project suggestion: create folder, update config, reclassify."""

@router.post("/api/project-suggestions/{id}/dismiss")
async def dismiss_project(id: int, store: Store = Depends(get_store)) -> JSONResponse:
    """Dismiss a project suggestion."""

@router.post("/api/project-suggestions/{id}/snooze")
async def snooze_project(
    id: int,
    days: int = 7,
    store: Store = Depends(get_store),
) -> JSONResponse:
    """Snooze a project suggestion for N days."""
```

**`engine/triage.py`** — After classification, check for new project suggestions:

```python
if result.suggested_new_project:
    await self._handle_new_project_suggestion(email, result)
```

**`config.py`** — Add standalone config writer function (reuses the same read-modify-validate-backup-write-reload pattern established in Phase 2 Feature 2E for `append_auto_rule()`):

```python
def append_project(
    config_path: Path,
    name: str,
    folder: str,
    signals: dict | None = None,
) -> None:
    """Append a new project to config.yaml and trigger reload.

    Reads current YAML file, appends to projects list, validates,
    writes back with backup (config.yaml.bak.{timestamp}).
    Calls reload_config_if_changed() after successful write.

    Raises ConfigValidationError if the modified config is invalid.
    """
```

### Database Changes

New table for project suggestions:

```sql
CREATE TABLE IF NOT EXISTS project_suggestions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_name TEXT NOT NULL,
    suggested_folder TEXT NOT NULL,
    first_seen_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    trigger_email_ids TEXT,           -- JSON array of email IDs that triggered this
    occurrence_count INTEGER DEFAULT 1,
    status TEXT DEFAULT 'pending',    -- 'pending', 'created', 'dismissed', 'snoozed'
    snoozed_until DATETIME,
    resolved_at DATETIME
);
```

### Deduplication

Multiple emails might suggest the same new project with slightly different names. Before creating a new project suggestion:
1. Normalize the suggested name (lowercase, strip whitespace)
2. Check for existing pending/snoozed suggestions with similar names (fuzzy match)
3. If similar exists, increment `occurrence_count` and append email ID
4. If no similar exists, create new suggestion

Higher `occurrence_count` = stronger signal that the project is real.

### Config Changes

None. Project creation modifies the existing `projects` list in config.yaml.

### Error Handling

- Folder creation failure: log ERROR, keep suggestion as pending, show error in UI
- Config write failure: log ERROR, roll back to backup
- Duplicate project name: append a number (e.g., "Azure Migration 2")
- Snoozed project re-suggested: only un-snooze if `occurrence_count` increases by 3+

### Testing Strategy

**Unit tests:**
- Project suggestion creation from `suggested_new_project` field
- Deduplication of similar project names
- Snooze and dismiss logic
- Config append and backup

**Integration tests:**
- Full lifecycle: email classified → project suggested → user creates → folder exists → email reclassified

### Verification Checklist

- [ ] `suggested_new_project` field triggers project suggestion
- [ ] Duplicate project names deduplicated
- [ ] Create action creates folder and updates config
- [ ] Dismiss removes suggestion
- [ ] Snooze hides suggestion for configured days
- [ ] Config backup before modification
- [ ] Triggering emails reclassified to new folder

---

## 3D: Auto-Archive for Completed Projects

### What It Does

Detects project inactivity (no new emails in a configurable number of days) and surfaces an archive suggestion in the review UI.

### User-Facing Behavior

In the dashboard or a dedicated section:
```
PROJECT HEALTH
  Active (5):
    Projects/Tradecore Steel — 12 emails this week
    Projects/SOC 2 — 3 emails this week
    ...

  Potentially Completed (2):
    Projects/NET9 Migration — no emails in 45 days [Archive] [Keep Active]
    Projects/Old Vendor Deal — no emails in 62 days [Archive] [Keep Active]
```

On "Archive":
1. Move folder under `Archive/` in Outlook
2. Move project from `projects` to an `archived_projects` section in config
3. Reload config
4. Future emails matching the project's signals → classify normally (may trigger new project detection)

### How It Builds on Existing Code

**`engine/triage.py`** — Add periodic project health check (daily, not every cycle):

```python
async def _check_project_health(self) -> list[ProjectHealthStatus]:
    """Check all projects for inactivity.

    Query emails table for most recent email in each project's folder.
    Flag projects with no activity in archive_after_days.
    """
```

**`web/routes.py`** — Add project health section to dashboard and archive endpoint:

```python
@router.post("/api/projects/{folder_path}/archive")
async def archive_project(
    folder_path: str,
    store: Store = Depends(get_store),
) -> JSONResponse:
    """Archive a completed project.

    1. Move folder under Archive/ via Graph API
    2. Move project config to archived_projects section
    3. Reload config
    """
```

### Result Dataclass

```python
@dataclass(frozen=True)
class ProjectHealthStatus:
    """Health status of a project."""
    project_name: str
    folder: str
    last_email_at: datetime | None
    email_count_30d: int
    inactive_days: int
    status: str   # 'active', 'low_activity', 'inactive'
```

### Config Changes

New optional config fields:

```yaml
project_health:
  archive_after_days: 60          # Suggest archive after N days of inactivity
  low_activity_threshold: 14      # Flag as "low activity" after N days with no email
  check_interval_hours: 24        # How often to check project health
```

### Error Handling

- Folder move failure: log ERROR, keep suggestion visible, show error in UI
- Config write failure: roll back, log ERROR
- Project with active waiting-for items: do NOT suggest archive (override inactivity detection)

### Testing Strategy

**Unit tests:**
- Inactivity detection with various date scenarios
- Active waiting-for prevents archive suggestion
- Project health status calculation

**Integration tests:**
- Full lifecycle: project inactive → suggestion shown → user archives → folder moved → config updated

### Verification Checklist

- [ ] Inactive projects detected based on email date
- [ ] Projects with active waiting-for items not suggested for archive
- [ ] Archive moves folder under Archive/ in Outlook
- [ ] Config updated to reflect archived project
- [ ] Archived project's signals still trigger normal classification
- [ ] Dashboard shows project health status

---

## 3E: Weekly Review Report

### What It Does

Generates a deeper weekly analysis compared to the daily digest. Covers: week summary, project activity levels, sender patterns, accuracy trends, auto-rule suggestions, and auto-execution performance.

### User-Facing Behavior

Delivered weekly (configurable day/time) via the same channels as the daily digest. Also available via CLI: `python -m assistant weekly-review`.

### Report Sections

```
1. WEEK SUMMARY       — Emails processed, auto-ruled, classified, auto-executed, failed
2. PROJECT ACTIVITY    — Emails per project/area this week vs previous week (trending)
3. ACCURACY TRENDS     — Approval rate trend, correction rate trend, confidence calibration changes
4. SENDER INSIGHTS     — New senders, senders with changed patterns, auto-rule candidates
5. AUTO-EXECUTION      — Performance summary, any undone auto-executions, edge cases
6. RECOMMENDATIONS     — Suggested config changes, model upgrades, rule consolidation
```

### How It Builds on Existing Code

**New file: `engine/weekly_review.py`**

```python
class WeeklyReviewGenerator:
    """Generate weekly review reports with trend analysis.

    Deeper analysis than daily digest, focused on system performance
    and optimization recommendations.
    """

    def __init__(
        self,
        store: Store,
        anthropic_client: anthropic.AsyncAnthropic,
        config: AppConfig,
    ) -> None: ...

    async def generate(self) -> WeeklyReviewResult:
        """Gather weekly data and generate formatted review.

        Uses Claude Haiku to synthesize trends and generate
        actionable recommendations.
        """
```

**`engine/triage.py`** — Register as APScheduler job:

```python
scheduler.add_job(
    self._run_weekly_review,
    CronTrigger(day_of_week=review_day, hour=review_hour),
    id="weekly_review",
)
```

**`cli.py`** — Add command:
```bash
python -m assistant weekly-review          # Generate and deliver now
```

### Config Changes

New optional config section:

```yaml
weekly_review:
  enabled: true
  schedule_day: "monday"          # Day of week
  schedule_time: "09:00"          # Local time
  delivery: "stdout"              # Same options as digest
```

### Error Handling

Same pattern as daily digest — Claude failure falls back to plain-text template.

### Testing Strategy

**Unit tests:**
- Weekly data aggregation queries
- Trend calculation (this week vs last week)
- Recommendation generation logic

### Verification Checklist

- [ ] Weekly review generates at configured schedule
- [ ] All 6 sections populated with correct data
- [ ] Trend analysis shows week-over-week changes
- [ ] Recommendations are actionable
- [ ] CLI command generates on demand
- [ ] Delivery channels work (stdout/file/email)

---

## 3F: Email Delivery for Digests

### What It Does

Enables delivery of daily digests and weekly reviews as HTML emails sent to the user's own inbox via Graph API.

### User-Facing Behavior

When `digest.delivery: "email"`, the daily digest arrives as a formatted HTML email in the user's inbox with subject: "Outlook Assistant - Daily Digest - {date}".

### How It Builds on Existing Code

**`graph/messages.py`** — Add send method:

```python
async def send_message(
    self,
    to_email: str,
    subject: str,
    body_html: str,
) -> None:
    """Send an email via Graph API.

    Uses POST /me/sendMail endpoint.
    Requires Mail.Send permission (already in scopes).
    """
```

**`engine/digest.py`** — Extend delivery method:

```python
async def deliver(self, digest: DigestResult) -> None:
    match self._config.digest.delivery:
        case "stdout":
            print(digest.text)
        case "file":
            path = Path(f"data/digests/{digest.generated_at:%Y-%m-%d}.txt")
            path.write_text(digest.text)
        case "email":
            html = self._format_as_html(digest.text)
            await self._message_manager.send_message(
                to_email=self._user_email,
                subject=f"Outlook Assistant - Daily Digest - {digest.generated_at:%Y-%m-%d}",
                body_html=html,
            )
```

### HTML Formatting

Simple, clean HTML email template:
- Inline CSS (email clients strip `<style>` tags)
- Priority color coding matching the web UI
- Links to the review UI (`http://localhost:8080/review`)
- Plain text fallback in email body

### Config Changes

No new fields. Uses existing `digest.delivery` field which already supports "email" as a value.

### Error Handling

- Graph API send failure: log ERROR, fall back to stdout delivery
- Invalid user email: log ERROR, skip email delivery, deliver to stdout
- HTML rendering failure: fall back to plain text email

### Testing Strategy

**Unit tests:**
- HTML formatting produces valid HTML
- Delivery routing (email, stdout, file)
- Fallback to stdout on send failure

**Integration tests:**
- Send test email via Graph API

### Verification Checklist

- [ ] Email sent via Graph API with correct subject and body
- [ ] HTML formatting renders correctly in email clients
- [ ] Fallback to stdout on send failure
- [ ] Works for both daily digest and weekly review

---

## Principles Alignment

### Toyota Five Pillars

| Pillar | Application in Phase 3 |
|--------|----------------------|
| Not Over-Engineered | Auto-execution is a simple decision gate — one `if` chain, not a rule engine. Project detection reuses the existing `suggested_new_project` field. Weekly review is the same pattern as daily digest, just more sections. |
| Sophisticated Where Needed | Safety invariants (P1 never auto-executed) are hardcoded, not configurable — the right level of safety complexity. Daily limit prevents runaway execution. Undo mechanism for every auto-action. |
| Robust Error Handling | Every auto-execution has a fallback (revert to pending). Config modifications backup before writing. Folder moves are logged with original path for undo. |
| Complete Observability | Every auto-execution logged with full context (email, folder, confidence, method). Dashboard separates auto-executed from manual. Weekly review surfaces accuracy trends. |
| Proven Patterns | Auto-execution is a strategy pattern (same pipeline, different action at the end). Weekly review is the same generator pattern as daily digest. Config modification uses read-modify-write with backup. |

### Unix Philosophy

| Rule | Application in Phase 3 |
|------|----------------------|
| Representation | Auto-mode config in YAML (folders, actions, thresholds). Project health thresholds in config. All policy in data. |
| Least Surprise | Auto-executed items appear in the same review UI as manual suggestions — just with a different status. Undo works the same way regardless of how the action was triggered. |
| Modularity | Auto-execution is a new branch in the existing classification pipeline, not a new pipeline. Weekly review is a new generator, not embedded in the triage engine. |
| Separation | Decision logic (should we auto-execute?) is separate from execution logic (move the email). Thresholds are config; checks are code. |
| Repair | Auto-execution failures are loud: revert to pending, log ERROR, show in dashboard. Never silently fail. |

---

## Database Migration Strategy

Phase 3 adds one new table (`project_suggestions`) and new `agent_state` keys. No existing tables are modified.

```python
PHASE_3_MIGRATIONS = [
    """CREATE TABLE IF NOT EXISTS project_suggestions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_name TEXT NOT NULL,
        suggested_folder TEXT NOT NULL,
        first_seen_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        trigger_email_ids TEXT,
        occurrence_count INTEGER DEFAULT 1,
        status TEXT DEFAULT 'pending',
        snoozed_until DATETIME,
        resolved_at DATETIME
    )""",
]
```

---

## Config Additions Summary

All new config fields are optional with defaults. Existing configs continue to work unchanged.

```yaml
# New in Phase 3 (all optional):

auto_mode:
  enabled: false
  confidence_threshold: 0.90
  auto_folders: []
  auto_actions: []
  excluded_priorities: ["P1 - Urgent Important"]
  require_sender_history: true
  daily_auto_limit: 50

project_health:
  archive_after_days: 60
  low_activity_threshold: 14
  check_interval_hours: 24

weekly_review:
  enabled: true
  schedule_day: "monday"
  schedule_time: "09:00"
  delivery: "stdout"
```

---

## The Progressive Trust Model

Phase 3 is designed to be adopted gradually:

**Week 1:** Enable auto-mode for Reference folders only (newsletters, notifications)
```yaml
auto_mode:
  enabled: true
  auto_folders: ["Reference/Newsletters", "Reference/Dev Notifications"]
  auto_actions: ["FYI Only"]
```

**Week 2-4:** Monitor stats dashboard. If accuracy is >95%, expand to more folders
```yaml
auto_mode:
  auto_folders:
    - "Reference/Newsletters"
    - "Reference/Dev Notifications"
    - "Reference/Calendar"
    - "Areas/SYSPRO"
  auto_actions: ["FYI Only", "Review"]
```

**Month 2+:** Full auto-mode for most folders, human review only for P1 and low-confidence
```yaml
auto_mode:
  auto_folders:
    - "Reference/*"
    - "Areas/*"
    - "Projects/*"
  auto_actions: ["FYI Only", "Review", "Needs Reply", "Delegated"]
  # P1 still excluded by default
```

At every stage, the user retains full control and can disable auto-mode instantly. Every action is logged and undoable.
