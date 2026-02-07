# Step 4: Daily Usage and the Review Workflow

> **Previous:** [First Run](03-first-run.md) | **Next:** [Troubleshooting](05-troubleshooting.md) (if needed)

## How the Assistant Works Day-to-Day

Once running, the assistant operates on a simple cycle:

1. Every 15 minutes (configurable), it checks your Inbox for new email
2. Each email is classified: assigned a folder, priority level, and action type
3. Classifications appear as **suggestions** in the web UI
4. You review each suggestion and approve, correct, or reject it
5. Approved suggestions are executed -- the email is moved to the assigned folder

Most users spend **2-5 minutes per day** reviewing suggestions.

## The Web UI

Open [http://localhost:8080](http://localhost:8080) in your browser. The left sidebar has five pages:

### Dashboard

The Dashboard is your overview page. It shows:

- **Stat cards** at the top:
  - Pending Suggestions -- how many emails are waiting for your review
  - Aging Needs Reply -- emails marked "Needs Reply" that are getting old
  - Failed Classifications -- emails that could not be classified (usually due to API errors)
  - Today's stats -- emails processed, auto-routed, and classified in the last 24 hours

- **System Health** section:
  - Whether the triage engine is running
  - When the last triage cycle completed
  - Whether it is in degraded mode (auto-rules only, if Claude API is unavailable)

**What to pay attention to:** Check the pending suggestions count. If it is growing faster than you review, consider increasing the `triage.batch_size` or reviewing more frequently.

### Review Queue

This is where you spend most of your time. The Review Queue shows all pending suggestions, newest first.

Each **suggestion card** shows:

- **Email subject** and **sender**
- **Snippet** of the email body (first few lines, expandable)
- **Proposed classification:**
  - Folder (e.g., "Projects/Website Redesign") -- shown as a blue badge
  - Priority (e.g., "P2 - Important") -- shown as a colored badge (red for P1, orange for P2, blue for P3, gray for P4)
  - Action type (e.g., "Needs Reply") -- shown as a gray badge
- **Confidence score** -- a colored bar indicating how certain the assistant is (green = high, yellow = medium, red = low)
- **Reasoning** -- the assistant's explanation of why it chose this classification (expandable)

**Actions for each suggestion:**

| Action | What it does |
|--------|-------------|
| **Approve** | Accept the classification as-is. The email will be moved to the assigned folder. |
| **Correct** | Opens fields to change the folder, priority, or action type. Click "Apply" after editing. |
| **Reject** | Dismiss the suggestion. The email stays where it is. |
| **Open in Outlook** | Opens the email in Outlook Web App so you can read the full message. |

### The Review Workflow (Step by Step)

Here is the recommended process:

1. **Open the Review Queue** page
2. **Batch approve high-confidence suggestions** -- if there is an "Approve All High-Confidence" button at the top, use it to approve suggestions with confidence scores above 85%. These are almost always correct.
3. **Review remaining suggestions one by one:**
   - Read the subject and snippet
   - Check the proposed folder -- does it make sense?
   - Check the priority -- is it appropriate?
   - Check the action type -- should you reply, or is this FYI?
   - **Approve** if the classification is correct
   - **Correct** if one or more fields need changing (the assistant learns from corrections)
   - **Reject** if the email should not be classified at all
4. **Repeat daily** -- suggestions accumulate throughout the day as new email arrives

### How Corrections Improve Accuracy

When you correct a suggestion, the system records what was wrong and what the right answer was. Over time:

- Patterns you frequently correct may be added as **auto-rules**, which route those emails instantly without needing Claude at all
- The overall classification accuracy improves as the assistant builds a history of your preferences

This means the first week requires the most corrections, and accuracy steadily improves from there.

### Waiting For

The Waiting For page tracks emails where you are expecting a response from someone else. It shows:

- Who you are waiting on
- How long you have been waiting
- The original email subject

You can **mark items as resolved** when the expected response arrives, or let them age for visibility in your daily digest.

### Config

The Config page is a browser-based editor for your `config.yaml` file. You can:

- Edit the YAML directly with syntax highlighting
- Add new projects, areas, or auto-rules
- Change the triage interval
- Changes are validated before saving -- if the YAML is invalid, you will see an error

Changes take effect on the next triage cycle (no restart required, thanks to the config hot-reload feature).

### Activity Log

The Activity Log shows a chronological record of all actions the assistant has taken:

- Emails classified
- Suggestions approved, corrected, or rejected
- Emails moved between folders
- Auto-rules triggered
- Errors encountered

Use this to audit what the assistant has done, or to investigate if something was moved unexpectedly.

## Useful Commands

### Check Service Status

**Docker:**
```bash
docker ps                                    # Is the container running?
docker logs outlook-assistant --tail 50      # Recent logs
docker logs --follow outlook-assistant       # Stream logs in real time
```

### Run a Manual Triage Cycle

If you do not want to wait for the scheduled cycle:

**Docker:**
```bash
docker compose exec outlook-assistant uv run python -m assistant triage --once
```

**Local** (in a separate terminal):
```bash
uv run python -m assistant triage --once
```

### Stop the Service

**Docker:**
```bash
docker compose down
```

**Local:** Press Ctrl+C in the terminal where `serve` is running.

### Restart After Config Changes

The assistant picks up config changes automatically on the next triage cycle. If you want to force a restart:

**Docker:**
```bash
docker compose restart
```

For the full list of CLI commands and flags, see the [CLI commands table](../README.md#cli-commands) in the project README.

## Adjusting Your Configuration

### Changing the Triage Interval

Edit `config/config.yaml` (or use the Config page in the web UI):

```yaml
triage:
  interval_minutes: 5     # Check every 5 minutes instead of 15
```

### Adding a New Project

```yaml
projects:
  # ... existing projects ...
  - name: "Q3 Budget Planning"
    folder: "Projects/Q3 Budget"
    signals:
      subjects: ["budget", "Q3 planning", "fiscal"]
      senders: ["*@finance.company.com"]
      body_keywords: ["forecast", "allocation"]
    priority_default: "P2 - Important"
```

After saving, the next triage cycle will start classifying matching emails into the new project folder.

### Adding an Auto-Rule

Auto-rules bypass AI classification entirely, making them instant and free (no API cost). Use them for predictable, high-volume email:

```yaml
auto_rules:
  # ... existing rules ...
  - name: "Jira Notifications"
    match:
      senders: ["jira@company.atlassian.net"]
    action:
      folder: "Reference/Dev Notifications"
      category: "FYI Only"
      priority: "P4 - Low"
```

For the full list of configuration options, see [config.yaml.example](../config/config.yaml.example).

---

> **Next:** [Troubleshooting](05-troubleshooting.md) (if needed)
