# Step 3: First Run -- Bootstrap, Configure, and Test

> **Time required:** ~10 minutes (plus bootstrap analysis time, which depends on mailbox size)
> **Previous:** [Installation](02-installation.md) | **Next:** [Daily Usage](04-daily-usage.md)

## What This Step Does

The assistant will:
1. **Bootstrap** -- scan your recent email using Claude to discover projects, recurring topics, and sender patterns
2. You **review** the proposed organization and make adjustments
3. **Dry-run** -- test the classification on real emails without making any changes
4. **Serve** -- start the assistant for continuous operation

## Overview

```
Bootstrap           Review Config        Dry-Run             Serve
(scan email) ──────> (edit proposed  ──────> (test without ──────> (go live)
                      config.yaml)          changes)
```

## 3.1 Run Bootstrap

### What Bootstrap Does

Bootstrap uses Claude (Sonnet model) to analyze your recent emails and discover patterns: what projects you are working on, what areas of responsibility you have, and which senders send predictable email. It reads your email but **does not move, modify, or delete anything**.

The output is a proposed configuration file (`config/config.yaml.proposed`) with a folder taxonomy tailored to your mailbox.

### Run the Command

**Docker:**
```bash
docker compose run --rm bootstrap --days 90
```

**Local:**
```bash
python -m assistant bootstrap --days 90
```

The `--days 90` flag tells the assistant to look at the last 90 days of email. You can adjust this -- use `--days 30` for a quicker scan, or `--days 180` for a more thorough analysis.

### First-Time Authentication

Since this is the first time the assistant is connecting to your mailbox, it needs you to sign in. You will see a box in your terminal that looks like this:

```
┌─ Microsoft Authentication Required ──────────────────────┐
│                                                          │
│  To authenticate, open a browser and go to:              │
│                                                          │
│    https://microsoft.com/devicelogin                     │
│                                                          │
│  Enter this code: ABCD-EFGH                              │
│                                                          │
│  Waiting for authentication...                           │
│                                                          │
└──────────────────────────────────────────────────────────┘
```

Follow these steps:

1. Open the URL shown (`https://microsoft.com/devicelogin`) in any web browser -- this can be on the same computer or a different device like your phone
2. Enter the code shown in your terminal (e.g., `ABCD-EFGH`)
3. Sign in with the Microsoft account that has your email
4. You will see a permissions page listing what the app can access (read mail, send mail, etc.) -- click **Accept**
5. The browser will show "You have signed in" and you can close the browser tab
6. Back in your terminal, the assistant will detect the successful sign-in and continue automatically

> **The code changes every time.** If you need to re-run bootstrap later, you will get a new code. After the first successful sign-in, the assistant caches your credentials and subsequent runs will not prompt again (the cached token lasts 90 days).

### What You See During Bootstrap

After authentication, the assistant fetches and analyzes your emails. You will see progress output like:

```
Fetching emails from the last 90 days...
  Fetched 247 emails from Inbox

Analyzing email patterns (batch 1/5)...
Analyzing email patterns (batch 2/5)...
...
Consolidating patterns into taxonomy...

Bootstrap complete!
  Discovered 8 projects, 5 areas, 12 auto-rules
  Proposed config written to: config/config.yaml.proposed
```

This typically takes 2-10 minutes depending on the size of your mailbox.

> **Cost note:** Bootstrap uses Claude Sonnet, which is the more capable (and more expensive) model. For a typical mailbox (~1000-3000 emails in 90 days), expect roughly $0.50-2.00 in API costs. This is a one-time cost -- daily operations use the much cheaper Haiku model.

## 3.2 Review the Proposed Configuration

Bootstrap has created a file called `config/config.yaml.proposed`. Open it in your text editor.

The file looks like your existing `config/config.yaml` but with the `projects`, `areas`, and `auto_rules` sections filled in based on what bootstrap discovered in your email.

### Projects Section

Projects are active work with defined outcomes -- things you are working on that will eventually be completed.

```yaml
projects:
  - name: "Website Redesign"
    folder: "Projects/Website Redesign"
    signals:
      subjects: ["website", "redesign", "homepage"]
      senders: ["*@designagency.com"]
      body_keywords: ["mockup", "wireframe"]
    priority_default: "P2 - Important"
```

**What to check:**
- Do the project names make sense? Rename them if the auto-detected name is awkward.
- Are there false positives? Remove any "project" that is not actually a project.
- Are there duplicates? Bootstrap may discover the same project twice with different names -- merge them.
- Are any projects missing? You can add them manually following the same format.

### Areas Section

Areas are ongoing responsibilities with no end date -- things you manage continuously.

```yaml
areas:
  - name: "IT Support"
    folder: "Areas/IT Support"
    signals:
      subjects: ["helpdesk", "ticket"]
      senders: ["*@itsupport.company.com"]
      body_keywords: []
    priority_default: "P3 - Urgent Low"
```

