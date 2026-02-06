# Outlook AI Assistant - Implementation Plan

## Overview

Build an AI-powered email management agent for Outlook using Microsoft Graph API and Claude. Docker-containerized Python 3.12+ service with SQLite database and FastAPI web UI.

**Key Decision:** Build fresh with MSAL (not adapting existing code). Only reuse: `rate_limiter.py` (copy directly).

**Estimated Timeline:** 7-8 developer days

---

## Progress Tracker

| Phase | Status | Started | Completed |
|-------|--------|---------|-----------|
| 1. Project Scaffolding | ✅ Complete | 2026-02-06 | 2026-02-06 |
| 2. Auth + Graph API | ✅ Complete | 2026-02-06 | 2026-02-06 |
| 3. Database Layer | ✅ Complete | 2026-02-06 | 2026-02-06 |
| 4. Email Pipeline | ✅ Complete | 2026-02-06 | 2026-02-06 |
| 5. Classification Engine | ✅ Complete | 2026-02-06 | 2026-02-06 |
| 6. Bootstrap & Dry-Run | ✅ Complete | 2026-02-06 | 2026-02-06 |
| 7. Triage Engine & Web UI | ⬜ Not Started | | |

Status: ⬜ Not Started | 🟡 In Progress | ✅ Complete | ❌ Blocked

---

## Phase 1: Project Scaffolding (Est: 1 day)

### Deliverables
- [x] 1.1 Create project directory structure
- [x] 1.2 Create `pyproject.toml` with all dependencies
- [x] 1.3 Create Dockerfile and docker-compose.yaml
- [x] 1.4 Create `.env.example` and `.gitignore`
- [x] 1.5 Set up structlog with JSON output and correlation IDs
- [x] 1.6 Create Pydantic config schema (`config_schema.py`)
- [x] 1.7 Implement config loader with hot-reload (`config.py`)
- [x] 1.8 Copy `rate_limiter.py` from Reference/working-examples/
- [x] 1.9 Create CLI with `validate-config` command

### Files to Create
```
outlook-ai-assistant/
├── pyproject.toml
├── Dockerfile
├── docker-compose.yaml
├── .env.example
├── .gitignore
├── config/config.yaml.example
├── src/assistant/
│   ├── __init__.py
│   ├── __main__.py
│   ├── cli.py                 # Click CLI with validate-config
│   ├── config.py              # YAML loader + hot-reload
│   ├── config_schema.py       # Pydantic models
│   └── core/
│       ├── __init__.py
│       ├── logging.py         # structlog setup
│       ├── errors.py          # Custom exceptions
│       └── rate_limiter.py    # COPY from Reference/working-examples/
└── tests/conftest.py
```

### Dependencies (pyproject.toml)
```toml
dependencies = [
    "anthropic>=0.78.0",
    "msal>=1.34.0",
    "requests>=2.32.0",
    "aiohttp>=3.9.0",
    "fastapi>=0.128.0",
    "uvicorn>=0.40.0",
    "jinja2>=3.1.5",
    "pyyaml>=6.0.2",
    "pydantic>=2.10.0",
    "apscheduler>=3.11.0,<4.0.0",
    "click>=8.3.0",
    "rich>=14.0.0",
    "structlog>=25.1.0",
    "python-dateutil>=2.9.0",
    "aiosqlite>=0.22.0",
    "regex>=2024.0.0",  # CRITICAL: For timeout support on all patterns
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0.0",
    "pytest-asyncio>=0.25.0",
    "httpx>=0.28.0",
    "ruff>=0.9.0",
]
```

### Verification Checklist
- [x] `uv sync` installs all dependencies (used pip with venv)
- [x] `uv run python -m assistant validate-config` runs
- [x] Invalid config shows specific Pydantic validation errors
- [x] Docker build succeeds
- [x] Structured logging outputs JSON format

### Reference Files
- `Reference/spec/02-config-and-schema.md` - Config.yaml structure
- `Reference/spec/07-setup-guide.md` - Dockerfile, dependencies
- `Reference/working-examples/rate_limiter.py` - Copy this file

