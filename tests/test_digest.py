"""Tests for the Daily Digest Generator (Feature 2C).

Tests data gathering, Claude formatting, plain-text fallback,
delivery modes, and the all-clear case.
"""

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from assistant.config_schema import AppConfig
from assistant.db.store import DatabaseStore, Email
from assistant.engine.digest import DigestGenerator, DigestResult

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
    db_path = data_dir / "test_digest.db"
    s = DatabaseStore(db_path)
    await s.initialize()
    return s


@pytest.fixture
def mock_anthropic() -> MagicMock:
    """Return a mock Anthropic client with tool use response."""
    client = MagicMock()

    tool_block = MagicMock()
    tool_block.type = "tool_use"
    tool_block.name = "generate_digest"
    tool_block.input = {
        "summary": "All systems nominal. No overdue items.",
        "overdue_replies_section": "",
        "waiting_for_section": "",
        "activity_section": "Processed 10 emails today.",
        "pending_section": "No pending suggestions.",
    }

    response = MagicMock()
    response.content = [tool_block]
    client.messages = MagicMock()
    client.messages.create = AsyncMock(return_value=response)

    return client


@pytest.fixture
def generator(
    store: DatabaseStore,
    mock_anthropic: MagicMock,
    sample_config: AppConfig,
) -> DigestGenerator:
    """Return a DigestGenerator with mocked dependencies."""
    return DigestGenerator(store, mock_anthropic, sample_config)


async def _seed_email_with_suggestion(
    store: DatabaseStore,
    email_id: str,
    subject: str = "Test Email",
    sender_email: str = "sender@example.com",
    action_type: str = "Needs Reply",
    hours_ago: int = 36,
) -> int:
    """Seed an email with an approved suggestion."""
    received = datetime.now() - timedelta(hours=hours_ago)
    await store.save_email(
        Email(
            id=email_id,
            subject=subject,
            sender_email=sender_email,
            sender_name="Test Sender",
            received_at=received,
            snippet="test snippet",
        )
    )
    sid = await store.create_suggestion(
        email_id=email_id,
        suggested_folder="Areas/Test",
        suggested_priority="P2 - Important",
        suggested_action_type=action_type,
        confidence=0.85,
        reasoning="Test classification",
    )
    await store.approve_suggestion(sid)
    return sid


# ---------------------------------------------------------------------------
# Tests: Data gathering
# ---------------------------------------------------------------------------


async def test_overdue_replies_detection(store: DatabaseStore):
    """Overdue replies are detected past warning threshold."""
    await _seed_email_with_suggestion(
        store, "email-overdue", action_type="Needs Reply", hours_ago=36
    )

    overdue = await store.get_overdue_replies(warning_hours=24, critical_hours=48)

    assert len(overdue) == 1
    assert overdue[0]["level"] == "warning"


async def test_critical_overdue_replies(store: DatabaseStore):
    """Replies past critical threshold are flagged critical."""
    await _seed_email_with_suggestion(
        store, "email-critical", action_type="Needs Reply", hours_ago=72
    )

    overdue = await store.get_overdue_replies(warning_hours=24, critical_hours=48)

    assert len(overdue) == 1
    assert overdue[0]["level"] == "critical"


async def test_no_overdue_replies_within_threshold(store: DatabaseStore):
    """Recent emails are not flagged as overdue."""
    await _seed_email_with_suggestion(store, "email-recent", action_type="Needs Reply", hours_ago=6)

    overdue = await store.get_overdue_replies(warning_hours=24, critical_hours=48)
    assert len(overdue) == 0


async def test_processing_stats_empty(store: DatabaseStore):
    """Processing stats with no data returns zero counts."""
    since = datetime.now() - timedelta(days=1)
    stats = await store.get_processing_stats(since)

    assert stats["classified"] == 0
    assert stats["auto_ruled"] == 0
    assert stats["failed"] == 0


async def test_processing_stats_with_actions(store: DatabaseStore):
    """Processing stats reflect action_log entries."""
    await store.log_action(
        action_type="classify",
        email_id="test-1",
        details={"method": "auto_rule"},
        triggered_by="auto",
    )
    await store.log_action(
        action_type="classify",
        email_id="test-2",
        details={"method": "claude"},
        triggered_by="triage",
    )
    await store.log_action(
        action_type="move",
        email_id="test-3",
        details={},
        triggered_by="user_approved",
    )

    since = datetime.now() - timedelta(days=1)
    stats = await store.get_processing_stats(since)

    assert stats["auto_ruled"] == 1
    assert stats["classified"] == 1
    assert stats["user_approved"] == 1


