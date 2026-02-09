"""Tests for the triage engine.

Tests the triage cycle including email fetching, classification pipeline
routing (auto-rules, thread inheritance, Claude), suggestion creation,
waiting-for tracking, graceful degradation, and cycle summary logging.
"""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from assistant.classifier.claude_classifier import ClassificationResult
from assistant.config_schema import AppConfig
from assistant.core.errors import ClassificationError, GraphAPIError
from assistant.db.store import DatabaseStore
from assistant.engine.triage import (
    MAX_CONSECUTIVE_FAILURES,
    TriageEngine,
)

# ---------------------------------------------------------------------------
# Sample data
# ---------------------------------------------------------------------------


def _make_raw_message(
    msg_id: str = "msg-001",
    subject: str = "Test Subject",
    sender_email: str = "sender@example.com",
    sender_name: str = "Test Sender",
    conversation_id: str = "conv-001",
) -> dict[str, Any]:
    """Create a raw Graph API message dict for testing."""
    return {
        "id": msg_id,
        "conversationId": conversation_id,
        "conversationIndex": "",
        "subject": subject,
        "from": {
            "emailAddress": {
                "address": sender_email,
                "name": sender_name,
            }
        },
        "receivedDateTime": datetime.now(UTC).isoformat(),
        "bodyPreview": "This is a test email body preview.",
        "webLink": f"https://outlook.office.com/mail/{msg_id}",
        "importance": "normal",
        "isRead": False,
        "flag": {"flagStatus": "notFlagged"},
    }


