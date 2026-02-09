"""Waiting-for tracker with reply detection and escalation.

Monitors active waiting-for items, detects replies via the SentItemsCache,
and classifies items into escalation levels (normal, nudge, critical).

Usage:
    from assistant.engine.waiting_for import WaitingForTracker

    tracker = WaitingForTracker(store, sent_cache, config)
    result = await tracker.check_all(cycle_id)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from assistant.core.logging import get_logger

if TYPE_CHECKING:
    from assistant.config_schema import AppConfig
    from assistant.db.store import DatabaseStore, WaitingFor
    from assistant.graph.messages import SentItemsCache

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class WaitingForCheckResult:
    """Result of checking all waiting-for items in a cycle.

    Attributes:
        resolved: Items where a reply was detected (status -> 'received')
        nudged: Items past nudge threshold
        escalated: Items past escalate threshold
        unchanged: Items within normal thresholds
        errors: Items where checking failed
    """

    resolved: int = 0
    nudged: int = 0
    escalated: int = 0
    unchanged: int = 0
    errors: int = 0


class WaitingForTracker:
    """Monitor waiting-for items for replies and escalation.

    Each triage cycle, checks all active waiting-for items:
    1. Detect replies via SentItemsCache (user sent reply to conversation)
    2. Classify items by aging threshold (nudge / critical)
    """

    def __init__(
        self,
        store: DatabaseStore,
        sent_cache: SentItemsCache,
        config: AppConfig,
    ) -> None:
        self._store = store
        self._sent_cache = sent_cache
        self._config = config

    async def check_all(self, cycle_id: str | None = None) -> WaitingForCheckResult:
        """Check all active waiting-for items for replies and escalation.

        Args:
            cycle_id: Optional correlation ID for logging

        Returns:
            WaitingForCheckResult with counts for each outcome
        """
        try:
            items = await self._store.get_active_waiting_for()
        except Exception as e:
            logger.error("waiting_for_fetch_failed", error=str(e), cycle_id=cycle_id)
            return WaitingForCheckResult(errors=1)

        if not items:
            return WaitingForCheckResult()

        resolved = 0
        nudged = 0
        escalated = 0
        unchanged = 0
        errors = 0

        for item in items:
            try:
                # 1. Check for reply
                if self._check_for_reply(item):
                    # H5: Idempotent resolution — only count if actually updated
                    actually_resolved = await self._store.resolve_waiting_for(
                        item.id, status="received"
                    )
                    if actually_resolved:
                        resolved += 1
                        logger.info(
                            "waiting_for_resolved",
                            waiting_for_id=item.id,
                            expected_from=(item.expected_from or "")[:20],  # S2: PII truncation
                            cycle_id=cycle_id,
                        )
                    continue

                # 2. Check escalation level
                level = self._check_escalation(item)
                if level == "critical":
                    escalated += 1
                elif level == "nudge":
                    nudged += 1
                else:
                    unchanged += 1

            except Exception as e:
                logger.warning(
                    "waiting_for_check_failed",
                    waiting_for_id=item.id,
                    error=str(e),
                    cycle_id=cycle_id,
                )
                errors += 1

        result = WaitingForCheckResult(
            resolved=resolved,
            nudged=nudged,
            escalated=escalated,
            unchanged=unchanged,
            errors=errors,
        )

        logger.info(
            "waiting_for_check_complete",
            total=len(items),
            resolved=resolved,
            nudged=nudged,
            escalated=escalated,
            unchanged=unchanged,
            errors=errors,
            cycle_id=cycle_id,
        )

        return result

    def _check_for_reply(self, item: WaitingFor) -> bool:
        """Check if the user has replied to the waiting-for conversation.

        Uses the SentItemsCache for efficient batch lookup (no per-item
        Graph API call). Refreshes cache if stale (H4: max 1 minute for
        waiting-for checks).

        Args:
            item: Active waiting-for item

        Returns:
            True if user has replied since the waiting-for was created
        """
        if not item.conversation_id:
            return False

        # H4: Refresh cache if stale (1 minute max for waiting-for resolution)
        if self._sent_cache.is_stale(max_age_minutes=1):
            try:
                self._sent_cache.refresh(hours=self._config.triage.lookback_hours * 2)
            except Exception as e:
                logger.warning("sent_cache_refresh_in_wf_check_failed", error=str(e))

        # Check if user has sent a reply to this conversation
        if not self._sent_cache.has_replied(item.conversation_id):
            return False

        # Verify the reply was sent AFTER the waiting-for was created
        reply_time = self._sent_cache.get_last_reply_time(item.conversation_id)
        if reply_time and item.waiting_since:
            # Make both timezone-naive for comparison (sent cache times are UTC)
            reply_naive = reply_time.replace(tzinfo=None)
            waiting_naive = item.waiting_since.replace(tzinfo=None)
            if reply_naive < waiting_naive:
                return False  # Reply was before the waiting-for was created

        return True

    def _check_escalation(self, item: WaitingFor) -> str:
        """Determine the escalation level for a waiting-for item.

        Args:
            item: Active waiting-for item

        Returns:
            'normal', 'nudge', or 'critical'
        """
        if not item.waiting_since:
            return "normal"

        now = datetime.now()
        waiting_since = item.waiting_since.replace(tzinfo=None)
        hours_waiting = (now - waiting_since).total_seconds() / 3600

        escalate_hours = self._config.aging.waiting_for_escalate_hours
        nudge_hours = self._config.aging.waiting_for_nudge_hours

        if hours_waiting >= escalate_hours:
            return "critical"
        elif hours_waiting >= nudge_hours:
            return "nudge"
        else:
            return "normal"
