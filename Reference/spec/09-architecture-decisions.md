# Architecture Decisions: Infrastructure & Deployment Strategy

**Version:** 1.0 | **Last Updated:** 2026-02-07

> **Purpose:** This document records the outcome of a deliberate architecture review comparing Docker-local deployment against Azure cloud hosting. It documents what was decided, why, and — critically — what changes this introduces to the Phase 2 and Phase 3 plans in the existing specifications. Claude Code should treat this as authoritative guidance that supersedes conflicting details in other spec documents.

---

## 1. Decision Summary

| Question | Decision |
|----------|----------|
| Should we move to Azure hosting? | **No — stay Docker-local through Phase 3** |
| Should we use webhooks for near-real-time email? | **No — use aggressive delta query polling instead** |
| Does the current architecture block Phase 4 team deployment? | **No — the Dockerfile is the migration vehicle** |
| Do we need to change the database? | **No — SQLite stays, but isolate the data access layer** |

---

## 2. Analysis: Why Docker-Local Is Correct

### 2.1 SQLite Is Incompatible with Azure Container Hosting

This is the most important technical finding. Every Azure container hosting option (Container Apps, App Service, Container Instances) uses Azure Files (SMB/CIFS) for persistent storage. SQLite requires file-level locking that SMB does not support reliably. Microsoft's own documentation explicitly warns:

> "It's not recommended to use storage mounts for local databases such as SQLite, or for any other applications and components that rely on file handles and locks."

Real-world results confirm this: developers consistently hit `SQLITE_BUSY: database is locked` errors when running SQLite on Azure Files, even with WAL mode enabled. The recommended Azure fix is "migrate to PostgreSQL" — which adds $13+/month, a managed service dependency, connection string management, and a new failure mode. For a single-user agent, this is pure overhead.

**Running locally means SQLite just works.** WAL mode handles concurrent access from the triage engine and FastAPI review UI without issue.

### 2.2 Webhooks Require a Public HTTPS Endpoint — Polling Does Not

Microsoft Graph webhooks require a publicly accessible HTTPS endpoint for notification delivery and subscription validation. For a Docker container on a local machine, the options are:

- **Tunnel service (ngrok, Cloudflare Tunnel):** Adds a runtime dependency, introduces fragility, may have rate limits or cost
- **Azure Event Hubs:** Works well (Microsoft recommends it for this exact scenario), but adds Azure infrastructure (~$11/month for Basic tier + setup complexity for namespace, RBAC, Key Vault)
- **Reverse proxy with public IP:** Requires network infrastructure, TLS certificate management, firewall rules

None of these are justified for processing ~100 emails/day from a single mailbox. **Aggressive delta query polling achieves near-real-time without any of this infrastructure.**

### 2.3 Delegated Permissions Are More Secure for Single-User

The device code flow with delegated permissions (`Mail.ReadWrite`, `Mail.Send`, `MailboxSettings.Read`, `User.Read`) inherently scopes access to one mailbox. Moving to Azure would push toward client credentials with application permissions, which grants tenant-wide access to every mailbox in the 50-person org unless explicitly restricted via Exchange Online RBAC for Applications. The current approach is simpler and more secure.

### 2.4 Cost Is Effectively Zero Locally

The Docker container uses negligible compute on a machine that's already running. The only operating cost is Claude API usage (~$0.10–0.30/day). Moving to Azure would add:

- Azure Container Apps (always-on idle): ~$5–15/month
- Azure Database for PostgreSQL (if replacing SQLite): ~$13+/month
- Azure Event Hubs (if using webhooks): ~$11/month
- Azure Container Registry: ~$5/month

Total: ~$35–45/month for zero functional benefit in Phase 1–3.

---

## 3. Phase 4 Migration Path (When Team Deployment Requires It)

The Docker-local architecture does **not** create walls for Phase 4. When team deployment is needed, the migration is bounded and incremental:

| Component | Phase 1–3 (Local) | Phase 4 (Azure) |
|-----------|-------------------|-----------------|
| Compute | Docker Desktop | Azure Container Apps (Consumption plan) |
| Database | SQLite with WAL | Azure Database for PostgreSQL Flexible Server |
| Auth | Device code flow (delegated) | Client credentials (application) + Exchange RBAC scoping |
| Email notifications | Delta query polling | Azure Event Hubs + delta queries |
| Container registry | Local build | Azure Container Registry |

**The existing Dockerfile is the migration vehicle.** The same image that runs locally can be pushed to Azure Container Registry and deployed to Container Apps. The docker-compose.yaml maps almost directly to Container Apps configuration.