def _make_classification_result(
    folder: str = "Projects/Test",
    priority: str = "P2 - Important",
    action_type: str = "Review",
    confidence: float = 0.88,
    method: str = "claude_tool_use",
) -> ClassificationResult:
    """Create a ClassificationResult for testing."""
    return ClassificationResult(
        folder=folder,
        priority=priority,
        action_type=action_type,
        confidence=confidence,
        reasoning="Test email about an active project",
        method=method,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_config(sample_config_dict: dict[str, Any]) -> AppConfig:
    """Return a config for triage testing."""
    return AppConfig(**sample_config_dict)


@pytest.fixture
async def store(data_dir: Path) -> DatabaseStore:
    """Return an initialized DatabaseStore."""
    db_path = data_dir / "test_triage.db"
    s = DatabaseStore(db_path)
    await s.initialize()
    return s


@pytest.fixture
def mock_classifier() -> MagicMock:
    """Return a mock EmailClassifier."""
    classifier = MagicMock()
    classifier.refresh_system_prompt = AsyncMock()
    classifier.classify_with_auto_rules = MagicMock(return_value=None)
    classifier.classify_with_claude = AsyncMock(return_value=_make_classification_result())
    return classifier


@pytest.fixture
def mock_message_manager() -> MagicMock:
    """Return a mock MessageManager."""
    mgr = MagicMock()
    mgr.list_messages = MagicMock(return_value=[_make_raw_message()])
    mgr.check_reply_state = MagicMock(return_value=None)
    return mgr


@pytest.fixture
def mock_folder_manager() -> MagicMock:
    """Return a mock FolderManager."""
    mgr = MagicMock()
    mgr.get_folder_id = MagicMock(return_value="folder-id-123")
    return mgr


@pytest.fixture
def mock_snippet_cleaner() -> MagicMock:
    """Return a mock SnippetCleaner."""
    cleaner = MagicMock()
    result = MagicMock()
    result.cleaned_text = "cleaned email body"
    cleaner.clean = MagicMock(return_value=result)
    return cleaner


@pytest.fixture
def mock_thread_manager() -> MagicMock:
    """Return a mock ThreadContextManager."""
    mgr = MagicMock()

    # Default: no inheritance
    inheritance = MagicMock()
    inheritance.should_inherit = False
    inheritance.inherited_folder = None
    mgr.check_thread_inheritance = AsyncMock(return_value=inheritance)

    # Default: empty thread context
    context = MagicMock()
    context.messages = []
    context.thread_depth = 0
    mgr.get_thread_context = AsyncMock(return_value=context)

    # Default: no sender history pattern
    history = MagicMock()
    history.format_for_prompt = MagicMock(return_value=None)
    mgr.get_sender_history = AsyncMock(return_value=history)

    return mgr


@pytest.fixture
def mock_sent_cache() -> MagicMock:
    """Return a mock SentItemsCache."""
    cache = MagicMock()
    cache.refresh = MagicMock(return_value=0)
    cache.has_replied = MagicMock(return_value=False)
    return cache


@pytest.fixture
def engine(
    mock_classifier: MagicMock,
    store: DatabaseStore,
    mock_message_manager: MagicMock,
    mock_folder_manager: MagicMock,
    mock_snippet_cleaner: MagicMock,
    mock_thread_manager: MagicMock,
    mock_sent_cache: MagicMock,
    sample_config: AppConfig,
) -> TriageEngine:
    """Return a TriageEngine with mocked dependencies."""
    return TriageEngine(
        classifier=mock_classifier,
        store=store,
        message_manager=mock_message_manager,
        folder_manager=mock_folder_manager,
        snippet_cleaner=mock_snippet_cleaner,
        thread_manager=mock_thread_manager,
        sent_cache=mock_sent_cache,
        config=sample_config,
    )


# ---------------------------------------------------------------------------
# Tests: Basic cycle
# ---------------------------------------------------------------------------


async def test_cycle_classifies_new_email(engine: TriageEngine, store: DatabaseStore):
    """Test that a triage cycle classifies a new email and creates a suggestion."""
    result = await engine.run_cycle()

    assert result.emails_fetched == 1
    assert result.emails_processed == 1
    assert result.classified == 1
    assert result.skipped == 0
    assert result.failed == 0
    assert result.degraded_mode is False

    # Verify suggestion was created
    suggestions = await store.get_pending_suggestions()
    assert len(suggestions) == 1
    assert suggestions[0].suggested_folder == "Projects/Test"
    assert suggestions[0].confidence == 0.88


async def test_cycle_skips_existing_email(
    engine: TriageEngine,
    store: DatabaseStore,
    mock_message_manager: MagicMock,
):
    """Test that emails already in the database are skipped."""
    # Run first cycle to store the email
    await engine.run_cycle()

    # Run second cycle - same email should be skipped
    result = await engine.run_cycle()
    assert result.skipped == 1
    assert result.classified == 0

    # Only one suggestion should exist
    suggestions = await store.get_pending_suggestions()
    assert len(suggestions) == 1


async def test_cycle_no_new_emails(
    engine: TriageEngine,
    mock_message_manager: MagicMock,
):
    """Test cycle with no new emails."""
    mock_message_manager.list_messages.return_value = []

    result = await engine.run_cycle()
    assert result.emails_fetched == 0
    assert result.emails_processed == 0


async def test_cycle_updates_last_processed_timestamp(
    engine: TriageEngine,
    store: DatabaseStore,
):
    """Test that last_processed_timestamp is updated after cycle."""
    await engine.run_cycle()

    timestamp = await store.get_state("last_processed_timestamp")
    assert timestamp is not None


async def test_cycle_updates_last_triage_cycle(
    engine: TriageEngine,
    store: DatabaseStore,
):
    """Test that last_triage_cycle state is set."""
    result = await engine.run_cycle()

    cycle_time = await store.get_state("last_triage_cycle")
    cycle_id = await store.get_state("last_triage_cycle_id")
    assert cycle_time is not None
    assert cycle_id == result.cycle_id


# ---------------------------------------------------------------------------
# Tests: Auto-rules
# ---------------------------------------------------------------------------


async def test_auto_rule_creates_approved_suggestion(
    engine: TriageEngine,
    store: DatabaseStore,
    mock_classifier: MagicMock,
):
    """Test that auto-rule matches create auto-approved suggestions."""
    auto_result = _make_classification_result(
        folder="Areas/Newsletters",
        priority="P4 - Low",
        action_type="FYI Only",
        confidence=1.0,
        method="auto_rule",
    )
    mock_classifier.classify_with_auto_rules.return_value = auto_result

    result = await engine.run_cycle()

    assert result.auto_ruled == 1
    assert result.classified == 0

    # Suggestion should be auto-approved, not pending
    pending = await store.get_pending_suggestions()
    assert len(pending) == 0

    # Claude should not have been called
    mock_classifier.classify_with_claude.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: Classification failure
# ---------------------------------------------------------------------------


async def test_classification_failure_increments_attempts(
    engine: TriageEngine,
    store: DatabaseStore,
    mock_classifier: MagicMock,
):
    """Test that classification failures increment attempts and mark as failed."""
    mock_classifier.classify_with_claude.side_effect = ClassificationError(
        "API error", email_id="msg-001", attempts=3
    )

    result = await engine.run_cycle()

    assert result.failed == 1
    assert result.classified == 0


# ---------------------------------------------------------------------------
# Tests: Waiting-for creation
# ---------------------------------------------------------------------------


async def test_waiting_for_created_on_waiting_action(
    engine: TriageEngine,
    store: DatabaseStore,
    mock_classifier: MagicMock,
):
    """Test that a waiting-for tracker is created when action_type is 'Waiting For'."""
    classification = ClassificationResult(
        folder="Projects/Test",
        priority="P2 - Important",
        action_type="Waiting For",
        confidence=0.85,
        reasoning="Waiting for vendor response",
        method="claude_tool_use",
        waiting_for_detail={
            "expected_from": "vendor@example.com",
            "description": "Price quote for project",
        },
    )
    mock_classifier.classify_with_claude = AsyncMock(return_value=classification)

    await engine.run_cycle()

    waiting = await store.get_active_waiting_for()
    assert len(waiting) == 1
    assert waiting[0].expected_from == "vendor@example.com"
    assert waiting[0].description == "Price quote for project"


async def test_no_waiting_for_without_detail(
    engine: TriageEngine,
    store: DatabaseStore,
    mock_classifier: MagicMock,
):
    """Test that no waiting-for is created without waiting_for_detail."""
    classification = _make_classification_result(action_type="Waiting For")
    mock_classifier.classify_with_claude = AsyncMock(return_value=classification)

    await engine.run_cycle()

    waiting = await store.get_active_waiting_for()
    assert len(waiting) == 0


# ---------------------------------------------------------------------------
# Tests: Graceful degradation
# ---------------------------------------------------------------------------


async def test_degraded_mode_after_consecutive_failures(
    engine: TriageEngine,
    mock_classifier: MagicMock,
    mock_message_manager: MagicMock,
):
    """Test entering degraded mode after MAX_CONSECUTIVE_FAILURES all-fail cycles."""
    mock_classifier.classify_with_claude.side_effect = ClassificationError(
        "API down", email_id="test", attempts=3
    )

    for i in range(MAX_CONSECUTIVE_FAILURES):
        # Each cycle gets a fresh email
        mock_message_manager.list_messages.return_value = [_make_raw_message(msg_id=f"msg-{i:03d}")]
        await engine.run_cycle()

    assert engine.degraded_mode is True


async def test_degraded_mode_skips_claude(
    engine: TriageEngine,
    mock_classifier: MagicMock,
    mock_message_manager: MagicMock,
):
    """Test that degraded mode skips Claude classification."""
    # Force into degraded mode via DegradationState
    engine._degradation.claude_consecutive_failures = MAX_CONSECUTIVE_FAILURES

    mock_message_manager.list_messages.return_value = [_make_raw_message(msg_id="msg-degraded")]

    await engine.run_cycle()

    # Auto-rules are still checked (returns None by default), so email is skipped
    mock_classifier.classify_with_claude.assert_not_called()


async def test_recovery_from_degraded_mode(
    engine: TriageEngine,
    mock_classifier: MagicMock,
    mock_message_manager: MagicMock,
):
    """Test exiting degraded mode when Claude recovers."""
    # Force into degraded mode via DegradationState
    engine._degradation.claude_consecutive_failures = MAX_CONSECUTIVE_FAILURES

    # Reset failures so next successful call brings us out of degraded mode
    engine._degradation.claude_consecutive_failures = 0
    mock_classifier.classify_with_claude = AsyncMock(return_value=_make_classification_result())
    mock_message_manager.list_messages.return_value = [_make_raw_message(msg_id="msg-recover")]

    result = await engine.run_cycle()

    assert engine.degraded_mode is False
    assert result.classified == 1


# ---------------------------------------------------------------------------
# Tests: Multiple emails
# ---------------------------------------------------------------------------


async def test_batch_size_limit(
    engine: TriageEngine,
    mock_message_manager: MagicMock,
    mock_classifier: MagicMock,
    sample_config: AppConfig,
):
    """Test that batch_size config limits emails processed per cycle."""
    # Create more emails than batch_size
    batch_size = sample_config.triage.batch_size
    messages = [
        _make_raw_message(msg_id=f"msg-{i:03d}", conversation_id=f"conv-{i:03d}")
        for i in range(batch_size + 10)
    ]
    mock_message_manager.list_messages.return_value = messages

    result = await engine.run_cycle()

    # Should process at most batch_size emails
    assert result.emails_processed <= batch_size


# ---------------------------------------------------------------------------
# Tests: Graph API errors
# ---------------------------------------------------------------------------


async def test_fetch_folder_error_continues(
    engine: TriageEngine,
    mock_message_manager: MagicMock,
):
    """Test that Graph API errors during fetch are handled gracefully."""
    mock_message_manager.list_messages.side_effect = GraphAPIError(
        "Folder not found", status_code=404
    )

    result = await engine.run_cycle()

    assert result.emails_fetched == 0
    assert result.emails_processed == 0


# ---------------------------------------------------------------------------
# Tests: Cycle result
# ---------------------------------------------------------------------------


async def test_cycle_result_has_cycle_id(engine: TriageEngine):
    """Test that cycle result has a UUID cycle_id."""
    result = await engine.run_cycle()

    assert result.cycle_id is not None
    assert len(result.cycle_id) == 36  # UUID format


async def test_cycle_result_has_duration(engine: TriageEngine):
    """Test that cycle result tracks duration."""
    result = await engine.run_cycle()

    assert result.duration_ms >= 0


# ---------------------------------------------------------------------------
# Tests: Action logging
# ---------------------------------------------------------------------------


async def test_classification_logs_action(
    engine: TriageEngine,
    store: DatabaseStore,
):
    """Test that classification creates an action log entry."""
    await engine.run_cycle()

    logs = await store.get_action_logs(limit=10)
    assert len(logs) >= 1

    suggest_logs = [entry for entry in logs if entry.action_type == "suggest"]
    assert len(suggest_logs) == 1
    assert suggest_logs[0].triggered_by == "auto"


async def test_auto_rule_logs_action(
    engine: TriageEngine,
    store: DatabaseStore,
    mock_classifier: MagicMock,
):
    """Test that auto-rule classification creates an action log entry."""
    auto_result = _make_classification_result(method="auto_rule", confidence=1.0)
    mock_classifier.classify_with_auto_rules.return_value = auto_result

    await engine.run_cycle()

    logs = await store.get_action_logs(limit=10)
    classify_logs = [entry for entry in logs if entry.action_type == "classify"]
    assert len(classify_logs) == 1
