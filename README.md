# Outlook AI Assistant

AI-powered email management for Microsoft Outlook. Connects via the Microsoft Graph API, uses Claude as its intelligence layer, and organizes email according to a hybrid **PARA + GTD + Eisenhower** methodology.

The assistant analyzes your mailbox to discover projects and recurring patterns, then continuously classifies incoming email into an organized folder taxonomy with priority levels and action categories. You review suggestions through a local web UI before changes are applied.

## How It Works

```
Bootstrap (one-time)     Triage (continuous)           Review (web UI)
┌──────────────────┐     ┌──────────────────────┐     ┌─────────────────┐
│ Scan 90 days of  │     │ Poll for new mail     │     │ Approve / Edit  │
│ email with Claude│────>│ every 15 min          │────>│ / Reject each   │
│ Sonnet to discover│     │                      │     │ suggestion      │
│ projects & areas │     │ Auto-rules → Thread   │     │                 │
│                  │     │ inheritance → Claude  │     │ Corrections     │
│ Output: proposed │     │ Haiku classification  │     │ improve future  │
│ config.yaml      │     │                      │     │ accuracy        │
└──────────────────┘     └──────────────────────┘     └─────────────────┘
```

**Classification pipeline** (in priority order):
1. **Auto-rules** - Pattern match on sender/subject, skip Claude entirely
2. **Thread inheritance** - Reuse classification from prior messages in the same conversation (~50-60% of emails)
3. **Claude classification** - Haiku via tool use for structured output (folder, priority, action, confidence)

## Getting Started

**New to Azure AD?** Follow the step-by-step [Setup Guides](guides/README.md) -- they walk you through everything from app registration to daily usage, no prior experience required.

If you are already familiar with Azure AD app registration, the quick start below has everything you need.

## Prerequisites

