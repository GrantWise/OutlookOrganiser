# Outlook AI Assistant Ã¢â‚¬â€ Overview

**Version:** 2.0 | **Last Updated:** 2026-02-06

> **This is the index document.** Always read this first. It provides the project vision, architecture, and tells you where to find detailed specs for each subsystem.

---

## Spec Index

| Document | Contents | Read when... |
|----------|----------|-------------|
| `01-overview.md` | Vision, architecture, tech stack, model tiering, build phases, project structure, CLI | Always Ã¢â‚¬â€ start here |
| `02-config-and-schema.md` | Data model (SQLite), folder taxonomy, categories, config.yaml structure, Pydantic validation, hot-reload | Working on config, database, or schema |
| `03-agent-behaviors.md` | Bootstrap scanner, triage engine, review UI, digest, waiting-for tracker, snippet processing, rule creation, deep links | Implementing any agent behavior |
| `04-prompts.md` | All Claude prompt templates + tool definitions for classification | Working on classification, bootstrap analysis, or digest generation |
| `05-graph-api.md` | Microsoft Graph endpoints, pagination, rate limits, delta queries, reply state detection | Implementing or debugging Graph API integration |
| `06-safety-and-testing.md` | Autonomy boundaries, data privacy, audit trail, rollback, testing strategy | Implementing safety checks or writing tests |
| `07-setup-guide.md` | Azure AD registration, Docker config, Dockerfile, docker-compose, dependencies, .env, troubleshooting | Onboarding, infrastructure, or deployment |
| `08-classification-chat.md` | Chat assistant for reclassification, config refinement, rule creation from Review UI | Implementing the chat panel, chat tools, or config write logic |
| `09-architecture-decisions.md` | Infrastructure decisions: Docker-local vs Azure, webhook removal, delta query polling strategy | Evaluating deployment architecture or Phase 4 migration |
| `10-native-task-integration.md` | Phase 1.5 To Do integration, category management, hybrid architecture | Implementing task creation, category bootstrap, or native M365 integration features |

---

## 1. Vision

An AI-powered email management agent that connects to Microsoft Outlook via the Microsoft Graph API, uses Claude as its intelligence layer, and organizes email according to a hybrid PARA + GTD + Eisenhower methodology. The agent runs as a Docker-containerized Python service on the user's local machine.

## 2. Problem Statement

A CEO managing a 50-person software company with 350+ manufacturing customers receives high volumes of email spanning active project implementations, ongoing operational areas, sales inquiries, vendor communications, automated notifications, and personal correspondence. Without automated triage, critical items get buried, follow-ups slip, and context-switching between threads is expensive.

## 3. Core Value Proposition

1. **Bootstrap**: Scan existing email to discover and propose an organizational taxonomy (projects, areas, contacts, patterns)
2. **Classify**: Automatically categorize incoming email by project/area, priority, and required action type
3. **Surface**: Highlight what needs attention Ã¢â‚¬â€ aging replies, overdue follow-ups, stuck threads
4. **Organize**: Move email into the right folders and apply the right labels (once trust is established)

## 4. Operating Modes

- **Suggest-Only Mode** (default, MVP): The agent proposes classifications and actions. The user reviews, approves, corrects, or rejects via a simple interface. Corrections feed back into improving classification accuracy.
- **Autonomous Mode** (future): The agent executes approved action types automatically based on confidence thresholds and learned patterns.

---

## 5. Architecture

### System Components

