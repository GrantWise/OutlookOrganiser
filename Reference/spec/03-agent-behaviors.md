# Outlook AI Assistant Ã¢â‚¬â€ Agent Behaviors

> **Parent doc:** `01-overview.md` | **Read when:** Implementing bootstrap, triage, review UI, digest, waiting-for tracker, snippet processing, or rule creation.

---

## 1. Bootstrap Scanner

**Purpose:** Analyze existing email to propose an initial taxonomy and populate config.yaml.

**Trigger:** Manual CLI command: `python -m assistant bootstrap`

### Two-Pass Design

```
=== PASS 1: Batch Analysis ===

Step 1: Connect to Outlook via Graph API
Step 2: Fetch emails from last N days (default: 90, configurable via --days flag)
        Show progress bar: "Fetching emails... [Ã¢â€“Ë†Ã¢â€“Ë†Ã¢â€“Ë†Ã¢â€“Ë†Ã¢â€“â€˜Ã¢â€“â€˜Ã¢â€“â€˜Ã¢â€“â€˜] 1,200/3,000"
Step 3: Extract metadata for each email:
        - Subject, sender email, sender name, received date
        - Cleaned snippet (see Section 6 below)
        - Conversation thread ID
        - Any existing folder/category assignments
Step 4: Batch emails to Claude (Sonnet) for analysis (batches of 50)
        Show progress: "Analyzing batch 23/60..."
Step 5: Each batch prompt (see 04-prompts.md Section 1) returns:
        - Identified projects with signal keywords
        - Identified areas with signal keywords
        - Sender clusters (newsletters, automated, key contacts)
        - Estimated email volume per category

=== PASS 2: Consolidation ===

Step 6: Feed ALL batch results into a single Claude (Sonnet) consolidation call
        Prompt (see 04-prompts.md Section 2): deduplicate, merge, resolve conflicts
Step 7: Write consolidated taxonomy to config/config.yaml.proposed
Step 8: Print summary to console with instructions to review
```

**Why two passes?** When scanning 3,000 emails in batches of 50, each batch may independently identify the same project with slightly different names (e.g., "Tradecore Steel Project" vs "Tradecore Outbound Implementation") or conflicting sender classifications. The consolidation pass resolves these conflicts with full visibility across all batch results.

**Output:** `config/config.yaml.proposed` Ã¢â‚¬â€ a complete config file the user reviews, edits, and renames to `config.yaml`.

**Progress indication:** Use `rich` (already in requirements) for progress bars during both fetching and analysis phases.

### Bootstrap Idempotency

If `bootstrap` is run again after a previous run:
1. Check for existing `config/config.yaml.proposed` — if present, prompt the user: "A proposed config already exists. Overwrite? (y/N)"
2. Check `agent_state.last_bootstrap_run` — if set, warn: "Bootstrap was last run on {date}. Running again will re-analyze all email and generate a new proposed config. Continue? (y/N)"
3. Bootstrap does NOT modify an existing `config.yaml` — it always writes to `config.yaml.proposed`
4. The `--force` flag skips confirmation prompts (useful for scripted re-runs)

### Sender Profile Population

During bootstrap Pass 1, the agent builds initial sender profiles from the scanned email corpus:
1. For each unique sender email, count total emails and determine the most common folder classification from the bootstrap analysis
2. Categorize senders based on Claude's batch analysis: newsletters, automated notifications, key contacts, clients, vendors, internal
3. Write sender profiles to the `sender_profiles` table after consolidation (Pass 2)
4. Sender profiles with >90% of emails to a single folder and 10+ total emails are flagged as `auto_rule_candidate = 1`

> **Ref:** Inbox Zero's sender-level categorization approach — categorizing senders (not just individual emails) enables faster routing and a "manage senders" UI.
> https://github.com/elie222/inbox-zero (sender categorization feature)

### Dry-Run Classifier

After the user has edited and finalized `config.yaml`, they can run:

```bash
python -m assistant dry-run --days 90
```

This re-processes the same emails using the finalized config and outputs a classification report:

```
=== Dry Run Classification Report ===

Folder Distribution:
  Projects/Tradecore Steel     87 emails (14.2%)
  Areas/Sales                  134 emails (21.9%)
  Areas/Support                 56 emails  (9.2%)
  Reference/Newsletters         98 emails (16.0%)
  [Unclassified]                23 emails  (3.8%)
  ...

Sample Classifications (20 shown):
  Ã¢Å“â€° "Re: Outbound scanning go-live date" from john@tradecore...
    Ã¢â€ â€™ Projects/Tradecore Steel | P2 - Important | Needs Reply

  Ã¢Å“â€° "SYSPRO Partner Newsletter - January" from news@syspro.com
    Ã¢â€ â€™ Reference/Newsletters | P4 - Low | FYI Only
  ...

Unclassified Emails (need config refinement):
  Ã¢Å“â€° "Lunch Thursday?" from mike@personal.com
    Ã¢â€ â€™ No matching project or area
  ...
```

**CLI flags:**
- `--days N`: How many days of email to scan (default: 90)
- `--sample N`: Number of example classifications to show in the report (default: 20). All emails are classified regardless.
- `--limit N`: Cap total emails classified (useful for testing without burning through API calls). Default: no limit.
- `--dry-run`: (Also available as `triage --once --dry-run`) — Classify without creating suggestions or modifying state. Outputs report to stdout only.

### Confusion Matrix Output (when historical corrections exist)

When the `suggestions` table contains resolved corrections from prior triage cycles, the dry-run report includes a confusion matrix section showing where the classifier most frequently disagrees with the user:

```
=== Classification Accuracy (based on 312 historical corrections) ===

Folder Accuracy: 87.2% (272/312 matched user's choice)
  Most confused: Areas/Sales ↔ Areas/Support (14 swaps)
  Most confused: Projects/SOC 2 ↔ Areas/Development (8 swaps)

Priority Accuracy: 91.0% (284/312 matched)
  Most common correction: P3 → P2 (18 times)
  Most common correction: P2 → P1 (9 times)

Action Type Accuracy: 93.3% (291/312 matched)
  Most common correction: FYI Only → Needs Reply (12 times)
```

This helps users identify where config refinement or model upgrades would have the most impact.

> **Ref:** LangChain agents-from-scratch emphasizes evaluation datasets and systematic accuracy tracking.
> https://github.com/langchain-ai/agents-from-scratch (Module 5: evaluation)
> Also: gmail-llm-labeler tracks metrics per label for accuracy monitoring.
> https://github.com/ColeMurray/gmail-llm-labeler

---

## 2. Triage Engine

**Purpose:** Classify new incoming email and generate suggestions.

**Trigger:** APScheduler, every N minutes (configured in `triage.interval_minutes`).

### Process

```
Step 1: Query Graph API for emails in watched folders received since last triage
        Filter: receivedDateTime > last_processed_timestamp (from agent_state table)
        Filter: parentFolderId IN configured watch_folders (default: Inbox only)
        Request fields: id, conversationId, conversationIndex, subject, from,
                        receivedDateTime, bodyPreview, parentFolderId, categories,
                        webLink, flag, isRead, importance
Step 2: For each email not already in the emails table:
        a. Extract metadata (subject, sender, cleaned snippet, conversation_id,
           importance, isRead, flag, conversationIndex)
        b. Check reply state: query Sent Items for messages with same
           conversationId and later timestamp to determine has_user_reply
        c. Check auto_rules first Ã¢â‚¬" if a rule matches, apply directly
           (still create a suggestion record, but mark as auto-approved)
        d. Check thread inheritance (see below) Ã¢â‚¬" if a prior message in the same
           conversationId has an approved/pending classification, inherit the folder
           assignment at high confidence. Only invoke Claude if subject changed
           significantly or sender domain is new to the thread.
        e. If no auto_rule matches and thread inheritance doesn't apply,
           fetch thread context (see below) and send to Claude for classification
           using tool use (see 04-prompts.md Section 3)
        f. Claude returns structured classification via tool call
        g. Store classification in emails table
        h. Create compound suggestion record in suggestions table
Step 3: Update last_processed_timestamp in agent_state table
Step 4: Log all actions to action_log
Step 5: Log triage cycle summary at INFO level
```

### ETL Pipeline Structure

The triage process is structured as a three-stage ETL (Extract → Transform → Load) pipeline. Separating these stages makes retry logic simpler (re-classify without re-fetching), enables dry-run against already-fetched emails, and simplifies testing.

