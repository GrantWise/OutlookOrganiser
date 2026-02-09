"""Tests for the Waiting-For Tracker (Feature 2B).

Tests reply detection, escalation thresholds, triage integration,
and the extend/escalate API actions.
"""

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from assistant.config_schema import AppConfig
from assistant.db.store import DatabaseStore, Email, WaitingFor
from assistant.engine.waiting_for import WaitingForCheckResult, WaitingForTracker

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_config(sample_config_dict: dict[str, Any]) -> AppConfig:
    """Config with aging thresholds."""
    d = {**sample_config_dict}
    d["aging"] = {
        "needs_reply_warning_hours": 24,
        "needs_reply_critical_hours": 48,
        "waiting_for_nudge_hours": 48,
        "waiting_for_escalate_hours": 96,
    }
    return AppConfig(**d)


@pytest.fixture
async def store(data_dir: Path) -> DatabaseStore:
    """Return an initialized DatabaseStore."""
    db_path = data_dir / "test_waiting_for.db"
    s = DatabaseStore(db_path)
    await s.initialize()
    return s


@pytest.fixture
def mock_sent_cache() -> MagicMock:
    """Return a mock SentItemsCache."""
    cache = MagicMock()
    cache.has_replied.return_value = False
    cache.get_last_reply_time.return_value = None
    return cache


@pytest.fixture
def tracker(
    store: DatabaseStore,
    mock_sent_cache: MagicMock,
    sample_config: AppConfig,
) -> WaitingForTracker:
    """Return a WaitingForTracker with mocked dependencies."""
    return WaitingForTracker(store, mock_sent_cache, sample_config)


async def _seed_waiting_for(
    store: DatabaseStore,
    email_id: str,
    conversation_id: str = "conv-123",
    expected_from: str = "vendor@example.com",
    description: str = "Waiting for reply",
    hours_ago: int = 12,
    nudge_after_hours: int = 48,
) -> int:
    """Seed an email and waiting-for item."""
    await store.save_email(
        Email(
            id=email_id,
            conversation_id=conversation_id,
            subject="Test Email",
            sender_email="me@example.com",
            sender_name="Me",
            received_at=datetime.now(),
            snippet="test",
        )
    )
    wf_id = await store.create_waiting_for(
        email_id=email_id,
        conversation_id=conversation_id,
        expected_from=expected_from,
        description=description,
        nudge_after_hours=nudge_after_hours,
    )
    # Backdate the waiting_since
    backdated = (datetime.now() - timedelta(hours=hours_ago)).isoformat()
    async with store._db() as db:
        await db.execute(
            "UPDATE waiting_for SET waiting_since = ? WHERE id = ?",
            (backdated, wf_id),
        )
        await db.commit()
    return wf_id


# ---------------------------------------------------------------------------
# Tests: Reply detection
# ---------------------------------------------------------------------------


async def test_reply_detected_resolves_item(
    tracker: WaitingForTracker,
    store: DatabaseStore,
    mock_sent_cache: MagicMock,
):
    """Reply in SentItemsCache resolves waiting-for item."""
    await _seed_waiting_for(store, "email-reply", conversation_id="conv-replied")

    # Simulate user reply
    mock_sent_cache.has_replied.return_value = True
    mock_sent_cache.get_last_reply_time.return_value = datetime.now()

    result = await tracker.check_all("test-cycle")

    assert result.resolved == 1
    assert result.unchanged == 0

    # Verify the item is resolved in DB
    items = await store.get_active_waiting_for()
    assert len(items) == 0


async def test_no_reply_within_threshold_unchanged(
    tracker: WaitingForTracker,
    store: DatabaseStore,
    mock_sent_cache: MagicMock,
):
    """Item within normal threshold and no reply stays unchanged."""
    await _seed_waiting_for(store, "email-normal", hours_ago=12)

    mock_sent_cache.has_replied.return_value = False

    result = await tracker.check_all("test-cycle")

    assert result.unchanged == 1
    assert result.resolved == 0
    assert result.nudged == 0


async def test_old_reply_before_waiting_not_resolved(
    tracker: WaitingForTracker,
    store: DatabaseStore,
    mock_sent_cache: MagicMock,
):
    """Reply sent BEFORE waiting-for creation does not resolve the item."""
    await _seed_waiting_for(store, "email-old-reply", hours_ago=12)

    # Reply was 24 hours ago (before the 12-hour-old waiting-for)
    mock_sent_cache.has_replied.return_value = True
    mock_sent_cache.get_last_reply_time.return_value = datetime.now() - timedelta(hours=24)

    result = await tracker.check_all("test-cycle")

    assert result.resolved == 0
    assert result.unchanged == 1


