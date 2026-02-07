"""Tests for chat assistant tool execution functions.

Covers reclassify (single/thread), auto-rule management, signal updates,
and project/area creation — all with mocked Graph API managers.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import yaml

from assistant.chat.tools import (
    ToolExecutionContext,
    execute_add_auto_rule,
    execute_create_project_or_area,
    execute_reclassify,
    execute_tool,
    execute_update_signals,
)
from assistant.config import reset_config
from assistant.config_schema import (
    AppConfig,
)
from assistant.db.store import DatabaseStore, Email, Suggestion

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def store(data_dir: Path) -> DatabaseStore:
    """Return an initialized DatabaseStore."""
    db_path = data_dir / "test_chat_tools.db"
    s = DatabaseStore(db_path)
    await s.initialize()
    return s


@pytest.fixture
def config_with_project(sample_config_dict: dict[str, Any], tmp_path: Path) -> AppConfig:
    """Return an AppConfig with one project and one area, backed by a config file."""
    sample_config_dict["projects"] = [
        {
            "name": "Alpha Build",
            "folder": "Projects/Alpha",
            "signals": {"subjects": ["alpha"], "senders": [], "body_keywords": []},
        }
    ]
    sample_config_dict["areas"] = [
        {
            "name": "Finance",
            "folder": "Areas/Finance",
            "signals": {"subjects": ["invoice"], "senders": [], "body_keywords": []},
        }
    ]
    # Write the config file so write_config_safely() can back it up
    config_path = tmp_path / "config" / "config.yaml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(yaml.dump(sample_config_dict, default_flow_style=False))
    return AppConfig(**sample_config_dict)


@pytest.fixture
def config_with_auto_rule(sample_config_dict: dict[str, Any]) -> AppConfig:
    """Return an AppConfig with one existing auto-rule."""
    sample_config_dict["auto_rules"] = [
        {
            "name": "Newsletter filter",
            "match": {"senders": ["*@newsletters.com"], "subjects": ["weekly digest"]},
            "action": {
                "folder": "Areas/Newsletters",
                "category": "FYI Only",
                "priority": "P4 - Low",
            },
        }
    ]
    return AppConfig(**sample_config_dict)


@pytest.fixture
async def seed_email(store: DatabaseStore) -> Email:
    """Insert and return a test email."""
    email = Email(
        id="msg-100",
        conversation_id="conv-100",
        subject="Test Subject",
        sender_email="sender@example.com",
        sender_name="Test Sender",
        received_at=datetime.now(UTC),
        snippet="Test snippet content",
        current_folder="Inbox",
    )
    await store.save_email(email)
    return email


@pytest.fixture
async def seed_suggestion(store: DatabaseStore, seed_email: Email) -> Suggestion:
    """Insert a suggestion for the test email and return it."""
    sid = await store.create_suggestion(
        email_id=seed_email.id,
        suggested_folder="Projects/Alpha",
        suggested_priority="P2 - Important",
        suggested_action_type="Review",
        confidence=0.85,
        reasoning="Test classification reasoning",
    )
    suggestion = await store.get_suggestion(sid)
    return suggestion


def _make_ctx(
    email: Email,
    suggestion: Suggestion,
    store: DatabaseStore,
    config: AppConfig,
    *,
    with_graph: bool = False,
) -> ToolExecutionContext:
    """Build a ToolExecutionContext, optionally with mock Graph managers."""
    folder_manager = None
    message_manager = None
    if with_graph:
        folder_manager = MagicMock()
        folder_manager.get_folder_id.return_value = "folder-id-123"
        message_manager = MagicMock()
        message_manager.move_message.return_value = {"id": "new-msg-id"}
        message_manager.set_categories.return_value = None
    return ToolExecutionContext(
        email=email,
        suggestion=suggestion,
        store=store,
        folder_manager=folder_manager,
        message_manager=message_manager,
        config=config,
    )


# ---------------------------------------------------------------------------
# Tests: reclassify_email
# ---------------------------------------------------------------------------


async def test_reclassify_single_approves_and_logs(
    store: DatabaseStore,
    seed_email: Email,
    seed_suggestion: Suggestion,
    sample_config: AppConfig,
):
    """Reclassify with scope=single approves the suggestion and logs an action."""
    ctx = _make_ctx(seed_email, seed_suggestion, store, sample_config)

    result = await execute_reclassify(
        {
            "folder": "Areas/Finance",
            "priority": "P1 - Urgent Important",
            "action_type": "Needs Reply",
            "scope": "single",
            "reasoning": "Belongs to finance",
        },
        ctx,
    )

    assert "Reclassified 1 email" in result

    # Suggestion should be resolved with new values
    # Status is "partial" because approved values differ from suggested values
    updated = await store.get_suggestion(seed_suggestion.id)
    assert updated.status in ("approved", "partial")
    assert updated.approved_folder == "Areas/Finance"
    assert updated.approved_priority == "P1 - Urgent Important"

    # Action should be logged
    logs = await store.get_action_logs(limit=10, action_type="move")
    assert any(log.triggered_by == "chat_assistant" for log in logs)


async def test_reclassify_thread_processes_multiple_emails(
    store: DatabaseStore,
    seed_email: Email,
    seed_suggestion: Suggestion,
    sample_config: AppConfig,
):
    """Reclassify with scope=thread processes all emails in the conversation."""
    # Add a second email in the same conversation
    email2 = Email(
        id="msg-101",
        conversation_id="conv-100",
        subject="Re: Test Subject",
        sender_email="other@example.com",
        sender_name="Other Person",
        received_at=datetime.now(UTC),
        snippet="Reply content",
        current_folder="Inbox",
    )
    await store.save_email(email2)

    ctx = _make_ctx(seed_email, seed_suggestion, store, sample_config)

    result = await execute_reclassify(
        {
            "folder": "Projects/Alpha",
            "priority": "P2 - Important",
            "action_type": "Review",
            "scope": "thread",
            "reasoning": "Thread belongs to Alpha",
        },
        ctx,
    )

    assert "Reclassified 2 email" in result

    # Both emails should have inherited_folder updated
    em1 = await store.get_email("msg-100")
    em2 = await store.get_email("msg-101")
    assert em1.inherited_folder == "Projects/Alpha"
    assert em2.inherited_folder == "Projects/Alpha"


async def test_reclassify_with_graph_calls_move(
    store: DatabaseStore,
    seed_email: Email,
    seed_suggestion: Suggestion,
    sample_config: AppConfig,
):
    """When Graph managers are available, reclassify moves emails."""
    ctx = _make_ctx(seed_email, seed_suggestion, store, sample_config, with_graph=True)

    result = await execute_reclassify(
        {
            "folder": "Areas/Finance",
            "priority": "P2 - Important",
            "action_type": "FYI Only",
            "scope": "single",
            "reasoning": "Just FYI",
        },
        ctx,
    )

    assert "Reclassified 1" in result
    ctx.folder_manager.get_folder_id.assert_called_once_with("Areas/Finance")
    ctx.message_manager.move_message.assert_called_once()


# ---------------------------------------------------------------------------
# Tests: add_auto_rule
# ---------------------------------------------------------------------------


async def test_add_auto_rule_success(
    store: DatabaseStore,
    seed_email: Email,
    seed_suggestion: Suggestion,
    config_with_project: AppConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Adding a valid auto-rule writes config and logs the action."""
    config_path = tmp_path / "config" / "config.yaml"
    monkeypatch.setenv("ASSISTANT_CONFIG_PATH", str(config_path))
    reset_config()

    ctx = _make_ctx(seed_email, seed_suggestion, store, config_with_project)

    result = await execute_add_auto_rule(
        {
            "name": "Test Rule",
            "senders": ["*@test.com"],
            "subjects": [],
            "folder": "Projects/Alpha",
            "category": "Review",
            "priority": "P2 - Important",
        },
        ctx,
    )

    assert "added successfully" in result

    # Verify the config was actually written
    loaded = yaml.safe_load(config_path.read_text())
    assert len(loaded["auto_rules"]) == 1
    assert loaded["auto_rules"][0]["name"] == "Test Rule"

    # Action log entry created
    logs = await store.get_action_logs(limit=10, action_type="config_change")
    assert len(logs) >= 1


