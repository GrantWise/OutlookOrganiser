# Outlook AI Assistant Ã¢â‚¬â€ Safety, Guardrails & Testing

> **Parent doc:** `01-overview.md` | **Read when:** Implementing safety checks, audit logging, rollback, or writing tests.

---

## 1. Autonomy Boundaries

**Agent MAY do autonomously:**
- Read any email in the mailbox (Inbox and Sent Items)
- Create folders in Outlook (following the taxonomy)
- Create Outlook categories
- Write to local SQLite database
- Generate suggestions and store them
- Send digest to stdout/file
- Fetch user profile from Graph API `/me`

**Agent MUST get user approval for:**
- Moving any email between folders (in suggest mode)
- Applying categories to emails (in suggest mode)
- Deleting any email (never Ã¢â‚¬â€ the agent does not delete)
- Sending any email on behalf of the user
- Modifying email content
- Creating new projects/areas in config

**Agent MUST NEVER:**
- Delete emails
- Mark emails as read (unless explicitly configured)
- Forward or reply to emails
- Access other users' mailboxes
- Store full email bodies (only cleaned snippets)
- Send email content to any service other than Anthropic's Claude API

---

## 2. Data Privacy

- Cleaned email snippets (up to 1,000 chars) are sent to Claude API for classification
- Full email bodies are NOT stored locally or sent to Claude
- The Claude API does not train on API inputs (per Anthropic's data policy)
- Authentication tokens are stored in the local data volume only
- No telemetry or external reporting
- **LLM request log:** Full prompts and responses are logged locally in the `llm_request_log` table for debugging. These contain email snippets. The log is automatically pruned after `llm_logging.retention_days` (default: 30 days). The log never leaves the local machine.

> **Ref:** Aomail (https://github.com/aomail-ai/aomail-app) explicitly states "no AI training on
> your data" and uses stateless API calls — the same approach we use. Our LLM request log is
> local-only and exists purely for debugging and prompt iteration.

---

## 3. Audit Trail

Every action the agent takes is logged in `action_log` with:
- Timestamp
- Action type
- Email ID reference
- Full details as JSON
- Whether triggered automatically or by user approval

---

## 4. Rollback

- All folder moves are logged with the original folder path
- A CLI command `python -m assistant undo --last N` can reverse the last N actions
- Bulk undo: `python -m assistant undo --since "2026-02-01"` reverses all actions since a date

---

## 5. Testing Strategy

### Unit Tests
- Auto-rules matching against sample emails
- Auto-rules conflict detection: verify overlapping sender/subject patterns are flagged
- Config parsing and Pydantic validation (valid configs, invalid configs, edge cases)
- Config schema migration: verify v1→v2 migration adds new fields with defaults
- Database operations (CRUD for all tables including agent_state, sender_profiles, llm_request_log)
- Prompt template rendering (including thread context formatting, sender history, learned preferences, sender profile)
- Snippet cleaning pipeline (signatures, disclaimers, HTML stripping)
- Tool use response parsing
- Thread inheritance logic: verify folder is inherited when conversation_id matches, verify Claude is called when subject changes, verify Claude is called when new sender domain enters thread
- Sender history lookup: verify query returns correct folder distribution, verify >80% threshold logic, verify minimum 5 data points requirement
- Sender profile updates: verify email_count increment, default_folder recalculation, auto_rule_candidate flag
- conversationIndex parsing: verify thread depth calculation from base64-encoded value
- Metadata enrichment: verify importance/flag/isRead fields are correctly extracted and passed to prompt templates
- Email deduplication: verify same Graph API id is not processed twice, verify folder-change detection
- Delta query 410 Gone: verify fallback to timestamp-based polling
- LLM request log: verify entries are created for each Claude call with correct fields
- LLM log retention: verify entries older than retention_days are pruned
- Suggestion queue expiry: verify pending suggestions older than expire_after_days are auto-expired
- Bootstrap idempotency: verify re-run prompts for confirmation, verify --force flag skips prompts

### Integration Tests
- Claude API classification with sample emails (uses real API, tool use)
- Claude API classification with thread context and sender history (verify improved accuracy)
- Claude API classification with inherited folder (verify priority/action-only classification)
- Claude API classification with learned preferences in prompt (verify preferences influence output)
- Graph API operations against test mailbox (requires test account)
- Graph API thread context fetching (verify conversationId filter and result ordering)
- Graph API delta query lifecycle: initial sync → delta token → incremental sync → 410 Gone → recovery
- Bootstrap two-pass flow with mock email data
- Bootstrap sender profile population: verify profiles are created from bootstrap analysis
- Full triage cycle with thread inheritance: process 3 emails in the same thread, verify first goes to Claude, second inherits folder
- Full triage cycle in degraded mode: simulate Claude API failure, verify auto-rules-only processing
- Triage dry-run mode: verify no suggestions or state changes are created

### Test Fixtures
- `fixtures/sample_emails.json`: 50+ representative emails covering all project/area types, newsletters, automated notifications, personal email, and edge cases
- Each fixture email includes expected classification for regression testing
- Fixture emails include noisy bodies (signatures, disclaimers) to test snippet cleaning
- Fixture emails include multi-message thread sequences (same conversationId) to test thread inheritance and context fetching
- Fixture emails include varied importance/flag/isRead values to test metadata enrichment
- Fixture emails include short ambiguous replies ("Sounds good", "Let me check") paired with thread context to verify context improves classification
- Fixture emails include sender profiles with known categories to test sender-profile-enriched classification
- `fixtures/sample_corrections.json`: 20+ correction scenarios (user changes folder, priority, or action) for testing learned preferences updates and confusion matrix generation

### Classification Accuracy Regression Tests

Maintain a golden dataset of emails with known-correct classifications. On each prompt change or model upgrade, run the dataset through the classifier and compare results to the golden labels. Report any regressions.

> **Ref:** LangChain agents-from-scratch emphasizes maintaining evaluation datasets and running
> systematic evals. Prompt changes improving one category often regress another.
> https://github.com/langchain-ai/agents-from-scratch (Module 5: evaluation)

### Manual Testing
- Bootstrap against real mailbox (developer's own)
- Dry-run report review (including confusion matrix output)
- Full triage cycle with review UI walkthrough
- Sender management page walkthrough
- Stats/accuracy page validation against known correction data

