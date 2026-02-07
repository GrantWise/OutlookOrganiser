# Outlook AI Assistant — What's Coming Next

Phase 1 gives you the foundation: the assistant scans your mailbox, learns your organisational structure, and begins classifying incoming email with your review and approval. Here's what comes after that.

---

## Phase 2 — Smarter, Faster, More Aware

Phase 2 focuses on three things: processing your email faster, learning from your feedback to get more accurate over time, and giving you better visibility into what's happening across your inbox.

### ⚡ Near-Real-Time Email Processing

Instead of checking for new mail every 15 minutes, the assistant will be notified the moment a new email arrives in your inbox. Classification and suggestions will appear within seconds of receiving a message, not minutes. The periodic check stays in place as a safety net so nothing slips through.

### 🧠 Learning from Your Corrections

Every time you correct a classification — changing a folder, adjusting a priority, or overriding an action type — the assistant remembers. It builds up a set of learned preferences that influence future classifications. Over time, the suggestions become more closely aligned with how you actually think about your email.

- Corrections are analysed in batches to identify patterns
- Preferences are described in plain language and fed back into the classification process
- Example: "Emails mentioning infrastructure monitoring should go to Development, even when they mention security"

### ⏳ Waiting-For Tracker

The assistant will track threads where you're waiting for someone to get back to you. It detects these automatically when your last message in a thread was a question or request, and you can also mark threads manually. You'll be reminded in your daily digest when responses are overdue, with escalating urgency.

- Automatic detection based on your sent messages and reply state
- Configurable nudge threshold (default: 48 hours) and escalation threshold (default: 96 hours)
- Dedicated page in the web interface to see all pending items at a glance
- Automatically resolved when a reply comes in

### 📬 Daily Digest

Each morning you'll receive a concise summary of what needs your attention. Overdue replies are listed first, followed by stalled waiting-for items, yesterday's activity stats, and any suggestions still awaiting your review. Designed to be scanned in under a minute.

- Scheduled at a time you choose (default: 8:00 AM)
- Delivered to your console, saved as a file, or emailed to you directly
- Includes failed classifications that need manual review

### 👤 Sender Management

A new page in the web interface lets you manage senders rather than individual emails. See everyone who emails you, how often, and where their mail typically gets classified. The assistant will flag senders whose email goes to the same folder more than 90% of the time — for these, you can create a one-click routing rule that skips AI classification entirely.

- Sort senders by frequency, last contact, or category
- Change a sender's default category (client, vendor, newsletter, automated, etc.)
- Create auto-routing rules directly from the sender list

### 📊 Stats & Accuracy Dashboard

A dedicated page showing how well the assistant is performing. See your approval rate over time, which folders and priorities get corrected most often, and how confident the AI's predictions actually are versus how often you agree with them. Includes cost tracking so you can see exactly what the AI classification is costing per day.

### 🛡️ Resilience & Housekeeping

Behind the scenes, Phase 2 adds several reliability improvements to keep things running smoothly.

- **Graceful degradation:** If the AI service goes down, the assistant continues routing emails using your existing rules. Anything it can't classify is queued and processed once service is restored.
- **Suggestion expiry:** Old pending suggestions that you haven't reviewed are automatically expired after 14 days so the review queue doesn't grow stale.
- **Rule hygiene:** As routing rules accumulate, the assistant watches for conflicts, stale rules that no longer match anything, and suggests consolidation opportunities.
- **Token encryption:** Your authentication tokens are encrypted at rest for added security.

---

## Phase 3 — Hands-Off When You Trust It

Once you're confident the assistant understands your preferences, Phase 3 lets you graduate from reviewing every suggestion to letting it act on your behalf — with guardrails you control.

### 🤖 Autonomous Mode

A new operating mode where the assistant moves emails into the correct folders and applies categories automatically — no approval needed — when it's confident enough in its classification. You set the confidence threshold. Anything below the threshold still goes to your review queue as before.

- Toggle autonomous mode on or off at any time from the config
- Set your own confidence threshold (e.g., "only auto-move when 95%+ confident")
- Every autonomous action is logged and fully reversible via the undo command
- The review queue still shows what was auto-processed so you can spot-check

### 🆕 New Project Detection

When the assistant notices a cluster of emails that don't fit any existing project or area — a new client engagement, a new initiative, a new vendor relationship — it will suggest creating a new project folder. You review the suggestion and the proposed name, signals, and keywords before anything is created.

### 📦 Automatic Archival

When a project goes quiet — no new emails for a configurable period — the assistant will suggest moving the project folder to your Archive. Keeps your active folder list clean and focused on what's current.

### 📋 Weekly Review Report

A deeper analysis than the daily digest, delivered once a week. Covers trends across projects, sender activity, how your time allocation across areas has shifted, and highlights threads that may have fallen through the cracks. Think of it as a weekly executive briefing on your inbox.

### ✉️ Email Digest Delivery

Your daily and weekly digests can be delivered directly to your inbox as a formatted email, so you can read them on any device without opening the web interface.

---

## Phase 4 — On the Horizon

Longer-term capabilities we're exploring. These are not yet committed to a timeline but represent where the product is headed.

### 💬 Ask Your Inbox

Ask natural language questions like "What's the latest on the Tradecore project?" or "Have we heard back from legal?" and get an instant, accurate answer drawn from your email history.

### ✍️ Smart Follow-Up Drafting

Tell the assistant "Draft a follow-up to the SOC 2 evidence request" and it will compose a contextually appropriate message based on the thread history, ready for your review before sending.

### 🔀 Multi-Account Support

Manage more than one email account through a single assistant — useful if you operate across multiple business entities or have separate work and personal mailboxes.

### 👥 Team Deployment

Share a common organisational taxonomy across a team while maintaining individual preferences and classification rules. Everyone benefits from the same folder structure; everyone gets personalised routing.

---

*Each phase builds on the one before it. Phase 1 must be operational and stable before Phase 2 features are introduced. Features within a phase may be delivered incrementally.*