> **Ref:** gmail-llm-labeler uses an explicit ETL pipeline pattern for the same reasons.
> https://github.com/ColeMurray/gmail-llm-labeler
> Blog: https://www.colemurray.com/blog/automate-email-labeling-gmail-llm

**Extract:** Steps 1-2a above — fetch from Graph API, store raw metadata in `emails` table with `classification_status = 'pending'`.

**Transform:** Steps 2b-2f — check rules, inheritance, classify via Claude. All Claude interactions are logged to `llm_request_log` (see `02-config-and-schema.md` Section 3).

**Load:** Steps 2g-2h — store classification result, create suggestion record.

This means `triage --once --dry-run` can run the Transform stage against already-extracted emails without creating suggestion records or modifying the database.

### Structured Logging with Correlation IDs

Each triage cycle generates a unique `triage_cycle_id` (UUID4) that is attached to every log line and every `llm_request_log` entry within that cycle. This enables tracing a single email's journey through the entire pipeline.

```json
{
  "timestamp": "2026-02-06T14:30:00Z",
  "level": "info",
  "event": "email_classified",
  "triage_cycle_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "email_id": "AAMkAGI2...",
  "classification": "Projects/Tradecore Steel",
  "confidence": 0.92,
  "method": "claude_tool_use",
  "duration_ms": 340
}
```

### Sender Profile Updates

After each triage cycle, the agent updates `sender_profiles` for every sender seen in the cycle:
1. Increment `email_count`, update `last_seen`
2. Recalculate `default_folder` based on the most common approved folder
3. If the sender now has 10+ emails with >90% to a single folder, set `auto_rule_candidate = 1`
4. Optionally surface auto-rule candidates in the Review UI (see Section 3)

### Learned Classification Preferences

When users correct suggestions in the Review UI, the corrections are analyzed to update the `classification_preferences` text in `agent_state`. This goes beyond simple auto-rules by capturing nuanced patterns in natural language that Claude can interpret.

> **Ref:** LangChain agents-from-scratch (Module 4) maintains structured "triage preferences" memory
> that is updated by an LLM after each HITL interaction. The preferences are included in future
> classification prompts as context.
> https://github.com/langchain-ai/agents-from-scratch

**Update logic (runs after each batch of corrections):**
1. Gather recent corrections (last 7 days)
2. Send corrections to Claude with current preferences: "Given these user corrections, update the classification preferences"
3. Claude returns updated preferences text (e.g., "Emails about infrastructure monitoring should go to Areas/Development even when they mention 'security'. The user prefers P2 over P3 for emails from SYSPRO regardless of content.")
4. Store updated preferences in `agent_state.classification_preferences`
5. Include preferences in every triage classification prompt (see `04-prompts.md` Section 3)

### Graceful Degradation

When the Claude API is unavailable for an extended period (hours, not just single-cycle failures):

1. **First 3 cycles with failures:** Normal retry behavior per the error handling table below
2. **After 3 consecutive failed cycles:** Switch to "auto-rules only" mode — only process emails that match an auto_rule, queue the rest as `classification_status = 'pending'`
3. **Log WARNING:** "Claude API unavailable for {N} consecutive cycles. Operating in auto-rules-only mode. {M} emails queued for classification when API recovers."
4. **On recovery:** Process the backlog of pending emails in FIFO order, rate-limited to avoid burst API usage
5. **Dashboard indicator:** Show degraded mode status on the Review UI dashboard

### Email Deduplication

The Graph API can return the same email in multiple delta sync pages, or when emails are moved between watched folders. The agent deduplicates using the email's Graph API `id`:
- Before processing, check if `id` already exists in the `emails` table
- If it exists and `classification_status` is not `'pending'`, skip entirely
- If it exists and the `current_folder` has changed (email was moved), update the folder field but do not re-classify

### Thread Inheritance (Classification Shortcut)

Most emails in a CEO inbox are replies within existing threads. If the agent has already classified a prior message in the same conversation, the new message almost certainly belongs in the same folder. Thread inheritance avoids redundant Claude API calls and ensures consistency within a thread.

**Logic:**