```
Ã¢â€Å'Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€Â
Ã¢â€â€š                    Docker Container                       Ã¢â€â€š
Ã¢â€â€š                                                          Ã¢â€â€š
Ã¢â€â€š  Ã¢â€Å'Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€Â   Ã¢â€Å'Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€Â   Ã¢â€Å'Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€Â          Ã¢â€â€š
Ã¢â€â€š  Ã¢â€â€š SchedulerÃ¢â€â€šÃ¢â€â‚¬Ã¢â€â‚¬Ã¢â€"Â¶Ã¢â€â€š  Triage  Ã¢â€â€šÃ¢â€â‚¬Ã¢â€â‚¬Ã¢â€"Â¶Ã¢â€â€š  Suggestion  Ã¢â€â€š          Ã¢â€â€š
Ã¢â€â€š  Ã¢â€â€š (APSched)Ã¢â€â€š   Ã¢â€â€š  Engine  Ã¢â€â€š   Ã¢â€â€š    Store     Ã¢â€â€š          Ã¢â€â€š
Ã¢â€â€š  Ã¢â€â€Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€Ëœ   Ã¢â€â€Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€Â¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€Ëœ   Ã¢â€â€š  (SQLite)    Ã¢â€â€š          Ã¢â€â€š
Ã¢â€â€š                      Ã¢â€â€š         Ã¢â€â€Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€Â¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€Ëœ          Ã¢â€â€š
Ã¢â€â€š                      Ã¢â€"Â¼                Ã¢â€â€š                  Ã¢â€â€š
Ã¢â€â€š              Ã¢â€Å'Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€Â         Ã¢â€â€š                  Ã¢â€â€š
Ã¢â€â€š              Ã¢â€â€š Claude API   Ã¢â€â€š         Ã¢â€â€š                  Ã¢â€â€š
Ã¢â€â€š              Ã¢â€â€š (Classifier) Ã¢â€â€š         Ã¢â€â€š                  Ã¢â€â€š
Ã¢â€â€š              Ã¢â€â€Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€Ëœ         Ã¢â€â€š                  Ã¢â€â€š
Ã¢â€â€š                      Ã¢â€â€š                Ã¢â€â€š                  Ã¢â€â€š
Ã¢â€â€š                      Ã¢â€"Â¼                Ã¢â€"Â¼                  Ã¢â€â€š
Ã¢â€â€š              Ã¢â€Å'Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€Â  Ã¢â€Å'Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€Â   Ã¢â€â€š
Ã¢â€â€š              Ã¢â€â€š MS Graph API Ã¢â€â€šÃ¢â€"â‚¬Ã¢â€"Â¶Ã¢â€â€š  Review Interface  Ã¢â€â€š   Ã¢â€â€š
Ã¢â€â€š              Ã¢â€â€š  (Outlook)   Ã¢â€â€š  Ã¢â€â€š  (FastAPI + HTML)  Ã¢â€â€š   Ã¢â€â€š
Ã¢â€â€š              Ã¢â€â€Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€Ëœ  Ã¢â€â€Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€Ëœ   Ã¢â€â€š
Ã¢â€â€š                                  Ã¢â€"Â²  reads suggestions   Ã¢â€â€š
Ã¢â€â€š  Ã¢â€Å'Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€Â                Ã¢â€â€š  writes approvals    Ã¢â€â€š
Ã¢â€â€š  Ã¢â€â€š  Bootstrap   Ã¢â€â€š                Ã¢â€â€š  executes moves      Ã¢â€â€š
Ã¢â€â€š  Ã¢â€â€š  Scanner     Ã¢â€â€š                Ã¢â€â€š  via Graph API       Ã¢â€â€š
Ã¢â€â€š  Ã¢â€â€Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€Ëœ                                       Ã¢â€â€š
Ã¢â€â€š                                                          Ã¢â€â€š
Ã¢â€â€š  Config: config.yaml (volume-mounted)                    Ã¢â€â€š
Ã¢â€â€š  Logs: structured JSON to stdout                         Ã¢â€â€š
Ã¢â€â€Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€Ëœ
```

### Technology Stack

| Component | Technology | Rationale |
|-----------|-----------|-----------|
| Language | Python 3.12+ | Rich ecosystem for API integration, Claude SDK, rapid development |
| Runtime | Docker Desktop | Isolated, reproducible, easy teardown, user has Docker experience |
| AI Engine | Anthropic Claude API (tiered by task) | Cost-effective model selection per task complexity |
| Email API | Microsoft Graph API v1.0 | Official Outlook access, supports folders, categories, search |
| Auth | MSAL (Microsoft Authentication Library) | OAuth2 device code flow for Graph API access |
| Database | SQLite | Lightweight, zero-config, file-based, perfect for local agent |
| Scheduler | APScheduler | Lightweight Python job scheduler |
| Review UI | FastAPI + Jinja2 templates | Simple web UI for reviewing suggestions, runs on localhost |
| Config | YAML + Pydantic validation | Human-readable config with schema enforcement on load |
| Logging | `structlog` with JSON output to stdout | Structured logging captured by Docker natively |

