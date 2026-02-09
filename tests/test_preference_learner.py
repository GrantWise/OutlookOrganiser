"""Tests for preference learning from user corrections (Feature 2D).

Tests the PreferenceLearner class, correction detection, preference update
prompt assembly, manage_category tool, and available categories section.
"""

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from assistant.chat.tools import (
    MANAGE_CATEGORY_TOOL,
    ToolExecutionContext,
    execute_manage_category,
)
from assistant.classifier.preference_learner import PreferenceLearner
from assistant.classifier.prompts import (
    PREFERENCE_UPDATE_PROMPT,
    build_available_categories_section,
)
from assistant.config_schema import AppConfig
from assistant.db.store import DatabaseStore, Email

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_config(sample_config_dict: dict[str, Any]) -> AppConfig:
    """Config with learning enabled."""
    d = {**sample_config_dict}
    d["learning"] = {
        "enabled": True,
        "min_corrections_to_update": 3,
        "lookback_days": 7,
        "max_preferences_words": 500,
    }
    return AppConfig(**d)


@pytest.fixture
def sample_config_disabled(sample_config_dict: dict[str, Any]) -> AppConfig:
    """Config with learning disabled."""
    d = {**sample_config_dict}
    d["learning"] = {"enabled": False}
    return AppConfig(**d)


@pytest.fixture
async def store(data_dir: Path) -> DatabaseStore:
    """Return an initialized DatabaseStore."""
    db_path = data_dir / "test_learner.db"
    s = DatabaseStore(db_path)
    await s.initialize()
    return s


@pytest.fixture
def mock_anthropic() -> MagicMock:
    """Return a mock Anthropic client."""
    client = MagicMock()

    # Mock response with text block
    response = MagicMock()
    text_block = MagicMock()
    text_block.type = "text"
    text_block.text = (
        "- Emails from legal@translution.com should be P2 - Important\n"
        "- SYSPRO infrastructure emails go to Areas/Development"
    )
    response.content = [text_block]
    client.messages = MagicMock()
    client.messages.create = AsyncMock(return_value=response)

    return client


@pytest.fixture
def learner(
    store: DatabaseStore,
    mock_anthropic: MagicMock,
    sample_config: AppConfig,
) -> PreferenceLearner:
    """Return a PreferenceLearner with mocked dependencies."""
    return PreferenceLearner(store, mock_anthropic, sample_config)


@pytest.fixture
def learner_disabled(
    store: DatabaseStore,
    mock_anthropic: MagicMock,
    sample_config_disabled: AppConfig,
) -> PreferenceLearner:
    """Return a PreferenceLearner with learning disabled."""
    return PreferenceLearner(store, mock_anthropic, sample_config_disabled)


async def _seed_correction(
    store: DatabaseStore,
    email_id: str,
    subject: str = "Test Email",
    sender_email: str = "test@example.com",
    suggested_folder: str = "Reference/Newsletters",
    approved_folder: str = "Areas/Development",
    suggested_priority: str = "P4 - Low",
    approved_priority: str = "P2 - Important",
    age_hours: int = 12,
) -> int:
    """Seed an email + partial suggestion (correction) in the DB."""
    await store.save_email(
        Email(
            id=email_id,
            subject=subject,
            sender_email=sender_email,
            sender_name="Test Sender",
            received_at=datetime.now(),
            snippet="test snippet",
        )
    )
    sid = await store.create_suggestion(
        email_id=email_id,
        suggested_folder=suggested_folder,
        suggested_priority=suggested_priority,
        suggested_action_type="FYI Only",
        confidence=0.75,
        reasoning="Test classification",
    )
    # Approve with corrections (status becomes 'partial')
    await store.approve_suggestion(
        sid,
        approved_folder=approved_folder,
        approved_priority=approved_priority,
        approved_action_type="Review",
    )
    # Backdate resolved_at
    backdated = (datetime.now() - timedelta(hours=age_hours)).isoformat()
    async with store._db() as db:
        await db.execute(
            "UPDATE suggestions SET resolved_at = ? WHERE id = ?",
            (backdated, sid),
        )
        await db.commit()

    return sid


# ---------------------------------------------------------------------------
# Tests: Correction detection
# ---------------------------------------------------------------------------


async def test_get_recent_corrections_detects_folder_mismatch(store: DatabaseStore):
    """Corrections are detected when approved_folder differs from suggested_folder."""
    await _seed_correction(
        store,
        "email-c1",
        suggested_folder="Reference/Newsletters",
        approved_folder="Areas/Development",
    )

    corrections = await store.get_recent_corrections(days=7)

    assert len(corrections) == 1
    assert corrections[0]["suggested_folder"] == "Reference/Newsletters"
    assert corrections[0]["approved_folder"] == "Areas/Development"