```
For each new email with conversation_id:
  |
  +-- Query emails table: SELECT suggested_folder, suggested_priority
  |   FROM suggestions s JOIN emails e ON s.email_id = e.id
  |   WHERE e.conversation_id = '{conversation_id}'
  |   AND s.status IN ('approved', 'pending')
  |   ORDER BY s.created_at DESC LIMIT 1
  |
  +-- Prior classification exists?
  |   |
  |   +-- YES -> Check for significant change:
  |   |   |
  |   |   +-- Subject prefix changed (not just Re:/Fwd:)? -> Send to Claude (topic shifted)
  |   |   |
  |   |   +-- Sender domain differs from all prior thread participants? -> Send to Claude
  |   |   |
  |   |   +-- Otherwise -> Inherit folder from prior classification.
  |   |       Set confidence = 0.95. Still classify priority and action_type
  |   |       via Claude (priority may escalate within a thread, action_type
  |   |       depends on who sent the latest message and reply state).
  |   |
  |   +-- NO -> Send to Claude for full classification
```

**Why still classify priority/action via Claude?** A thread classified as "Projects/Tradecore Steel" stays about that project, but the latest reply might escalate from P2 to P1 ("we need this by EOD"), or the action type might change from "Needs Reply" to "FYI Only" (someone else on the thread answered). Folder inheritance is safe; priority/action inheritance is not.

**Cost savings:** In a typical inbox, ~60-70% of emails are thread replies. Thread inheritance can reduce Claude API calls by roughly half during daily triage. For the remaining calls where only priority/action are needed (folder inherited), the prompt is lighter.

### Thread Context Fetching

For emails that do require Claude classification (new threads or threads without prior classification), the agent fetches recent thread context to improve accuracy. A reply saying "Sounds good, let's do it" is unclassifiable without the preceding message.

**Implementation:**

```
For each email requiring Claude classification:
  |
  +-- Query Graph API for thread context:
  |   GET /me/messages
  |       ?$filter=conversationId eq '{conversation_id}'
  |       &$orderby=receivedDateTime desc
  |       &$top=4
  |       &$select=subject,from,receivedDateTime,bodyPreview
  |
  +-- Exclude the current email from the result set
  |
  +-- For each prior message (up to 3):
  |   - Clean the bodyPreview snippet (same pipeline as primary snippet)
  |   - Truncate to 500 chars each (shorter than primary Ã¢â‚¬" just for context)
  |
  +-- Include in Claude prompt as thread_context (see 04-prompts.md Section 3)
```

**API cost:** This adds 1 Graph API call per email that requires Claude classification. Since thread inheritance handles ~60-70% of thread replies without any API call, the net impact is modest. The classification accuracy improvement is substantial Ã¢â‚¬" particularly for short replies, forwarded chains, and multi-topic threads.

**Caching:** Thread context can be partially served from the local emails table (if prior messages were already processed). Only fetch from Graph API for messages not yet in the local database.

### Metadata-Enriched Classification

The Graph API provides several metadata fields beyond subject/sender/body that improve classification accuracy. The agent passes all available metadata to Claude:

| Field | Source | Classification Value |
|-------|--------|---------------------|
| `importance` | Sender-set message importance (low/normal/high) | High importance from a key contact strongly signals P1/P2 |
| `flag` | Outlook flag status | If the user manually flagged the email, it likely needs action |
| `isRead` | Read/unread status | An unread email from 3 days ago is more likely to need attention |
| `conversationIndex` | Thread position (binary, base64-encoded) | Length indicates thread depth Ã¢â‚¬" longer = deeper reply. First message in a thread (22 bytes decoded) needs full classification; deep replies are more likely to inherit |
| Sender classification history | Local emails table | "Emails from john@tradecore have been classified to Projects/Tradecore Steel 47/50 times" Ã¢â‚¬" strong prior for folder assignment |

**Sender history lookup:** Before invoking Claude, the agent queries the local emails table for historical classification patterns for the sender's email address and domain:

```sql
SELECT suggested_folder, COUNT(*) as cnt
FROM suggestions s JOIN emails e ON s.email_id = e.id
WHERE e.sender_email = '{sender_email}'
  AND s.status IN ('approved', 'partial')
  AND s.approved_folder IS NOT NULL
GROUP BY suggested_folder
ORDER BY cnt DESC
LIMIT 3
```

