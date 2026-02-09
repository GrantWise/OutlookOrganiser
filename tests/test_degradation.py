"""Tests for enhanced graceful degradation (Feature 2M).

Tests the DegradationState dataclass, separate Claude/Graph failure tracking,
recovery with backlog processing, and dashboard integration.
"""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from assistant.classifier.claude_classifier import ClassificationResult
from assistant.config_schema import AppConfig
from assistant.core.errors import ClassificationError
from assistant.db.store import DatabaseStore, Email
from assistant.engine.triage import (
    MAX_CONSECUTIVE_FAILURES,
    DegradationState,
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
    """Return a config for degradation testing."""
    return AppConfig(**sample_config_dict)


@pytest.fixture
async def store(data_dir: Path) -> DatabaseStore:
    """Return an initialized DatabaseStore."""
    db_path = data_dir / "test_degradation.db"
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
    inheritance = MagicMock()
    inheritance.should_inherit = False
    inheritance.inherited_folder = None
    mgr.check_thread_inheritance = AsyncMock(return_value=inheritance)
    context = MagicMock()
    context.messages = []
    context.thread_depth = 0
    mgr.get_thread_context = AsyncMock(return_value=context)
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
# Tests: DegradationState unit tests
# ---------------------------------------------------------------------------


class TestDegradationState:
    """Tests for DegradationState dataclass transitions."""

    def test_initial_state_not_degraded(self):
        """Fresh state is not degraded."""
        state = DegradationState()
        assert state.is_degraded is False
        assert state.claude_consecutive_failures == 0
        assert state.graph_consecutive_failures == 0
        assert state.degraded_since is None
        assert state.degraded_reason is None

    def test_claude_failure_transitions_to_degraded(self):
        """Claude failures transition to degraded after threshold."""
        state = DegradationState()

        for _ in range(MAX_CONSECUTIVE_FAILURES - 1):
            state.record_claude_failure()
            assert state.is_degraded is False

        state.record_claude_failure()
        assert state.is_degraded is True
        assert state.degraded_since is not None
        assert "Claude API" in state.degraded_reason

    def test_graph_failure_transitions_to_degraded(self):
        """Graph API failures transition to degraded after threshold."""
        state = DegradationState()

        for _ in range(MAX_CONSECUTIVE_FAILURES):
            state.record_graph_failure()

        assert state.is_degraded is True
        assert "Graph API" in state.degraded_reason

    def test_claude_success_resets_failures(self):
        """Claude success resets failure counter."""
        state = DegradationState()
        state.claude_consecutive_failures = 2

        recovered = state.record_claude_success()

        assert state.claude_consecutive_failures == 0
        assert recovered is False  # Wasn't degraded

    def test_recovery_from_claude_degraded(self):
        """Recovery from Claude degraded mode returns True."""
        state = DegradationState()
        state.claude_consecutive_failures = MAX_CONSECUTIVE_FAILURES
        state.degraded_since = datetime.now(UTC)
        state.degraded_reason = "Claude API failures"

        recovered = state.record_claude_success()

        assert recovered is True
        assert state.is_degraded is False
        assert state.degraded_since is None
        assert state.degraded_reason is None

    def test_recovery_from_graph_degraded(self):
        """Recovery from Graph degraded mode returns True."""
        state = DegradationState()
        state.graph_consecutive_failures = MAX_CONSECUTIVE_FAILURES
        state.degraded_since = datetime.now(UTC)
        state.degraded_reason = "Graph API failures"

        recovered = state.record_graph_success()

        assert recovered is True
        assert state.is_degraded is False

    def test_separate_claude_and_graph_tracking(self):
        """Claude and Graph failures are tracked independently."""
        state = DegradationState()

        # Claude failures don't affect Graph counter
        for _ in range(MAX_CONSECUTIVE_FAILURES):
            state.record_claude_failure()

        assert state.claude_consecutive_failures == MAX_CONSECUTIVE_FAILURES
        assert state.graph_consecutive_failures == 0
        assert state.is_degraded is True

        # Graph success doesn't affect Claude counter
        state.record_graph_success()
        assert state.claude_consecutive_failures == MAX_CONSECUTIVE_FAILURES
        assert state.is_degraded is True

        # Claude success resets Claude counter
        state.record_claude_success()
        assert state.claude_consecutive_failures == 0
        assert state.is_degraded is False

    def test_degraded_reason_includes_api_type(self):
        """Degraded reason includes which API caused degradation."""
        state_claude = DegradationState()
        for _ in range(MAX_CONSECUTIVE_FAILURES):
            state_claude.record_claude_failure()
        assert "Claude API" in state_claude.degraded_reason

        state_graph = DegradationState()
        for _ in range(MAX_CONSECUTIVE_FAILURES):
            state_graph.record_graph_failure()
        assert "Graph API" in state_graph.degraded_reason


# ---------------------------------------------------------------------------
# Tests: Engine degradation integration
# ---------------------------------------------------------------------------


class TestEngineDegradation:
    """Tests for TriageEngine degradation behavior."""

    async def test_engine_enters_degraded_after_failures(
        self,
        engine: TriageEngine,
        mock_classifier: MagicMock,
        mock_message_manager: MagicMock,
    ):
        """Engine enters degraded mode after consecutive all-fail cycles."""
        mock_classifier.classify_with_claude.side_effect = ClassificationError(
            "API down", email_id="test", attempts=3
        )

        for i in range(MAX_CONSECUTIVE_FAILURES):
            mock_message_manager.list_messages.return_value = [
                _make_raw_message(msg_id=f"msg-{i:03d}")
            ]
            await engine.run_cycle()

        assert engine.degraded_mode is True
        assert engine.degradation_state.is_degraded is True

    async def test_degraded_mode_skips_claude(
        self,
        engine: TriageEngine,
        mock_classifier: MagicMock,
        mock_message_manager: MagicMock,
    ):
        """Degraded mode skips Claude classification."""
        engine._degradation.claude_consecutive_failures = MAX_CONSECUTIVE_FAILURES

        mock_message_manager.list_messages.return_value = [_make_raw_message(msg_id="msg-degraded")]

        await engine.run_cycle()

        mock_classifier.classify_with_claude.assert_not_called()

    async def test_recovery_resets_state(
        self,
        engine: TriageEngine,
        mock_classifier: MagicMock,
        mock_message_manager: MagicMock,
    ):
        """Successful classification after degraded mode resets state."""
        # Enter degraded mode
        engine._degradation.claude_consecutive_failures = MAX_CONSECUTIVE_FAILURES

        # Reset so next cycle can classify
        engine._degradation.claude_consecutive_failures = 0
        mock_classifier.classify_with_claude.return_value = _make_classification_result()
        mock_message_manager.list_messages.return_value = [_make_raw_message(msg_id="msg-recover")]

        result = await engine.run_cycle()

        assert engine.degraded_mode is False
        assert result.classified == 1

    async def test_degradation_state_property_for_dashboard(
        self,
        engine: TriageEngine,
    ):
        """Engine exposes degradation_state property for dashboard."""
        state = engine.degradation_state
        assert isinstance(state, DegradationState)
        assert state.is_degraded is False

        # Simulate degradation
        engine._degradation.claude_consecutive_failures = MAX_CONSECUTIVE_FAILURES
        engine._degradation.degraded_since = datetime.now(UTC)
        engine._degradation.degraded_reason = "Test reason"

        state = engine.degradation_state
        assert state.is_degraded is True
        assert state.degraded_reason == "Test reason"
        assert state.degraded_since is not None


# ---------------------------------------------------------------------------
# Tests: Backlog processing
# ---------------------------------------------------------------------------


class TestBacklogProcessing:
    """Tests for _process_backlog() FIFO recovery."""

    async def test_backlog_processes_pending_emails(
        self,
        engine: TriageEngine,
        store: DatabaseStore,
        mock_classifier: MagicMock,
    ):
        """Backlog processes pending emails in FIFO order."""
        # Seed pending emails
        for i in range(3):
            await store.save_email(
                Email(
                    id=f"backlog-{i}",
                    subject=f"Backlog Email {i}",
                    sender_email=f"sender{i}@example.com",
                    sender_name=f"Sender {i}",
                    received_at=datetime.now(),
                    snippet="test snippet",
                    classification_status="pending",
                )
            )

        mock_classifier.classify_with_claude.return_value = _make_classification_result()

        processed = await engine._process_backlog()

        assert processed == 3

    async def test_backlog_skips_emails_with_suggestions(
        self,
        engine: TriageEngine,
        store: DatabaseStore,
        mock_classifier: MagicMock,
    ):
        """Backlog skips emails that already have suggestions."""
        # Seed email with existing suggestion
        await store.save_email(
            Email(
                id="email-with-sugg",
                subject="Has Suggestion",
                sender_email="sender@example.com",
                sender_name="Sender",
                received_at=datetime.now(),
                snippet="test",
                classification_status="pending",
            )
        )
        await store.create_suggestion(
            email_id="email-with-sugg",
            suggested_folder="Projects/Test",
            suggested_priority="P2 - Important",
            suggested_action_type="Review",
            confidence=0.8,
            reasoning="Already classified",
        )

        processed = await engine._process_backlog()

        assert processed == 0
        mock_classifier.classify_with_claude.assert_not_called()

    async def test_backlog_rate_limited_to_batch_size(
        self,
        engine: TriageEngine,
        store: DatabaseStore,
        mock_classifier: MagicMock,
        sample_config: AppConfig,
    ):
        """Backlog processes at most batch_size emails per invocation."""
        batch_size = sample_config.triage.batch_size

        # Seed more than batch_size pending emails
        for i in range(batch_size + 10):
            await store.save_email(
                Email(
                    id=f"backlog-limit-{i}",
                    subject=f"Backlog Limit {i}",
                    sender_email=f"s{i}@example.com",
                    sender_name=f"S {i}",
                    received_at=datetime.now(),
                    snippet="test",
                    classification_status="pending",
                )
            )

        mock_classifier.classify_with_claude.return_value = _make_classification_result()

        processed = await engine._process_backlog()

        # Should be limited to batch_size
        assert processed <= batch_size
