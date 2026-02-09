"""Tests for suggestion queue management (Feature 2G).

Tests auto-approval of high-confidence suggestions, expiry of old suggestions,
and Graph API move execution with failure revert.
"""

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from assistant.config_schema import AppConfig
from assistant.core.errors import GraphAPIError
from assistant.db.store import DatabaseStore, Email
from assistant.engine.triage import TriageEngine

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_config(sample_config_dict: dict[str, Any]) -> AppConfig:
    """Config with auto-approve enabled."""
    d = {**sample_config_dict}
    d["suggestion_queue"] = {
        "expire_after_days": 7,
        "auto_approve_confidence": 0.90,
        "auto_approve_delay_hours": 2,
    }
    return AppConfig(**d)


@pytest.fixture
async def store(data_dir: Path) -> DatabaseStore:
    """Return an initialized DatabaseStore."""
    db_path = data_dir / "test_queue.db"
    s = DatabaseStore(db_path)
    await s.initialize()
    return s


@pytest.fixture
def mock_classifier() -> MagicMock:
    """Return a mock EmailClassifier."""
    classifier = MagicMock()
    classifier.refresh_system_prompt = AsyncMock()
    classifier.classify_with_auto_rules = MagicMock(return_value=None)
    classifier.classify_with_claude = AsyncMock()
    return classifier


@pytest.fixture
def mock_message_manager() -> MagicMock:
    """Return a mock MessageManager."""
    mgr = MagicMock()
    mgr.list_messages = MagicMock(return_value=[])
    mgr.check_reply_state = MagicMock(return_value=None)
    mgr.move_message = MagicMock()
    return mgr


@pytest.fixture
def mock_folder_manager() -> MagicMock:
    """Return a mock FolderManager."""
    mgr = MagicMock()
    mgr.get_folder_id = MagicMock(return_value="folder-id-456")
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
    return cache


@pytest.fixture
def mock_graph_client() -> MagicMock:
    """Return a mock GraphClient for batch operations."""
    client = MagicMock()
    # Default: delta queries return empty (tests focus on auto-approve, not fetching)
    client.get_delta_messages = MagicMock(return_value=([], None))

    # Default: batch_move_messages returns success for all moves
    def _batch_move_success(moves: list) -> list:
        return [
            {"id": email_id, "success": True, "status": 200, "new_id": email_id}
            for email_id, _folder_id in moves
        ]

    client.batch_move_messages = MagicMock(side_effect=_batch_move_success)
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
        graph_client=mock_graph_client,
    )


async def _seed_email(store: DatabaseStore, email_id: str = "email-001") -> None:
    """Insert a minimal email for foreign key reference."""
    await store.save_email(
        Email(
            id=email_id,
            subject="Test Email",
            sender_email="test@example.com",
            sender_name="Test Sender",
            received_at=datetime.now(),
            snippet="test snippet",
        )
    )


async def _seed_suggestion(
    store: DatabaseStore,
    email_id: str = "email-001",
    confidence: float = 0.95,
    priority: str = "P2 - Important",
    folder: str = "Projects/Test",
    age_hours: int = 4,
) -> int:
    """Insert a pending suggestion with a specific age."""
    suggestion_id = await store.create_suggestion(
        email_id=email_id,
        suggested_folder=folder,
        suggested_priority=priority,
        suggested_action_type="Review",
        confidence=confidence,
        reasoning="Test classification",
    )

    # Backdate the created_at to simulate age
    backdated = (datetime.now() - timedelta(hours=age_hours)).isoformat()
    async with store._db() as db:
        await db.execute(
            "UPDATE suggestions SET created_at = ? WHERE id = ?",
            (backdated, suggestion_id),
        )
        await db.commit()

    return suggestion_id


# ---------------------------------------------------------------------------
# Tests: Auto-approval
# ---------------------------------------------------------------------------


async def test_high_confidence_auto_approved(store: DatabaseStore):
    """High-confidence suggestions are auto-approved after delay."""
    await _seed_email(store, "email-q1")
    await _seed_suggestion(store, "email-q1", confidence=0.95, age_hours=4)

    approvable = await store.get_auto_approvable_suggestions(
        min_confidence=0.90,
        min_age_hours=2,
    )

    assert len(approvable) == 1
    assert approvable[0].email_id == "email-q1"
    assert approvable[0].confidence == 0.95