### Model Tiering Strategy

| Task | Model | Rationale |
|------|-------|-----------|
| Bootstrap Scanner (Pass 1) | `claude-sonnet-4-5-20250929` (Sonnet 4.5) | Complex pattern discovery from unstructured data. Runs infrequently. |
| Bootstrap Consolidation (Pass 2) | `claude-sonnet-4-5-20250929` (Sonnet 4.5) | Deduplication and merging across batch analyses. Single call. |
| Triage Classification | `claude-haiku-4-5-20251001` (Haiku 4.5) | High-volume, repetitive structured classification. Speed and cost matter. |
| Dry-Run Classifier | `claude-haiku-4-5-20251001` (Haiku 4.5) | Same classification logic as triage, run in batch. |
| Digest Generation | `claude-haiku-4-5-20251001` (Haiku 4.5) | Summarising structured data into formatted output. |
| Waiting-For Detection | `claude-haiku-4-5-20251001` (Haiku 4.5) | Thread analysis and pattern recognition. |

**Estimated costs:**
- Bootstrap scan (~3,000 emails, Sonnet 4.5): ~$2Ã¢â‚¬â€œ5 one-off
- Daily triage (~100 emails/day, Haiku 4.5): ~$0.10Ã¢â‚¬â€œ0.30/day
- Daily digest (Haiku 4.5): <$0.01/day

**Escalation path:** If Haiku classification accuracy falls below an acceptable threshold (measurable via user correction rate in the suggestions table), the config allows upgrading individual tasks to Sonnet without code changes. Models are configured per task in `config.yaml` and can be changed at any time without restarting the agent.

### Logging Strategy

The agent uses `structlog` for structured JSON logging to stdout. Docker captures this natively via `docker logs`.

**Log levels:**
- `DEBUG`: Individual email processing details, API request/response metadata
- `INFO`: Triage cycle summaries, config reload events, auth token refresh
- `WARNING`: Failed classifications, low-confidence results, config validation warnings
- `ERROR`: API failures (Claude or Graph), authentication failures, unhandled exceptions

**Log format:**
```json
{
  "timestamp": "2026-02-06T14:30:00Z",
  "level": "info",
  "event": "triage_cycle_complete",
  "triage_cycle_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "emails_processed": 12,
  "auto_routed": 3,
  "classified": 8,
  "failed": 1,
  "duration_ms": 4520
}
```

### Authentication Flow (Summary)

The agent uses Microsoft's **device code flow** for OAuth2. On first run, it prints a URL and code to the console. User authenticates in a browser. Tokens are cached and auto-refreshed. Full details in `07-setup-guide.md`.

Required Graph API permissions (delegated): `Mail.ReadWrite`, `Mail.Send`, `MailboxSettings.ReadWrite`, `User.Read`, `Tasks.ReadWrite`. See `07-setup-guide.md` for the full permissions table including Phase 2 additions.

---

## 6. Project Structure