async def test_add_auto_rule_requires_senders_or_subjects(
    store: DatabaseStore,
    seed_email: Email,
    seed_suggestion: Suggestion,
    sample_config: AppConfig,
):
    """Error if neither senders nor subjects are provided."""
    ctx = _make_ctx(seed_email, seed_suggestion, store, sample_config)

    result = await execute_add_auto_rule(
        {
            "name": "Empty Rule",
            "folder": "Inbox",
            "category": "FYI Only",
            "priority": "P4 - Low",
        },
        ctx,
    )

    assert "Error" in result
    assert "senders" in result.lower() or "subjects" in result.lower()


async def test_add_auto_rule_conflict_detection(
    store: DatabaseStore,
    seed_email: Email,
    seed_suggestion: Suggestion,
    config_with_auto_rule: AppConfig,
):
    """Overlapping patterns with existing rules are rejected."""
    ctx = _make_ctx(seed_email, seed_suggestion, store, config_with_auto_rule)

    result = await execute_add_auto_rule(
        {
            "name": "Duplicate Rule",
            "senders": ["*@newsletters.com"],
            "folder": "Areas/Other",
            "category": "Review",
            "priority": "P3 - Urgent Low",
        },
        ctx,
    )

    assert "Warning" in result
    assert "Overlapping" in result
    assert "NOT added" in result