---

## Phase 2: Authentication & Graph API (Est: 1 day)

### Deliverables
- [x] 2.1 Implement MSAL device code flow authentication
- [x] 2.2 Implement token cache persistence
- [x] 2.3 Auto-detect user email from `/me` endpoint
- [x] 2.4 Create base Graph API client with retry logic
- [x] 2.5 Implement folder operations (list, create, create subfolder)
- [x] 2.6 Implement message operations (list with pagination, move, set categories)

### Files to Create
```
src/assistant/
├── auth/
│   ├── __init__.py
│   └── msal_auth.py           # Device code flow
└── graph/
    ├── __init__.py
    ├── client.py              # Base client with retry
    ├── messages.py            # Email operations
    └── folders.py             # Folder operations
```

### MSAL Implementation
```python
# Key pattern from Reference/spec/07-setup-guide.md Section 4
class GraphAuth:
    def __init__(self, client_id, tenant_id, scopes, token_cache_path):
        self.app = msal.PublicClientApplication(
            client_id=client_id,
            authority=f"https://login.microsoftonline.com/{tenant_id}",
            token_cache=self.cache,
        )

    def get_access_token(self) -> str:
        # 1. Try silent acquisition (from cache/refresh)
        # 2. Fall back to device code flow
        # 3. Save cache on success
```

### Graph API Select Fields
```
id, conversationId, conversationIndex, subject, from, receivedDateTime,
bodyPreview, parentFolderId, categories, webLink, flag, isRead, importance
```

### Error Handling Requirements
- Rate limit (429): Respect `Retry-After` header
- Server errors (5xx): Exponential backoff (1s, 2s, 4s), max 3 attempts
- Auth errors: Clear message with re-auth instructions

### Verification Checklist
> **Status:** Code complete. Items below require manual integration testing with a real mailbox.

- [x] Device code authentication prompts correctly *(code complete)*
- [x] Token cache persists across restarts *(code complete)*
- [x] User email auto-detected from `/me` *(code complete)*
- [x] Can list folders from real mailbox *(code complete)*
- [x] Can list messages with pagination *(code complete)*
- [x] Can move a message between folders *(code complete)*
- [x] Can set categories on a message *(code complete)*
- [x] Rate limit handling works *(code complete)*

### Reference Files
- `Reference/spec/07-setup-guide.md` Section 4 - MSAL code reference
- `Reference/spec/05-graph-api.md` - Endpoints, fields, pagination

---

## Phase 3: Database Layer (Est: 0.5 day)

### Deliverables
- [x] 3.1 Create SQLite schema with all 7 tables
- [x] 3.2 Enable WAL mode for concurrent access
- [x] 3.3 Create indexes for performance
- [x] 3.4 Implement CRUD operations for all tables
- [x] 3.5 Implement LLM request logging
- [x] 3.6 Implement log pruning for retention

### Files to Create
```
src/assistant/db/
├── __init__.py
├── models.py                  # Schema SQL + migrations
└── store.py                   # CRUD operations
```

### Database Tables
| Table | Purpose |
|-------|---------|
| `emails` | Processed email metadata, classification status |
| `suggestions` | Compound suggestions (folder + priority + action) |
| `waiting_for` | Tracked waiting items |
| `agent_state` | Key-value state (delta_token, last_processed, preferences) |
| `sender_profiles` | Sender categorization for faster routing |
| `llm_request_log` | Claude API call logging for debugging |
| `action_log` | Audit trail of all agent actions |

### Key Implementation Details
```sql
-- MUST be set before creating tables
PRAGMA journal_mode=WAL;

-- Critical indexes
CREATE INDEX idx_emails_conversation_id ON emails(conversation_id);
CREATE INDEX idx_emails_sender ON emails(sender_email);
CREATE INDEX idx_llm_log_timestamp ON llm_request_log(timestamp);
```