```
outlook-ai-assistant/
Ã¢â€Å"Ã¢â€â‚¬Ã¢â€â‚¬ docker-compose.yaml
Ã¢â€Å"Ã¢â€â‚¬Ã¢â€â‚¬ Dockerfile
Ã¢â€Å"Ã¢â€â‚¬Ã¢â€â‚¬ pyproject.toml
Ã¢â€Å"Ã¢â€â‚¬Ã¢â€â‚¬ uv.lock                                # Lockfile Ã¢â‚¬â€ committed, deterministic builds
Ã¢â€Å"Ã¢â€â‚¬Ã¢â€â‚¬ README.md
Ã¢â€Å"Ã¢â€â‚¬Ã¢â€â‚¬ .gitignore
Ã¢â€Å"Ã¢â€â‚¬Ã¢â€â‚¬ .env.example
Ã¢â€Å"Ã¢â€â‚¬Ã¢â€â‚¬ config/
Ã¢â€â€š   Ã¢â€Å"Ã¢â€â‚¬Ã¢â€â‚¬ config.yaml.example
Ã¢â€â€š   Ã¢â€â€Ã¢â€â‚¬Ã¢â€â‚¬ config.yaml                        # User config (gitignored)
Ã¢â€Å"Ã¢â€â‚¬Ã¢â€â‚¬ src/
Ã¢â€â€š   Ã¢â€â€Ã¢â€â‚¬Ã¢â€â‚¬ assistant/
Ã¢â€â€š       Ã¢â€Å"Ã¢â€â‚¬Ã¢â€â‚¬ __init__.py
Ã¢â€â€š       Ã¢â€Å"Ã¢â€â‚¬Ã¢â€â‚¬ __main__.py                    # CLI entry point
Ã¢â€â€š       Ã¢â€Å"Ã¢â€â‚¬Ã¢â€â‚¬ cli.py                         # Click CLI: bootstrap, dry-run, triage, serve, validate-config
Ã¢â€â€š       Ã¢â€Å"Ã¢â€â‚¬Ã¢â€â‚¬ config.py                      # Config loader with Pydantic validation + hot-reload
Ã¢â€â€š       Ã¢â€Å"Ã¢â€â‚¬Ã¢â€â‚¬ config_schema.py               # Pydantic models for config.yaml structure
Ã¢â€â€š       Ã¢â€Å"Ã¢â€â‚¬Ã¢â€â‚¬ auth/
Ã¢â€â€š       Ã¢â€â€š   Ã¢â€Å"Ã¢â€â‚¬Ã¢â€â‚¬ __init__.py
Ã¢â€â€š       Ã¢â€â€š   Ã¢â€â€Ã¢â€â‚¬Ã¢â€â‚¬ graph_auth.py              # MSAL OAuth2 device code flow
Ã¢â€â€š       Ã¢â€Å"Ã¢â€â‚¬Ã¢â€â‚¬ graph/
Ã¢â€â€š       Ã¢â€â€š   Ã¢â€Å"Ã¢â€â‚¬Ã¢â€â‚¬ __init__.py
Ã¢â€â€š       Ã¢â€â€š   Ã¢â€Å"Ã¢â€â‚¬Ã¢â€â‚¬ client.py                  # Graph API client wrapper with retry logic
Ã¢â€â€š       Ã¢â€â€š   Ã¢â€Å"Ã¢â€â‚¬Ã¢â€â‚¬ messages.py                # Email operations (list, move, categorize, reply state)
Ã¢â€â€š       Ã¢â€â€š   Ã¢â€â€Ã¢â€â‚¬Ã¢â€â‚¬ folders.py                 # Folder operations (create, list)
Ã¢â€â€š       Ã¢â€Å"Ã¢â€â‚¬Ã¢â€â‚¬ classifier/
Ã¢â€â€š       Ã¢â€â€š   Ã¢â€Å"Ã¢â€â‚¬Ã¢â€â‚¬ __init__.py
Ã¢â€â€š       Ã¢â€â€š   Ã¢â€Å"Ã¢â€â‚¬Ã¢â€â‚¬ auto_rules.py              # Pattern-based auto-routing
Ã¢â€â€š       Ã¢â€â€š   Ã¢â€Å"Ã¢â€â‚¬Ã¢â€â‚¬ claude_classifier.py       # Claude API classification (tool use)
Ã¢â€â€š       Ã¢â€â€š   Ã¢â€Å"Ã¢â€â‚¬Ã¢â€â‚¬ prompts.py                 # Prompt templates
Ã¢â€â€š       Ã¢â€â€š   Ã¢â€â€Ã¢â€â‚¬Ã¢â€â‚¬ snippet.py                 # Email body cleaning pipeline
Ã¢â€â€š       Ã¢â€Å"Ã¢â€â‚¬Ã¢â€â‚¬ engine/
Ã¢â€â€š       Ã¢â€â€š   Ã¢â€Å"Ã¢â€â‚¬Ã¢â€â‚¬ __init__.py
Ã¢â€â€š       Ã¢â€â€š   Ã¢â€Å"Ã¢â€â‚¬Ã¢â€â‚¬ bootstrap.py               # Bootstrap scanner (two-pass)
Ã¢â€â€š       Ã¢â€â€š   Ã¢â€Å"Ã¢â€â‚¬Ã¢â€â‚¬ triage.py                  # Triage engine (scheduler loop)
Ã¢â€â€š       Ã¢â€â€š   Ã¢â€Å"Ã¢â€â‚¬Ã¢â€â‚¬ digest.py                  # Daily digest generator
Ã¢â€â€š       Ã¢â€â€š   Ã¢â€â€Ã¢â€â‚¬Ã¢â€â‚¬ waiting_for.py             # Waiting-for tracker
Ã¢â€â€š       Ã¢â€Å"Ã¢â€â‚¬Ã¢â€â‚¬ db/
Ã¢â€â€š       Ã¢â€â€š   Ã¢â€Å"Ã¢â€â‚¬Ã¢â€â‚¬ __init__.py
Ã¢â€â€š       Ã¢â€â€š   Ã¢â€Å"Ã¢â€â‚¬Ã¢â€â‚¬ models.py                  # SQLite schema & migrations
Ã¢â€â€š       Ã¢â€â€š   Ã¢â€â€Ã¢â€â‚¬Ã¢â€â‚¬ store.py                   # Database operations
Ã¢â€â€š       Ã¢â€â€Ã¢â€â‚¬Ã¢â€â‚¬ web/
Ã¢â€â€š           Ã¢â€Å"Ã¢â€â‚¬Ã¢â€â‚¬ __init__.py
Ã¢â€â€š           Ã¢â€Å"Ã¢â€â‚¬Ã¢â€â‚¬ app.py                     # FastAPI application
Ã¢â€â€š           Ã¢â€Å"Ã¢â€â‚¬Ã¢â€â‚¬ routes.py                  # API routes + page routes
Ã¢â€â€š           Ã¢â€Å"Ã¢â€â‚¬Ã¢â€â‚¬ static/
Ã¢â€â€š           Ã¢â€â€š   Ã¢â€â€Ã¢â€â‚¬Ã¢â€â‚¬ style.css
Ã¢â€â€š           Ã¢â€â€Ã¢â€â‚¬Ã¢â€â‚¬ templates/
Ã¢â€â€š               Ã¢â€Å"Ã¢â€â‚¬Ã¢â€â‚¬ base.html
Ã¢â€â€š               Ã¢â€Å"Ã¢â€â‚¬Ã¢â€â‚¬ dashboard.html
Ã¢â€â€š               Ã¢â€Å"Ã¢â€â‚¬Ã¢â€â‚¬ review.html
Ã¢â€â€š               Ã¢â€Å"Ã¢â€â‚¬Ã¢â€â‚¬ waiting.html
Ã¢â€â€š               Ã¢â€Å"Ã¢â€â‚¬Ã¢â€â‚¬ config.html
Ã¢â€â€š               Ã¢â€â€Ã¢â€â‚¬Ã¢â€â‚¬ log.html
               ├── stats.html
               ├── senders.html
Ã¢â€Å"Ã¢â€â‚¬Ã¢â€â‚¬ tests/
Ã¢â€â€š   Ã¢â€Å"Ã¢â€â‚¬Ã¢â€â‚¬ conftest.py
Ã¢â€â€š   Ã¢â€Å"Ã¢â€â‚¬Ã¢â€â‚¬ test_auto_rules.py
Ã¢â€â€š   Ã¢â€Å"Ã¢â€â‚¬Ã¢â€â‚¬ test_classifier.py
Ã¢â€â€š   Ã¢â€Å"Ã¢â€â‚¬Ã¢â€â‚¬ test_bootstrap.py
Ã¢â€â€š   Ã¢â€Å"Ã¢â€â‚¬Ã¢â€â‚¬ test_triage.py
Ã¢â€â€š   Ã¢â€Å"Ã¢â€â‚¬Ã¢â€â‚¬ test_config_validation.py
Ã¢â€â€š   Ã¢â€Å"Ã¢â€â‚¬Ã¢â€â‚¬ test_snippet_cleaning.py
Ã¢â€â€š   Ã¢â€â€Ã¢â€â‚¬Ã¢â€â‚¬ fixtures/
Ã¢â€â€š       Ã¢â€â€Ã¢â€â‚¬Ã¢â€â‚¬ sample_emails.json
│   ├── test_sender_profiles.py
│   ├── test_delta_queries.py
Ã¢â€â€Ã¢â€â‚¬Ã¢â€â‚¬ scripts/
    Ã¢â€â€Ã¢â€â‚¬Ã¢â€â‚¬ setup_graph_app.md
```

