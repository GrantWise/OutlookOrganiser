"""Daily digest generator for the Outlook AI Assistant.

Gathers overdue replies, waiting-for items, processing stats, and pending
suggestions, then formats them into a structured daily digest using Claude
Haiku via tool use.

Usage:
    from assistant.engine.digest import DigestGenerator

    generator = DigestGenerator(store, anthropic_client, config)
    result = await generator.generate()
    print(result.text)
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from assistant.classifier.prompts import DIGEST_SYSTEM_PROMPT, GENERATE_DIGEST_TOOL
from assistant.core.logging import get_logger

if TYPE_CHECKING:
    import anthropic

    from assistant.config_schema import AppConfig
    from assistant.db.store import DatabaseStore

logger = get_logger(__name__)


@dataclass(frozen=True)
class DigestResult:
    """Result of digest generation.

    Attributes:
        text: Formatted digest text
        overdue_replies: Count of overdue 'Needs Reply' items
        overdue_waiting: Count of overdue 'Waiting For' items
        pending_suggestions: Count of pending suggestions
        failed_classifications: Count of failed classifications
        stats: Processing stats dict
        generated_at: When the digest was generated
    """

    text: str
    overdue_replies: int = 0
    overdue_waiting: int = 0
    pending_suggestions: int = 0
    failed_classifications: int = 0
    stats: dict[str, Any] = field(default_factory=dict)
    generated_at: datetime = field(default_factory=datetime.now)


class DigestGenerator:
    """Generate daily digest reports from email processing data.

    Gathers data from the database, formats it into a prompt, and uses
    Claude Haiku to produce a structured digest via tool use. Falls back
    to plain-text formatting if Claude fails.
    """

    def __init__(
        self,
        store: DatabaseStore,
        anthropic_client: anthropic.AsyncAnthropic,
        config: AppConfig,
    ) -> None:
        self._store = store
        self._client = anthropic_client
        self._config = config

    async def generate(self) -> DigestResult:
        """Generate a daily digest.

        Steps:
        0. Check cooldown to prevent duplicate digests on retry (R2)
        1. Query overdue replies (past warning threshold)
        2. Query overdue waiting-for (past nudge threshold)
        3. Query processing stats from action_log
        4. Count pending suggestions and failed classifications
        5. Format via Claude Haiku with tool use
        6. Fall back to plain-text on Claude failure

        Returns:
            DigestResult with formatted text and counts
        """
        # R2: Deduplication cooldown (1 hour)
        last_run = await self._store.get_state("last_digest_run")
        if last_run:
            try:
                last_dt = datetime.fromisoformat(last_run)
                if datetime.now() - last_dt < timedelta(hours=1):
                    logger.info("digest_cooldown_skip", last_run=last_run)
                    return DigestResult(text="Digest already generated within the last hour.")
            except ValueError:
                pass

        aging = self._config.aging

        # 1. Overdue replies
        overdue_replies = await self._store.get_overdue_replies(
            warning_hours=aging.needs_reply_warning_hours,
            critical_hours=aging.needs_reply_critical_hours,
        )

        # 2. Overdue waiting-for
        waiting_items = await self._store.get_active_waiting_for()
        overdue_waiting = []
        now = datetime.now()
        for w in waiting_items:
            if w.waiting_since:
                age_hours = (now - w.waiting_since).total_seconds() / 3600
                if age_hours >= aging.waiting_for_nudge_hours:
                    overdue_waiting.append(
                        {
                            "description": w.description,
                            "expected_from": (w.expected_from or "")[:20],  # S2: PII truncation
                            "hours_waiting": int(age_hours),
                            "level": "critical"
                            if age_hours >= aging.waiting_for_escalate_hours
                            else "nudge",
                        }
                    )

        # 3. Processing stats (last 24 hours)
        since = datetime.now() - timedelta(days=1)
        stats = await self._store.get_processing_stats(since)

        # 4. Pending suggestions and failed classifications
        db_stats = await self._store.get_stats()
        pending = db_stats.get("pending_suggestions", 0)
        failed = db_stats.get("emails_by_status", {}).get("failed", 0)

        # 5. Attempt Claude formatting
        data = {
            "overdue_replies": overdue_replies,
            "overdue_waiting": overdue_waiting,
            "stats": stats,
            "pending_suggestions": pending,
            "failed_classifications": failed,
        }

        text = await self._format_with_claude(data)
        if not text:
            # 6. Fallback to plain text
            text = self._generate_plain_text(data)

        # R2: Update cooldown timestamp
        await self._store.set_state("last_digest_run", datetime.now().isoformat())

        return DigestResult(
            text=text,
            overdue_replies=len(overdue_replies),
            overdue_waiting=len(overdue_waiting),
            pending_suggestions=pending,
            failed_classifications=failed,
            stats=stats,
        )

    async def _format_with_claude(self, data: dict[str, Any]) -> str | None:
        """Format digest data using Claude Haiku with tool use.

        Args:
            data: Digest data dict

        Returns:
            Formatted digest string, or None on failure
        """
        prompt = f"""Generate a daily digest from this email processing data:

