"""Two-pass bootstrap scanner for initial taxonomy discovery.

Scans the user's mailbox, discovers projects/areas/sender patterns via
Claude Sonnet, and generates a proposed config.yaml.

Pass 1: Batch analysis — emails grouped into batches of 50, each sent to
    Claude Sonnet for pattern analysis (projects, areas, sender clusters).
Pass 2: Consolidation — all batch results merged into a single unified
    taxonomy via a final Claude Sonnet call.

Spec reference: Reference/spec/03-agent-behaviors.md Section 1,
                Reference/spec/04-prompts.md Sections 1-2

Usage:
    from assistant.engine.bootstrap import BootstrapEngine

    engine = BootstrapEngine(
        anthropic_client=client,
        message_manager=msg_mgr,
        folder_manager=folder_mgr,
        store=db_store,
        snippet_cleaner=cleaner,
        config=app_config,
    )
    stats = await engine.run(days=90, force=False)
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import anthropic
import yaml
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

from assistant.classifier.bootstrap_prompts import (
    build_batch_analysis_prompt,
    build_consolidation_prompt,
    format_email_for_batch,
    get_yaml_retry_message,
    parse_batch_yaml_response,
    parse_consolidated_yaml_response,
)
from assistant.classifier.snippet import SnippetCleaner
from assistant.core.errors import ClassificationError
from assistant.core.logging import get_logger
from assistant.db.store import DatabaseStore, Email

if TYPE_CHECKING:
    from assistant.config_schema import AppConfig
    from assistant.graph.folders import FolderManager
    from assistant.graph.messages import MessageManager

logger = get_logger(__name__)

# Default batch size for Pass 1 analysis
BATCH_SIZE = 50

# Delay between Graph API page requests (seconds) to avoid rate limits
PAGE_FETCH_DELAY = 0.1

# Max tokens for Claude YAML responses
BOOTSTRAP_MAX_TOKENS = 4096

# Default proposed config output path
PROPOSED_CONFIG_PATH = Path("config/config.yaml.proposed")

# Safety limit for bootstrap email fetch to bound time and memory.
# At 50 per page with 0.1s delay, 10000 emails = 200 pages = ~20s max.
MAX_BOOTSTRAP_EMAILS = 10000


@dataclass
class BootstrapStats:
    """Statistics from a bootstrap run for console output."""

    total_emails_fetched: int = 0
    total_batches: int = 0
    batches_succeeded: int = 0
    batches_failed: int = 0
    projects_discovered: int = 0
    areas_discovered: int = 0
    auto_rules_generated: int = 0
    senders_profiled: int = 0
    auto_rule_candidates: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    duration_seconds: float = 0.0


class BootstrapEngine:
    """Two-pass bootstrap scanner for initial taxonomy discovery.

    Orchestrates email fetching, batch analysis, consolidation,
    config file writing, and sender profile population.

    Attributes:
        _client: Anthropic API client (configured with max_retries=3)
        _message_manager: MessageManager for email fetching
        _folder_manager: FolderManager for folder path resolution
        _store: DatabaseStore for persistence
        _snippet_cleaner: SnippetCleaner for cleaning email bodies
        _config: Application configuration
        _console: Rich console for output
    """

    def __init__(
        self,
        anthropic_client: anthropic.Anthropic,
        message_manager: MessageManager,
        folder_manager: FolderManager,
        store: DatabaseStore,
        snippet_cleaner: SnippetCleaner,
        config: AppConfig,
        console: Console | None = None,
    ):
        self._client = anthropic_client
        self._message_manager = message_manager
        self._folder_manager = folder_manager
        self._store = store
        self._snippet_cleaner = snippet_cleaner
        self._config = config
        self._console = console or Console()

    async def run(
        self,
        days: int = 90,
        force: bool = False,
    ) -> BootstrapStats:
        """Execute the full bootstrap process.

        Args:
            days: Number of days of email to analyze
            force: Skip confirmation prompts

        Returns:
            BootstrapStats with counts and timing

        Raises:
            ClassificationError: If Pass 2 consolidation fails
            SystemExit: If user declines at idempotency prompt
        """
        start_time = time.monotonic()
        stats = BootstrapStats()

        # 1. Idempotency checks
        await self._check_idempotency(force)

        # 2. Fetch emails from Graph API
        self._console.print(f"\n[bold]Fetching emails from last {days} days...[/bold]")
        raw_messages = self._fetch_emails(days)
        stats.total_emails_fetched = len(raw_messages)

        if not raw_messages:
            self._console.print("[yellow]No emails found in the specified period.[/yellow]")
            return stats

        self._console.print(f"  Found [cyan]{len(raw_messages)}[/cyan] emails")

        # 3. Transform to Email dataclasses
        emails = self._transform_emails(raw_messages)

        # 4. Batch-insert to database
        saved = await self._store.save_emails_batch(emails)
        logger.info("Bootstrap emails saved to database", count=saved)

        # 5. Pass 1: Batch analysis
        self._console.print("\n[bold]Pass 1: Analyzing email patterns...[/bold]")
        batch_results = await self._run_pass1(emails, stats)

        if not batch_results:
            self._console.print(
                "[red]All batches failed analysis. Cannot proceed to consolidation.[/red]"
            )
            stats.duration_seconds = time.monotonic() - start_time
            return stats

        # 6. Pass 2: Consolidation
        self._console.print("\n[bold]Pass 2: Consolidating taxonomy...[/bold]")
        taxonomy = await self._run_pass2(batch_results, stats)

        # 7. Write proposed config
        proposed_path = self._write_proposed_config(taxonomy)

        # 7b. Update discovery counts from taxonomy
        stats.projects_discovered = len(taxonomy.get("projects", []))
        stats.areas_discovered = len(taxonomy.get("areas", []))
        stats.auto_rules_generated = len(taxonomy.get("auto_rules", []))

        # 8. Populate sender profiles
        senders_count = await self._populate_sender_profiles(emails, taxonomy, stats)
        stats.senders_profiled = senders_count

        # 9. Update agent state
        await self._store.set_state("last_bootstrap_run", datetime.now(UTC).isoformat())

        # 10. Optimize database after bulk operations
        await self._store.analyze()

        # 11. Print summary
        stats.duration_seconds = time.monotonic() - start_time
        self._print_summary(stats, proposed_path)

        return stats

    async def _check_idempotency(self, force: bool) -> None:
        """Check for existing proposed config and prior bootstrap runs.

        Args:
            force: If True, skip all prompts

        Raises:
            SystemExit: If user declines to proceed
        """
        import click

        if force:
            return

        # Check for existing proposed config
        if PROPOSED_CONFIG_PATH.exists():
            if not click.confirm(
                f"\n{PROPOSED_CONFIG_PATH} already exists. Overwrite?",
                default=False,
            ):
                self._console.print("[yellow]Cancelled.[/yellow]")
                raise SystemExit(0)

        # Check for prior bootstrap run
        last_run = await self._store.get_state("last_bootstrap_run")
        if last_run:
            if not click.confirm(
                f"\nBootstrap was last run on {last_run}. Run again?",
                default=False,
            ):
                self._console.print("[yellow]Cancelled.[/yellow]")
                raise SystemExit(0)

    def _fetch_emails(self, days: int) -> list[dict[str, Any]]:
        """Fetch emails from last N days via Graph API.

        Uses MessageManager.list_messages with date filter and pagination.
        Deduplicates by message ID to handle Graph API pagination overlaps.

        Args:
            days: Number of days to look back

        Returns:
            List of unique raw Graph API message dicts
        """
        cutoff = datetime.now(UTC) - timedelta(days=days)
        cutoff_str = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")

        filter_query = f"receivedDateTime ge {cutoff_str}"

        raw_messages = self._message_manager.list_messages(
            folder="Inbox",
            filter_query=filter_query,
            order_by="receivedDateTime desc",
            delay_between_pages=PAGE_FETCH_DELAY,
            max_items=MAX_BOOTSTRAP_EMAILS,
        )

        # Deduplicate by message ID
        seen_ids: set[str] = set()
        unique: list[dict[str, Any]] = []
        for msg in raw_messages:
            msg_id = msg.get("id", "")
            if msg_id and msg_id not in seen_ids:
                unique.append(msg)
                seen_ids.add(msg_id)

        dups = len(raw_messages) - len(unique)
        if dups:
            logger.info("Deduplicated fetched emails", duplicates_removed=dups)

        return unique

    def _transform_emails(
        self,
        raw_messages: list[dict[str, Any]],
    ) -> list[Email]:
        """Transform raw Graph API messages to Email dataclasses.

        Cleans snippets and extracts all metadata fields.

        Args:
            raw_messages: List of Graph API message dicts

        Returns:
            List of Email dataclasses
        """
        emails: list[Email] = []

        for msg in raw_messages:
            # Extract sender info
            from_data = msg.get("from", {}).get("emailAddress", {})
            sender_email = from_data.get("address", "")
            sender_name = from_data.get("name", "")

            # Clean the snippet
            body_preview = msg.get("bodyPreview", "")
            cleaned = self._snippet_cleaner.clean(body_preview, is_html=False)

            # Parse received datetime
            received_str = msg.get("receivedDateTime", "")
            received_at = None
            if received_str:
                try:
                    received_at = datetime.fromisoformat(received_str.replace("Z", "+00:00"))
                except ValueError:
                    logger.warning("Invalid receivedDateTime", value=received_str)

            # Extract flag status
            flag_data = msg.get("flag", {})
            flag_status = (
                flag_data.get("flagStatus", "notFlagged")
                if isinstance(flag_data, dict)
                else "notFlagged"
            )

            # Resolve folder path from ID
            parent_folder_id = msg.get("parentFolderId", "")
            current_folder = self._folder_manager.get_folder_path(parent_folder_id)

            email = Email(
                id=msg.get("id", ""),
                conversation_id=msg.get("conversationId"),
                conversation_index=msg.get("conversationIndex"),
                subject=msg.get("subject", ""),
                sender_email=sender_email,
                sender_name=sender_name,
                received_at=received_at,
                snippet=cleaned.cleaned_text,
                current_folder=current_folder,
                web_link=msg.get("webLink"),
                importance=msg.get("importance", "normal"),
                is_read=bool(msg.get("isRead", False)),
                flag_status=flag_status,
                classification_status="pending",
            )
            emails.append(email)

        return emails

    async def _run_pass1(
        self,
        emails: list[Email],
        stats: BootstrapStats,
    ) -> list[dict[str, Any]]:
        """Pass 1: Batch analysis with Claude Sonnet.

        Batches emails into groups of BATCH_SIZE, sends each to Claude
        for pattern analysis, and collects results.

        Args:
            emails: All emails to analyze
            stats: Stats object to update with token counts

        Returns:
            List of parsed batch result dicts
        """
        batches = self._build_batches(emails)
        stats.total_batches = len(batches)
        batch_results: list[dict[str, Any]] = []

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=self._console,
        ) as progress:
            task = progress.add_task("Analyzing batches...", total=len(batches))

            for i, batch in enumerate(batches, 1):
                progress.update(task, description=f"Analyzing batch {i}/{len(batches)}...")

                try:
                    result = await self._call_claude_for_batch(
                        batch=batch,
                        batch_number=i,
                        total_batches=len(batches),
                        stats=stats,
                    )
                    batch_results.append(result)
                    stats.batches_succeeded += 1
                except (ValueError, ClassificationError) as e:
                    stats.batches_failed += 1
                    logger.warning(
                        "Batch analysis failed, skipping",
                        batch_number=i,
                        error=str(e),
                    )

                progress.advance(task)

        self._console.print(
            f"  Analyzed [cyan]{stats.batches_succeeded}[/cyan]/{stats.total_batches} batches"
        )
        if stats.batches_failed > 0:
            self._console.print(
                f"  [yellow]{stats.batches_failed} batches failed (skipped)[/yellow]"
            )

        return batch_results

    def _build_batches(self, emails: list[Email]) -> list[list[Email]]:
        """Split emails into batches of BATCH_SIZE.

        Args:
            emails: All emails to batch

        Returns:
            List of email batches
        """
        return [emails[i : i + BATCH_SIZE] for i in range(0, len(emails), BATCH_SIZE)]

    async def _call_claude_with_yaml_retry(
        self,
        model: str,
        messages: list[dict[str, Any]],
        parse_fn: Callable[[str], dict[str, Any]],
        task_type: str,
        stats: BootstrapStats,
        error_context: str,
    ) -> dict[str, Any]:
        """Call Claude and parse the YAML response, retrying once on parse failure.

        Shared retry logic for both Pass 1 (batch analysis) and Pass 2
        (consolidation). On malformed YAML, appends the failed response and
        a corrective prompt, then retries once.

        Args:
            model: Claude model ID to use
            messages: Initial message list (mutated on retry)
            parse_fn: YAML parse function (batch or consolidated)
            task_type: For logging ("bootstrap_pass1" or "bootstrap_pass2")
            stats: Stats object to update with token counts
            error_context: Human-readable context for errors (e.g., "batch 3")

        Returns:
            Parsed YAML dict

        Raises:
            ClassificationError: If Claude API call fails or YAML parsing
                fails after retry
        """
        # First attempt
        start_time = time.monotonic()
        try:
            response = self._client.messages.create(
                model=model,
                max_tokens=BOOTSTRAP_MAX_TOKENS,
                messages=messages,
            )
        except (
            anthropic.RateLimitError,
            anthropic.APIConnectionError,
            anthropic.APIStatusError,
        ) as e:
            duration_ms = int((time.monotonic() - start_time) * 1000)
            await self._log_bootstrap_request(
                model=model,
                messages=messages,
                response=None,
                duration_ms=duration_ms,
                task_type=task_type,
                error=str(e),
            )
            raise ClassificationError(
                f"Claude API error during {error_context}: {e}",
                attempts=1,
            ) from e

        duration_ms = int((time.monotonic() - start_time) * 1000)
        raw_text = self._extract_text_response(response)
        self._update_token_stats(stats, response)

        # Try to parse the YAML response
        try:
            result = parse_fn(raw_text)
            await self._log_bootstrap_request(
                model=model,
                messages=messages,
                response=raw_text,
                duration_ms=duration_ms,
                task_type=task_type,
            )
            return result
        except ValueError as first_error:
            logger.warning(
                "YAML parse failed, retrying with corrective prompt",
                context=error_context,
                error=str(first_error),
            )

        # Retry with corrective prompt
        messages.append({"role": "assistant", "content": raw_text})
        messages.append({"role": "user", "content": get_yaml_retry_message()})

        start_time = time.monotonic()
        try:
            response = self._client.messages.create(
                model=model,
                max_tokens=BOOTSTRAP_MAX_TOKENS,
                messages=messages,
            )
        except (
            anthropic.RateLimitError,
            anthropic.APIConnectionError,
            anthropic.APIStatusError,
        ) as e:
            duration_ms = int((time.monotonic() - start_time) * 1000)
            await self._log_bootstrap_request(
                model=model,
                messages=messages,
                response=None,
                duration_ms=duration_ms,
                task_type=task_type,
                error=str(e),
            )
            raise ClassificationError(
                f"Claude API error during {error_context} retry: {e}",
                attempts=2,
            ) from e

        duration_ms = int((time.monotonic() - start_time) * 1000)
        raw_text = self._extract_text_response(response)
        self._update_token_stats(stats, response)

        try:
            result = parse_fn(raw_text)
        except ValueError as e:
            await self._log_bootstrap_request(
                model=model,
                messages=messages,
                response=raw_text,
                duration_ms=duration_ms,
                task_type=task_type,
                error=str(e),
            )
            raise ClassificationError(
                f"{error_context} failed after retry. YAML parse error: {e}. "
                "Re-run with --force to try again.",
                attempts=2,
            ) from e

        await self._log_bootstrap_request(
            model=model,
            messages=messages,
            response=raw_text,
            duration_ms=duration_ms,
            task_type=task_type,
        )
        return result

    async def _call_claude_for_batch(
        self,
        batch: list[Email],
        batch_number: int,
        total_batches: int,
        stats: BootstrapStats,
    ) -> dict[str, Any]:
        """Call Claude for a single batch and parse the YAML response.

        On malformed YAML, retries once with a corrective prompt.

        Args:
            batch: List of emails in this batch
            batch_number: Current batch number (1-indexed)
            total_batches: Total number of batches
            stats: Stats object to update with token counts

        Returns:
            Parsed batch analysis dict

        Raises:
            ClassificationError: If Claude API call or YAML parsing fails
        """
        # Format emails for the prompt
        email_texts = []
        for j, email in enumerate(batch, 1):
            email_text = format_email_for_batch(
                sender_name=email.sender_name or "",
                sender_email=email.sender_email or "",
                subject=email.subject or "(no subject)",
                received_date=email.received_at.isoformat() if email.received_at else "unknown",
                snippet=email.snippet or "",
                current_folder=email.current_folder,
            )
            email_texts.append(f"--- Email {j} ---\n{email_text}")

        email_batch_text = "\n\n".join(email_texts)
        prompt = build_batch_analysis_prompt(
            batch_number=batch_number,
            total_batches=total_batches,
            email_batch=email_batch_text,
        )

        return await self._call_claude_with_yaml_retry(
            model=self._config.models.bootstrap,
            messages=[{"role": "user", "content": prompt}],
            parse_fn=parse_batch_yaml_response,
            task_type="bootstrap_pass1",
            stats=stats,
            error_context=f"batch {batch_number}",
        )

    async def _run_pass2(
        self,
        batch_results: list[dict[str, Any]],
        stats: BootstrapStats,
    ) -> dict[str, Any]:
        """Pass 2: Consolidation with Claude Sonnet.

        Feeds all batch results into a single Claude call to merge
        duplicates, resolve conflicts, and produce unified taxonomy.

        Args:
            batch_results: List of parsed batch result dicts
            stats: Stats object to update

        Returns:
            Consolidated taxonomy dict

        Raises:
            ClassificationError: If consolidation fails after retry
        """
        # Serialize all batch results as YAML for the prompt
        batch_yaml_parts = []
        for i, result in enumerate(batch_results, 1):
            batch_yaml_parts.append(
                f"--- Batch {i} ---\n{yaml.dump(result, default_flow_style=False)}"
            )

        all_batch_results = "\n\n".join(batch_yaml_parts)
        prompt = build_consolidation_prompt(
            batch_count=len(batch_results),
            all_batch_results=all_batch_results,
        )

        return await self._call_claude_with_yaml_retry(
            model=self._config.models.bootstrap_merge,
            messages=[{"role": "user", "content": prompt}],
            parse_fn=parse_consolidated_yaml_response,
            task_type="bootstrap_pass2",
            stats=stats,
            error_context="consolidation",
        )

    def _write_proposed_config(
        self,
        taxonomy: dict[str, Any],
    ) -> Path:
        """Write config.yaml.proposed from consolidated taxonomy.

        Merges Claude's taxonomy with existing config structure,
        validates via Pydantic, and writes to disk.

        Args:
            taxonomy: Consolidated taxonomy dict from Pass 2

        Returns:
            Path to the written proposed config file
        """
        from assistant.config_schema import AppConfig

        # Build the proposed config dict by merging taxonomy with existing config
        proposed: dict[str, Any] = {
            "schema_version": 1,
            "auth": {
                "client_id": self._config.auth.client_id,
                "tenant_id": self._config.auth.tenant_id,
                "scopes": list(self._config.auth.scopes),
                "token_cache_path": self._config.auth.token_cache_path,
            },
            "timezone": self._config.timezone,
            "triage": {
                "interval_minutes": self._config.triage.interval_minutes,
                "lookback_hours": self._config.triage.lookback_hours,
                "batch_size": self._config.triage.batch_size,
                "mode": self._config.triage.mode,
                "watch_folders": list(self._config.triage.watch_folders),
            },
            "models": {
                "bootstrap": self._config.models.bootstrap,
                "bootstrap_merge": self._config.models.bootstrap_merge,
                "triage": self._config.models.triage,
                "dry_run": self._config.models.dry_run,
                "digest": self._config.models.digest,
                "waiting_for": self._config.models.waiting_for,
            },
            "snippet": {
                "max_length": self._config.snippet.max_length,
                "strip_signatures": self._config.snippet.strip_signatures,
                "strip_disclaimers": self._config.snippet.strip_disclaimers,
                "strip_forwarded_headers": self._config.snippet.strip_forwarded_headers,
            },
        }

        # Populate projects from taxonomy
        proposed["projects"] = []
        for project in taxonomy.get("projects", []):
            if isinstance(project, dict) and project.get("name"):
                proposed["projects"].append(
                    {
                        "name": project["name"],
                        "folder": project.get("folder", f"Projects/{project['name']}"),
                        "signals": {
                            "subjects": project.get("signals", {}).get("subjects", []),
                            "senders": project.get("signals", {}).get("senders", []),
                            "body_keywords": project.get("signals", {}).get("body_keywords", []),
                        },
                        "priority_default": project.get("priority_default", "P2 - Important"),
                    }
                )

        # Populate areas from taxonomy
        proposed["areas"] = []
        for area in taxonomy.get("areas", []):
            if isinstance(area, dict) and area.get("name"):
                proposed["areas"].append(
                    {
                        "name": area["name"],
                        "folder": area.get("folder", f"Areas/{area['name']}"),
                        "signals": {
                            "subjects": area.get("signals", {}).get("subjects", []),
                            "senders": area.get("signals", {}).get("senders", []),
                            "body_keywords": area.get("signals", {}).get("body_keywords", []),
                        },
                        "priority_default": area.get("priority_default", "P3 - Urgent Low"),
                    }
                )

        # Populate auto_rules from taxonomy
        proposed["auto_rules"] = []
        for rule in taxonomy.get("auto_rules", []):
            if isinstance(rule, dict) and rule.get("name"):
                proposed["auto_rules"].append(
                    {
                        "name": rule["name"],
                        "match": {
                            "senders": rule.get("match", {}).get("senders", []),
                            "subjects": rule.get("match", {}).get("subjects", []),
                        },
                        "action": {
                            "folder": rule.get("action", {}).get("folder", "Reference/Newsletters"),
                            "category": rule.get("action", {}).get("category", "FYI Only"),
                            "priority": rule.get("action", {}).get("priority", "P4 - Low"),
                        },
                    }
                )

        # Populate key_contacts from taxonomy
        proposed["key_contacts"] = []
        for contact in taxonomy.get("key_contacts", []):
            if isinstance(contact, dict) and contact.get("email"):
                proposed["key_contacts"].append(
                    {
                        "email": contact["email"],
                        "role": contact.get("role", ""),
                        "priority_boost": contact.get("priority_boost", 1),
                    }
                )

        # Add operational defaults
        proposed["aging"] = {
            "needs_reply_warning_hours": self._config.aging.needs_reply_warning_hours,
            "needs_reply_critical_hours": self._config.aging.needs_reply_critical_hours,
            "waiting_for_nudge_hours": self._config.aging.waiting_for_nudge_hours,
            "waiting_for_escalate_hours": self._config.aging.waiting_for_escalate_hours,
        }
        proposed["digest"] = {
            "enabled": self._config.digest.enabled,
            "schedule": self._config.digest.schedule,
            "delivery": self._config.digest.delivery,
            "include_sections": list(self._config.digest.include_sections),
        }

        # Validate before writing
        try:
            AppConfig(**proposed)
        except Exception as e:
            logger.warning(
                "Proposed config failed validation, writing anyway for manual review",
                error=str(e),
            )

        # Write to disk
        PROPOSED_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(PROPOSED_CONFIG_PATH, "w") as f:
            yaml.dump(proposed, f, default_flow_style=False, sort_keys=False, allow_unicode=True)

        logger.info("Proposed config written", path=str(PROPOSED_CONFIG_PATH))
        return PROPOSED_CONFIG_PATH

    async def _populate_sender_profiles(
        self,
        emails: list[Email],
        taxonomy: dict[str, Any],
        stats: BootstrapStats,
    ) -> int:
        """Populate sender_profiles table from bootstrap data.

        Counts emails per sender, uses taxonomy for categorization,
        and identifies auto-rule candidates. Uses a single batch
        transaction for all profiles to avoid N+1 DB calls.

        Args:
            emails: All processed emails
            taxonomy: Consolidated taxonomy with sender_clusters
            stats: Stats to update with auto_rule_candidate count

        Returns:
            Number of sender profiles created/updated
        """
        # Build sender -> email count mapping
        sender_counts: dict[str, int] = {}
        sender_names: dict[str, str] = {}
        sender_folders: dict[str, dict[str, int]] = {}

        for email in emails:
            if not email.sender_email:
                continue
            addr = email.sender_email.lower()
            sender_counts[addr] = sender_counts.get(addr, 0) + 1
            if email.sender_name:
                sender_names[addr] = email.sender_name
            # Track folder distribution
            if email.current_folder:
                if addr not in sender_folders:
                    sender_folders[addr] = {}
                folder = email.current_folder
                sender_folders[addr][folder] = sender_folders[addr].get(folder, 0) + 1

        # Build sender -> category mapping from taxonomy
        sender_categories = self._build_sender_category_map(taxonomy)

        # Build batch profile data
        profile_batch: list[dict[str, str | int | bool | None]] = []
        for addr, email_count in sender_counts.items():
            category = sender_categories.get(addr, "unknown")
            display_name = sender_names.get(addr)
            is_candidate = False
            default_folder: str | None = None

            # Check for auto-rule candidate: >90% to single folder AND 10+ emails
            folders = sender_folders.get(addr, {})
            if email_count >= 10 and folders:
                top_folder = max(folders, key=folders.get)
                top_count = folders[top_folder]
                if top_count / email_count >= 0.9:
                    is_candidate = True
                    default_folder = top_folder
                    stats.auto_rule_candidates += 1

            profile_batch.append(
                {
                    "email": addr,
                    "display_name": display_name,
                    "category": category,
                    "email_count": email_count,
                    "auto_rule_candidate": is_candidate,
                    "default_folder": default_folder,
                }
            )

        count = await self._store.upsert_sender_profiles_batch(profile_batch)
        logger.info("Sender profiles populated", count=count)
        return count

    def _build_sender_category_map(
        self,
        taxonomy: dict[str, Any],
    ) -> dict[str, str]:
        """Build a mapping of sender email -> category from taxonomy.

        Args:
            taxonomy: Consolidated taxonomy with sender_clusters

        Returns:
            Dict mapping lowercase email to category string
        """
        categories: dict[str, str] = {}
        clusters = taxonomy.get("sender_clusters", {})

        for email in clusters.get("newsletters", []):
            if isinstance(email, str):
                categories[email.lower()] = "newsletter"

        for email in clusters.get("automated", []):
            if isinstance(email, str):
                categories[email.lower()] = "automated"

        for email in clusters.get("clients", []):
            if isinstance(email, str):
                categories[email.lower()] = "client"

        for email in clusters.get("vendors", []):
            if isinstance(email, str):
                categories[email.lower()] = "vendor"

        for email in clusters.get("internal", []):
            if isinstance(email, str):
                categories[email.lower()] = "internal"

        # Key contacts are dicts with email + role
        for contact in clusters.get("key_contacts", []):
            if isinstance(contact, dict) and contact.get("email"):
                categories[contact["email"].lower()] = "key_contact"

        return categories

    def _extract_text_response(self, response: anthropic.types.Message) -> str:
        """Extract text content from a Claude API response.

        Args:
            response: Anthropic API response

        Returns:
            Concatenated text content
        """
        parts = []
        for block in response.content:
            if block.type == "text":
                parts.append(block.text)
        return "\n".join(parts)

    def _update_token_stats(
        self,
        stats: BootstrapStats,
        response: anthropic.types.Message,
    ) -> None:
        """Update token usage stats from a Claude response.

        Args:
            stats: Stats object to update
            response: Anthropic API response with usage info
        """
        stats.total_input_tokens += response.usage.input_tokens
        stats.total_output_tokens += response.usage.output_tokens

    async def _log_bootstrap_request(
        self,
        model: str,
        messages: list[dict[str, Any]],
        response: str | None,
        duration_ms: int,
        task_type: str,
        error: str | None = None,
    ) -> None:
        """Log a bootstrap LLM request to the database.

        Args:
            model: Model used
            messages: Messages sent
            response: Raw text response (or None)
            duration_ms: Duration in milliseconds
            task_type: Task type ('bootstrap_pass1' or 'bootstrap_pass2')
            error: Error message if failed
        """
        if not self._config.llm_logging.enabled:
            return

        try:
            prompt_data: dict[str, Any] = {"messages": messages}
            response_data: dict[str, Any] | None = None
            if response and self._config.llm_logging.log_responses:
                response_data = {"text": response[:5000]}  # Truncate for storage

            await self._store.log_llm_request(
                task_type=task_type,
                model=model,
                prompt=prompt_data if self._config.llm_logging.log_prompts else {"redacted": True},
                response=response_data,
                duration_ms=duration_ms,
                error=error,
            )
        except Exception as e:
            # Logging failures should never block bootstrap
            logger.warning("Failed to log bootstrap LLM request", error=str(e))

    def _print_summary(
        self,
        stats: BootstrapStats,
        proposed_path: Path,
    ) -> None:
        """Print formatted bootstrap summary using rich.

        Args:
            stats: Bootstrap statistics
            proposed_path: Path to the proposed config file
        """
        self._console.print("\n" + "=" * 60)
        self._console.print("[bold green]Bootstrap Complete[/bold green]")
        self._console.print("=" * 60)

        table = Table(show_header=False, box=None, padding=(0, 2))
        table.add_column("Metric", style="cyan")
        table.add_column("Value", style="white")

        table.add_row("Emails analyzed", str(stats.total_emails_fetched))
        table.add_row(
            "Batches (succeeded/total)", f"{stats.batches_succeeded}/{stats.total_batches}"
        )
        table.add_row("Projects discovered", str(stats.projects_discovered))
        table.add_row("Areas discovered", str(stats.areas_discovered))
        table.add_row("Auto-rules generated", str(stats.auto_rules_generated))
        table.add_row("Sender profiles", str(stats.senders_profiled))
        table.add_row("Auto-rule candidates", str(stats.auto_rule_candidates))
        table.add_row("Total tokens", f"{stats.total_input_tokens + stats.total_output_tokens:,}")
        table.add_row("Duration", f"{stats.duration_seconds:.1f}s")

        self._console.print(table)
        self._console.print(
            f"\n[bold]Proposed config written to:[/bold] [cyan]{proposed_path}[/cyan]"
        )
        self._console.print("\n[bold]Next steps:[/bold]")
        self._console.print("  1. Review and edit the proposed config")
        self._console.print("  2. Rename to config/config.yaml")
        self._console.print("  3. Run: python -m assistant dry-run --days 90")