If a single folder accounts for >80% of a sender's approved classifications (with at least 5 data points), this is included as context in the Claude prompt: "Historical pattern: 94% of emails from this sender are classified to Projects/Tradecore Steel (47/50 emails)." Claude can use this as a strong prior while still overriding when the content warrants it.
### Reply State Detection

To accurately determine whether an email "Needs Reply", the agent must know if the user has already responded. For each inbound email's conversation thread:

1. Query: `GET /me/mailFolders/sentitems/messages?$filter=conversationId eq '{id}'&$orderby=receivedDateTime desc&$top=1`
2. If a sent message exists with `receivedDateTime` after the inbound email: set `has_user_reply = 1`
3. Pass this flag to Claude as context: "User has already replied to this thread" or "User has NOT replied"
4. Claude can then accurately assign "Needs Reply" vs "FYI Only"

**Optimization:** On each triage cycle, batch-fetch recent sent items (last N hours) and cache conversation IDs locally. This reduces API calls from O(emails) to O(1) per cycle.

### Classification Decision Flow

```
Email arrives in watched folder
  |
  +-- Already in emails table? --> Skip (already processed)
  |
  +-- Matches auto_rule? --> Apply rule, create auto-approved suggestion, log, done
  |
  +-- Sender in key_contacts? --> Note for priority boost in Claude context
  |
  +-- Check reply state (Sent Items query)
  |
  +-- Check thread inheritance:
  |   |
  |   +-- Prior classification in same conversation_id?
  |   |   +-- YES + no significant change --> Inherit folder (confidence 0.95)
  |   |   |   Still send to Claude for priority + action_type only
  |   |   +-- YES + subject/sender changed --> Full Claude classification
  |   |   +-- NO --> Full Claude classification
  |
  +-- Fetch thread context (last 3 messages in thread, 500 chars each)
  |
  +-- Look up sender classification history from emails table
       |
       v
     Send to Claude via tool use with:
       - Email metadata (subject, sender, cleaned snippet)
       - Graph metadata (importance, flag, isRead, thread depth)
       - Current project/area list from config
       - Reply state (has_user_reply flag)
       - Thread context (prior messages in conversation)
       - Sender history ("94% of emails from this sender -> Projects/Tradecore Steel")
       - Key contact annotations
       - Inherited folder (if thread inheritance applies Ã¢â‚¬" Claude classifies priority/action only)
       - Sender profile (from sender_profiles table — category, default_folder)
       - Learned classification preferences (from agent_state — natural language context)
       |
       v
     Claude returns structured tool call response
       |
       +-- Confidence >= 0.85 --> In suggest mode: create suggestion as "pending"
       |                         In auto mode (future): execute immediately
       |
       +-- Confidence 0.5-0.85 --> Create suggestion as "pending" (always needs review)
       |
       +-- Confidence < 0.5 --> Create suggestion with flag "low_confidence"
                                 Include in daily digest for manual review
```

### Error Handling for Claude API Calls

| Failure | Handling |
|---------|----------|
| Network timeout / 5xx | Retry with exponential backoff: 1s, 2s, 4s. Max 3 attempts per email per triage cycle. |
| Rate limit (429) | Respect `Retry-After` header. Pause triage cycle, resume after delay. |
| Invalid/unparseable response | Log raw response at ERROR level. Mark email `classification_status = 'failed'`, increment `classification_attempts`. |
| Classification failed 3 times | Stop retrying. Set `classification_status = 'failed'`. Include in daily digest for manual review. |
| Claude API fully down | Log ERROR, skip classification for this cycle. All unprocessed emails remain in queue for next cycle. |

All failed classifications are visible in the Review UI under a "Failed" tab and in the daily digest.

---

## 3. Suggestion Review Interface

**Purpose:** Web-based UI for reviewing and acting on agent suggestions.

**Access:** `http://localhost:8080`

### Pages

1. **Dashboard** (`/`)
   - Count of pending suggestions
   - Count of aging "Needs Reply" items
   - Count of overdue "Waiting For" items
   - Count of failed classifications needing manual review
   - Quick stats: emails processed today, auto-ruled, classified
   - Health indicator: last triage cycle time, Claude API status