async def test_get_recent_corrections_detects_priority_mismatch(store: DatabaseStore):
    """Corrections include priority mismatches."""
    await _seed_correction(
        store,
        "email-c2",
        suggested_priority="P4 - Low",
        approved_priority="P2 - Important",
    )

    corrections = await store.get_recent_corrections(days=7)

    assert len(corrections) == 1
    assert corrections[0]["suggested_priority"] == "P4 - Low"
    assert corrections[0]["approved_priority"] == "P2 - Important"


async def test_corrections_outside_window_excluded(store: DatabaseStore):
    """Corrections older than lookback window are excluded."""
    await _seed_correction(store, "email-old", age_hours=200)  # ~8 days old

    corrections = await store.get_recent_corrections(days=7)

    assert len(corrections) == 0


async def test_correction_count_since(store: DatabaseStore):
    """Count corrections since a timestamp."""
    for i in range(5):
        await _seed_correction(store, f"email-cnt-{i}", age_hours=12)

    since = datetime.now() - timedelta(days=1)
    count = await store.get_correction_count_since(since)

    assert count == 5


# ---------------------------------------------------------------------------
# Tests: PreferenceLearner
# ---------------------------------------------------------------------------


async def test_min_threshold_respected(
    learner: PreferenceLearner,
    store: DatabaseStore,
):
    """Update is skipped when corrections below threshold."""
    # Only 2 corrections (threshold is 3)
    await _seed_correction(store, "email-t1")
    await _seed_correction(store, "email-t2")

    result = await learner.check_and_update()

    assert result is None


async def test_update_triggers_above_threshold(
    learner: PreferenceLearner,
    store: DatabaseStore,
    mock_anthropic: MagicMock,
):
    """Update triggers when corrections meet threshold."""
    for i in range(4):
        await _seed_correction(store, f"email-trigger-{i}")

    result = await learner.check_and_update()

    assert result is not None
    assert result.corrections_analyzed == 4
    assert result.changed is True
    mock_anthropic.messages.create.assert_called_once()


async def test_prompt_includes_corrections_and_preferences(
    learner: PreferenceLearner,
    store: DatabaseStore,
    mock_anthropic: MagicMock,
):
    """Prompt assembly includes formatted corrections and current preferences."""
    # Set existing preferences
    await store.set_state(
        "classification_preferences", "Existing preference: newsletters go to P4."
    )

    for i in range(3):
        await _seed_correction(store, f"email-prompt-{i}")

    await learner.update_preferences()

    # Check the prompt sent to Claude
    call_args = mock_anthropic.messages.create.call_args
    prompt_text = call_args.kwargs["messages"][0]["content"]
    assert "Correction 1:" in prompt_text
    assert "Existing preference: newsletters go to P4." in prompt_text


async def test_storage_roundtrip(
    learner: PreferenceLearner,
    store: DatabaseStore,
):
    """Updated preferences are stored and retrievable from agent_state."""
    for i in range(3):
        await _seed_correction(store, f"email-rt-{i}")

    await learner.update_preferences()

    stored = await store.get_state("classification_preferences")
    assert stored is not None
    assert "legal@translution.com" in stored or "SYSPRO" in stored


async def test_existing_preferences_preserved(
    learner: PreferenceLearner,
    store: DatabaseStore,
    mock_anthropic: MagicMock,
):
    """Existing preferences are passed to Claude for preservation."""
    await store.set_state(
        "classification_preferences",
        "Always classify CEO emails as P1.",
    )

    for i in range(3):
        await _seed_correction(store, f"email-pres-{i}")

    await learner.update_preferences()

    call_args = mock_anthropic.messages.create.call_args
    prompt_text = call_args.kwargs["messages"][0]["content"]
    assert "Always classify CEO emails as P1." in prompt_text


async def test_claude_failure_keeps_existing_preferences(
    learner: PreferenceLearner,
    store: DatabaseStore,
    mock_anthropic: MagicMock,
):
    """Claude API failure keeps existing preferences unchanged."""
    await store.set_state("classification_preferences", "Original preferences.")
    mock_anthropic.messages.create.side_effect = Exception("API down")

    for i in range(3):
        await _seed_correction(store, f"email-fail-{i}")

    result = await learner.update_preferences()

    assert result.changed is False
    assert result.preferences_after == "Original preferences."
    stored = await store.get_state("classification_preferences")
    assert stored == "Original preferences."


async def test_word_count_limit_truncation(
    learner: PreferenceLearner,
    store: DatabaseStore,
    mock_anthropic: MagicMock,
    sample_config: AppConfig,
):
    """Preferences exceeding max words are truncated."""
    # Return a very long response
    long_text = " ".join(["word"] * 1000)
    text_block = MagicMock()
    text_block.type = "text"
    text_block.text = long_text
    response = MagicMock()
    response.content = [text_block]
    mock_anthropic.messages.create.return_value = response

    for i in range(3):
        await _seed_correction(store, f"email-trunc-{i}")

    result = await learner.update_preferences()

    assert result.changed is True
    word_count = len(result.preferences_after.split())
    assert word_count <= sample_config.learning.max_preferences_words


async def test_learning_disabled_skips(learner_disabled: PreferenceLearner):
    """Learning disabled in config returns None."""
    result = await learner_disabled.check_and_update()
    assert result is None