│       └── sample_corrections.json
---

## 7. CLI Interface

```bash
python -m assistant validate-config          # Validate config file structure
python -m assistant bootstrap --days 90      # Bootstrap: scan existing mail, propose taxonomy
python -m assistant dry-run --days 90 --sample 20  # Test classification against existing mail
python -m assistant dry-run --days 30 --limit 50   # Limit total emails (for testing)
python -m assistant serve                    # Start triage engine + web UI
python -m assistant triage --once            # Run a single triage cycle
python -m assistant digest                   # Generate digest now
python -m assistant undo --last 5            # Undo recent actions
python -m assistant undo --since "2026-02-01"
python -m assistant stats --days 7           # Show stats
python -m assistant triage --once --dry-run    # Classify without creating suggestions
python -m assistant rules --audit              # Audit auto-rules: match counts, conflicts, stale rules
python -m assistant bootstrap --force          # Re-run bootstrap (skip confirmation prompts)
```

---

## 8. Build Phases

### Phase 1 Ã¢â‚¬â€ Foundation (MVP)

**Goal:** Bootstrap scanner + suggest-only triage + basic review UI

1. Docker setup with Python, dependencies, structured logging
2. Pydantic config schema and validation (`validate-config` command)
3. Microsoft Graph API authentication (device code flow)
4. User identity auto-detection from Graph API `/me`
5. Graph API client: list messages (Inbox + Sent Items), list folders, create folders, move messages, with retry logic and exponential backoff
6. Config loader (YAML + Pydantic, hot-reload with validation)
7. SQLite database setup with WAL mode, migrations (all tables including `agent_state`, `sender_profiles`, `llm_request_log`, indexes for conversation_id and sender_email)
8. Email snippet cleaning pipeline
9. **Bootstrap scanner (two-pass)**: scan -> batch analyze -> consolidate -> proposed config.yaml, with progress bars
10. **Dry-run classifier**: re-classify using finalized config, output report with `--sample` and `--limit`
11. **Auto-rules engine**: pattern matching for high-confidence routing
12. **Claude classifier**: tool-use-based classification with structured output, enriched with Graph metadata (importance, flag, isRead, conversationIndex)
13. Reply state detection (Sent Items query with caching)
14. **Thread inheritance**: inherit folder classification from prior messages in the same conversation thread, reducing Claude API calls by ~50-60% for thread replies
15. **Thread context fetching**: fetch last 3 messages in a conversation thread to provide Claude with context for accurate classification of short replies and forwarded chains
16. **Sender history lookups**: query local database for historical classification patterns per sender to provide Claude with strong priors
17. **Triage engine**: scheduled polling, classify new emails, store compound suggestions, error handling with retry
18. **Review UI**: dashboard, review queue with per-field approve/correct/reject, rule creation from corrections, failed classifications tab, Outlook deep links (OWA)
19. CLI commands: `validate-config`, `bootstrap`, `dry-run`, `serve`, `triage --once`
20. .gitignore and .env.example scaffolding
21. Bootstrap idempotency (re-run protection, --force flag)
22. Sender profile population during bootstrap
23. LLM request/response logging for debugging and prompt iteration
24. Structured logging with triage_cycle_id correlation IDs
25. Config schema versioning with migration support