**What to check:** Same as projects -- rename, remove false positives, merge duplicates, add missing ones.

### Auto-Rules Section

Auto-rules are patterns that are so predictable they do not need AI classification. Email from these senders or with these subjects always goes to the same place.

```yaml
auto_rules:
  - name: "GitHub Notifications"
    match:
      senders: ["notifications@github.com"]
    action:
      folder: "Reference/Dev Notifications"
      category: "FYI Only"
      priority: "P4 - Low"
```

**What to check:**
- Are the folder assignments correct? Newsletter senders should probably go to `Reference/Newsletters`, not a project folder.
- Are there senders you want to add? Think about automated notifications, mailing lists, and other predictable email.

### Activate the Configuration

Once you are happy with your edits:

**Linux/macOS:**
```bash
cp config/config.yaml.proposed config/config.yaml
```

**Windows:**
```cmd
copy config\config.yaml.proposed config\config.yaml
```

Then validate it:

**Docker:**
```bash
docker compose run --rm bootstrap validate-config
```

**Local:**
```bash
python -m assistant validate-config
```

## 3.3 Run Dry-Run to Test

Dry-run classifies your recent emails using the configuration you just finalized, but **does not create suggestions or make any changes**. It shows you how the assistant would organize your email so you can check the results before going live.

**Docker:**
```bash
docker compose run --rm dry-run --days 30 --sample 20
```

**Local:**
```bash
python -m assistant dry-run --days 30 --sample 20
```

### Reading the Output

The dry-run output shows:

1. **Folder distribution** -- a table showing how many emails would go to each folder:
   ```
   Folder                        Count    %
   Projects/Website Redesign        12   15%
   Areas/IT Support                  8   10%
   Reference/Newsletters            22   28%
   ...
   ```

2. **Sample classifications** -- 20 example emails with their proposed classification:
   ```
   Subject: "Re: Homepage mockup v3"
     Folder:   Projects/Website Redesign
     Priority: P2 - Important
     Action:   Needs Reply
     Confidence: 92%
   ```

### What to Look For

- **Are the folder assignments sensible?** If emails are going to the wrong folders, adjust the `signals` in your projects/areas config.
- **Are there many "Reference" classifications?** This might mean you need to add more projects or areas to capture those emails.
- **Are priorities reasonable?** Emails from key contacts should generally be P1 or P2.
- **Is the confidence generally high (80%+)?** Low confidence across the board may mean your project/area definitions are too vague.

### Iterate if Needed

If the results are not satisfactory, edit `config/config.yaml`, then run dry-run again. Most users run dry-run 1-3 times before they are satisfied with the classification quality.

## 3.4 Start the Service

Once dry-run results look good, start the assistant for continuous operation.

**Docker:**
```bash
docker compose up -d
```

The `-d` flag runs the service in the background. It will keep running until you stop it.

**Local:**
```bash
python -m assistant serve
```

This runs in the foreground. Press Ctrl+C to stop.

### What Happens

Two things start:

1. **Triage engine** -- polls for new email every 15 minutes (configurable in `config.yaml` under `triage.interval_minutes`) and classifies incoming messages
2. **Web UI** -- a local web interface for reviewing classification suggestions

### Open the Web UI

Open [http://localhost:8080](http://localhost:8080) in your browser.

You should see the **Dashboard** page with:
- A sidebar on the left with navigation links (Dashboard, Review Queue, Waiting For, Config, Activity Log)
- Stat cards showing "Pending Suggestions: 0" (no emails classified yet since the service just started)
- A system health section showing the engine status

### Verify It Is Working

The first triage cycle runs after the configured interval (default: 15 minutes). You can either:

- **Wait** for the first cycle to complete -- check the Dashboard for the "Last cycle" timestamp
- **Run a manual cycle** to see results immediately:

  **Docker** (in a separate terminal):
  ```bash
  docker compose exec outlook-assistant python -m assistant triage --once
  ```

  **Local** (in a separate terminal):
  ```bash
  python -m assistant triage --once
  ```

After a cycle completes, check the **Review Queue** page. You should see suggestion cards for any new emails that were classified.

## What You Have Now

- A running assistant that classifies your incoming email every 15 minutes
- A web UI at [http://localhost:8080](http://localhost:8080) for reviewing suggestions
- A configuration tuned to your mailbox based on the bootstrap analysis

The next guide explains the daily review workflow and how to use each page of the web UI.

## If Something Went Wrong

- **Authentication failed:** See [Troubleshooting > Authentication Issues](05-troubleshooting.md#authentication-issues)
- **Bootstrap found no emails:** See [Troubleshooting > Bootstrap Issues](05-troubleshooting.md#bootstrap-issues)
- **Cannot access localhost:8080:** See [Troubleshooting > Web UI Issues](05-troubleshooting.md#web-ui-issues)

---

> **Next:** [Daily Usage](04-daily-usage.md)