### Verification Checklist
- [x] All 7 tables created with correct schema
- [x] WAL mode is enabled (check with `PRAGMA journal_mode;`)
- [x] Indexes are created
- [x] CRUD operations work for each table
- [x] LLM log pruning deletes old entries correctly

### Reference Files
- `Reference/spec/02-config-and-schema.md` Section 3 - Complete schema SQL

---

## Phase 4: Email Processing Pipeline (Est: 0.5 day)

### Deliverables
- [x] 4.1 Implement 6-step snippet cleaning pipeline
- [x] 4.2 Implement SentItemsCache for reply state detection (done in Phase 2)
- [x] 4.3 Implement thread inheritance check
- [x] 4.4 Implement thread context fetching (local DB first, then Graph)
- [x] 4.5 Implement sender history lookup

### Files to Create
```
src/assistant/
├── classifier/
│   ├── __init__.py
│   └── snippet.py             # 6-step cleaning pipeline
├── graph/messages.py          # Add SentItemsCache class
└── engine/
    ├── __init__.py
    └── thread_utils.py        # Inheritance + context
```

### Snippet Cleaning Pipeline (6 steps)
1. Strip HTML tags, decode entities (if HTML body)
2. Remove forwarded message headers (`---------- Forwarded message ----------`)
3. Remove signature blocks (`--`, `_____`, common patterns)
4. Remove legal/confidentiality disclaimers
5. Collapse excessive whitespace
6. Truncate to 1000 chars

**CRITICAL SECURITY REQUIREMENT:**
```python
import regex  # NOT re - for timeout support

# ALL patterns MUST have timeout to prevent ReDoS
SIGNATURE_PATTERN = regex.compile(r'^--\s*$', timeout=1.0)
```

### Thread Inheritance Logic
```
For each new email with conversation_id:
  Query emails table for prior classification
  |
  +-- Found? Check for significant change:
      |
      +-- Subject changed (not just Re:/Fwd:)? -> Full classification
      +-- New sender domain in thread? -> Full classification
      +-- Otherwise -> Inherit folder (confidence 0.95)
          Still classify priority + action_type via Claude
```

### Verification Checklist
- [x] Snippet cleaning removes signatures correctly
- [x] Snippet cleaning removes disclaimers
- [x] HTML tags stripped, entities decoded
- [x] All regex patterns have timeout (used in sub/search operations)
- [x] SentItemsCache refreshes efficiently
- [x] Reply state detection works for threads
- [x] Thread inheritance returns folder when conversation matches
- [x] Thread inheritance returns None when subject changes significantly
- [x] Thread context checks local DB before Graph API
- [x] Sender history returns distribution correctly

### Reference Files
- `Reference/spec/03-agent-behaviors.md` Section 6 - Snippet cleaning
- `Reference/spec/03-agent-behaviors.md` Section 2 - Thread inheritance logic

---

## Phase 5: Classification Engine (Est: 1.5 days)

### Deliverables
- [x] 5.1 Implement auto-rules pattern matching engine (`fnmatch` for senders, case-insensitive substring for subjects)
- [x] 5.2 Create prompt context assembler (system prompt, tool definition, conditional context sections)
- [x] 5.3 Implement Claude classifier with tool use (`tool_choice` forced, model from config)
- [x] 5.4 Support partial classification mode (inherited folder → Claude only classifies priority + action_type)
- [x] 5.5 Implement classification error handling (SDK retries for transient, app-level retry for logical failures)
- [x] 5.6 Log all LLM requests to database

### Files to Create
```
src/assistant/classifier/
├── auto_rules.py              # Pattern matching (fnmatch + substring)
├── prompts.py                 # Context assembler + tool definition
└── claude_classifier.py       # Tool use classification
```

