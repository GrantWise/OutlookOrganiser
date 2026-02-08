# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

AI-powered email management agent for Microsoft Outlook. Connects via Microsoft Graph API, uses Claude as its intelligence layer, organizes email using a hybrid PARA + GTD + Eisenhower methodology. Python 3.12+ service with SQLite database and FastAPI web UI.

## Environment

The project uses a local `.venv` virtual environment. **Always activate the venv** before running commands:

```bash
source .venv/bin/activate                        # Activate venv (must do first)
```

All commands below assume the venv is activated. Do NOT use `uv run` prefix — run commands directly within the activated venv.

## Build and Run Commands

```bash
pip install -e ".[dev]"                          # Install dependencies (including dev)
python -m assistant validate-config              # Validate config file
python -m assistant bootstrap --days 90          # Scan existing mail, propose taxonomy
python -m assistant dry-run --days 90 --sample 20  # Test classification (no changes)
python -m assistant serve                        # Start triage engine + web UI
python -m assistant triage --once                # Run single triage cycle
```

### Testing

```bash
pytest                                           # Run all tests
pytest tests/test_classifier.py                  # Run specific test file
pytest tests/test_classifier.py::TestAutoRules::test_sender_match  # Single test
pytest --cov=src/assistant                       # With coverage
```

### Linting

```bash
ruff check src/ tests/                           # Lint
ruff format src/ tests/                          # Format
```

## Architecture

### Data Flow

1. Graph API fetch → email ETL pipeline
2. Check auto-rules → if match, apply directly (no Claude call)
3. Check thread inheritance → if prior classification exists in same conversation, inherit folder
4. Otherwise → Claude classification via tool use
5. Store suggestion → user reviews via web UI
6. On approval → execute via Graph API (move/flag/categorize)

### Model Tiering

| Task | Model | Response Format |
|------|-------|-----------------|
| Bootstrap | Sonnet 4.5 | YAML (two-pass: batch analyze then consolidate) |
| Triage | Haiku 4.5 | Tool use (forced `tool_choice` for structured output) |
| Digest | Haiku 4.5 | Tool use |

### Dependency Initialization

`cli.py` uses a frozen `CLIDeps` dataclass to centralize all dependency creation. Each CLI command calls `_init_cli_deps()` which handles auth, Graph client, DB store, and classifier initialization with proper error handling. The web layer (`web/app.py`) uses FastAPI's lifespan context manager for the same initialization, storing deps on `app.state`.

### Config System

`config.py` implements a thread-safe singleton with hot-reload. `get_config()` returns the cached config; `reload_config_if_changed()` checks file mtime and reloads if changed (called each triage cycle). On reload failure, the old config is kept and a warning is logged.

### Triage Engine + Web Server Bridge

APScheduler runs in a background thread while FastAPI runs the async event loop. The scheduler bridges to async via `asyncio.run_coroutine_threadsafe()`. After 3 consecutive cycles of 100% Claude failure, the engine degrades to auto-rules-only mode.

### Thread Inheritance

`engine/thread_utils.py` - `ThreadContextManager` checks if prior messages in the same `conversation_id` already have a classification. If so, inherits the folder without calling Claude. Reduces API calls ~50-60%.

## Code Conventions

### Python Version & Type Hints

- Target Python 3.12+. Use PEP 695 type parameters: `def func[F: Callable](x: F) -> F`
- Import `Generator` from `collections.abc`, not `typing`
- Use `TYPE_CHECKING` for forward reference imports

### Async Patterns

Everything downstream of the CLI/web entry points is async: database (aiosqlite), classifier, triage engine. Tests use `asyncio_mode = "auto"` (no `@pytest.mark.asyncio` needed).

### Frozen Dataclasses for Results

Immutable results use `@dataclass(frozen=True)`: `ClassificationResult`, `CLIDeps`, `ThreadMessage`, `AutoRuleMatch`, `CleanedSnippet`. This prevents accidental mutation after creation.

### Exception Hierarchy

`core/errors.py` defines specific exceptions inheriting from `AssistantError`:
- `ConfigLoadError`, `ConfigValidationError` - config issues
- `AuthenticationError` - MSAL failures
- `GraphAPIError` (has `status_code`, `error_code`) - Graph API failures
- `RateLimitExceeded` - token bucket wait would exceed 20s
- `ConflictError` - 412 Precondition Failed (ETag mismatch)

Always catch specific exceptions, add `from e` or `from None` when re-raising.

### Structured Logging

Uses structlog with JSON output. `core/logging.py` provides `get_logger(__name__)`. Each triage cycle sets a correlation ID via `set_correlation_id()` so all logs within a cycle are traceable.

### Regex Safety

**CRITICAL**: Never use `re` module. Always use `regex` library with `timeout=1.0` to prevent ReDoS:
```python
import regex
result = regex.search(pattern, text, timeout=1.0)
```

### Graph API Patterns

- Token bucket rate limiter at 10 req/sec (`core/rate_limiter.py`)
- Optimistic concurrency with ETags: fetch with ETag, update with `If-Match` header
- Handle 412 Precondition Failed with retry loop (MAX_CONFLICT_RETRIES = 3)
- Idempotency checks before mutations (e.g., check if message already in destination folder)

### Auto-Rules

Auto-rules use `fnmatch` (glob-style) for sender matching and substring search for subjects. No regex in auto-rules - keeps them simple and safe.

### Security

- Server binds to `127.0.0.1` only
- Token cache file permissions: mode 600
- No full email bodies stored, only cleaned snippets (first 1000 chars)
- Pydantic validators prevent path traversal in config paths

### Ruff Configuration

Line length 100, target py312. Enabled rule sets: E, W, F, I, B, C4, UP. `E501` ignored (formatter handles line length). FastAPI files (`web/routes.py`, `web/dependencies.py`) suppress B008 for `Depends()` defaults.

## Code Quality Standards

Follow the Toyota Five Pillars (see `PRINCIPLES.md`) and Unix Philosophy. Key points:
- **YAGNI**: Implement only what's currently needed
- **Fail fast**: Specific errors with actionable messages (What failed? Where? Why? How to fix?)
- **Observability**: Structured logging with context on every significant operation
- **Proven patterns**: Standard Python/framework idioms, no custom architectures

Full coding standards and review checklist: `CODING_STANDARDS.md`

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
| `Reference/spec/08-classification-chat.md` | Chat assistant for reclassification, config editing, rule creation |
| `Reference/spec/09-architecture-decisions.md` | Infrastructure decisions: Docker-local vs Azure, polling vs webhooks |
| `Reference/spec/10-native-task-integration.md` | Native M365 integration: To Do tasks, categories, immutable IDs |
| `guides/PHASE_2_INTELLIGENCE.md` | Detailed Phase 2 implementation guide with code examples |
| `PRINCIPLES.md` | Toyota + Unix philosophy deep dive |
| `CODING_STANDARDS.md` | Detailed coding standards and review checklist |
| `IMPLEMENTATION_PLAN.md` | Build phases and progress tracker |