# ---------------------------------------------------------------------------
# Tests: Escalation thresholds
# ---------------------------------------------------------------------------


async def test_past_nudge_threshold_flagged(
    tracker: WaitingForTracker,
    store: DatabaseStore,
):
    """Item past nudge threshold (48h) is flagged as nudge."""
    await _seed_waiting_for(store, "email-nudge", hours_ago=60)

    result = await tracker.check_all("test-cycle")

    assert result.nudged == 1
    assert result.escalated == 0


async def test_past_escalate_threshold_critical(
    tracker: WaitingForTracker,
    store: DatabaseStore,
):
    """Item past escalation threshold (96h) is flagged as critical."""
    await _seed_waiting_for(store, "email-escalate", hours_ago=120)

    result = await tracker.check_all("test-cycle")

    assert result.escalated == 1
    assert result.nudged == 0


async def test_check_all_returns_correct_counts(
    tracker: WaitingForTracker,
    store: DatabaseStore,
    mock_sent_cache: MagicMock,
):
    """check_all returns accurate counts across multiple items."""
    # Normal item (12h)
    await _seed_waiting_for(store, "email-a", conversation_id="conv-a", hours_ago=12)
    # Nudge item (60h)
    await _seed_waiting_for(store, "email-b", conversation_id="conv-b", hours_ago=60)
    # Critical item (120h)
    await _seed_waiting_for(store, "email-c", conversation_id="conv-c", hours_ago=120)
    # Resolved item (reply detected)
    await _seed_waiting_for(store, "email-d", conversation_id="conv-d", hours_ago=24)

    def _has_replied(conv_id: str) -> bool:
        return conv_id == "conv-d"

    mock_sent_cache.has_replied.side_effect = _has_replied
    mock_sent_cache.get_last_reply_time.return_value = datetime.now()

    result = await tracker.check_all("test-cycle")

    assert result.resolved == 1
    assert result.unchanged == 1
    assert result.nudged == 1
    assert result.escalated == 1
    assert result.errors == 0


# ---------------------------------------------------------------------------
# Tests: Escalation level classification
# ---------------------------------------------------------------------------


def test_escalation_level_normal(tracker: WaitingForTracker):
    """Item within normal threshold classified as normal."""
    item = WaitingFor(
        id=1,
        email_id="test",
        waiting_since=datetime.now() - timedelta(hours=12),
    )
    assert tracker._check_escalation(item) == "normal"


def test_escalation_level_nudge(tracker: WaitingForTracker):
    """Item past nudge but before escalation classified as nudge."""
    item = WaitingFor(
        id=1,
        email_id="test",
        waiting_since=datetime.now() - timedelta(hours=60),
    )
    assert tracker._check_escalation(item) == "nudge"


def test_escalation_level_critical(tracker: WaitingForTracker):
    """Item past escalation threshold classified as critical."""
    item = WaitingFor(
        id=1,
        email_id="test",
        waiting_since=datetime.now() - timedelta(hours=100),
    )
    assert tracker._check_escalation(item) == "critical"


# ---------------------------------------------------------------------------
# Tests: DB operations
# ---------------------------------------------------------------------------


async def test_extend_waiting_for_deadline(store: DatabaseStore):
    """Extending deadline increases nudge_after_hours."""
    wf_id = await _seed_waiting_for(store, "email-extend", nudge_after_hours=48)

    await store.extend_waiting_for_deadline(wf_id, additional_hours=24)

    items = await store.get_active_waiting_for()
    assert len(items) == 1
    assert items[0].nudge_after_hours == 72


async def test_resolve_waiting_for_as_expired(store: DatabaseStore):
    """Resolving with expired status removes from active items."""
    wf_id = await _seed_waiting_for(store, "email-expire")

    await store.resolve_waiting_for(wf_id, status="expired")

    items = await store.get_active_waiting_for()
    assert len(items) == 0


# ---------------------------------------------------------------------------
# Tests: Empty state
# ---------------------------------------------------------------------------


async def test_check_all_with_no_items(tracker: WaitingForTracker):
    """check_all with no active items returns zero counts."""
    result = await tracker.check_all("test-cycle")

    assert result == WaitingForCheckResult()
    assert result.resolved == 0
    assert result.errors == 0
