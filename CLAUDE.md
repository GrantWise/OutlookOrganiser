# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

An AI-powered email management agent that connects to Microsoft Outlook via the Microsoft Graph API, uses Claude as its intelligence layer, and organizes email according to a hybrid PARA + GTD + Eisenhower methodology. Runs as a Docker-containerized Python 3.12+ service.

## Build and Run Commands

```bash
# Install uv (package manager)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Install dependencies
uv sync

# Validate config file
uv run python -m assistant validate-config

# Bootstrap: scan existing mail, propose taxonomy
uv run python -m assistant bootstrap --days 90

# Test classification against existing mail (no changes)
uv run python -m assistant dry-run --days 90 --sample 20

# Start triage engine + web UI
uv run python -m assistant serve

# Run single triage cycle
uv run python -m assistant triage --once

# Generate digest
uv run python -m assistant digest

# Undo recent actions
uv run python -m assistant undo --last 5

# Audit auto-rules
uv run python -m assistant rules --audit
```

### Docker

```bash
# Start the service
docker compose up -d

# Run bootstrap as one-off
docker compose run --rm bootstrap --days 90

# View logs
docker logs outlook-assistant
```

### Testing

```bash
# Install dev dependencies
uv sync --dev

# Run all tests
uv run pytest

# Run specific test file
uv run pytest tests/test_classifier.py

# Run with coverage
uv run pytest --cov=src/assistant
```

### Linting

```bash
uv run ruff check src/ tests/
uv run ruff format src/ tests/
```

## Architecture

```
outlook-ai-assistant/
├── src/assistant/
│   ├── __main__.py         # CLI entry point
│   ├── cli.py              # Click CLI commands
│   ├── config.py           # Config loader + hot-reload
│   ├── config_schema.py    # Pydantic models for config.yaml
│   ├── auth/               # MSAL OAuth2 device code flow
│   ├── graph/              # Microsoft Graph API client
│   ├── classifier/         # Claude classification + auto-rules
│   ├── engine/             # Bootstrap, triage, digest engines
│   ├── db/                 # SQLite schema + operations
│   └── web/                # FastAPI review UI
├── config/config.yaml      # User config (gitignored)
├── data/                   # SQLite DB + token cache (gitignored)
└── tests/
```

### Key Components

- **Bootstrap Scanner**: Two-pass analysis (Sonnet) - batch analyze emails, then consolidate into proposed taxonomy
- **Triage Engine**: Scheduled polling (APScheduler), classifies new emails via Claude tool use (Haiku), stores suggestions
- **Auto-Rules**: Pattern matching for high-confidence routing, skips Claude API
- **Thread Inheritance**: Inherits folder from prior messages in same conversation, reduces API calls ~50-60%
- **Review UI**: FastAPI + Jinja2 on localhost:8080

### Model Tiering

| Task | Model | Rationale |
|------|-------|-----------|
| Bootstrap | Sonnet 4.5 | Complex pattern discovery, runs infrequently |
| Triage | Haiku 4.5 | High-volume, repetitive classification |
| Digest | Haiku 4.5 | Summarizing structured data |

### Data Flow

1. Graph API fetch → Extract/Transform/Load pipeline
2. Check auto-rules → if match, apply directly
3. Check thread inheritance → if prior classification exists, inherit folder
4. Otherwise → Claude classification via tool use
5. Store suggestion → user reviews via web UI
6. On approval → execute via Graph API

## Code Quality Standards

### Toyota Principle - Five Pillars (ALL must be satisfied)

1. **Not Over-Engineered**: Implement only what's currently needed (YAGNI). Simplest solution wins.
2. **Sophisticated Where Needed**: Add complexity ONLY for reliability, security, or performance.
3. **Robust Error Handling**: Fail fast with specific errors and actionable messages.
4. **Complete Observability**: Structured logging with context (structlog JSON).
5. **Proven Patterns**: Follow standard Python/framework patterns.

### Unix Philosophy

- **Representation**: Store knowledge in data/config, not hardcoded logic
- **Least Surprise**: Methods do what names suggest, consistent naming
- **Modularity**: Single responsibility per class/module
- **Separation**: Business rules in config, algorithms in code

### Error Messages

Every error message should answer: What failed? Where? Why? How to fix? Where to learn more?

```python
raise ValueError(
    f"Invalid temperature {temp}°C in sensor_id={sensor_id}. "
    f"Must be between -40°C and 85°C. "
    f"Check sensor calibration. Docs: https://..."
)
```

### Critical Security Requirements

- **ALL regex patterns MUST have timeouts** (use `regex` library with timeout, not `re`)
- Validate and sanitize all file paths to prevent traversal attacks
- Never store full email bodies, only cleaned snippets
- Token cache file needs restricted permissions

## Documentation Index

| Document | Contents |
|----------|----------|
| `Reference/spec/01-overview.md` | Vision, architecture, tech stack, build phases |
| `Reference/spec/02-config-and-schema.md` | SQLite schema, config.yaml structure, Pydantic validation |
| `Reference/spec/03-agent-behaviors.md` | Bootstrap, triage engine, review UI, digest, thread inheritance |
| `Reference/spec/04-prompts.md` | Claude prompt templates + tool definitions |
| `Reference/spec/05-graph-api.md` | Graph API endpoints, pagination, rate limits, delta queries |
| `Reference/spec/06-safety-and-testing.md` | Autonomy boundaries, data privacy, testing strategy |
| `Reference/spec/07-setup-guide.md` | Azure AD registration, Docker, dependencies |
| `Reference/working-examples/` | Working Graph API client, rate limiter, config from existing project |
| `PRINCIPLES.md` | Toyota + Unix philosophy deep dive |
| `CODING_STANDARDS.md` | Detailed coding standards and review checklist |