**Estimated Phase 4 Azure cost:** ~$30–50/month for a small team (Container Apps + PostgreSQL Flexible Server burstable B1ms + Event Hubs Basic).

---

## 4. Changes to Phase 2 Specification

These changes supersede the webhook-related items in `01-overview.md` Phase 2 and `05-graph-api.md` Section 8.

### 4.1 REMOVED: Webhook + Delta Query Hybrid (Original Phase 2, Item 8)

The original spec called for:

> "Webhook + delta query hybrid for near-real-time processing"

With webhook subscription management, lifecycle notification URLs, public HTTPS endpoint handling, and subscription renewal every 2 days.

**This is removed from Phase 2.** Webhooks add significant infrastructure complexity (public endpoint, subscription renewal, lifecycle events, validation handling) for a marginal latency improvement on a single mailbox. The implementation sequence in the architecture audit document — "(1) delta queries, (2) webhook subscriptions, (3) wire webhooks to trigger delta queries, (4) reduce polling to 6-hour fallback" — assumed an eventual cloud deployment.

### 4.2 ADDED: Aggressive Delta Query Polling (Replaces Webhooks)

Instead of webhooks, Phase 2 upgrades the existing polling mechanism:

**Current (Phase 1):** Full message list polling every 15 minutes via `GET /me/mailFolders('Inbox')/messages`

**Phase 2 upgrade:**

1. **Implement delta queries** as a drop-in replacement for full-list polling. Use `GET /me/mailFolders('Inbox')/messages/delta?$select=subject,from,receivedDateTime,categories` and persist the `deltaLink` in `agent_state`.
2. **Reduce polling interval to 5 minutes.** Delta queries are extremely lightweight — they return only changes since the last sync. At ~100 emails/day, most delta queries will return zero results and consume negligible API quota.
3. **Keep APScheduler** as the trigger mechanism (no webhook infrastructure needed).
4. **Handle 410 Gone** (delta token expiry) as already specified: log warning, fall back to timestamp-based query, re-establish delta token on next cycle.
5. **Handle deduplication** as already specified: collect all message IDs from delta response, deduplicate by `id`, check for folder moves on existing messages.

**Latency impact:** Worst case 5 minutes vs. "seconds" with webhooks. For a CEO reviewing email in batches, this is imperceptible. The Graph API rate limit of 10,000 requests per 10 minutes makes 5-minute polling entirely safe.

**API efficiency:** Delta queries with an active token typically return responses in <100ms with payloads under 1KB when there are no changes. This is dramatically more efficient than the current full-list polling.

### 4.3 UNCHANGED: All Other Phase 2 Items

The following Phase 2 items are unaffected by this architecture decision:

1. Waiting-for tracker (automatic detection + manual marking)
2. Daily digest generation
3. Learning from corrections via classification preferences memory
4. Delta queries for efficient inbox polling ← **now the primary near-real-time mechanism, not just an efficiency upgrade**
5. Confidence calibration
6. Sender affinity auto-rules
7. ~~Token cache encryption at rest~~ → **REMOVED (file permissions mode 600 sufficient for Docker-local)**
8. ~~Webhook + delta query hybrid~~ → **REMOVED (replaced by item 4 with 5-minute polling)**
9. Auto-rules hygiene
10. Suggestion queue management
11. Stats & accuracy dashboard (`/stats`)
12. Sender management page (`/senders`)
13. Graceful degradation

### 4.4 Updated Phase 2 Item Numbering

For clarity, the revised Phase 2 build list is:

1. **Delta queries with 5-minute polling interval** (replaces both the "delta queries for efficiency" item and the "webhook hybrid" item — these are now one feature)
2. Learning from corrections via classification preferences memory
3. Suggestion queue management (auto-expire + auto-approve)
4. Sender affinity auto-rules
5. Waiting-for tracker enhancement
6. Daily digest generation
7. Auto-rules hygiene
8. Stats & accuracy dashboard (`/stats`)
9. Confidence calibration (integrates into stats dashboard)
10. Sender management page (`/senders`)
11. Enhanced graceful degradation

---

## 5. Changes to Phase 3 Specification

### 5.1 No Infrastructure Changes

Phase 3 (autonomous mode, new project detection, auto-archive, weekly reports, email digest delivery) is entirely unaffected by this architecture decision. All Phase 3 features operate within the existing Docker-local architecture:

- **Autonomous mode** is a behaviour change in the triage engine (confidence threshold → auto-execute), not an infrastructure change
- **New project detection** is a classifier enhancement
- **Auto-archive** operates on existing Graph API folder operations
- **Weekly review report** extends the digest generation logic
- **Email digest delivery** uses `Mail.Send` permission already granted

