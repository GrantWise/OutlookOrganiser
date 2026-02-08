# Outlook AI Assistant -- Setup Guide

> **Parent doc:** `01-overview.md` | **Read when:** Setting up Azure AD, configuring Docker, managing dependencies, or troubleshooting authentication.

---

## 1. Azure AD App Registration

This guide walks through registering an Azure AD (now Microsoft Entra ID) application to allow the Outlook AI Assistant to access your mailbox via the Microsoft Graph API.

### 1.1 Prerequisites

- A Microsoft 365 account (Business, Enterprise, or personal Outlook.com)
- Access to the Azure Portal (https://portal.azure.com) -- a free Azure account is sufficient; no paid Azure subscription is required for app registration
- Admin consent may be required if your organization restricts app registrations (check with your IT admin)

### 1.2 Register the Application

1. Navigate to **https://portal.azure.com**
2. In the left sidebar, select **Microsoft Entra ID** (formerly Azure Active Directory)
   - If you don't see it, search for "Microsoft Entra ID" in the top search bar
3. Select **App registrations** from the left menu
4. Click **+ New registration** at the top

Fill in the registration form:

| Field | Value |
|-------|-------|
| Name | `Outlook AI Assistant` |
| Supported account types | Select **Accounts in this organizational directory only** for work/school accounts, OR **Accounts in any organizational directory and personal Microsoft accounts** if using a personal Outlook.com account |
| Redirect URI | Leave blank (not needed for device code flow) |

5. Click **Register**

### 1.3 Record Application Identifiers

After registration, record these two values for `config.yaml`:

| Value | Where to find it | Example |
|-------|-------------------|---------|
| **Application (client) ID** | Overview page, top section | `a1b2c3d4-e5f6-7890-abcd-ef1234567890` |
| **Directory (tenant) ID** | Overview page, top section | `f9e8d7c6-b5a4-3210-fedc-ba0987654321` |

For personal Microsoft accounts, use `common` as the tenant ID instead of the directory ID.

### 1.4 Configure Authentication

1. In the left menu, select **Authentication**
2. Scroll down to **Advanced settings**
3. Set **Allow public client flows** to **Yes** (enables device code flow)
4. Click **Save**

### 1.5 Configure API Permissions

1. In the left menu, select **API permissions**
2. Click **+ Add a permission** -> **Microsoft Graph** -> **Delegated permissions**
3. Add each permission:

| Permission | Purpose | Required for |
|------------|---------|-------------|
| `Mail.ReadWrite` | Read emails and move between folders | All phases -- core functionality |
| `Mail.Send` | Send emails on behalf of the user | Phase 2+ -- digest delivery via email |
| `MailboxSettings.ReadWrite` | Read/write user timezone, mailbox config, and manage master categories | All phases -- upgraded from Read in Phase 1.5 |
| `User.Read` | Read basic user profile (name, email) | All phases -- identity auto-detection |
| `Tasks.ReadWrite` | Create and manage To Do tasks linked to emails | Phase 1.5+ -- task tracking and follow-ups |
| `Calendars.Read` | Read calendar events and free/busy schedule | Phase 2+ -- schedule-aware features |

4. Click **Add permissions**
5. If you see "Grant admin consent for [org]", click it and confirm
   - For personal accounts, consent is granted automatically on first login

### 1.6 Verify Permission Status

After adding permissions, the API permissions page should show:

```
Microsoft Graph (5)                              <-- 6 after Phase 2 adds Calendars.Read
├── Mail.ReadWrite             Delegated    ✅ Granted
├── Mail.Send                  Delegated    ✅ Granted
├── MailboxSettings.ReadWrite  Delegated    ✅ Granted
├── User.Read                  Delegated    ✅ Granted
└── Tasks.ReadWrite            Delegated    ✅ Granted
```

### 1.7 Add Credentials to Config

```yaml
auth:
  client_id: "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
  tenant_id: "f9e8d7c6-b5a4-3210-fedc-ba0987654321"   # or "common" for personal accounts
  scopes:
    - "Mail.ReadWrite"
    - "Mail.Send"
    - "MailboxSettings.ReadWrite"
    - "User.Read"
    - "Tasks.ReadWrite"
    # Calendars.Read added in Phase 2
  token_cache_path: "/app/data/token_cache.json"
```

---

## 2. First-Time Authentication Flow

When you run the agent for the first time (e.g., `python -m assistant bootstrap`), it will:

1. Detect that no cached token exists
2. Initiate the device code flow via MSAL
3. Print a message to the console:

```
╔══════════════════════════════════════════════════════════════╗
║  To authenticate, open a browser and go to:                  ║
║  https://microsoft.com/devicelogin                           ║
║                                                              ║
║  Enter this code: ABCD-EFGH                                  ║
║                                                              ║
║  Waiting for authentication...                               ║
╚══════════════════════════════════════════════════════════════╝
```

4. Open the URL in any browser (can be on a different device)
5. Enter the code, sign in, accept permissions
6. The agent detects successful authentication and continues
7. Tokens are cached to `data/token_cache.json`
8. User email is auto-detected from `/me` and stored in `agent_state`

---

## 2.1 Upgrading from Phase 1 (Re-authentication)

If the assistant was previously running with Phase 1 permissions (4 scopes), upgrading to Phase 1.5 requires re-authentication to consent to the new and upgraded permissions (`Tasks.ReadWrite` and `MailboxSettings.ReadWrite` replacing `MailboxSettings.Read`):

1. Stop the assistant
2. Delete the cached token: `rm data/token_cache.json`
3. Update `config.yaml` to add the new scopes and `integrations` section
4. Start the assistant -- it will initiate a new device code flow
5. Authenticate and consent to the expanded permissions
6. The assistant will run the category bootstrap (create framework + taxonomy categories, interactive cleanup)
7. The assistant will detect `immutable_ids_migrated` is not set and run the one-time ID migration

---

## 3. Token Lifecycle

| Token | Lifetime | Handling |
|-------|----------|---------|
| Access token | ~60-90 minutes | MSAL refreshes automatically before expiry |
| Refresh token | 90 days (rolling) | Renewed each time it's used; if unused for 90 days, re-authentication required |
| Token cache | Persistent on disk | Stored in Docker volume at `data/token_cache.json` |

---

## 4. MSAL Implementation Reference

```python
# src/assistant/auth/graph_auth.py

import msal
from pathlib import Path

class GraphAuth:
    """Handles Microsoft Graph API authentication via MSAL device code flow."""

    def __init__(self, client_id: str, tenant_id: str, scopes: list[str],
                 token_cache_path: str):
        self.client_id = client_id
        self.tenant_id = tenant_id
        self.scopes = scopes
        self.token_cache_path = Path(token_cache_path)
        self.cache = msal.SerializableTokenCache()
        self._load_cache()

        self.app = msal.PublicClientApplication(
            client_id=self.client_id,
            authority=f"https://login.microsoftonline.com/{self.tenant_id}",
            token_cache=self.cache,
        )

    def get_access_token(self) -> str:
        """Get a valid access token, refreshing or re-authenticating as needed."""
        # 1. Try to get token silently (from cache / refresh)
        accounts = self.app.get_accounts()
        if accounts:
            result = self.app.acquire_token_silent(
                scopes=self.scopes, account=accounts[0]
            )
            if result and "access_token" in result:
                self._save_cache()
                return result["access_token"]

        # 2. Fall back to device code flow
        flow = self.app.initiate_device_flow(scopes=self.scopes)
        if "user_code" not in flow:
            raise Exception(f"Device flow initiation failed: {flow}")

        print(f"\nTo authenticate, visit: {flow['verification_uri']}")
        print(f"Enter code: {flow['user_code']}\n")
        print("Waiting for authentication...")

        result = self.app.acquire_token_by_device_flow(flow)
        if "access_token" not in result:
            raise Exception(f"Authentication failed: {result.get('error_description')}")

        self._save_cache()
        return result["access_token"]

    def _load_cache(self):
        if self.token_cache_path.exists():
            self.cache.deserialize(self.token_cache_path.read_text())

    def _save_cache(self):
        if self.cache.has_state_changed:
            self.token_cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.token_cache_path.write_text(self.cache.serialize())
```

---

## 5. Troubleshooting

| Problem | Cause | Solution |
|---------|-------|----------|
| `AADSTS7000218: request body must contain 'client_assertion' or 'client_secret'` | "Allow public client flows" not enabled | Go to Authentication -> Advanced settings -> set to Yes |
| `AADSTS65001: user or admin has not consented` | Permissions not granted | Ask admin to grant consent, or use admin account to click "Grant admin consent" |
| `AADSTS50076: configuration change by administrator` | Conditional access policy blocking device code | Contact IT admin; may need to allowlist the app |
| `AADSTS700016: Application not found` | Wrong client_id or tenant | Verify both in config.yaml match Azure portal |
| Token cache errors after Docker rebuild | Volume not mounted | Verify `./data:/app/data` in docker-compose.yaml |
| Agent re-prompts for login frequently | Refresh token expiring | Ensure agent runs at least once every 90 days |

---

## 6. Security Considerations

- **Token cache file** (`token_cache.json`) contains sensitive refresh tokens. The Docker volume should have restricted host permissions (`chmod 700 ./data`).
- **Client ID is not a secret** for public client apps (device code flow doesn't use a client secret). Safe in config files but should not be committed to public repositories.
- **Scope minimization**: The agent only requests the minimum permissions needed. `Mail.Send` can be removed from scopes if email digest delivery is not needed.
- **Token encryption at rest**: For production use, encrypt the token cache using the `cryptography` package with a key derived from a user-provided passphrase or machine-specific entropy. Implementation deferred to Phase 2.
- **Revocation**: To revoke the agent's access at any time, go to https://myapps.microsoft.com -> click on "Outlook AI Assistant" -> click "Revoke". Alternatively, delete the app registration in Azure Portal.

---

## 7. Docker Configuration

### 7.1 Dockerfile

```dockerfile
FROM python:3.12-slim

WORKDIR /app

# Install system dependencies (gcc for C extensions, curl for uv installer)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc curl \
    && rm -rf /var/lib/apt/lists/*

# Install uv (fast Python package manager)
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Install Python dependencies (uv resolves from pyproject.toml + uv.lock)
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Copy application code
COPY src/ ./src/

# Default command: start the triage engine + web UI
CMD ["uv", "run", "python", "-m", "assistant", "serve"]

# Expose the review UI port
EXPOSE 8080
```

### 7.2 docker-compose.yaml

```yaml
version: "3.8"
services:
  outlook-assistant:
    build: .
    container_name: outlook-assistant
    ports:
      - "8080:8080"
    volumes:
      - ./config:/app/config              # Config files (writable for bootstrap + config editor)
      - ./data:/app/data                  # SQLite DB, token cache, logs
    environment:
      - ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}
      - PYTHONPATH=/app/src
      - TZ=${TZ:-America/New_York}
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "uv", "run", "python", "-c", "import requests; requests.get('http://localhost:8080/health')"]
      interval: 60s
      timeout: 10s
      retries: 3

  # Optional: run bootstrap as a one-off command
  # Usage: docker compose run --rm bootstrap --days 90
  bootstrap:
    build: .
    profiles: ["tools"]
    volumes:
      - ./config:/app/config
      - ./data:/app/data
    environment:
      - ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}
      - PYTHONPATH=/app/src
    entrypoint: ["uv", "run", "python", "-m", "assistant", "bootstrap"]
```

**Note:** The config volume is intentionally writable because the bootstrap scanner writes `config.yaml.proposed` to it and the web-based config editor saves changes to it. Application-level safeguards (atomic writes, backup-before-save) protect against corruption.

### 7.3 .env.example

```bash
# .env -- Environment variables for docker-compose
# Copy this to .env and fill in your values
ANTHROPIC_API_KEY=sk-ant-...your-key-here...
TZ=America/New_York
```

### 7.4 .dockerignore

```
.git
.env
.venv
data/
__pycache__
*.pyc
.pytest_cache
.vscode
.idea
```

---

## 8. Python Dependencies

### Package Management: uv

The project uses **[uv](https://docs.astral.sh/uv/)** for dependency management -- a fast, Rust-based Python package manager that replaces pip, pip-tools, and virtualenv. Benefits over a plain `requirements.txt`:

- **Lockfile (`uv.lock`)**: Deterministic, reproducible installs across all environments. The lockfile pins exact versions of every dependency (including transitive ones) so builds are byte-for-byte identical.
- **Automatic dependency resolution**: No need to manually track compatible version ranges -- uv resolves them from the constraints in `pyproject.toml`.
- **Fast**: 10-100x faster than pip for installs and resolution.
- **Standard `pyproject.toml`**: Uses the PEP 621 standard, compatible with any Python tooling.

**Developer workflow:**

```bash
# Install uv (if not already installed)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Install all dependencies (creates .venv automatically)
uv sync

# Add a new dependency
uv add some-package

# Update all dependencies to latest compatible versions
uv lock --upgrade

# Update a specific dependency
uv lock --upgrade-package anthropic

# Run commands in the project's virtual environment
uv run python -m assistant serve
```

### pyproject.toml

```toml
[project]
name = "outlook-ai-assistant"
version = "0.1.0"
description = "AI-powered email management agent for Outlook"
requires-python = ">=3.12"

dependencies = [
    "anthropic>=0.78.0",           # Claude API client (tool use, structured output)
    "msal>=1.34.0",                # Microsoft authentication (device code flow)
    "requests>=2.32.0",            # HTTP client for Graph API
    "fastapi>=0.128.0",            # Web framework for review UI
    "uvicorn>=0.40.0",             # ASGI server
    "jinja2>=3.1.5",               # HTML templates
    "pyyaml>=6.0.2",               # Config file parsing
    "pydantic>=2.10.0",            # Config schema validation
    "apscheduler>=3.11.0,<4.0.0",  # Job scheduling (3.x stable API; 4.x is a breaking rewrite)
    "click>=8.3.0",                # CLI framework
    "rich>=14.0.0",                # Console output formatting (progress bars, tables)
    "structlog>=25.1.0",           # Structured JSON logging
    "python-dateutil>=2.9.0",      # Date parsing
    "aiosqlite>=0.22.0",           # Async SQLite
    "cryptography>=46.0.0",        # Token encryption at rest (Phase 2)
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0.0",
    "pytest-asyncio>=0.25.0",
    "httpx>=0.28.0",               # For FastAPI test client
    "ruff>=0.9.0",                 # Linting and formatting
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.backends"
```

**Version strategy:** Minimum versions are set to recent stable releases as of February 2026. The `uv.lock` file (committed to the repo) pins exact versions for reproducibility. Running `uv lock --upgrade` updates to the latest compatible versions at any time.

**Notable version choices:**
- `apscheduler>=3.11.0,<4.0.0`: APScheduler 4.x is a full rewrite with breaking API changes (different scheduler classes, removed sync interfaces). We pin to 3.x for stability.
- `anthropic>=0.78.0`: The SDK moves fast -- pin to a recent version that supports tool use and the latest model strings.
- `fastapi>=0.128.0`: Recent version with Pydantic v2 as minimum, dropped Pydantic v1 support.

### .dockerignore (updated)

```
.git
.env
.venv
data/
__pycache__
*.pyc
.pytest_cache
.vscode
.idea
uv.lock
```

**Note:** `uv.lock` is listed in `.dockerignore` only if you don't want lockfile-based installs in Docker. Since our Dockerfile uses `--frozen` (lockfile required), **remove `uv.lock` from `.dockerignore`** and keep it committed.