async def test_p1_never_auto_approved(store: DatabaseStore):
    """P1 suggestions are never auto-approved regardless of confidence."""
    await _seed_email(store, "email-p1")
    await _seed_suggestion(
        store,
        "email-p1",
        confidence=0.99,
        priority="P1 - Urgent Important",
        age_hours=24,
    )

    approvable = await store.get_auto_approvable_suggestions(
        min_confidence=0.90,
        min_age_hours=2,
    )

    assert len(approvable) == 0


async def test_delay_respected(store: DatabaseStore):
    """Suggestions younger than min_age_hours are not auto-approved."""
    await _seed_email(store, "email-young")
    await _seed_suggestion(store, "email-young", confidence=0.95, age_hours=1)

    approvable = await store.get_auto_approvable_suggestions(
        min_confidence=0.90,
        min_age_hours=2,  # Requires 2 hours, but only 1 hour old
    )

    assert len(approvable) == 0


async def test_low_confidence_not_approved(store: DatabaseStore):
    """Suggestions below threshold are not auto-approved."""
    await _seed_email(store, "email-low")
    await _seed_suggestion(store, "email-low", confidence=0.75, age_hours=4)

    approvable = await store.get_auto_approvable_suggestions(
        min_confidence=0.90,
        min_age_hours=2,
    )

    assert len(approvable) == 0


async def test_auto_approve_sets_approved_fields(store: DatabaseStore):
    """Auto-approved suggestions copy suggested_* to approved_* fields."""
    await _seed_email(store, "email-fields")
    sid = await _seed_suggestion(
        store,
        "email-fields",
        confidence=0.95,
        folder="Projects/Important",
        age_hours=4,
    )

    # C4: Split query + approval pattern (query, then mark individually)
    approvable = await store.get_auto_approvable_suggestions(
        min_confidence=0.90,
        min_age_hours=2,
    )
    for s in approvable:
        await store.mark_suggestion_auto_approved(s.id)

    # Fetch the suggestion directly to check fields
    suggestion = await store.get_suggestion(sid)
    assert suggestion.status == "auto_approved"
    assert suggestion.approved_folder == "Projects/Important"
    assert suggestion.approved_priority == "P2 - Important"
    assert suggestion.approved_action_type == "Review"
    assert suggestion.resolved_at is not None


async def test_graph_move_executed_for_auto_approved(
    engine: TriageEngine,
    store: DatabaseStore,
    mock_graph_client: MagicMock,
    mock_folder_manager: MagicMock,
):
    """Graph API batch move is executed for auto-approved suggestions."""
    await _seed_email(store, "email-move")
    await _seed_suggestion(store, "email-move", confidence=0.95, age_hours=4)

    result = await engine.run_cycle()

    assert result.suggestions_auto_approved == 1
    # C4: batch_move_messages called with [(email_id, folder_id)]
    mock_graph_client.batch_move_messages.assert_called_once_with([("email-move", "folder-id-456")])


async def test_graph_failure_reverts_to_pending(
    engine: TriageEngine,
    store: DatabaseStore,
    mock_graph_client: MagicMock,
):
    """Graph API batch move failure keeps suggestion as pending."""
    await _seed_email(store, "email-fail")
    sid = await _seed_suggestion(store, "email-fail", confidence=0.95, age_hours=4)

    mock_graph_client.batch_move_messages.side_effect = GraphAPIError(
        "Move failed", status_code=500
    )

    result = await engine.run_cycle()

    assert result.suggestions_auto_approved == 0
    suggestion = await store.get_suggestion(sid)
    assert suggestion.status == "pending"


async def test_expired_status_for_old_suggestions(store: DatabaseStore):
    """Old suggestions are marked as expired, not rejected."""
    await _seed_email(store, "email-old")
    sid = await _seed_suggestion(store, "email-old", confidence=0.5, age_hours=200)

    expired_count = await store.expire_old_suggestions(days=7)

    assert expired_count >= 1
    suggestion = await store.get_suggestion(sid)
    assert suggestion.status == "expired"


async def test_action_log_records_auto_approved(
    engine: TriageEngine,
    store: DatabaseStore,
    mock_message_manager: MagicMock,
):
    """Action log records triggered_by='auto_approved' for auto-approved moves."""
    await _seed_email(store, "email-log")
    await _seed_suggestion(store, "email-log", confidence=0.95, age_hours=4)

    await engine.run_cycle()

    # Check action log
    actions = await store.get_action_logs(limit=10)
    auto_approved_actions = [a for a in actions if a.triggered_by == "auto_approved"]
    assert len(auto_approved_actions) >= 1
    assert auto_approved_actions[0].action_type == "move"
