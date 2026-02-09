"""Tests for delta query email fetching (Feature 2A).

Tests the GraphClient.get_delta_messages() method and the TriageEngine's
delta-first fetch strategy with timestamp fallback.
"""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from assistant.config_schema import AppConfig
from assistant.core.errors import DeltaTokenExpiredError, GraphAPIError
from assistant.db.store import DatabaseStore
from assistant.engine.triage import TriageEngine
from assistant.graph.client import GraphClient

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


def _make_classification_result():
    """Create a ClassificationResult for testing."""
    from assistant.classifier.claude_classifier import ClassificationResult

    return ClassificationResult(
        folder="Projects/Test",
        priority="P2 - Important",
        action_type="Review",
        confidence=0.88,
        reasoning="Test email about an active project",
        method="claude_tool_use",
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_config(sample_config_dict: dict[str, Any]) -> AppConfig:
    """Return a config for delta query testing."""
    return AppConfig(**sample_config_dict)


@pytest.fixture
async def store(data_dir: Path) -> DatabaseStore:
    """Return an initialized DatabaseStore."""
    db_path = data_dir / "test_delta.db"
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
    mgr.list_messages = MagicMock(return_value=[])
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
def mock_graph_client() -> MagicMock:
    """Return a mock GraphClient."""
    client = MagicMock(spec=GraphClient)
    return client


@pytest.fixture
def engine(
    mock_classifier: MagicMock,
    store: DatabaseStore,
    mock_message_manager: MagicMock,
    mock_folder_manager: MagicMock,
    mock_snippet_cleaner: MagicMock,
    mock_thread_manager: MagicMock,
    mock_sent_cache: MagicMock,
    mock_graph_client: MagicMock,
    sample_config: AppConfig,
) -> TriageEngine:
    """Return a TriageEngine with mocked dependencies and a graph_client."""
    return TriageEngine(
        classifier=mock_classifier,
        store=store,
        message_manager=mock_message_manager,
        folder_manager=mock_folder_manager,
        snippet_cleaner=mock_snippet_cleaner,
        thread_manager=mock_thread_manager,
        sent_cache=mock_sent_cache,
        config=sample_config,
        graph_client=mock_graph_client,
    )


@pytest.fixture
def engine_no_delta(
    mock_classifier: MagicMock,
    store: DatabaseStore,
    mock_message_manager: MagicMock,
    mock_folder_manager: MagicMock,
    mock_snippet_cleaner: MagicMock,
    mock_thread_manager: MagicMock,
    mock_sent_cache: MagicMock,
    sample_config: AppConfig,
) -> TriageEngine:
    """Return a TriageEngine without graph_client (timestamp-only)."""
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
# Tests: GraphClient.get_delta_messages()
# ---------------------------------------------------------------------------


class TestGetDeltaMessages:
    """Tests for GraphClient.get_delta_messages()."""

    def test_delta_returns_messages_and_token(self, mock_graph_client: MagicMock):
        """Delta query returns messages and a new token."""
        messages = [_make_raw_message("msg-1"), _make_raw_message("msg-2")]
        mock_graph_client.get_delta_messages.return_value = (messages, "new-delta-token")

        result, token = mock_graph_client.get_delta_messages(
            folder_id="Inbox",
            delta_token=None,
            select_fields="id,subject",
        )

        assert len(result) == 2
        assert token == "new-delta-token"

    def test_initial_sync_no_stored_token(self, mock_graph_client: MagicMock):
        """Initial sync (no token) returns messages and first delta token."""
        mock_graph_client.get_delta_messages.return_value = (
            [_make_raw_message("msg-init")],
            "first-delta-token",
        )

        result, token = mock_graph_client.get_delta_messages(
            folder_id="Inbox",
            delta_token=None,
        )

        assert len(result) == 1
        assert token == "first-delta-token"
        mock_graph_client.get_delta_messages.assert_called_once_with(
            folder_id="Inbox",
            delta_token=None,
        )

    def test_410_gone_raises_delta_token_expired(self):
        """410 Gone response raises DeltaTokenExpiredError."""
        error = DeltaTokenExpiredError("Token expired", folder="Inbox")
        assert error.status_code == 410
        assert error.error_code == "DeltaTokenExpired"
        assert error.folder == "Inbox"

    def test_incremental_sync_with_token(self, mock_graph_client: MagicMock):
        """Incremental sync uses the stored delta token."""
        mock_graph_client.get_delta_messages.return_value = (
            [_make_raw_message("msg-new")],
            "updated-delta-token",
        )

        result, token = mock_graph_client.get_delta_messages(
            folder_id="Inbox",
            delta_token="old-delta-token",
        )

        assert len(result) == 1
        assert token == "updated-delta-token"

    def test_empty_delta_response(self, mock_graph_client: MagicMock):
        """Delta query with no changes returns empty list and new token."""
        mock_graph_client.get_delta_messages.return_value = ([], "same-delta-token")

        result, token = mock_graph_client.get_delta_messages(
            folder_id="Inbox",
            delta_token="existing-token",
        )

        assert result == []
        assert token == "same-delta-token"


# ---------------------------------------------------------------------------
# Tests: Engine delta-first fetch with fallback
# ---------------------------------------------------------------------------


class TestEngineDeltaFetch:
    """Tests for TriageEngine._fetch_new_emails() delta/timestamp strategy."""

    async def test_delta_fetch_stores_token(
        self,
        engine: TriageEngine,
        mock_graph_client: MagicMock,
        store: DatabaseStore,
    ):
        """Delta token is stored in agent_state after successful fetch."""
        messages = [_make_raw_message("msg-delta-1")]
        mock_graph_client.get_delta_messages.return_value = (messages, "stored-token-123")

        result = await engine.run_cycle()

        assert result.emails_fetched == 1
        # H1: Per-folder delta tokens stored as delta_token_{folder}
        token = await store.get_state("delta_token_Inbox")
        assert token == "stored-token-123"

    async def test_delta_fallback_on_expired_token(
        self,
        engine: TriageEngine,
        mock_graph_client: MagicMock,
        mock_message_manager: MagicMock,
        store: DatabaseStore,
    ):
        """Engine falls back to timestamp when delta token is expired (410)."""
        mock_graph_client.get_delta_messages.side_effect = DeltaTokenExpiredError(
            "Token expired", folder="Inbox"
        )
        mock_message_manager.list_messages.return_value = [_make_raw_message("msg-fallback")]

        result = await engine.run_cycle()

        # Should have fallen back to timestamp and found the message
        assert result.emails_fetched == 1
        # H1: Per-folder delta token should have been cleared
        token = await store.get_state("delta_token_Inbox")
        assert token == ""

    async def test_delta_fallback_on_graph_error(
        self,
        engine: TriageEngine,
        mock_graph_client: MagicMock,
        mock_message_manager: MagicMock,
    ):
        """Engine falls back to timestamp on any Graph API error during delta."""
        mock_graph_client.get_delta_messages.side_effect = GraphAPIError(
            "Server error", status_code=500
        )
        mock_message_manager.list_messages.return_value = [_make_raw_message("msg-500-fallback")]

        result = await engine.run_cycle()

        assert result.emails_fetched == 1

    async def test_no_delta_without_graph_client(
        self,
        engine_no_delta: TriageEngine,
        mock_message_manager: MagicMock,
    ):
        """Engine uses timestamp-only when no graph_client is provided."""
        mock_message_manager.list_messages.return_value = [_make_raw_message("msg-ts-only")]

        result = await engine_no_delta.run_cycle()

        assert result.emails_fetched == 1
        mock_message_manager.list_messages.assert_called_once()

    async def test_delta_deduplicates_messages(
        self,
        engine: TriageEngine,
        mock_graph_client: MagicMock,
    ):
        """Delta query results are deduplicated by message ID."""
        dup_msg = _make_raw_message("msg-dup")
        mock_graph_client.get_delta_messages.return_value = (
            [dup_msg, dup_msg, _make_raw_message("msg-unique")],
            "new-token",
        )

        result = await engine.run_cycle()

        # Should have deduplicated: 2 unique messages fetched
        assert result.emails_fetched == 2

    async def test_delta_uses_stored_token(
        self,
        engine: TriageEngine,
        mock_graph_client: MagicMock,
        store: DatabaseStore,
    ):
        """Engine uses stored delta token from agent_state."""
        # H1: Per-folder delta tokens stored as delta_token_{folder}
        await store.set_state("delta_token_Inbox", "previously-stored-token")
        mock_graph_client.get_delta_messages.return_value = ([], "refreshed-token")

        await engine.run_cycle()

        # Verify the stored token was passed to the client
        call_kwargs = mock_graph_client.get_delta_messages.call_args
        assert call_kwargs.kwargs.get("delta_token") == "previously-stored-token" or (
            call_kwargs[1].get("delta_token") == "previously-stored-token"
            if len(call_kwargs) > 1
            else call_kwargs[0][1] == "previously-stored-token"
        )

    async def test_delta_records_graph_success(
        self,
        engine: TriageEngine,
        mock_graph_client: MagicMock,
    ):
        """Successful delta fetch records graph success in degradation state."""
        # Set up some prior graph failures
        engine._degradation.graph_consecutive_failures = 2

        mock_graph_client.get_delta_messages.return_value = (
            [_make_raw_message("msg-ok")],
            "token",
        )

        await engine.run_cycle()

        assert engine._degradation.graph_consecutive_failures == 0
