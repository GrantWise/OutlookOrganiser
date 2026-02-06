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
| 3. Database Layer | ⬜ Not Started | | |
| 4. Email Pipeline | ⬜ Not Started | | |
| 5. Classification Engine | ⬜ Not Started | | |
| 6. Bootstrap & Dry-Run | ⬜ Not Started | | |
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
- [ ] Device code authentication prompts correctly
- [ ] Token cache persists across restarts
- [ ] User email auto-detected from `/me`
- [ ] Can list folders from real mailbox
- [ ] Can list messages with pagination
- [ ] Can move a message between folders
- [ ] Can set categories on a message
- [ ] Rate limit handling works (can simulate with short delay)

### Reference Files
- `Reference/spec/07-setup-guide.md` Section 4 - MSAL code reference
- `Reference/spec/05-graph-api.md` - Endpoints, fields, pagination

---

## Phase 3: Database Layer (Est: 0.5 day)

### Deliverables
- [ ] 3.1 Create SQLite schema with all 7 tables
- [ ] 3.2 Enable WAL mode for concurrent access
- [ ] 3.3 Create indexes for performance
- [ ] 3.4 Implement CRUD operations for all tables
- [ ] 3.5 Implement LLM request logging
- [ ] 3.6 Implement log pruning for retention

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
- [ ] All 7 tables created with correct schema
- [ ] WAL mode is enabled (check with `PRAGMA journal_mode;`)
- [ ] Indexes are created
- [ ] CRUD operations work for each table
- [ ] LLM log pruning deletes old entries correctly

### Reference Files
- `Reference/spec/02-config-and-schema.md` Section 3 - Complete schema SQL

---

## Phase 4: Email Processing Pipeline (Est: 0.5 day)

### Deliverables
- [ ] 4.1 Implement 6-step snippet cleaning pipeline
- [ ] 4.2 Implement SentItemsCache for reply state detection
- [ ] 4.3 Implement thread inheritance check
- [ ] 4.4 Implement thread context fetching (local DB first, then Graph)
- [ ] 4.5 Implement sender history lookup

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
- [ ] Snippet cleaning removes signatures correctly
- [ ] Snippet cleaning removes disclaimers
- [ ] HTML tags stripped, entities decoded
- [ ] All regex patterns have timeout (grep for `regex.compile`)
- [ ] SentItemsCache refreshes efficiently
- [ ] Reply state detection works for threads
- [ ] Thread inheritance returns folder when conversation matches
- [ ] Thread inheritance returns None when subject changes significantly
- [ ] Thread context checks local DB before Graph API
- [ ] Sender history returns distribution correctly

### Reference Files
- `Reference/spec/03-agent-behaviors.md` Section 6 - Snippet cleaning
- `Reference/spec/03-agent-behaviors.md` Section 2 - Thread inheritance logic

---

## Phase 5: Classification Engine (Est: 1 day)

### Deliverables
- [ ] 5.1 Implement auto-rules pattern matching engine
- [ ] 5.2 Create prompt templates (system prompt, tool definition)
- [ ] 5.3 Implement Claude classifier with tool use
- [ ] 5.4 Implement classification error handling (retry, fail after 3)
- [ ] 5.5 Log all LLM requests to database

### Files to Create
```
src/assistant/classifier/
├── auto_rules.py              # Pattern matching
├── prompts.py                 # System prompt + tool definition
└── claude_classifier.py       # Tool use classification
```

### Auto-Rules Engine
```python
# Match patterns with timeout
import regex

class AutoRulesEngine:
    def match(self, email: dict) -> Optional[AutoRule]:
        # Check sender patterns (wildcards like *@domain.com)
        # Check subject patterns (case-insensitive)
        # Return first matching rule or None
```

### Claude Tool Definition
```json
{
  "name": "classify_email",
  "input_schema": {
    "properties": {
      "folder": {"type": "string"},
      "priority": {"enum": ["P1 - Urgent Important", "P2 - Important", "P3 - Urgent Low", "P4 - Low"]},
      "action_type": {"enum": ["Needs Reply", "Review", "Delegated", "FYI Only", "Waiting For", "Scheduled"]},
      "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
      "reasoning": {"type": "string"}
    },
    "required": ["folder", "priority", "action_type", "confidence", "reasoning"]
  }
}
```

### Error Handling
| Error | Action |
|-------|--------|
| Network timeout / 5xx | Retry: 1s, 2s, 4s exponential backoff, max 3 attempts |
| Rate limit (429) | Respect `Retry-After` header, pause cycle |
| Invalid response | Log ERROR, mark `classification_status = 'failed'` |
| Failed 3 times | Stop retrying, include in daily digest |

### Verification Checklist
- [ ] Auto-rules match sender wildcards correctly
- [ ] Auto-rules match subjects case-insensitively
- [ ] Claude returns structured tool call response
- [ ] Tool call result parsed correctly
- [ ] LLM requests logged to `llm_request_log` table
- [ ] Retry logic works for transient failures
- [ ] After 3 failures, email marked as failed

### Reference Files
- `Reference/spec/04-prompts.md` - Complete prompt templates and tool definition
- `Reference/spec/03-agent-behaviors.md` Section 2 - Error handling table

---

## Phase 6: Bootstrap & Dry-Run (Est: 1 day)

### Deliverables
- [ ] 6.1 Implement bootstrap Pass 1 (batch analysis with progress bar)
- [ ] 6.2 Implement bootstrap Pass 2 (consolidation)
- [ ] 6.3 Write `config.yaml.proposed` output
- [ ] 6.4 Populate `sender_profiles` during bootstrap
- [ ] 6.5 Implement bootstrap idempotency checks
- [ ] 6.6 Implement dry-run classifier with distribution report
- [ ] 6.7 Implement confusion matrix (when corrections exist)
- [ ] 6.8 Add CLI commands: `bootstrap`, `dry-run`

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
- [ ] Bootstrap shows progress bar during email fetch
- [ ] Bootstrap batches emails to Claude correctly
- [ ] Bootstrap consolidation merges duplicates
- [ ] `config.yaml.proposed` written with valid YAML
- [ ] `sender_profiles` table populated
- [ ] Idempotency prompt shown for existing proposed config
- [ ] `--force` flag skips prompts
- [ ] Dry-run shows folder distribution report
- [ ] Dry-run shows sample classifications
- [ ] Confusion matrix shown when corrections exist

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