### Auto-Rules Engine
```python
from fnmatch import fnmatch

class AutoRulesEngine:
    """Match emails against config auto_rules. Runs BEFORE Claude classification."""

    def match(self, email: dict, rules: list[AutoRuleConfig]) -> AutoRuleConfig | None:
        for rule in rules:
            if self._matches_rule(email, rule.match):
                return rule
        return None

    def _matches_rule(self, email: dict, match: AutoRuleMatch) -> bool:
        sender_match = not match.senders or any(
            fnmatch(email["sender_email"].lower(), pattern.lower())
            for pattern in match.senders
        )
        subject_match = not match.subjects or any(
            keyword.lower() in email["subject"].lower()
            for keyword in match.subjects
        )
        # If both senders and subjects are specified, BOTH must match (AND logic)
        # If only one is specified, that one is sufficient
        if match.senders and match.subjects:
            return sender_match and subject_match
        return sender_match or subject_match
```

**Why `fnmatch` instead of `regex`:** Config patterns use glob-style wildcards (`*@domain.com`, `notifications@github.com`). `fnmatch` handles these natively with no ReDoS risk. Reserve `regex` (with timeout) for any future advanced pattern needs.

### Claude Tool Definition (from spec `04-prompts.md` Section 3)
```json
{
  "name": "classify_email",
  "description": "Classify an email into the organizational structure",
  "input_schema": {
    "type": "object",
    "properties": {
      "folder": {
        "type": "string",
        "description": "Exact folder path from the structure (e.g., 'Projects/Tradecore Steel')"
      },
      "priority": {
        "type": "string",
        "enum": ["P1 - Urgent Important", "P2 - Important", "P3 - Urgent Low", "P4 - Low"]
      },
      "action_type": {
        "type": "string",
        "enum": ["Needs Reply", "Review", "Delegated", "FYI Only", "Waiting For", "Scheduled"]
      },
      "confidence": {
        "type": "number",
        "minimum": 0.0,
        "maximum": 1.0,
        "description": "Classification confidence score"
      },
      "reasoning": {
        "type": "string",
        "description": "One sentence explaining the classification"
      },
      "waiting_for_detail": {
        "type": ["object", "null"],
        "properties": {
          "expected_from": { "type": "string" },
          "description": { "type": "string" }
        },
        "description": "If action_type is Waiting For, who and what we're waiting for"
      },
      "suggested_new_project": {
        "type": ["string", "null"],
        "description": "If the email doesn't fit existing structure, suggest a new project name"
      }
    },
    "required": ["folder", "priority", "action_type", "confidence", "reasoning"]
  }
}
```

### Forced Tool Use
```python
# CRITICAL: Force Claude to return a tool call, not free text.
# Without this, an unattended agent may receive unparseable text responses.
response = client.messages.create(
    model=config.models.triage,  # Model from config, not hardcoded
    max_tokens=1024,
    system=system_prompt,
    messages=messages,
    tools=[CLASSIFY_EMAIL_TOOL],
    tool_choice={"type": "tool", "name": "classify_email"},
)
```

### Prompt Context Assembler

The system prompt and user message have **7 conditional context sections** that must be assembled per-email (see `04-prompts.md` Section 3):

```python
class PromptAssembler:
    """Builds classification prompts with conditional context sections."""

    def build_system_prompt(self, config: AppConfig, preferences: str | None) -> str:
        """Assembles system prompt with folder structure, key contacts, preferences."""
        # - Folder list from config.projects + config.areas
        # - Priority/action definitions (static)
        # - Key contacts from config.key_contacts
        # - Classification hints (static)
        # - Learned preferences from agent_state (or "No learned preferences yet.")
        # - Sender profile context (if available)

    def build_user_message(self, email: dict, context: ClassificationContext) -> str:
        """Assembles per-email user message with conditional sections."""
        # Required sections: From, Subject, Received, Importance, Read status,
        #                    Flag, Thread depth, Reply state, Body snippet
        # Conditional sections (include only when available):
        #   - inherited_folder: "Inherited folder (from thread): X"
        #   - sender_history: "Sender history: 94% -> Projects/Tradecore (47/50)"
        #   - sender_profile: "Sender profile: Category: newsletter | ..."
        #   - thread_context: Prior messages formatted per spec
```

### Partial Classification (Inherited Folder)