2. **Review Queue** (`/review`)
   - List of pending suggestions, newest first
   - Each suggestion shows:
     - Email subject, sender, snippet
     - Proposed classification: folder, priority, action type (as a compound card)
     - Confidence score (color-coded: green Ã¢â€°Â¥0.85, yellow 0.5-0.85, red <0.5)
     - Claude's reasoning (expandable)
   - Actions per suggestion (per-field granularity):
     - Ã¢Å“â€¦ Approve All Ã¢â‚¬â€ accept folder, priority, and action type as suggested
     - Ã¢Å“ÂÃ¯Â¸Â Correct Ã¢â‚¬â€ per-field dropdowns to change folder, priority, or action type independently
     - Ã¢ÂÅ’ Reject Ã¢â‚¬â€ leave email in inbox, mark suggestion rejected
     - Ã°Å¸â€â€” Open in Outlook Ã¢â‚¬â€ deep link (see Section 8)
     - Ã°Å¸â€œÅ’ Create Rule Ã¢â‚¬â€ "Always route emails from [sender] to [folder]?" (writes auto_rule to config.yaml)
   - Bulk actions: "Approve all high-confidence" (Ã¢â€°Â¥ 0.85)
   - **Failed tab**: Emails where classification failed after 3 attempts. Manual classification form.

3. **Waiting For** (`/waiting`)
   - List of tracked "Waiting For" items
   - Shows: who we're waiting on, how long, original email link
   - Actions: Mark resolved, Extend deadline, Escalate

4. **Config Editor** (`/config`)
   - View and edit config.yaml through the browser
   - Syntax-highlighted YAML editor
   - Save validates against Pydantic schema, shows errors inline if invalid
   - Valid save triggers config reload

5. **Activity Log** (`/log`)
   - Scrollable log of all agent actions
   - Filterable by action type, date range, email sender

6. **Stats & Accuracy** (`/stats`)
   - Classification accuracy over time (approval rate, correction rate per folder/priority)
   - Confidence calibration chart: predicted confidence vs. actual user approval rate
   - Correction heatmap: which folder/priority/action combinations are most often corrected
   - Cost tracking: Claude API token usage and estimated cost per day/week/month
   - Model performance comparison (if models have been changed in config)

   > **Ref:** gmail-llm-labeler tracks per-label metrics and accuracy over time.
   > https://github.com/ColeMurray/gmail-llm-labeler

7. **Sender Management** (`/senders`)
   - List of all known senders from `sender_profiles` table
   - Sortable by email count, last seen, category
   - Quick actions: change sender category, set default folder, create auto-rule
   - Highlight auto-rule candidates (senders with >90% to a single folder, 10+ emails)

   > **Ref:** Inbox Zero's sender categorization UI — managing senders (not just emails) gives
   > users a higher-level view of their inbox composition.
   > https://github.com/elie222/inbox-zero

---

## 4. Daily Digest

**Purpose:** Morning summary of email status.

**Trigger:** Scheduled at configured time (default 08:00 local).

**Content:**

```
Ã¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢Â
  Ã°Å¸â€œÂ¬ Outlook Assistant Ã¢â‚¬â€ Daily Digest
  Thursday, February 5, 2026
Ã¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢Â

Ã°Å¸â€Â´ OVERDUE REPLIES (3)
  Ã¢â‚¬Â¢ "Re: Go-live timeline confirmation" from John @ Tradecore
    Received 52 hours ago | P1 - Urgent Important
  Ã¢â‚¬Â¢ "Contract renewal terms" from legal@syspro.com
    Received 36 hours ago | P2 - Important
  Ã¢â‚¬Â¢ "Budget approval needed" from CFO
    Received 28 hours ago | P1 - Urgent Important

Ã¢ÂÂ³ WAITING FOR (2 overdue)
  Ã¢â‚¬Â¢ Waiting on Sarah for SOC 2 evidence package (72 hours)
  Ã¢â‚¬Â¢ Waiting on DevOps for staging deployment (50 hours)

Ã°Å¸â€œÅ  YESTERDAY'S ACTIVITY
  Ã¢â‚¬Â¢ 47 emails processed
  Ã¢â‚¬Â¢ 12 auto-routed (newsletters, notifications)
  Ã¢â‚¬Â¢ 31 classified by AI (28 approved, 3 pending review)
  Ã¢â‚¬Â¢ 4 unclassified (need manual review)
  Ã¢â‚¬Â¢ 2 classifications failed (see /review Ã¢â€ â€™ Failed tab)

Ã°Å¸â€œâ€¹ PENDING REVIEW (7 suggestions awaiting your input)
  Ã¢â€ â€™ http://localhost:8080/review
Ã¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢ÂÃ¢â€¢Â
```

