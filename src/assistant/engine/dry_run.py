"""Dry-run classification engine for testing config against real emails.

Classifies emails using the existing classifier (auto-rules + Claude)
without creating database suggestions. Shows a distribution report,
sample classifications, and optional confusion matrix.

This engine is read-only: it does not modify the database or create
suggestions. All classification results are stored in memory only.

Spec reference: Reference/spec/03-agent-behaviors.md Section 1.2

Usage:
    from assistant.engine.dry_run import DryRunEngine

    engine = DryRunEngine(
        classifier=email_classifier,
        store=db_store,
        message_manager=msg_mgr,
        snippet_cleaner=cleaner,
        thread_manager=thread_mgr,
        config=app_config,
    )
    report = await engine.run(days=90, sample=20, limit=None)
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

from assistant.classifier.prompts import ClassificationContext
from assistant.core.errors import ClassificationError
from assistant.core.logging import get_logger
from assistant.db.store import DatabaseStore, Email

if TYPE_CHECKING:
    from assistant.classifier.claude_classifier import EmailClassifier
    from assistant.classifier.snippet import SnippetCleaner
    from assistant.config_schema import AppConfig
    from assistant.engine.thread_utils import ThreadContextManager
    from assistant.graph.messages import MessageManager

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass
class DryRunClassification:
    """Single classification result for dry-run display."""

    email_id: str
    subject: str
    sender_email: str
    sender_name: str
    folder: str
    priority: str
    action_type: str
    confidence: float
    reasoning: str
    method: str  # 'auto_rule', 'claude_tool_use', etc.


@dataclass
class FolderDistribution:
    """Folder distribution entry for the report."""

    folder: str
    count: int
    percentage: float


@dataclass
class ConfusionEntry:
    """Single confusion pair with count."""

    suggested: str
    actual: str
    count: int


@dataclass
class AccuracyReport:
    """Accuracy report from historical corrections."""

    total_resolved: int
    folder_accuracy: float
    folder_correct: int
    folder_total: int
    folder_confusions: list[ConfusionEntry]
    priority_accuracy: float
    priority_correct: int
    priority_total: int
    priority_confusions: list[ConfusionEntry]
    action_accuracy: float
    action_correct: int
    action_total: int
    action_confusions: list[ConfusionEntry]


@dataclass
class DryRunReport:
    """Complete dry-run report."""

    total_emails: int = 0
    classified_count: int = 0
    failed_count: int = 0
    auto_ruled_count: int = 0
    claude_count: int = 0
    folder_distribution: list[FolderDistribution] = field(default_factory=list)
    sample_classifications: list[DryRunClassification] = field(default_factory=list)
    accuracy_report: AccuracyReport | None = None
    duration_seconds: float = 0.0


class DryRunEngine:
    """Classifies emails in dry-run mode and generates reports.

    All classification results are stored in memory only — no database
    writes, no suggestion creation, no state updates.

    Attributes:
        _classifier: EmailClassifier for auto-rules + Claude
        _store: DatabaseStore for reading emails and historical corrections
        _message_manager: MessageManager for fallback email fetching
        _snippet_cleaner: SnippetCleaner for cleaning email bodies
        _thread_manager: ThreadContextManager for thread context
        _config: Application configuration
        _console: Rich console for output
    """

    def __init__(
        self,
        classifier: EmailClassifier,
        store: DatabaseStore,
        message_manager: MessageManager,
        snippet_cleaner: SnippetCleaner,
        thread_manager: ThreadContextManager,
        config: AppConfig,
        console: Console | None = None,
    ):
        self._classifier = classifier
        self._store = store
        self._message_manager = message_manager
        self._snippet_cleaner = snippet_cleaner
        self._thread_manager = thread_manager
        self._config = config
        self._console = console or Console()

    async def run(
        self,
        days: int = 90,
        sample: int = 20,
        limit: int | None = None,
    ) -> DryRunReport:
        """Execute dry-run classification and generate report.

        Args:
            days: Number of days of email to classify
            sample: Number of sample classifications to show
            limit: Maximum emails to process (None for all)

        Returns:
            DryRunReport with distribution, samples, and accuracy
        """
        start_time = time.monotonic()
        report = DryRunReport()

        # Ensure classifier has fresh system prompt
        await self._classifier.refresh_system_prompt()

        # 1. Load emails
        self._console.print(f"\n[bold]Loading emails from last {days} days...[/bold]")
        emails = await self._fetch_or_load_emails(days, limit)
        report.total_emails = len(emails)

        if not emails:
            self._console.print("[yellow]No emails found to classify.[/yellow]")
            return report

        self._console.print(f"  Found [cyan]{len(emails)}[/cyan] emails to classify")

        # 2. Classify each email
        self._console.print("\n[bold]Classifying emails...[/bold]")
        classifications: list[DryRunClassification] = []

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=self._console,
        ) as progress:
            task = progress.add_task("Classifying...", total=len(emails))

            for i, email in enumerate(emails):
                progress.update(task, description=f"Classifying {i + 1}/{len(emails)}...")

                result = await self._classify_email(email)
                if result:
                    classifications.append(result)
                    if result.method == "auto_rule":
                        report.auto_ruled_count += 1
                    else:
                        report.claude_count += 1
                else:
                    report.failed_count += 1

                progress.advance(task)

        report.classified_count = len(classifications)

        # 3. Build folder distribution
        report.folder_distribution = self._build_distribution(classifications)

        # 4. Select sample classifications
        if classifications:
            sample_size = min(sample, len(classifications))
            report.sample_classifications = random.sample(classifications, sample_size)

        # 5. Build confusion matrix (from historical corrections)
        report.accuracy_report = await self._build_confusion_matrix()

        # 6. Print report
        report.duration_seconds = time.monotonic() - start_time
        self._print_report(report)

        return report

    async def _fetch_or_load_emails(
        self,
        days: int,
        limit: int | None,
    ) -> list[Email]:
        """Fetch emails from DB if available, else from Graph API.

        Prefers local database (from prior bootstrap run) to avoid
        re-fetching from the Graph API.

        Args:
            days: Number of days to look back
            limit: Maximum emails to return

        Returns:
            List of Email dataclasses
        """
        # Try loading from database first (from prior bootstrap)
        db_limit = limit or 10000
        emails = await self._store.get_emails_by_date_range(days, db_limit)

        if emails:
            logger.info("Loaded emails from database", count=len(emails))
            return emails[:limit] if limit else emails

        # Fallback: fetch from Graph API
        logger.info("No emails in database, fetching from Graph API")
        cutoff = datetime.now(UTC) - timedelta(days=days)
        cutoff_str = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")

        raw_messages = self._message_manager.list_messages(
            folder="Inbox",
            filter_query=f"receivedDateTime ge {cutoff_str}",
            order_by="receivedDateTime desc",
            max_items=limit,
        )

        # Deduplicate by message ID (Graph API pagination can return overlaps)
        seen_ids: set[str] = set()
        unique_messages: list[dict] = []
        for msg in raw_messages:
            msg_id = msg.get("id", "")
            if msg_id and msg_id not in seen_ids:
                unique_messages.append(msg)
                seen_ids.add(msg_id)

        dups = len(raw_messages) - len(unique_messages)
        if dups:
            logger.info("Deduplicated fetched emails", duplicates_removed=dups)

        # Transform to Email dataclasses
        emails = []
        for msg in unique_messages:
            from_data = msg.get("from", {}).get("emailAddress", {})
            sender_email = from_data.get("address", "")
            sender_name = from_data.get("name", "")
            body_preview = msg.get("bodyPreview", "")
            cleaned = self._snippet_cleaner.clean(body_preview, is_html=False)

            received_str = msg.get("receivedDateTime", "")
            received_at = None
            if received_str:
                try:
                    received_at = datetime.fromisoformat(received_str.replace("Z", "+00:00"))
                except ValueError:
                    logger.warning("Invalid receivedDateTime", value=received_str)

            flag_data = msg.get("flag", {})
            flag_status = (
                flag_data.get("flagStatus", "notFlagged")
                if isinstance(flag_data, dict)
                else "notFlagged"
            )

            email = Email(
                id=msg.get("id", ""),
                conversation_id=msg.get("conversationId"),
                conversation_index=msg.get("conversationIndex"),
                subject=msg.get("subject", ""),
                sender_email=sender_email,
                sender_name=sender_name,
                received_at=received_at,
                snippet=cleaned.cleaned_text,
                current_folder=None,
                web_link=msg.get("webLink"),
                importance=msg.get("importance", "normal"),
                is_read=bool(msg.get("isRead", False)),
                flag_status=flag_status,
                classification_status="pending",
            )
            emails.append(email)

        return emails

    async def _classify_email(
        self,
        email: Email,
    ) -> DryRunClassification | None:
        """Classify a single email for dry-run.

        Tries auto-rules first, then Claude. On failure, returns None.
        Does NOT create suggestions or update database.

        Args:
            email: Email to classify

        Returns:
            DryRunClassification or None if classification failed
        """
        # Try auto-rules first
        auto_result = self._classifier.classify_with_auto_rules(
            sender_email=email.sender_email or "",
            subject=email.subject or "",
        )
        if auto_result:
            return DryRunClassification(
                email_id=email.id,
                subject=email.subject or "",
                sender_email=email.sender_email or "",
                sender_name=email.sender_name or "",
                folder=auto_result.folder,
                priority=auto_result.priority,
                action_type=auto_result.action_type,
                confidence=auto_result.confidence,
                reasoning=auto_result.reasoning,
                method="auto_rule",
            )

        # Build minimal classification context
        context = ClassificationContext(
            thread_depth=0,
            has_user_reply=False,
        )

        # Try Claude classification
        try:
            claude_result = await self._classifier.classify_with_claude(
                email_id=email.id,
                sender_name=email.sender_name or "",
                sender_email=email.sender_email or "",
                subject=email.subject or "",
                received_datetime=email.received_at.isoformat() if email.received_at else "unknown",
                importance=email.importance,
                is_read=email.is_read,
                flag_status=email.flag_status,
                snippet=email.snippet or "",
                context=context,
                model=self._config.models.dry_run,
            )

            return DryRunClassification(
                email_id=email.id,
                subject=email.subject or "",
                sender_email=email.sender_email or "",
                sender_name=email.sender_name or "",
                folder=claude_result.folder,
                priority=claude_result.priority,
                action_type=claude_result.action_type,
                confidence=claude_result.confidence,
                reasoning=claude_result.reasoning,
                method=claude_result.method,
            )
        except ClassificationError as e:
            logger.warning(
                "Dry-run classification failed",
                email_id=email.id,
                error=str(e),
            )
            return None

    async def _build_confusion_matrix(self) -> AccuracyReport | None:
        """Build confusion matrix from historical corrections.

        Queries the suggestions table for resolved entries and compares
        suggested values against approved values.

        Returns:
            AccuracyReport or None if fewer than 10 resolved corrections
        """
        resolved = await self._store.get_resolved_suggestions()

        if len(resolved) < 10:
            return None

        # Compare suggested vs approved for each dimension
        folder_correct = 0
        folder_confusions: dict[tuple[str, str], int] = {}
        priority_correct = 0
        priority_confusions: dict[tuple[str, str], int] = {}
        action_correct = 0
        action_confusions: dict[tuple[str, str], int] = {}

        for s in resolved:
            # Folder
            if s.suggested_folder and s.approved_folder:
                if s.suggested_folder == s.approved_folder:
                    folder_correct += 1
                else:
                    key = (s.suggested_folder, s.approved_folder)
                    folder_confusions[key] = folder_confusions.get(key, 0) + 1

            # Priority
            if s.suggested_priority and s.approved_priority:
                if s.suggested_priority == s.approved_priority:
                    priority_correct += 1
                else:
                    key = (s.suggested_priority, s.approved_priority)
                    priority_confusions[key] = priority_confusions.get(key, 0) + 1

            # Action type
            if s.suggested_action_type and s.approved_action_type:
                if s.suggested_action_type == s.approved_action_type:
                    action_correct += 1
                else:
                    key = (s.suggested_action_type, s.approved_action_type)
                    action_confusions[key] = action_confusions.get(key, 0) + 1

        total = len(resolved)

        # Sort confusions by count descending and take top 5
        def top_confusions(d: dict[tuple[str, str], int]) -> list[ConfusionEntry]:
            return [
                ConfusionEntry(suggested=k[0], actual=k[1], count=v)
                for k, v in sorted(d.items(), key=lambda x: x[1], reverse=True)[:5]
            ]

        return AccuracyReport(
            total_resolved=total,
            folder_accuracy=folder_correct / total if total else 0.0,
            folder_correct=folder_correct,
            folder_total=total,
            folder_confusions=top_confusions(folder_confusions),
            priority_accuracy=priority_correct / total if total else 0.0,
            priority_correct=priority_correct,
            priority_total=total,
            priority_confusions=top_confusions(priority_confusions),
            action_accuracy=action_correct / total if total else 0.0,
            action_correct=action_correct,
            action_total=total,
            action_confusions=top_confusions(action_confusions),
        )

    def _build_distribution(
        self,
        classifications: list[DryRunClassification],
    ) -> list[FolderDistribution]:
        """Build folder distribution sorted by count descending.

        Args:
            classifications: All classification results

        Returns:
            List of FolderDistribution entries
        """
        folder_counts: dict[str, int] = {}
        for c in classifications:
            folder_counts[c.folder] = folder_counts.get(c.folder, 0) + 1

        total = len(classifications)
        distribution = [
            FolderDistribution(
                folder=folder,
                count=count,
                percentage=(count / total * 100) if total else 0.0,
            )
            for folder, count in folder_counts.items()
        ]
        distribution.sort(key=lambda d: d.count, reverse=True)
        return distribution

    def _print_report(self, report: DryRunReport) -> None:
        """Print the formatted dry-run report using rich.

        Args:
            report: Complete DryRunReport
        """
        self._console.print("\n" + "=" * 60)
        self._console.print("[bold]Dry Run Classification Report[/bold]")
        self._console.print("=" * 60)

        # Summary stats
        self._console.print(
            f"\nClassified: [cyan]{report.classified_count}[/cyan]/{report.total_emails} emails"
            f"  |  Auto-ruled: [green]{report.auto_ruled_count}[/green]"
            f"  |  Claude: [blue]{report.claude_count}[/blue]"
            f"  |  Failed: [red]{report.failed_count}[/red]"
            f"  |  Duration: {report.duration_seconds:.1f}s"
        )

        # Folder distribution
        if report.folder_distribution:
            self._console.print("\n[bold]Folder Distribution:[/bold]")
            table = Table(box=None, padding=(0, 2))
            table.add_column("Folder", style="cyan")
            table.add_column("Count", justify="right")
            table.add_column("Percentage", justify="right")

            for dist in report.folder_distribution:
                table.add_row(
                    dist.folder,
                    str(dist.count),
                    f"{dist.percentage:.1f}%",
                )
            self._console.print(table)

        # Sample classifications
        if report.sample_classifications:
            self._console.print(
                f"\n[bold]Sample Classifications ({len(report.sample_classifications)} shown):[/bold]"
            )
            for c in report.sample_classifications:
                method_color = "green" if c.method == "auto_rule" else "blue"
                self._console.print(
                    f'\n  [dim]"{c.subject}"[/dim] from {c.sender_name} <{c.sender_email}>'
                )
                self._console.print(
                    f"    -> {c.folder} | {c.priority} | {c.action_type} "
                    f"[{method_color}]({c.method}, {c.confidence:.0%})[/{method_color}]"
                )
                if c.reasoning:
                    self._console.print(f"    [dim]{c.reasoning}[/dim]")

        # Confusion matrix
        if report.accuracy_report:
            ar = report.accuracy_report
            self._console.print(
                f"\n[bold]Classification Accuracy (based on {ar.total_resolved} historical corrections):[/bold]"
            )

            self._console.print(
                f"\n  Folder Accuracy: [cyan]{ar.folder_accuracy:.1%}[/cyan]"
                f" ({ar.folder_correct}/{ar.folder_total})"
            )
            for c in ar.folder_confusions[:3]:
                self._console.print(f"    Most confused: {c.suggested} -> {c.actual} ({c.count}x)")

            self._console.print(
                f"\n  Priority Accuracy: [cyan]{ar.priority_accuracy:.1%}[/cyan]"
                f" ({ar.priority_correct}/{ar.priority_total})"
            )
            for c in ar.priority_confusions[:3]:
                self._console.print(f"    Most common: {c.suggested} -> {c.actual} ({c.count}x)")

            self._console.print(
                f"\n  Action Accuracy: [cyan]{ar.action_accuracy:.1%}[/cyan]"
                f" ({ar.action_correct}/{ar.action_total})"
            )
            for c in ar.action_confusions[:3]:
                self._console.print(f"    Most common: {c.suggested} -> {c.actual} ({c.count}x)")