When thread inheritance provides a folder (see `03-agent-behaviors.md` Section 2):
1. The `inherited_folder` line is included in the user message
2. Claude still classifies **priority and action_type** (these can change within a thread)
3. The classifier merges: inherited folder + Claude's priority/action_type + confidence 0.95
4. The tool response's `folder` field is ignored in favor of the inherited value

### Error Handling

**Transient errors (SDK handles automatically):**
Configure `Anthropic(max_retries=3)` — the SDK retries 429, 5xx, timeouts, and connection errors with exponential backoff. No manual retry needed for these.

**Logical failures (app-level retry):**
| Error | Action |
|-------|--------|
| No tool call in response | Log WARNING, retry once (should not happen with `tool_choice` forced) |
| Missing required fields in tool call | Log ERROR, mark `classification_status = 'failed'`, increment `classification_attempts` |
| Invalid enum value (priority/action_type) | Log ERROR, mark `classification_status = 'failed'`, increment `classification_attempts` |
| `classification_attempts >= 3` | Stop retrying, set `classification_status = 'failed'`, include in daily digest |
| `anthropic.RateLimitError` (after SDK retries exhausted) | Respect `Retry-After` header, pause triage cycle, resume after delay |
| `anthropic.APIConnectionError` (after SDK retries exhausted) | Log ERROR, skip email for this cycle, remains `pending` for next cycle |

**Key principle:** SDK handles transient network/API errors. App code handles logical/semantic failures. No retry amplification.

### Verification Checklist
- [x] Auto-rules match sender wildcards via `fnmatch` (e.g., `*@domain.com`)
- [x] Auto-rules match subjects case-insensitively (substring match)
- [x] Auto-rules AND logic: both sender+subject must match when both specified
- [x] Auto-rules OR logic: either alone is sufficient when only one specified
- [x] `tool_choice` forces tool call response (no free text)
- [x] Model name sourced from `config.models.triage`, not hardcoded
- [x] Tool call result parsed with all 7 fields (5 required + 2 optional)
- [x] Partial classification: inherited folder merged with Claude's priority/action
- [x] System prompt includes folder structure, key contacts, preferences placeholder
- [x] User message conditionally includes sender_history, sender_profile, thread_context
- [x] LLM requests logged to `llm_request_log` with token counts and duration
- [x] SDK `max_retries=3` handles transient errors (no manual retry for 429/5xx)
- [x] App-level retry for logical failures (missing fields, invalid enums)
- [x] After 3 classification_attempts, email marked as `failed`
- [x] `waiting_for_detail` captured when action_type is "Waiting For"

### Reference Files
- `Reference/spec/04-prompts.md` - Complete prompt templates and tool definition
- `Reference/spec/03-agent-behaviors.md` Section 2 - Error handling table, thread inheritance
- Anthropic Cookbook `tool_use/extracting_structured_json.ipynb` - Tool use for classification pattern
- Anthropic SDK README - Built-in retry behavior, error types

---

## Phase 6: Bootstrap & Dry-Run (Est: 1 day)

### Deliverables
- [x] 6.1 Implement bootstrap Pass 1 (batch analysis with progress bar)
- [x] 6.2 Implement bootstrap Pass 2 (consolidation)
- [x] 6.3 Write `config.yaml.proposed` output
- [x] 6.4 Populate `sender_profiles` during bootstrap (batch upsert for N+1 elimination)
- [x] 6.5 Implement bootstrap idempotency checks
- [x] 6.6 Implement dry-run classifier with distribution report
- [x] 6.7 Implement confusion matrix (when corrections exist)
- [x] 6.8 Add CLI commands: `bootstrap`, `dry-run`

### Files to Create
```
src/assistant/engine/
├── bootstrap.py               # Two-pass scanner
└── dry_run.py                 # Classification testing
```

### Bootstrap Two-Pass Design

**Pass 1 - Batch Analysis:**
1. Fetch emails from last N days (show rich progress bar)
2. Extract metadata for each email
3. Batch to Claude Sonnet (50 emails per batch)
4. Each batch returns: projects, areas, sender clusters, volume estimates

