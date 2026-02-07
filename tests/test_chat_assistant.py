"""Tests for the ChatAssistant orchestrator.

Covers text-only responses, single/multi-round tool calls,
MAX_TOOL_ROUNDS safety, and API error handling — all with
mocked Anthropic client.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from assistant.chat.assistant import (
    ChatAssistant,
    ChatResponse,
    _extract_text,
    _serialize_content_block,
)
from assistant.config_schema import AppConfig
from assistant.db.store import DatabaseStore, Email

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def store(data_dir: Path) -> DatabaseStore:
    """Return an initialized DatabaseStore."""
    db_path = data_dir / "test_chat_assistant.db"
    s = DatabaseStore(db_path)
    await s.initialize()
    return s


@pytest.fixture
def config(sample_config_dict: dict[str, Any]) -> AppConfig:
    """Return a minimal AppConfig for chat tests."""
    return AppConfig(**sample_config_dict)


@pytest.fixture
async def seed_data(store: DatabaseStore) -> tuple[Email, int]:
    """Seed an email and suggestion, return (email, suggestion_id)."""
    email = Email(
        id="chat-msg-001",
        conversation_id="chat-conv-001",
        subject="Chat Test Email",
        sender_email="sender@chat-test.com",
        sender_name="Chat Tester",
        received_at=datetime.now(UTC),
        snippet="This is a chat test email body.",
        current_folder="Inbox",
    )
    await store.save_email(email)

    sid = await store.create_suggestion(
        email_id=email.id,
        suggested_folder="Projects/Chat",
        suggested_priority="P2 - Important",
        suggested_action_type="Review",
        confidence=0.80,
        reasoning="Test reasoning",
    )
    return email, sid


# ---------------------------------------------------------------------------
# Mock helpers
# ---------------------------------------------------------------------------


def _make_text_block(text: str) -> SimpleNamespace:
    """Create a mock text content block."""
    return SimpleNamespace(type="text", text=text)


def _make_tool_use_block(tool_id: str, name: str, tool_input: dict[str, Any]) -> SimpleNamespace:
    """Create a mock tool_use content block."""
    return SimpleNamespace(type="tool_use", id=tool_id, name=name, input=tool_input)


def _make_response(
    content: list[SimpleNamespace],
    model: str = "claude-sonnet-4-5-20250929",
    input_tokens: int = 100,
    output_tokens: int = 50,
) -> SimpleNamespace:
    """Create a mock Anthropic Message response."""
    return SimpleNamespace(
        content=content,
        model=model,
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens),
        stop_reason="end_turn",
    )


def _make_mock_client(responses: list[SimpleNamespace]) -> MagicMock:
    """Create a mock Anthropic client that returns responses in order."""
    client = MagicMock()
    client.messages.create = MagicMock(side_effect=responses)
    return client


# ---------------------------------------------------------------------------
# Tests: Text-only responses
# ---------------------------------------------------------------------------


async def test_text_only_response(
    store: DatabaseStore, config: AppConfig, seed_data: tuple[Email, int]
):
    """Text-only response returns the reply without tool calls."""
    _email, sid = seed_data

    mock_response = _make_response([_make_text_block("This email is about finance.")])
    client = _make_mock_client([mock_response])

    assistant = ChatAssistant(client, store, config)
    result = await assistant.chat(
        suggestion_id=sid,
        user_messages=[{"role": "user", "content": "What is this email about?"}],
        folder_manager=None,
        message_manager=None,
    )

    assert isinstance(result, ChatResponse)
    assert result.reply == "This email is about finance."
    assert result.actions_taken == []
    assert result.error is None


async def test_multiline_text_response(
    store: DatabaseStore, config: AppConfig, seed_data: tuple[Email, int]
):
    """Multiple text blocks are joined with newlines."""
    _email, sid = seed_data

    mock_response = _make_response(
        [
            _make_text_block("Line 1"),
            _make_text_block("Line 2"),
        ]
    )
    client = _make_mock_client([mock_response])

    assistant = ChatAssistant(client, store, config)
    result = await assistant.chat(
        suggestion_id=sid,
        user_messages=[{"role": "user", "content": "Tell me more"}],
        folder_manager=None,
        message_manager=None,
    )

    assert result.reply == "Line 1\nLine 2"


# ---------------------------------------------------------------------------
# Tests: Single tool call round
# ---------------------------------------------------------------------------


async def test_single_tool_call_round(
    store: DatabaseStore, config: AppConfig, seed_data: tuple[Email, int]
):
    """A single tool call round executes the tool and returns the final text."""
    _email, sid = seed_data

    # Round 1: Claude calls a tool
    tool_response = _make_response(
        [
            _make_text_block("Let me reclassify that."),
            _make_tool_use_block(
                "toolu_123",
                "reclassify_email",
                {
                    "folder": "Areas/Finance",
                    "priority": "P2 - Important",
                    "action_type": "Review",
                    "scope": "single",
                    "reasoning": "Finance email",
                },
            ),
        ]
    )

    # Round 2: Claude responds with text after tool result
    final_response = _make_response(
        [_make_text_block("Done! I've moved the email to Areas/Finance.")]
    )

    client = _make_mock_client([tool_response, final_response])

    assistant = ChatAssistant(client, store, config)
    result = await assistant.chat(
        suggestion_id=sid,
        user_messages=[{"role": "user", "content": "Move this to finance"}],
        folder_manager=None,
        message_manager=None,
    )

    assert "Areas/Finance" in result.reply
    assert len(result.actions_taken) == 1
    assert result.actions_taken[0]["tool_name"] == "reclassify_email"
    assert result.error is None

    # Verify the LLM was called twice (tool round + final response)
    assert client.messages.create.call_count == 2


# ---------------------------------------------------------------------------
# Tests: Multi-round tool calls
# ---------------------------------------------------------------------------


async def test_multi_round_tool_calls(
    store: DatabaseStore, config: AppConfig, seed_data: tuple[Email, int]
):
    """Multiple tool call rounds each execute correctly."""
    _email, sid = seed_data

    # Round 1: First tool call
    resp1 = _make_response(
        [
            _make_tool_use_block(
                "toolu_1",
                "reclassify_email",
                {
                    "folder": "Areas/Finance",
                    "priority": "P2 - Important",
                    "action_type": "Review",
                    "scope": "single",
                    "reasoning": "Finance",
                },
            ),
        ]
    )

    # Round 2: Second tool call
    resp2 = _make_response(
        [
            _make_tool_use_block(
                "toolu_2",
                "update_project_signals",
                {
                    "target_name": "Nonexistent",
                    "add_subjects": ["finance"],
                },
            ),
        ]
    )

    # Round 3: Final text
    resp3 = _make_response([_make_text_block("Both actions completed.")])

    client = _make_mock_client([resp1, resp2, resp3])

    assistant = ChatAssistant(client, store, config)
    result = await assistant.chat(
        suggestion_id=sid,
        user_messages=[{"role": "user", "content": "Reclassify and update signals"}],
        folder_manager=None,
        message_manager=None,
    )

    assert result.reply == "Both actions completed."
    assert len(result.actions_taken) == 2
    assert client.messages.create.call_count == 3


# ---------------------------------------------------------------------------
# Tests: MAX_TOOL_ROUNDS safety
# ---------------------------------------------------------------------------


async def test_max_tool_rounds_safety(
    store: DatabaseStore, config: AppConfig, seed_data: tuple[Email, int]
):
    """Loop exits with fallback message after MAX_TOOL_ROUNDS."""
    _email, sid = seed_data

    # Create 6 tool-only responses (more than MAX_TOOL_ROUNDS=5)
    tool_responses = [
        _make_response(
            [
                _make_tool_use_block(
                    f"toolu_{i}",
                    "update_project_signals",
                    {"target_name": "Nonexistent", "add_subjects": [f"kw{i}"]},
                ),
            ]
        )
        for i in range(6)
    ]

    client = _make_mock_client(tool_responses)

    assistant = ChatAssistant(client, store, config)
    result = await assistant.chat(
        suggestion_id=sid,
        user_messages=[{"role": "user", "content": "Do everything"}],
        folder_manager=None,
        message_manager=None,
    )

    # Should get the fallback message, not crash
    assert "completed" in result.reply.lower() or "changes" in result.reply.lower()
    # Should have called create exactly MAX_TOOL_ROUNDS times
    assert client.messages.create.call_count == 5


# ---------------------------------------------------------------------------
# Tests: Error handling
# ---------------------------------------------------------------------------


async def test_suggestion_not_found(store: DatabaseStore, config: AppConfig):
    """Non-existent suggestion returns error in ChatResponse."""
    client = _make_mock_client([])

    assistant = ChatAssistant(client, store, config)
    result = await assistant.chat(
        suggestion_id=99999,
        user_messages=[{"role": "user", "content": "Hello"}],
        folder_manager=None,
        message_manager=None,
    )

    assert result.error is not None
    assert "99999" in result.error
    assert result.reply == ""


async def test_email_not_found(
    store: DatabaseStore,
    config: AppConfig,
    seed_data: tuple[Email, int],
):
    """If email for suggestion is missing, returns error."""
    _email, sid = seed_data

    client = _make_mock_client([])

    # Mock get_email to return None, simulating a missing email record
    original_get_email = store.get_email

    async def mock_get_email(email_id: str):
        return None

    store.get_email = mock_get_email

    try:
        assistant = ChatAssistant(client, store, config)
        result = await assistant.chat(
            suggestion_id=sid,
            user_messages=[{"role": "user", "content": "Hello"}],
            folder_manager=None,
            message_manager=None,
        )

        assert result.error is not None
        assert "not found" in result.error.lower()
    finally:
        store.get_email = original_get_email


async def test_rate_limit_error(
    store: DatabaseStore, config: AppConfig, seed_data: tuple[Email, int]
):
    """Anthropic RateLimitError returns user-friendly error."""
    import anthropic

    _email, sid = seed_data

    client = MagicMock()
    client.messages.create = MagicMock(
        side_effect=anthropic.RateLimitError(
            message="rate limited",
            response=MagicMock(status_code=429, headers={}),
            body=None,
        )
    )

    assistant = ChatAssistant(client, store, config)
    result = await assistant.chat(
        suggestion_id=sid,
        user_messages=[{"role": "user", "content": "Hello"}],
        folder_manager=None,
        message_manager=None,
    )

    assert result.error is not None
    assert "busy" in result.error.lower() or "try again" in result.error.lower()
    assert result.reply == ""


async def test_connection_error(
    store: DatabaseStore, config: AppConfig, seed_data: tuple[Email, int]
):
    """Anthropic APIConnectionError returns user-friendly error."""
    import anthropic

    _email, sid = seed_data

    client = MagicMock()
    client.messages.create = MagicMock(
        side_effect=anthropic.APIConnectionError(request=MagicMock())
    )

    assistant = ChatAssistant(client, store, config)
    result = await assistant.chat(
        suggestion_id=sid,
        user_messages=[{"role": "user", "content": "Hello"}],
        folder_manager=None,
        message_manager=None,
    )

    assert result.error is not None
    assert "connect" in result.error.lower()


# ---------------------------------------------------------------------------
# Tests: Helper functions
# ---------------------------------------------------------------------------


def test_extract_text_joins_blocks():
    """_extract_text joins multiple text blocks with newlines."""
    response = _make_response(
        [
            _make_text_block("Hello"),
            _make_text_block("World"),
        ]
    )
    assert _extract_text(response) == "Hello\nWorld"


def test_extract_text_ignores_tool_blocks():
    """_extract_text skips non-text blocks."""
    response = _make_response(
        [
            _make_text_block("Before"),
            _make_tool_use_block("t1", "some_tool", {}),
            _make_text_block("After"),
        ]
    )
    assert _extract_text(response) == "Before\nAfter"


def test_serialize_text_block():
    """Text blocks serialize correctly."""
    block = _make_text_block("test text")
    result = _serialize_content_block(block)
    assert result == {"type": "text", "text": "test text"}


def test_serialize_tool_use_block():
    """Tool use blocks serialize with id, name, and input."""
    block = _make_tool_use_block("id-1", "my_tool", {"key": "val"})
    result = _serialize_content_block(block)
    assert result == {
        "type": "tool_use",
        "id": "id-1",
        "name": "my_tool",
        "input": {"key": "val"},
    }


def test_serialize_unknown_block_type():
    """Unknown block types serialize with just the type field."""
    block = SimpleNamespace(type="thinking")
    result = _serialize_content_block(block)
    assert result == {"type": "thinking"}
