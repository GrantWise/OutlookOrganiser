# Step 2: Installation

> **Time required:** ~5 minutes
> **Previous:** [Azure AD Setup](01-azure-setup.md) | **Next:** [First Run](03-first-run.md)

## What This Step Does

You will download the project, enter the credentials from Step 1, and verify that the configuration is valid. By the end, the assistant is installed and configured but not yet running.

## Option A: Docker (Recommended)

Docker runs the assistant in an isolated container, so you do not need to worry about Python versions or system dependencies. This is the simplest path.

**Prerequisite:** Docker Desktop must be installed and running. If you type `docker --version` in your terminal and see a version number, you are ready. If not, install Docker from [docker.com/get-docker](https://www.docker.com/get-docker) and start it before continuing.

### A.1 Clone the Repository

Open your terminal and run:

```bash
git clone <repo-url>
cd OutlookOrganiser
```

After cloning, you should see files like `README.md`, `Dockerfile`, `docker-compose.yaml`, and directories like `config/`, `src/`, and `tests/`.

### A.2 Set Up Your API Key

The assistant needs an Anthropic API key to use Claude for email classification.

1. Copy the example environment file:
   ```bash
   cp .env.example .env
   ```

2. Open `.env` in a text editor. You will see:
   ```
   ANTHROPIC_API_KEY=sk-ant-...your-key-here...
   TZ=America/New_York
   ```

3. Replace `sk-ant-...your-key-here...` with your actual API key from [console.anthropic.com](https://console.anthropic.com/). If you do not have one yet:
   - Go to [console.anthropic.com](https://console.anthropic.com/)
   - Create an account or sign in
   - Go to API Keys and create a new key
   - Copy the key (it starts with `sk-ant-`)

4. Change `TZ` to your timezone if you are not in US Eastern time (e.g., `America/Chicago`, `Europe/London`, `Asia/Tokyo`).

5. Save the file.

> **Important:** The `.env` file contains your secret API key. It is already listed in `.gitignore` so it will not be accidentally committed to version control.

### A.3 Set Up Your Configuration File

1. Copy the example configuration:
   ```bash
   cp config/config.yaml.example config/config.yaml
   ```

2. Open `config/config.yaml` in a text editor. Near the top, find the `auth` section:
   ```yaml
   auth:
     client_id: ""                 # Azure AD Application (client) ID
     tenant_id: ""                 # Azure AD Directory (tenant) ID
   ```

3. Paste the **Application (client) ID** and **Directory (tenant) ID** that you copied in Step 1:
   ```yaml
   auth:
     client_id: "a1b2c3d4-e5f6-7890-abcd-ef1234567890"   # Your actual client ID
     tenant_id: "12345678-abcd-efgh-ijkl-123456789012"     # Your actual tenant ID
   ```

   > **Personal Outlook.com accounts:** If you are using a personal Microsoft account, set `tenant_id` to `"common"` instead of the UUID.

4. Update the `timezone` field to match your `.env` file:
   ```yaml
   timezone: "America/New_York"    # Change to your timezone
   ```

5. Leave everything else as-is. The bootstrap process in the next step will populate your projects, areas, and rules.

6. Save the file.

> **Full config reference:** The example file has comments explaining every option. For the complete schema, see [config.yaml.example](../config/config.yaml.example).

### A.4 Validate Your Configuration

Run the validation command to check that everything is correct:

```bash
docker compose run --rm bootstrap validate-config
```

> **Note:** The first time you run a Docker command, it will build the container image. This downloads Python and installs dependencies, which takes 1-3 minutes. Subsequent runs reuse the cached image and start instantly.

If the configuration is valid, you will see:

```
Validating config: config/config.yaml

 ✓ Configuration is valid
```

If there is an error, the message will tell you which field is wrong and how to fix it. Common issues:
- Empty `client_id` -- paste your Application ID from Step 1
- Invalid UUID format for `tenant_id` -- it should be in the format `xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx` or the word `common`

## Option B: Local Install (Without Docker)

Choose this if you prefer running Python directly, already have Python 3.12+ installed, or cannot use Docker.

### B.1 Install uv

uv is a fast Python package manager. Install it with:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

On Windows (PowerShell):
```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Close and reopen your terminal, then verify the installation:

```bash
uv --version
```

You should see a version number like `uv 0.6.x`.

### B.2 Clone and Install Dependencies

```bash
git clone <repo-url>
cd OutlookOrganiser
uv sync
```

`uv sync` creates a virtual environment and installs all required packages. This takes 10-30 seconds.

### B.3 Configure Credentials

Follow the same steps as A.2 and A.3 above:

1. `cp .env.example .env` and add your Anthropic API key
2. `cp config/config.yaml.example config/config.yaml` and add your Azure client ID and tenant ID

### B.4 Validate Configuration

```bash
uv run python -m assistant validate-config
```

You should see the same `✓ Configuration is valid` output described in A.4.

## What You Have Now

- The assistant is installed (Docker image built, or Python environment set up)
- Your Azure credentials and API key are configured
- The configuration file passes validation

The assistant is ready to scan your mailbox.

## If Something Went Wrong

See [Troubleshooting > Installation Issues](05-troubleshooting.md#installation-issues) for help with Docker, Python, and configuration problems.

---

> **Next:** [First Run](03-first-run.md)