### 5.2 Email Digest Delivery Note

The Phase 3 feature "email digest delivery" (sending the digest to the user's own inbox) works perfectly with the local architecture using the existing `Mail.Send` delegated permission. No SMTP server or external email service is needed — the Graph API handles delivery.

---

## 6. Changes to `05-graph-api.md`

### 6.1 Section 8 ("Webhook + Delta Query Hybrid") — Reclassified

Section 8 of `05-graph-api.md` should be understood as **Phase 4 reference material**, not Phase 2 implementation guidance. The webhook subscription management details (subscription creation, renewal, lifecycle notifications, validation handling) remain technically accurate and will be relevant when/if the service moves to Azure for team deployment. But they are not part of the Phase 2 build plan.

### 6.2 Delta Query Implementation (Section 7) — Now Primary

Section 7 of `05-graph-api.md` ("Delta Query Polling") becomes the primary near-real-time mechanism. The only change from what's already specified:

- **Polling interval:** Change from the Phase 1 default of 15 minutes to **5 minutes** once delta queries are implemented
- **No new config fields needed:** The existing `triage.interval_minutes` field is sufficient — just change the default to 5

Everything else in Section 7 (delta token persistence, 410 Gone handling, deduplication, pagination) is implemented exactly as specified.

---

## 7. Database Abstraction Requirement

To ensure Phase 4 migration remains a bounded task, **all database operations must go through `store.py`** (or the equivalent data access module). This is the only file that should import `aiosqlite`.

**Guideline for Claude Code:** When implementing any feature that reads from or writes to the database, route it through the store module. Do not import `aiosqlite` or execute SQL directly in triage engine, classifier, review UI, or any other module. This ensures that swapping to `asyncpg` (PostgreSQL) for Phase 4 is a single-module change, not a codebase-wide refactor.

Current store module responsibilities:
- All CRUD operations on `emails`, `suggestions`, `sender_profiles`, `auto_rules`, `agent_state`, `llm_request_log`
- Delta token persistence and retrieval
- Migration execution
- WAL mode initialization

Phase 4 addition (future, not now):
- Connection pooling via `asyncpg`
- PostgreSQL-compatible SQL dialect (minor differences from SQLite)
- Connection string from environment variable instead of file path

---

## 8. Configuration Impact

### 8.1 New/Modified Config Values for Phase 2

```yaml
triage:
  # Phase 1 default: 15 minutes (full-list polling)
  # Phase 2 default: 5 minutes (delta query polling)
  interval_minutes: 5
```

No `use_delta_queries` flag is needed — delta queries are used automatically when a delta token is available in `agent_state`, falling back to timestamp polling otherwise. This keeps the config surface minimal.

### 8.2 Removed Config Values

The following config values mentioned or implied in earlier specifications are **not needed**:

- `webhook.*` — no public endpoint, no webhook subscriptions (removed from Phase 2)
- `auth.encrypt_token_cache` — file permissions mode 600 sufficient for Docker-local (removed from Phase 2)

---

## 9. Summary of Spec Document Impact

| Document | Impact | Details |
|----------|--------|---------|
| `01-overview.md` | Phase 2 item list updated | Item 8 (webhooks) removed, item 4 (delta queries) expanded to include near-real-time polling |
| `02-config-and-schema.md` | Minor config additions | Updated `interval_minutes` default guidance |
| `03-agent-behaviors.md` | No changes | Triage engine behaviour unchanged — it's triggered by scheduler regardless of polling mechanism |
| `04-prompts.md` | No changes | Classification prompts are infrastructure-agnostic |
| `05-graph-api.md` | Section 8 reclassified as Phase 4 reference | Delta queries (Section 7) become the primary near-real-time mechanism |
| `06-safety-and-testing.md` | No changes | Safety boundaries and testing strategy unaffected |
| `07-setup-guide.md` | No changes | Docker setup remains as specified |
| `08-classification-chat.md` | No changes | Classification logic unaffected |

---

## 10. Decision Rationale Summary

The core insight is that **infrastructure complexity should be proportional to user count**. For a single-user agent processing ~100 emails/day:

- SQLite > PostgreSQL (zero config, no external service, no connection management)
- Local Docker > Azure Container Apps (zero cost, no deployment pipeline, no Azure subscription management)
- Delta query polling > Webhooks (zero public infrastructure, 5-minute latency is imperceptible, dramatically simpler implementation)
- Delegated permissions > Application permissions (inherently scoped, no Exchange RBAC configuration)

Every one of these decisions reverses naturally when the user count grows beyond one. The architecture is designed so that reversal is a bounded migration task, not a rewrite.