---

## 5. Waiting For Tracker

**Purpose:** Track emails where the user is waiting for an external response.

**Trigger:** Two sources:
1. Claude identifies an email thread where the user's last message was a question or request (automatic detection via reply state analysis)
2. User manually marks an email as "Waiting For" via the review UI

**Behavior:**
- Stores the `conversation_id` in the `waiting_for` table
- On each triage cycle, checks for new replies in tracked threads
- If a reply arrives from `expected_from`, marks the waiting-for item as "received"
- If no reply after `nudge_after_hours`, includes in daily digest
- If no reply after `escalate_after_hours`, flags as critical in digest

---

## 6. Snippet Processing

Email bodies often contain noise that degrades classification accuracy. The agent cleans snippets as follows:

**Cleaning pipeline (in order):**
1. Strip HTML tags, decode entities (if body is HTML)
2. Remove forwarded message headers (`---------- Forwarded message ----------`, `From:`, `Sent:`, etc.)
3. Remove signature blocks (detect `--\n`, `_____`, or common signature patterns)
4. Remove legal/confidentiality disclaimers (detect "CONFIDENTIAL", "This email is intended for", etc.)
5. Collapse excessive whitespace
6. Truncate to configured `snippet.max_length` (default: 1,000 characters)

**Rationale for 1,000 chars:** At 500 chars, many business emails are majority signature/disclaimer with little actual content. At 1,000 chars post-cleaning, classification accuracy improves significantly. The cost difference with Haiku is negligible (~0.001 cents per email).

---

## 7. Rule Creation from Corrections

When a user corrects a suggestion in the Review UI, the interface offers an optional quick-action: **"Always route emails from [sender] to [folder]?"**

If accepted:
1. A new auto_rule is generated with the sender pattern and the user's chosen folder/priority/action
2. The rule is appended to `config.yaml` under `auto_rules`
3. Config is reloaded
4. Future emails from that sender bypass Claude classification entirely

This turns user corrections into permanent rules, reducing future API calls and improving response time. The user can review and modify auto-generated rules via the Config Editor.

### Auto-Rule Hygiene

As the system operates over weeks and months, auto-rules can accumulate. Without management, they create conflicts and make the config unwieldy.

> **Ref:** Inbox Zero's ARCHITECTURE.md documents problems with auto-generated rules accumulating
> over time — hundreds of rules with some conflicting.
> https://github.com/elie222/inbox-zero (ARCHITECTURE.md — rule management lessons)

**Safeguards:**
1. **Rule count warning:** When `auto_rules` exceeds `auto_rules_hygiene.max_rules` (default: 100), the dashboard shows a warning and the CLI `validate-config` command outputs a notice
2. **Conflict detection:** On config load, scan for overlapping sender/subject patterns across rules. If two rules could match the same email, log a WARNING with both rule names
3. **Stale rule detection:** Periodically (on digest generation), check each auto_rule against recent email. Rules with 0 matches in the last `consolidation_check_days` are flagged as "potentially stale" in the Config Editor
4. **CLI audit command:** `python -m assistant rules --audit` lists all auto-rules with match counts, last match date, and conflict warnings

---

## 8. Outlook Deep Links

The Review UI links directly to emails in Outlook for quick access.

**Outlook Web App (OWA) Ã¢â‚¬â€ primary format:**
```
https://outlook.office.com/mail/deeplink/read/{webMessageId}
```

The `webMessageId` is available from the Graph API message response (field: `webLink`). This is the most reliable cross-platform link format.

**Outlook Desktop Ã¢â‚¬â€ optional, future (Phase 4):**
Desktop deep links (`outlook://`) are more complex, vary by Outlook version, and require URL encoding of the message entry ID.

**Implementation:** When fetching messages from the Graph API, always request `$select=...,webLink` and store the webLink in the emails table.