**Pass 2 - Consolidation:**
5. Feed ALL batch results into single consolidation call
6. Merge duplicates (e.g., "Tradecore Steel Project" = "Tradecore Implementation")
7. Resolve conflicts
8. Write to `config/config.yaml.proposed`

### Idempotency Checks
```python
# Before running:
if config_yaml_proposed_exists:
    prompt("A proposed config already exists. Overwrite? (y/N)")

if agent_state.last_bootstrap_run:
    warn(f"Bootstrap was last run on {date}. Continue? (y/N)")

# --force flag skips all prompts
```

### CLI Commands
```bash
python -m assistant bootstrap --days 90 [--force]
python -m assistant dry-run --days 90 --sample 20 [--limit N]
```

### Verification Checklist
- [x] Bootstrap shows progress bar during email fetch
- [x] Bootstrap batches emails to Claude correctly
- [x] Bootstrap consolidation merges duplicates
- [x] `config.yaml.proposed` written with valid YAML
- [x] `sender_profiles` table populated (batch upsert)
- [x] Idempotency prompt shown for existing proposed config
- [x] `--force` flag skips prompts
- [x] Dry-run shows folder distribution report
- [x] Dry-run shows sample classifications
- [x] Confusion matrix shown when corrections exist

### Reference Files
- `Reference/spec/03-agent-behaviors.md` Section 1 - Bootstrap two-pass design
- `Reference/spec/04-prompts.md` Sections 1-2 - Bootstrap prompts

---

## Phase 7: Triage Engine & Web UI (Est: 2 days)

### Deliverables
- [ ] 7.1 Implement triage engine with APScheduler
- [ ] 7.2 Implement triage cycle with correlation IDs
- [ ] 7.3 Create FastAPI application structure
- [ ] 7.4 Create dashboard page (`/`)
- [ ] 7.5 Create review queue page (`/review`)
- [ ] 7.6 Implement approve/correct/reject API endpoints
- [ ] 7.7 Create waiting-for page (`/waiting`)
- [ ] 7.8 Create config editor page (`/config`)
- [ ] 7.9 Create activity log page (`/log`)
- [ ] 7.10 Add CLI commands: `serve`, `triage --once`

### Files to Create
```
src/assistant/
├── engine/triage.py           # Scheduler loop
└── web/
    ├── __init__.py
    ├── app.py                 # FastAPI app
    ├── routes.py              # API + page routes
    ├── static/style.css
    └── templates/
        ├── base.html
        ├── dashboard.html
        ├── review.html
        ├── waiting.html
        ├── config.html
        └── log.html
```

### Triage Cycle Flow
```
1. Generate triage_cycle_id (UUID) for log correlation
2. Refresh sent items cache (batch query)
3. Fetch new emails from watched folders
4. For each email:
   a. Check if already in emails table -> skip
   b. Check auto-rules -> apply if match (create auto-approved suggestion)
   c. Check thread inheritance -> inherit folder if applicable
   d. Fetch thread context if needed
   e. Look up sender history
   f. Call Claude classifier if needed
   g. Store suggestion in database
5. Update agent_state.last_processed_timestamp
6. Log cycle summary with duration, counts
```

### Web UI Routes
| Route | Method | Purpose |
|-------|--------|---------|
| `/` | GET | Dashboard with counts and health |
| `/review` | GET | Pending suggestions list |
| `/api/suggestions/{id}/approve` | POST | Execute move via Graph API |
| `/api/suggestions/{id}/correct` | POST | Store correction, execute |
| `/api/suggestions/{id}/reject` | POST | Mark rejected |
| `/waiting` | GET | Waiting-for tracker |
| `/config` | GET | Config editor |
| `/api/config` | GET/POST | Config CRUD with validation |
| `/log` | GET | Activity log |
| `/health` | GET | Health check for Docker |