### Phase 1.5 -- Native Microsoft 365 Integration

**Goal:** Establish lean plumbing for To Do tasks, category management, and immutable message IDs

1. Add `Tasks.ReadWrite` permission; upgrade `MailboxSettings.Read` to `ReadWrite`
2. Implement immutable message ID migration (`Prefer: IdType="ImmutableId"` header)
3. `graph_tasks.py` module: To Do task list discovery/creation, task CRUD with linkedResources
4. `graph_tasks.py` module: master category list management (read/create/delete)
5. Category bootstrap: ensure framework categories (priorities + action types) exist with correct colors; create taxonomy categories for each project/area in config; interactive cleanup of orphaned categories
6. `task_sync` SQLite table and `store.py` CRUD operations
7. Config schema update: integrations section with todo config
8. Triage engine hooks: apply compound categories (priority + action + taxonomy) to emails and create To Do tasks on suggestion approval
9. Chat tool extension: `add_project_or_area` also creates taxonomy category
10. Config hot-reload: create categories for newly added projects/areas
11. Tests: category bootstrap, task creation, immutable IDs, category application
12. Token cache migration: delete existing token cache to force re-auth with expanded scopes

> See `Reference/spec/10-native-task-integration.md` for full architecture details.

### Phase 2 -- Intelligence

