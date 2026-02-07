# Troubleshooting Guide

> **Previous:** [Daily Usage](04-daily-usage.md)

This guide covers the most common problems you may encounter when setting up and running the Outlook AI Assistant.

## Azure AD Issues

### I cannot find Microsoft Entra ID in Azure Portal

Microsoft Entra ID may not be visible in the portal sidebar by default. Type **"Microsoft Entra ID"** in the search bar at the top of the Azure Portal page and select it from the results.

If you see "Azure Active Directory" instead, that is the same service under its old name. Click it -- it works the same way.

### I cannot register an app -- it says "You do not have permission"

Your organization's IT administrator has restricted who can register apps. You have two options:

1. **Ask your IT admin** to either register the app for you (following the steps in [Azure AD Setup](01-azure-setup.md)) or grant you the "Application Developer" role in Entra ID
2. **Use a personal Microsoft account** instead -- personal accounts can always register apps. Create one at [outlook.com](https://outlook.com) if needed.

### "AADSTS7000218: request body must contain client_assertion or client_secret"

**Cause:** "Allow public client flows" was not enabled during setup.

**Fix:**
1. Go to [Azure Portal](https://portal.azure.com) > Microsoft Entra ID > App registrations
2. Click your app (e.g., "Outlook AI Assistant")
3. Click **Authentication** in the left sidebar
4. Scroll down to **Advanced settings**
5. Set **Allow public client flows** to **Yes**
6. Click **Save**

### "AADSTS65001: user or admin has not consented to use the application"

**Cause:** The API permissions you added have not been granted consent.

**Fix (if you have admin access):**
1. Go to App registrations > your app > API permissions
2. Click **Grant admin consent for [your organization]**
3. Click **Yes** to confirm

**Fix (if you do not have admin access):**
Ask your IT administrator to grant admin consent for the app. Provide them with the Application (client) ID and the list of permissions (Mail.ReadWrite, Mail.Send, MailboxSettings.Read, User.Read).

**For personal accounts:** This error should not occur. Consent is granted automatically during sign-in. If you see it, try running bootstrap again -- the consent prompt may appear during the browser sign-in flow.

### "AADSTS700016: Application with identifier '...' was not found"

**Cause:** The `client_id` or `tenant_id` in your `config.yaml` does not match what is registered in Azure Portal.

**Fix:**
1. Go to App registrations > your app > Overview
2. Compare the **Application (client) ID** with the `client_id` in your `config/config.yaml`
3. Compare the **Directory (tenant) ID** with the `tenant_id` in your config
4. Make sure both match exactly (including hyphens)
5. For personal accounts, make sure `tenant_id` is set to `"common"`

### "AADSTS50076: Due to a configuration change by your administrator"

**Cause:** Your organization has a Conditional Access policy that blocks the device code flow (common in organizations with strict security policies).

**Fix:** Contact your IT administrator and ask them to either:
- Allowlist the app for device code flow
- Or create an exception in the Conditional Access policy for this app

## Installation Issues

### "docker: command not found" or "docker-compose: command not found"

Docker Desktop is not installed or not in your system PATH.

**Fix:**
1. Install Docker Desktop from [docker.com/get-docker](https://www.docker.com/get-docker)
2. Start Docker Desktop (look for the whale icon in your system tray/menu bar)
3. Open a **new** terminal window (existing terminals will not see the newly installed command)
4. Verify: `docker --version`

> **Note:** On Linux, Docker may need to be installed via your package manager. See [Docker's Linux install guide](https://docs.docker.com/engine/install/).

### "permission denied while trying to connect to the Docker daemon socket"

**Cause (Linux):** Your user is not in the `docker` group.

**Fix:**
```bash
sudo usermod -aG docker $USER
```
Then **log out and log back in** (or restart your computer). The group membership does not take effect until your session is refreshed.

### "uv: command not found"

uv is not installed or not in your PATH.

**Fix:**
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```
Then close and **reopen your terminal**. Verify with `uv --version`.

### Python version error: "requires >= 3.12"

Your Python version is too old.

**Check your version:**
```bash
python3 --version
```

If it shows a version below 3.12, either:
- **Use Docker instead** -- the Dockerfile includes Python 3.12
- **Install Python 3.12+** from [python.org](https://www.python.org/downloads/) or via your system package manager

### Config validation fails

The validation error message tells you which field is wrong. Common issues:

| Error | Cause | Fix |
|-------|-------|-----|
| `client_id: field required` or `client_id is empty` | The client_id field is blank | Paste your Application (client) ID from Azure Portal |
| `tenant_id: invalid format` | The tenant_id is not a valid UUID or "common" | Check for typos; it should look like `xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx` |
| `No such file or directory: 'config/config.yaml'` | Config file does not exist | Run `cp config/config.yaml.example config/config.yaml` |
| `YAML parsing error` | Invalid YAML syntax | Check for incorrect indentation, missing colons, or unquoted special characters |

## Authentication Issues

### Device code flow times out

The device code is valid for about 15 minutes. If it expires before you complete the sign-in:

1. The assistant will show an error
2. Simply run the command again -- you will get a new code
3. Complete the browser sign-in more quickly this time

**Common causes of timeout:**
- Opened the wrong browser URL
- Entered the code incorrectly (codes are case-sensitive)
- Signed in with a different Microsoft account than the one configured

### I keep getting prompted to authenticate every time

The token cache may not be persisting between runs.

**Docker:** Check that the `data/` volume is mounted. In `docker-compose.yaml`, you should see:
```yaml
volumes:
  - ./data:/app/data:rw
```
And the `data/` directory should exist and be writable.

**Local:** Check that the `data/` directory exists and is writable:
```bash
ls -la data/
```
You should see a `token_cache.json` file after the first successful authentication.

**Other cause:** If the assistant has not run for 90+ days, the refresh token expires and you need to re-authenticate. This is expected behavior.

### "403 Forbidden" errors when accessing emails

**Cause:** Authentication succeeded, but the API permissions were not properly consented.

**Fix:**
1. Go to Azure Portal > App registrations > your app > API permissions
2. Check that all four permissions have green checkmarks in the Status column
3. If they do not, click **Grant admin consent** (or ask your IT admin to do so)
4. Delete the cached token and re-authenticate:
   ```bash
   rm data/token_cache.json
   ```
   Then run any command (e.g., `bootstrap`) to trigger a fresh sign-in.

## Bootstrap Issues

### "No emails found" or bootstrap completes with 0 emails

**Possible causes:**

1. **Wrong account:** You authenticated with a Microsoft account that has no email. Verify by checking your email at [outlook.office.com](https://outlook.office.com) with the same account.

2. **Empty Inbox:** The assistant only scans the Inbox folder by default. If all your email is already organized into subfolders, there is nothing to scan. Try increasing the lookback period:
   ```bash
   docker compose run --rm bootstrap --days 180
   ```

3. **New account:** If the account is new and has very few emails, bootstrap may not find enough patterns. Add projects and areas manually in `config/config.yaml` instead.

### Bootstrap is very slow

Bootstrap speed depends on the number of emails:

| Emails | Expected Time |
|--------|--------------|
| ~500 | 1-2 minutes |
| ~1000 | 2-4 minutes |
| ~3000 | 5-10 minutes |
| ~5000+ | 10-20 minutes |

If it seems stuck, check that your network connection is stable. Bootstrap makes API calls to both Microsoft Graph (to fetch emails) and Anthropic (to analyze them).

To speed things up, reduce the scan window:
```bash
docker compose run --rm bootstrap --days 30
```

### "Anthropic API key error" or "AuthenticationError: Invalid API key"

**Fix:**
1. Check your `.env` file -- the key should start with `sk-ant-`
2. Make sure there are no extra spaces or quotes around the key
3. Verify the key is active at [console.anthropic.com](https://console.anthropic.com/) > API Keys
4. **Docker:** Make sure `.env` is in the project root directory (same folder as `docker-compose.yaml`)

## Web UI Issues

### Cannot access http://localhost:8080

**Is the service running?**

Docker:
```bash
docker ps
```
Look for a container named `outlook-assistant` with status "Up." If it is not listed, start it:
```bash
docker compose up -d
```

If the container is listed but shows "Restarting" or "Exited," check the logs:
```bash
docker logs outlook-assistant --tail 30
```

**Port conflict:** Another application may be using port 8080. Change the port in `docker-compose.yaml`:
```yaml
ports:
  - "8081:8080"    # Use 8081 instead
```
Then restart: `docker compose down && docker compose up -d`

For local installs, you can use the `--port` flag:
```bash
uv run python -m assistant serve --port 8081
```

### Dashboard shows "No cycles run yet"

The first triage cycle runs after `interval_minutes` (default: 15 minutes) from when the service started. Either:

- **Wait** for the interval to pass
- **Run a manual cycle** immediately:
  ```bash
  docker compose exec outlook-assistant uv run python -m assistant triage --once
  ```

### Review Queue is empty even though I have email

The triage engine only processes **new email that arrives after the service starts**. It does not retroactively classify email that was already in your Inbox before the service began.

To classify existing email, run bootstrap or dry-run to analyze historical email:
```bash
docker compose run --rm dry-run --days 30 --sample 50
```

## Getting More Help

### Enable Debug Logging

Add the `--debug` flag to any command for verbose output:

```bash
uv run python -m assistant --debug triage --once
```

For Docker, check logs:
```bash
docker logs outlook-assistant --tail 100
```

### Further Reading

- **Token lifecycle and MSAL internals:** [Reference/spec/07-setup-guide.md](../Reference/spec/07-setup-guide.md)
- **System behavior details:** [Reference/spec/03-agent-behaviors.md](../Reference/spec/03-agent-behaviors.md)
- **Architecture and code conventions:** [CLAUDE.md](../CLAUDE.md)

## Revoking Access

If you want to stop the assistant from accessing your email:

1. **Stop the service:**
   ```bash
   docker compose down
   ```

2. **Revoke the app's permissions** at [myapps.microsoft.com](https://myapps.microsoft.com):
   - Find "Outlook AI Assistant" in the list
   - Click it and select **Revoke permissions**

3. **Delete the token cache:**
   ```bash
   rm data/token_cache.json
   ```

4. **Optionally, delete the app registration** in Azure Portal:
   - Go to App registrations > your app > Overview
   - Click **Delete** at the top