# ---------------------------------------------------------------------------
# Tests: Claude formatting
# ---------------------------------------------------------------------------


async def test_digest_with_claude_formatting(generator: DigestGenerator):
    """Digest uses Claude to format output."""
    result = await generator.generate()

    assert isinstance(result, DigestResult)
    assert "DAILY DIGEST" in result.text
    assert "All systems nominal" in result.text


async def test_digest_claude_failure_uses_fallback(
    store: DatabaseStore,
    mock_anthropic: MagicMock,
    sample_config: AppConfig,
):
    """Claude failure falls back to plain-text formatting."""
    mock_anthropic.messages.create.side_effect = Exception("API down")

    generator = DigestGenerator(store, mock_anthropic, sample_config)
    result = await generator.generate()

    assert isinstance(result, DigestResult)
    assert "DAILY DIGEST" in result.text
    # Should use plain-text fallback (all clear since no data)
    assert "All clear" in result.text


# ---------------------------------------------------------------------------
# Tests: Plain-text fallback
# ---------------------------------------------------------------------------


def test_plain_text_all_clear(generator: DigestGenerator):
    """Plain text with no items produces all-clear message."""
    data = {
        "overdue_replies": [],
        "overdue_waiting": [],
        "stats": {},
        "pending_suggestions": 0,
        "failed_classifications": 0,
    }

    text = generator._generate_plain_text(data)

    assert "All clear" in text
    assert "DAILY DIGEST" in text


def test_plain_text_with_overdue_replies(generator: DigestGenerator):
    """Plain text includes overdue replies section."""
    data = {
        "overdue_replies": [
            {
                "subject": "Important question",
                "sender_email": "boss@example.com",
                "level": "critical",
            }
        ],
        "overdue_waiting": [],
        "stats": {},
        "pending_suggestions": 0,
        "failed_classifications": 0,
    }

    text = generator._generate_plain_text(data)

    assert "OVERDUE REPLIES" in text
    assert "Important question" in text
    assert "CRITICAL" in text
    assert "boss@example.com" in text


def test_plain_text_with_waiting(generator: DigestGenerator):
    """Plain text includes waiting-for section."""
    data = {
        "overdue_replies": [],
        "overdue_waiting": [
            {
                "description": "Awaiting contract review",
                "expected_from": "legal@vendor.com",
                "hours_waiting": 72,
                "level": "nudge",
            }
        ],
        "stats": {},
        "pending_suggestions": 0,
        "failed_classifications": 0,
    }

    text = generator._generate_plain_text(data)

    assert "WAITING FOR" in text
    assert "contract review" in text
    assert "72h" in text


def test_plain_text_with_pending_and_failed(generator: DigestGenerator):
    """Plain text includes pending and failed counts."""
    data = {
        "overdue_replies": [],
        "overdue_waiting": [],
        "stats": {"classified": 15, "auto_ruled": 5},
        "pending_suggestions": 3,
        "failed_classifications": 2,
    }

    text = generator._generate_plain_text(data)

    assert "PENDING REVIEW: 3" in text
    assert "FAILED CLASSIFICATIONS: 2" in text
    assert "ACTIVITY" in text
    assert "Classified: 15" in text


# ---------------------------------------------------------------------------
# Tests: DigestResult
# ---------------------------------------------------------------------------


async def test_digest_result_counts(
    store: DatabaseStore,
    mock_anthropic: MagicMock,
    sample_config: AppConfig,
):
    """DigestResult contains correct counts from gathered data."""
    # Seed overdue email
    await _seed_email_with_suggestion(
        store, "email-digest", action_type="Needs Reply", hours_ago=36
    )

    generator = DigestGenerator(store, mock_anthropic, sample_config)
    result = await generator.generate()

    assert result.overdue_replies == 1
    assert result.pending_suggestions == 0  # Was approved above
    assert isinstance(result.stats, dict)
    assert result.generated_at is not None


# ---------------------------------------------------------------------------
# Tests: Empty digest
# ---------------------------------------------------------------------------


async def test_empty_digest_all_clear(generator: DigestGenerator):
    """Empty database produces all-clear digest."""
    result = await generator.generate()

    assert result.overdue_replies == 0
    assert result.overdue_waiting == 0
    assert result.pending_suggestions == 0