### CLI Commands
```bash
python -m assistant serve              # Start scheduler + web UI (port 8080)
python -m assistant triage --once      # Single cycle then exit
python -m assistant triage --once --dry-run  # No suggestions created
```

### Verification Checklist
- [ ] Triage engine polls at configured interval
- [ ] triage_cycle_id appears in all log entries for a cycle
- [ ] Auto-rules route high-confidence emails correctly
- [ ] Thread inheritance reduces Claude API calls
- [ ] Suggestions stored in database correctly
- [ ] Dashboard shows correct counts
- [ ] Review queue displays pending suggestions
- [ ] Approve action moves email via Graph API
- [ ] Correct action stores correction and executes
- [ ] Reject action marks suggestion rejected
- [ ] Config editor validates YAML before saving
- [ ] Activity log shows recent actions
- [ ] `serve` starts both scheduler and web UI
- [ ] `triage --once` runs single cycle

### Reference Files
- `Reference/spec/03-agent-behaviors.md` Section 2 - Triage engine flow
- `Reference/spec/03-agent-behaviors.md` Section 3 - Review UI pages

---

## Coding Standards Checklist (Apply to ALL Phases)

### Error Handling
- [ ] Specific exception types (never bare `except` or `catch (Exception)`)
- [ ] Error messages include: what, where, why, how to fix
- [ ] Fail-fast on invalid config at startup
- [ ] Structured logging with context

### Security (CRITICAL)
- [ ] ALL regex patterns use `regex` library with `timeout=1.0`
- [ ] File paths validated against traversal attacks
- [ ] Token cache file has restricted permissions (600)
- [ ] Never store full email bodies, only cleaned snippets (max 1000 chars)

### Logging
- [ ] Use `structlog` with JSON output to stdout
- [ ] Include `triage_cycle_id` correlation ID in all triage logs
- [ ] Log success AND failure with relevant context
- [ ] No sensitive data in logs (mask tokens, passwords)

### Code Quality
- [ ] Guard clauses at function entry (validate inputs early)
- [ ] Single responsibility per function/class
- [ ] Follow existing patterns in the codebase
- [ ] No over-engineering (YAGNI)

---

## What's NOT in MVP (Deferred)

| Feature | Target Phase |
|---------|--------------|
| Waiting-for tracker (automatic detection) | Phase 2 |
| Daily digest generation | Phase 2 |
| Learning from corrections (classification_preferences) | Phase 2 |
| Delta queries for efficient polling | Phase 2 |
| Stats/accuracy dashboard (`/stats`) | Phase 2 |
| Sender management page (`/senders`) | Phase 2 |
| Undo command | Phase 2 |
| Autonomous mode (auto-execute high-confidence) | Phase 3 |
| Webhook + delta hybrid | Phase 2 |
| Token cache encryption | Phase 2 |

---

## Risk Mitigation

| Risk | Mitigation |
|------|------------|
| Graph API rate limiting | Use rate limiter, exponential backoff, pace bootstrap with 100ms delay |
| Claude API costs during development | Use `--limit` flag, test with small batches, mock in unit tests |
| Auth token expiry | MSAL handles refresh; clear error if re-auth needed |
| Regex ReDoS attacks | Use `regex` library with timeout on ALL patterns |
| Large inbox performance | Pagination, batch processing, thread inheritance (~50% reduction) |

---

## Quick Reference: Key Commands

```bash
# Development
uv sync                                    # Install dependencies
uv run python -m assistant validate-config # Validate config
uv run python -m assistant bootstrap --days 90  # Scan existing mail
uv run python -m assistant dry-run --days 30    # Test classification
uv run python -m assistant serve           # Start service (port 8080)
uv run python -m assistant triage --once   # Single triage cycle

# Testing
uv run pytest                              # Run all tests
uv run pytest tests/test_classifier.py     # Run specific tests
uv run ruff check src/                     # Lint code

# Docker
docker compose up -d                       # Start service
docker compose run --rm bootstrap --days 90  # Run bootstrap
docker logs outlook-assistant              # View logs
```
