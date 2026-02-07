# Phase 4 — Advanced (Future/Aspirational)

> **Prerequisites:** Phases 1-3 fully operational with proven accuracy and trust.
> **Theme:** Power features that extend the system beyond email triage into an intelligent email assistant.
> **Status:** Aspirational. These features may or may not be implemented. This document captures the vision and rough architecture to guide future thinking.

---

## Overview

Phases 1-3 build a complete autonomous email triage system: scan, classify, learn, act. Phase 4 moves beyond classification into active email assistance — answering questions about email content, drafting follow-up messages, managing multiple accounts, and supporting team deployments.

These features represent a significant step up in complexity and scope. Each one should be evaluated independently on whether it provides enough value to justify the implementation cost.

---

## Feature Summary

| # | Feature | Complexity | Value | Risk |
|---|---------|-----------|-------|------|
| 4A | Natural Language Queries | High | High | Prompt engineering, accuracy |
| 4B | Smart Follow-Up Drafting | Medium | High | Safety (sending on behalf of user) |
| 4C | Multi-Account Support | High | Medium | Architecture (parallel auth, unified DB) |
| 4D | Team Deployment | Very High | Medium | Shared state, permissions, conflicts |
| 4E | Outlook Desktop Deep Links | Low | Low | Platform-specific, fragile |

---

## 4A: Natural Language Queries

### Vision

Allow the user to ask questions about their email in natural language:

```
> "What's the status of the Tradecore project?"
> "When did I last hear from Sarah about the SOC 2 audit?"
> "How many unresolved support tickets do we have this week?"
> "Summarize the conversation with legal about the contract renewal"
```

### Architectural Approach

**RAG (Retrieval Augmented Generation) over local email data:**

1. **Query understanding**: Claude parses the natural language query into a structured search (folder filter, sender filter, date range, keywords)
2. **Retrieval**: Search the local `emails` table using the structured query. SQLite FTS5 (full-text search) on `subject` and `snippet` columns for keyword matching
3. **Augmentation**: Collect matching emails (up to 20) with their classifications, suggestions, and thread context
4. **Generation**: Send retrieved emails + original query to Claude. Claude generates a natural language answer with citations (email subject, sender, date)

### Key Technical Decisions

**SQLite FTS5 for search**: Add a virtual table for full-text search on email subject and snippet. This avoids the need for an external search engine while supporting efficient keyword and phrase queries.

```sql
CREATE VIRTUAL TABLE emails_fts USING fts5(
    subject, snippet,
    content='emails',
    content_rowid='rowid'
);
```

**Model choice**: Use Sonnet for query understanding and answer generation (needs reasoning ability). Haiku is insufficient for complex question answering.

**Scope limitation**: Queries can only access data in the local database. The system does not fetch additional emails from Graph API to answer queries — this keeps latency low and avoids unbounded API calls.

### Interface Options

- **CLI**: `python -m assistant query "What's the status of Tradecore?"`
- **Web UI**: New `/query` page with a text input and conversation-style response area
- **Both**: Start with CLI, add web UI later

### Files to Create/Modify

| File | Change |
|------|--------|
| `engine/query.py` | New — query parsing, retrieval, answer generation |
| `db/models.py` | Add FTS5 virtual table |
| `db/store.py` | Add full-text search methods |
| `classifier/query_prompts.py` | New — query understanding and answer generation prompts |
| `cli.py` | Add `query` command |
| `web/routes.py` | Add `/query` page and API endpoint |
| `web/templates/query.html` | New — query interface |

### Open Questions

- How to handle queries that span multiple projects? ("Compare activity across all projects this week")
- Should the system remember previous queries for follow-up? ("Tell me more about the second one")
- How to handle time-sensitive queries? ("What's urgent right now?" vs "What was urgent last week?")

### Rough Cost Estimate

Each query would use ~2,000-5,000 input tokens (retrieved emails) + ~500-1,000 output tokens (answer). At Sonnet pricing, roughly $0.02-0.05 per query. Acceptable for occasional use, expensive for frequent querying.

---

## 4B: Smart Follow-Up Drafting

### Vision

The system identifies emails that need follow-up (overdue "Waiting For" items, aging "Needs Reply" items) and offers to draft a follow-up message:

```
OVERDUE: "SOC 2 evidence request" — waiting on Sarah for 72 hours
[Draft Follow-Up]

Draft:
  To: sarah@translution.com
  Subject: Re: SOC 2 evidence request
  Body: Hi Sarah,

  Just following up on the SOC 2 evidence package request from last week.
  We're approaching the audit deadline and need the following items:
  - Access control documentation
  - Change management logs
  ...

  [Send] [Edit] [Discard]
```

### Architectural Approach

1. **Context assembly**: Fetch the full thread (via local DB + Graph API) for the email needing follow-up
2. **Draft generation**: Send thread context + follow-up instruction to Claude Sonnet
3. **User review**: Display draft in web UI for editing before sending
4. **Send**: On approval, send via Graph API `POST /me/sendMail`

### Safety Boundaries

This is the most safety-critical feature in the entire project. The agent would be composing messages on behalf of the user.

**Hard rules:**
- **NEVER send without explicit user approval** — the "Send" button is the only path
- **NEVER auto-draft** — drafts are only generated on user request
- **Always show full draft** including To, Subject, and Body before sending
- **Always include "Draft generated by Outlook Assistant" footer** for transparency
- **Log every sent message** to action_log with full content

### Model Choice

Use Sonnet for draft generation — Haiku is not reliable enough for composing business emails that will be sent externally.

### Prerequisites

Phase 3 Feature 3F (Email Delivery for Digests) must be implemented first — it provides the `send_message()` method in `graph/messages.py` that this feature reuses.

### Files to Create/Modify

| File | Change |
|------|--------|
| `engine/drafting.py` | New — draft generation logic |
| `classifier/prompts.py` | Add drafting prompt constants (following established pattern) |
| `graph/messages.py` | Add `create_draft()` method (reuse `send_message()` from Phase 3 Feature 3F) |
| `web/routes.py` | Add draft generation and send endpoints |
| `web/templates/draft.html` | New — draft review/edit interface |

### Open Questions

- Should drafts be saved as Outlook drafts (Graph API `POST /me/messages`) rather than sent directly? This would let the user review in Outlook's full editor
- How to handle reply-all vs reply-to situations?
- Should the system learn the user's writing style from sent items?

---

## 4C: Multi-Account Support

### Vision

Support managing multiple Outlook accounts from a single instance — useful for a CEO who manages both a company account and a personal account, or who has separate accounts for different business entities.

### Architectural Approach

**Per-account isolation with unified UI:**

```yaml
accounts:
  - name: "Translution"
    auth:
      client_id: "..."
      tenant_id: "..."
    config: "config_translution.yaml"
    database: "data/translution.db"

  - name: "Personal"
    auth:
      client_id: "..."
      tenant_id: "..."
    config: "config_personal.yaml"
    database: "data/personal.db"
```

Each account has:
- Separate MSAL auth session
- Separate config.yaml (different projects, areas, rules)
- Separate SQLite database (no data mixing)
- Separate triage scheduler

The web UI provides a unified view with an account selector.

### Key Technical Decisions

- **Separate databases**: Simplest isolation model. No risk of data cross-contamination. Each DB can have different schemas if accounts are at different phases.
- **Separate configs**: Each account has its own taxonomy. A project in one account doesn't exist in another.
- **Shared scheduler**: Single APScheduler instance with per-account jobs (e.g., `triage_translution`, `triage_personal`).
- **Unified web UI**: Single FastAPI instance with account context in routes (e.g., `/translution/review`, `/personal/review` or account selector in header).

### Scope of Changes

This is a significant architectural change:
- `CLIDeps` becomes per-account
- `app.state` stores multiple dep sets
- All routes need account context
- Dashboard aggregates across accounts
- CLI commands need `--account` flag

### Open Questions

- Should triage engines run in parallel or sequential (to avoid rate limit conflicts)?
- How to handle shared contacts across accounts?
- Should auto-rules be per-account or global?

---

## 4D: Team Deployment

### Vision

Deploy the assistant for a small team (5-10 people), with shared taxonomy but per-user preferences and auto-rules.

### Architectural Approach