async def test_empty_response_keeps_preferences(
    learner: PreferenceLearner,
    store: DatabaseStore,
    mock_anthropic: MagicMock,
):
    """Empty Claude response keeps existing preferences."""
    await store.set_state("classification_preferences", "Keep me.")
    text_block = MagicMock()
    text_block.type = "text"
    text_block.text = ""
    response = MagicMock()
    response.content = [text_block]
    mock_anthropic.messages.create.return_value = response

    for i in range(3):
        await _seed_correction(store, f"email-empty-{i}")

    result = await learner.update_preferences()

    assert result.changed is False
    assert result.preferences_after == "Keep me."


# ---------------------------------------------------------------------------
# Tests: manage_category tool
# ---------------------------------------------------------------------------


async def test_manage_category_create():
    """manage_category creates a user-tier category."""
    ctx = MagicMock(spec=ToolExecutionContext)
    ctx.category_manager = MagicMock()

    result = await execute_manage_category(
        {"action": "create", "category_name": "Board Meetings", "color_preset": "preset5"},
        ctx,
    )

    assert "Created" in result
    ctx.category_manager.create_category.assert_called_once_with("Board Meetings", "preset5")


async def test_manage_category_delete():
    """manage_category deletes a user-tier category."""
    ctx = MagicMock(spec=ToolExecutionContext)
    ctx.category_manager = MagicMock()

    result = await execute_manage_category(
        {"action": "delete", "category_name": "Obsolete Tag"},
        ctx,
    )

    assert "Deleted" in result
    ctx.category_manager.delete_category.assert_called_once_with("Obsolete Tag")


async def test_manage_category_rejects_framework_deletion():
    """manage_category rejects deletion of framework categories."""
    ctx = MagicMock(spec=ToolExecutionContext)
    ctx.category_manager = MagicMock()

    result = await execute_manage_category(
        {"action": "delete", "category_name": "P1 - Urgent Important"},
        ctx,
    )

    assert "framework category" in result
    ctx.category_manager.delete_category.assert_not_called()


async def test_manage_category_rejects_taxonomy():
    """manage_category rejects taxonomy categories (Projects/Areas)."""
    ctx = MagicMock(spec=ToolExecutionContext)
    ctx.category_manager = MagicMock()

    result = await execute_manage_category(
        {"action": "delete", "category_name": "Projects/Acme"},
        ctx,
    )

    assert "taxonomy category" in result
    ctx.category_manager.delete_category.assert_not_called()


async def test_manage_category_no_category_manager():
    """manage_category handles missing category_manager gracefully."""
    ctx = MagicMock(spec=ToolExecutionContext)
    ctx.category_manager = None

    result = await execute_manage_category(
        {"action": "create", "category_name": "Test"},
        ctx,
    )

    assert "not available" in result


# ---------------------------------------------------------------------------
# Tests: Available categories section
# ---------------------------------------------------------------------------


def test_available_categories_empty():
    """Empty categories returns placeholder."""
    result = build_available_categories_section([])
    assert result == "No categories configured."


def test_available_categories_groups_by_tier():
    """Categories are grouped by framework, taxonomy, user tiers."""
    categories = [
        "P1 - Urgent Important",
        "P2 - Important",
        "Needs Reply",
        "Projects/Acme",
        "Areas/Finance",
        "Board Meetings",
        "VIP Contacts",
    ]

    result = build_available_categories_section(categories)

    assert "Framework:" in result
    assert "P1 - Urgent Important" in result
    assert "Needs Reply" in result
    assert "Taxonomy:" in result
    assert "Projects/Acme" in result
    assert "Areas/Finance" in result
    assert "User:" in result
    assert "Board Meetings" in result
    assert "VIP Contacts" in result


def test_available_categories_framework_only():
    """Only framework categories produces single group."""
    result = build_available_categories_section(["P1 - Urgent Important", "Review"])
    assert "Framework:" in result
    assert "Taxonomy:" not in result
    assert "User:" not in result


# ---------------------------------------------------------------------------
# Tests: Preference update prompt format
# ---------------------------------------------------------------------------


def test_preference_update_prompt_template():
    """PREFERENCE_UPDATE_PROMPT template has required placeholders."""
    assert "{corrections_formatted}" in PREFERENCE_UPDATE_PROMPT
    assert "{current_preferences}" in PREFERENCE_UPDATE_PROMPT
    assert "{max_words}" in PREFERENCE_UPDATE_PROMPT
    assert "{lookback_days}" in PREFERENCE_UPDATE_PROMPT


def test_manage_category_tool_schema():
    """MANAGE_CATEGORY_TOOL has required schema fields."""
    assert MANAGE_CATEGORY_TOOL["name"] == "manage_category"
    props = MANAGE_CATEGORY_TOOL["input_schema"]["properties"]
    assert "action" in props
    assert "category_name" in props
    assert set(props["action"]["enum"]) == {"create", "delete"}