**Goal:** Improve classification, add tracking, add digest

**Prerequisites:** Phase 1 and Phase 1.5 fully implemented and tested.

1. Delta queries with 5-minute polling interval (replaces timestamp polling, enables near-real-time)
2. Learning from corrections via classification preferences memory + category growth through learning + `manage_category` chat tool + `AVAILABLE CATEGORIES` in prompts
3. Suggestion queue management: auto-expire old pending suggestions, auto-approve high-confidence after delay
4. Sender affinity auto-rules: when sender history shows >90% classification to a single folder with 10+ emails, automatically propose an auto_rule to skip Claude entirely
5. Waiting-for tracker + To Do sync + email flags (builds on Phase 1.5 `graph_tasks.py`, adds bidirectional task sync and `followUpFlag` operations)
6. Daily digest generation + calendar awareness (builds on Phase 1.5, adds `Calendars.Read` for schedule-aware delivery)
7. Auto-rules hygiene: conflict detection, stale rule warnings, consolidation suggestions
8. Stats & accuracy dashboard page (`/stats`)
9. Confidence calibration (integrates into stats dashboard)
10. Sender management page (`/senders`)
11. Enhanced graceful degradation: auto-rules-only mode with backlog recovery and dashboard indicators

> See `guides/PHASE_2_INTELLIGENCE.md` for detailed Phase 2 implementation guide.

### Phase 3 Ã¢â‚¬â€ Autonomy

**Goal:** Graduate to autonomous mode for high-confidence actions

1. Autonomous mode toggle in config
2. Confidence-threshold-based auto-execution
3. "New project detected" suggestions
4. Auto-archive for completed project threads
5. Weekly review report (deeper analysis than daily digest)
6. Email notification option for digest delivery

### Phase 4 Ã¢â‚¬â€ Advanced (Future)

1. Outlook desktop deep links (in addition to OWA)
2. Natural language query: "What's the status of the Tradecore project?"
3. Smart follow-up drafting: "Draft a follow-up to the SOC 2 evidence request"
4. Multi-account support
5. Team deployment (shared taxonomy, per-user config)

---

## 9. .gitignore

```
# Secrets
.env
data/

# User config (template is committed as config.yaml.example)
config/config.yaml
config/config.yaml.proposed

# Python
__pycache__/
*.pyc
*.pyo
.pytest_cache/
*.egg-info/
dist/
build/
.venv/

# IDE
.vscode/
.idea/
*.swp
*.swo

# OS
.DS_Store
Thumbs.db
```