{json.dumps(data, indent=2, default=str)}

If everything is clear (no overdue items, no failures), produce a brief "all clear" summary.
Otherwise, highlight the most important items that need attention."""

        try:
            response = await self._client.messages.create(
                model=getattr(self._config.models, "digest", None) or self._config.models.triage,
                max_tokens=1024,
                system=DIGEST_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
                tools=[GENERATE_DIGEST_TOOL],
                tool_choice={"type": "tool", "name": "generate_digest"},
            )

            # Extract tool call result
            for block in response.content:
                if block.type == "tool_use" and block.name == "generate_digest":
                    sections = []
                    tool_input = block.input

                    if tool_input.get("summary"):
                        sections.append(f"DAILY DIGEST\n{'=' * 40}\n{tool_input['summary']}")

                    if tool_input.get("overdue_replies_section"):
                        sections.append(
                            f"\nOVERDUE REPLIES\n{'-' * 40}\n"
                            f"{tool_input['overdue_replies_section']}"
                        )

                    if tool_input.get("waiting_for_section"):
                        sections.append(
                            f"\nWAITING FOR\n{'-' * 40}\n{tool_input['waiting_for_section']}"
                        )

                    if tool_input.get("activity_section"):
                        sections.append(f"\nACTIVITY\n{'-' * 40}\n{tool_input['activity_section']}")

                    if tool_input.get("pending_section"):
                        sections.append(
                            f"\nPENDING REVIEW\n{'-' * 40}\n{tool_input['pending_section']}"
                        )

                    return "\n".join(sections) if sections else None

            return None

        except Exception as e:
            logger.warning("digest_claude_failed", error=str(e))
            return None

    def _generate_plain_text(self, data: dict[str, Any]) -> str:
        """Generate plain-text digest fallback when Claude is unavailable.

        Args:
            data: Digest data dict

        Returns:
            Formatted plain-text digest
        """
        lines = [f"DAILY DIGEST\n{'=' * 40}"]

        overdue = data.get("overdue_replies", [])
        waiting = data.get("overdue_waiting", [])
        stats = data.get("stats", {})
        pending = data.get("pending_suggestions", 0)
        failed = data.get("failed_classifications", 0)

        # Check if everything is clear
        if not overdue and not waiting and pending == 0 and failed == 0:
            lines.append("\nAll clear - no items need attention.")
            return "\n".join(lines)

        # Overdue replies
        if overdue:
            lines.append(f"\nOVERDUE REPLIES ({len(overdue)})")
            lines.append("-" * 40)
            for item in overdue:
                level = item.get("level", "warning").upper()
                lines.append(
                    f"  [{level}] {item.get('subject', 'No subject')} "
                    f"from {item.get('sender_email', 'unknown')}"
                )

        # Waiting-for
        if waiting:
            lines.append(f"\nWAITING FOR ({len(waiting)})")
            lines.append("-" * 40)
            for item in waiting:
                level = item.get("level", "nudge").upper()
                lines.append(
                    f"  [{level}] {item.get('description', 'No description')} "
                    f"from {item.get('expected_from', 'unknown')} "
                    f"({item.get('hours_waiting', 0)}h)"
                )

        # Activity stats
        if stats:
            lines.append("\nACTIVITY (last 24h)")
            lines.append("-" * 40)
            lines.append(f"  Classified: {stats.get('classified', 0)}")
            lines.append(f"  Auto-ruled: {stats.get('auto_ruled', 0)}")
            lines.append(f"  Auto-approved: {stats.get('auto_approved', 0)}")
            lines.append(f"  User-approved: {stats.get('user_approved', 0)}")

        # Pending
        if pending > 0:
            lines.append(f"\nPENDING REVIEW: {pending} suggestions awaiting review")

        # Failed
        if failed > 0:
            lines.append(f"\nFAILED CLASSIFICATIONS: {failed}")

        return "\n".join(lines)

    async def deliver(self, digest: DigestResult, mode: str = "stdout") -> None:
        """Deliver a generated digest.

        Args:
            digest: Generated digest result
            mode: Delivery mode ('stdout' or 'file')
        """
        if mode == "stdout":
            print(digest.text)
        elif mode == "file":
            import os
            import tempfile

            output_path = f"data/digest_{digest.generated_at.strftime('%Y%m%d_%H%M')}.txt"
            # R6: Atomic write via temp file + rename
            os.makedirs(os.path.dirname(output_path), exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(output_path), suffix=".tmp")
            try:
                with os.fdopen(fd, "w") as f:
                    f.write(digest.text)
                os.replace(tmp_path, output_path)
            except OSError:
                # Clean up temp file on failure
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
                raise
            logger.info("digest_written", path=output_path)