- **Python 3.12+** (or Docker)
- **Anthropic API key** from [console.anthropic.com](https://console.anthropic.com/)
- **Microsoft 365 account** (Business, Enterprise, or personal Outlook.com)
- **Azure AD app registration** with delegated permissions: `Mail.ReadWrite`, `Mail.Send`, `MailboxSettings.Read`, `User.Read` (see [detailed walkthrough](guides/01-azure-setup.md) or [technical reference](Reference/spec/07-setup-guide.md))

## Quick Start

### With Docker (recommended)

```bash
# 1. Clone and configure
git clone <repo-url> && cd OutlookOrganiser
cp .env.example .env          # Add your ANTHROPIC_API_KEY
cp config/config.yaml.example config/config.yaml
# Edit config/config.yaml: set auth.client_id and auth.tenant_id

# 2. Validate configuration
docker compose run --rm bootstrap validate-config

# 3. Bootstrap - analyze your mailbox and generate taxonomy
docker compose run --rm bootstrap --days 90
# Follow the device code login prompt in your browser

# 4. Review the proposed config, then activate it
# Edit config/config.yaml.proposed as needed, then:
mv config/config.yaml.proposed config/config.yaml

# 5. Optional: preview classifications without making changes
docker compose run --rm dry-run --days 30 --sample 20

# 6. Start the service
docker compose up -d
# Web UI at http://localhost:8080
```

### Without Docker

```bash
# Install uv package manager
curl -LsSf https://astral.sh/uv/install.sh | sh

# Install dependencies
uv sync

# Configure (same as above - .env + config.yaml)
cp .env.example .env
cp config/config.yaml.example config/config.yaml

# Run commands with uv
uv run python -m assistant validate-config
uv run python -m assistant bootstrap --days 90
uv run python -m assistant dry-run --days 30 --sample 20
uv run python -m assistant serve
```

## Usage

### CLI Commands

| Command | Purpose | Key Flags |
|---------|---------|-----------|
| `validate-config` | Check config.yaml syntax and schema | `-c PATH` custom config path |
| `bootstrap` | Analyze mailbox, generate taxonomy proposal | `--days N` (default 90), `--force` |
| `dry-run` | Preview classifications without saving | `--days N`, `--sample N` (default 20), `--limit N` |
| `serve` | Start triage engine + web UI | `--host`, `--port` (default 8000) |
| `triage` | Run triage without web UI | `--once` single cycle, `--dry-run` |

All commands support `--debug` for verbose logging.

### Intended Workflow

1. **Bootstrap** your mailbox to auto-discover projects, areas, and sender patterns
2. **Review** the generated `config.yaml.proposed` - edit projects/areas/rules as needed
3. **Dry-run** to verify classification quality before going live
4. **Serve** for continuous operation - triage runs every 15 minutes (configurable)
5. **Review suggestions** on the web UI - approve, correct, or reject classifications
6. Corrections feed back into auto-rules, improving future accuracy

### Web UI

Available at `http://localhost:8080` when running `serve`:

- **Dashboard** - Pending suggestions, aging items, daily stats, system health
- **Review Queue** - Approve/correct/reject email classifications
- **Waiting For** - Track items awaiting external responses
- **Config Editor** - Edit config.yaml with validation
- **Activity Log** - All agent actions
- **Stats** - Classification accuracy, confidence calibration, cost tracking

## Configuration

The main config file is `config/config.yaml`. Key sections:

```yaml
auth:
  client_id: "your-azure-app-id"
  tenant_id: "your-tenant-id"       # or "common" for personal accounts

triage:
  interval_minutes: 15               # Polling frequency
  batch_size: 20                     # Emails per cycle
  mode: "suggest"                    # "suggest" = human review required

models:                              # Claude model per task (configurable)
  bootstrap: "claude-sonnet-4-5-20250929"
  triage: "claude-haiku-4-5-20251001"

projects:                            # Active projects (populated by bootstrap)
  - name: "Project Name"
    folder: "Projects/Project Name"
    signals:
      senders: ["*@domain.com"]
      subjects: ["keyword"]

auto_rules:                          # High-confidence routing (skips Claude)
  - name: "GitHub Notifications"
    match:
      senders: ["notifications@github.com"]
    action:
      folder: "Reference/Dev Notifications"
      priority: "P4 - Low"
```

See [config.yaml.example](config/config.yaml.example) for the full schema with all options.

### Folder Taxonomy

Follows the PARA method:

| Folder | Purpose |
|--------|---------|
| `Projects/*` | Active work with defined outcomes |
| `Areas/*` | Ongoing responsibilities (no end date) |
| `Reference/*` | Useful information, no action needed |
| `Archive/*` | Completed projects |

### Priority Levels (Eisenhower Matrix)

| Priority | Meaning |
|----------|---------|
| P1 - Urgent Important | Act now (escalations, same-day deadlines) |
| P2 - Important | Schedule time (strategic work, key decisions) |
| P3 - Urgent Low | Batch/delegate (quick replies, routine requests) |
| P4 - Low | Archive/defer (FYI, informational) |

### Action Categories

`Needs Reply` | `Waiting For` | `Delegated` | `FYI Only` | `Scheduled` | `Review`

## Architecture

```
src/assistant/
├── cli.py              # Click CLI + CLIDeps initialization
├── config.py           # Thread-safe config singleton with hot-reload
├── config_schema.py    # Pydantic models for config.yaml
├── auth/               # MSAL OAuth2 device code flow
├── graph/              # Microsoft Graph API client, folder/message managers
├── classifier/         # Claude classification, auto-rules, snippet cleaning
├── engine/             # Bootstrap, triage, dry-run, thread inheritance
├── db/                 # SQLite (aiosqlite, WAL mode) models + store
├── web/                # FastAPI + Jinja2 review UI
└── core/               # Logging (structlog), rate limiter, error types
```

## Security & Privacy

- Web server binds to **localhost only** (127.0.0.1) - no authentication layer
- Token cache file has **restricted permissions** (mode 600)
- **No full email bodies stored** - only cleaned snippets (first 1000 characters)
- Email metadata + snippets are sent to Anthropic's API for classification
- All data stored locally in SQLite (`data/assistant.db`)
- Revoke access anytime at [myapps.microsoft.com](https://myapps.microsoft.com)

## Development

```bash
uv sync --dev                                    # Install dev dependencies
uv run pytest                                    # Run tests
uv run pytest tests/test_classifier.py           # Single file
uv run ruff check src/ tests/                    # Lint
uv run ruff format src/ tests/                   # Format
```

See [CLAUDE.md](CLAUDE.md) for detailed architecture notes and code conventions.