# ---------------------------------------------------------------------------
# Tests: update_project_signals
# ---------------------------------------------------------------------------


async def test_update_signals_adds_new_keywords(
    store: DatabaseStore,
    seed_email: Email,
    seed_suggestion: Suggestion,
    config_with_project: AppConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Adding new signals to an existing project succeeds."""
    config_path = tmp_path / "config" / "config.yaml"
    monkeypatch.setenv("ASSISTANT_CONFIG_PATH", str(config_path))
    reset_config()

    ctx = _make_ctx(seed_email, seed_suggestion, store, config_with_project)

    result = await execute_update_signals(
        {
            "target_name": "Alpha Build",
            "add_subjects": ["beta", "gamma"],
            "add_senders": ["*@alpha.com"],
        },
        ctx,
    )

    assert "Updated" in result
    assert "2 new signal" in result or "3 new signal" in result

    # Verify config written correctly
    loaded = yaml.safe_load(config_path.read_text())
    project_signals = loaded["projects"][0]["signals"]
    assert "beta" in project_signals["subjects"]
    assert "*@alpha.com" in project_signals["senders"]


async def test_update_signals_deduplicates(
    store: DatabaseStore,
    seed_email: Email,
    seed_suggestion: Suggestion,
    config_with_project: AppConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Adding signals that already exist doesn't duplicate them."""
    config_path = tmp_path / "config" / "config.yaml"
    monkeypatch.setenv("ASSISTANT_CONFIG_PATH", str(config_path))
    reset_config()

    ctx = _make_ctx(seed_email, seed_suggestion, store, config_with_project)

    result = await execute_update_signals(
        {
            "target_name": "Alpha Build",
            "add_subjects": ["alpha"],  # Already exists
        },
        ctx,
    )

    assert "already exist" in result.lower()


async def test_update_signals_target_not_found(
    store: DatabaseStore,
    seed_email: Email,
    seed_suggestion: Suggestion,
    config_with_project: AppConfig,
):
    """Error returned when target project/area doesn't exist."""
    ctx = _make_ctx(seed_email, seed_suggestion, store, config_with_project)

    result = await execute_update_signals(
        {
            "target_name": "Nonexistent",
            "add_subjects": ["test"],
        },
        ctx,
    )

    assert "Error" in result
    assert "Nonexistent" in result


async def test_update_signals_works_on_areas(
    store: DatabaseStore,
    seed_email: Email,
    seed_suggestion: Suggestion,
    config_with_project: AppConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Signals can be updated on areas, not just projects."""
    config_path = tmp_path / "config" / "config.yaml"
    monkeypatch.setenv("ASSISTANT_CONFIG_PATH", str(config_path))
    reset_config()

    ctx = _make_ctx(seed_email, seed_suggestion, store, config_with_project)

    result = await execute_update_signals(
        {
            "target_name": "Finance",
            "add_body_keywords": ["payment", "receipt"],
        },
        ctx,
    )

    assert "Updated" in result
    assert "Finance" in result


# ---------------------------------------------------------------------------
# Tests: create_project_or_area
# ---------------------------------------------------------------------------


async def test_create_project_success(
    store: DatabaseStore,
    seed_email: Email,
    seed_suggestion: Suggestion,
    config_with_project: AppConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Creating a new project writes it to config."""
    config_path = tmp_path / "config" / "config.yaml"
    monkeypatch.setenv("ASSISTANT_CONFIG_PATH", str(config_path))
    reset_config()

    ctx = _make_ctx(seed_email, seed_suggestion, store, config_with_project)

    result = await execute_create_project_or_area(
        {
            "type": "project",
            "name": "New Widget",
            "folder": "Projects/New Widget",
            "subjects": ["widget"],
            "senders": ["*@widget.co"],
        },
        ctx,
    )

    assert "Created project" in result

    loaded = yaml.safe_load(config_path.read_text())
    assert len(loaded["projects"]) == 2
    assert loaded["projects"][1]["name"] == "New Widget"


async def test_create_area_success(
    store: DatabaseStore,
    seed_email: Email,
    seed_suggestion: Suggestion,
    config_with_project: AppConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Creating a new area writes it to config."""
    config_path = tmp_path / "config" / "config.yaml"
    monkeypatch.setenv("ASSISTANT_CONFIG_PATH", str(config_path))
    reset_config()

    ctx = _make_ctx(seed_email, seed_suggestion, store, config_with_project)

    result = await execute_create_project_or_area(
        {
            "type": "area",
            "name": "HR",
            "folder": "Areas/HR",
        },
        ctx,
    )

    assert "Created area" in result


async def test_create_project_wrong_folder_prefix(
    store: DatabaseStore,
    seed_email: Email,
    seed_suggestion: Suggestion,
    sample_config: AppConfig,
):
    """Error if folder doesn't start with a valid PARA prefix."""
    ctx = _make_ctx(seed_email, seed_suggestion, store, sample_config)

    result = await execute_create_project_or_area(
        {
            "type": "project",
            "name": "Bad Prefix",
            "folder": "Random/BadPrefix",
        },
        ctx,
    )

    assert "Error" in result
    assert "Projects/" in result  # Listed in error as valid prefix


async def test_create_duplicate_name_rejected(
    store: DatabaseStore,
    seed_email: Email,
    seed_suggestion: Suggestion,
    config_with_project: AppConfig,
):
    """Error if a project/area with the same name already exists."""
    ctx = _make_ctx(seed_email, seed_suggestion, store, config_with_project)

    result = await execute_create_project_or_area(
        {
            "type": "project",
            "name": "Alpha Build",  # Already exists
            "folder": "Projects/Alpha Duplicate",
        },
        ctx,
    )

    assert "Error" in result
    assert "already exists" in result


# ---------------------------------------------------------------------------
# Tests: Tool dispatcher
# ---------------------------------------------------------------------------


async def test_execute_tool_unknown_tool(
    store: DatabaseStore,
    seed_email: Email,
    seed_suggestion: Suggestion,
    sample_config: AppConfig,
):
    """Unknown tool name returns an error string, not an exception."""
    ctx = _make_ctx(seed_email, seed_suggestion, store, sample_config)

    result = await execute_tool("nonexistent_tool", {}, ctx)

    assert "Unknown tool" in result


async def test_execute_tool_catches_exceptions(
    store: DatabaseStore,
    seed_email: Email,
    seed_suggestion: Suggestion,
    sample_config: AppConfig,
):
    """If a tool handler raises, the dispatcher returns an error string."""
    ctx = _make_ctx(seed_email, seed_suggestion, store, sample_config)

    # reclassify_email with missing required fields will raise inside the handler
    result = await execute_tool("reclassify_email", {}, ctx)

    assert "failed" in result.lower() or "error" in result.lower()