**Shared taxonomy, per-user state:**

```
Shared (team-level):
  - Projects and areas taxonomy
  - Key contacts list
  - Base auto-rules

Per-user:
  - MSAL auth session
  - Classification preferences (learned from their corrections)
  - Personal auto-rules
  - Suggestion queue
  - Waiting-for items
```

### Key Technical Decisions

- **PostgreSQL instead of SQLite**: Team deployment needs concurrent multi-user write access. SQLite WAL mode handles one writer; PostgreSQL handles many.
- **API server**: The web UI becomes a shared server (not localhost). Needs authentication (team SSO or simple password).
- **Config hierarchy**: Team config → user overrides. Team admin manages shared taxonomy; users manage their own rules and preferences.

### Scope of Changes

This is essentially a rewrite of the data layer and web layer:
- Database migration from SQLite to PostgreSQL
- User authentication and sessions for the web UI
- Per-user data isolation in queries
- Config hierarchy (team + user overrides)
- Admin UI for team taxonomy management

### Recommendation

This feature is the highest complexity in the entire project and should only be considered if there is genuine team demand. The single-user version (Phases 1-3) delivers 90% of the value with 20% of the complexity.

---

## 4E: Outlook Desktop Deep Links

### Vision

In addition to OWA (Outlook Web App) links, provide direct links that open the email in the Outlook desktop client.

### Architectural Approach

Outlook desktop deep links use the `outlook://` protocol handler:

```
outlook://localhost/MailFolder/{folderEntryId}/MailItem/{itemEntryId}
```

The challenge is that entry IDs differ between MAPI (desktop) and REST (Graph API). The Graph API provides `webLink` (for OWA) but not MAPI entry IDs.

### Options

1. **EWS (Exchange Web Services)**: Can convert between REST IDs and MAPI IDs. But EWS is being deprecated by Microsoft.
2. **Graph API convertId**: `POST /me/translateExchangeIds` converts between REST and MAPI formats.
3. **Web link only**: Continue using OWA links (current approach). Works cross-platform, no desktop dependency.

### Recommendation

OWA links (already implemented) work for most users. Desktop deep links add marginal value with significant complexity and platform fragility. Implement only if users specifically request it.

### Implementation Sketch (If Needed)

```python
async def get_desktop_deep_link(self, message_id: str) -> str:
    """Convert Graph API message ID to Outlook desktop deep link.

    Uses POST /me/translateExchangeIds to convert from REST to MAPI format.
    """
    response = await self._graph_client.post(
        "/me/translateExchangeIds",
        json={
            "inputIds": [message_id],
            "sourceIdType": "restId",
            "targetIdType": "entryId",
        },
    )
    entry_id = response["value"][0]["targetId"]
    return f"outlook://localhost/MailItem/{entry_id}"
```

---

## Principles Alignment

Even for aspirational features, the project's principles guide design decisions:

| Principle | Application |
|-----------|------------|
| **YAGNI** | Each feature is evaluated independently. No "might need it" implementations. Multi-account and team deployment are deferred until real demand exists. |
| **Build on foundation** | Natural language queries reuse the existing DB and prompt patterns. Draft generation reuses the Graph API client. No parallel systems. |
| **Rule of Separation** | Query understanding (what to search) is separate from retrieval (how to search) and generation (how to answer). Draft policy (when to offer) is separate from draft mechanism (how to generate). |
| **Safety first** | Follow-up drafting has the strongest safety constraints in the project. Never send without approval. Always log. Always transparent. |
| **Proven patterns** | FTS5 for search (SQLite standard), RAG for Q&A (industry standard), OAuth2 for multi-account auth (framework standard). No novel architectures. |

---

## Decision Framework

Before implementing any Phase 4 feature, answer these questions:

1. **Is there real demand?** Has the user actually needed this, or is it theoretical?
2. **Does it justify the complexity?** Can the user achieve 80% of the value manually with less effort?
3. **Does it compromise existing reliability?** Would adding this feature make the core triage loop less stable?
4. **Is the cost proportional?** API costs, development time, maintenance burden — all proportional to value delivered?

If any answer is "no" or "unclear," defer the feature. The system's value comes from reliable triage, not from feature count.
