"""Tests for the POST /api/chat endpoint.

Covers round-trip with mocked ChatAssistant, missing suggestion handling,
and 503 when Anthropic client is unavailable.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from assistant.chat.assistant import ChatResponse
from assistant.config_schema import AppConfig
from assistant.db.store import DatabaseStore, Email
from assistant.web.app import create_app

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def config(sample_config_dict: dict[str, Any]) -> AppConfig:
    """Return a minimal AppConfig."""
    return AppConfig(**sample_config_dict)


@pytest.fixture
async def store(data_dir: Path) -> DatabaseStore:
    """Return an initialized DatabaseStore."""
    db_path = data_dir / "test_chat_endpoint.db"
    s = DatabaseStore(db_path)
    await s.initialize()
    return s


@asynccontextmanager
async def _noop_lifespan(app: FastAPI):
    """No-op lifespan that preserves existing app.state."""
    yield


def _make_app(store: DatabaseStore, config: AppConfig, *, with_anthropic: bool = True) -> FastAPI:
    """Create a FastAPI app with test dependencies."""
    app = create_app()
    app.state.store = store
    app.state.config = config
    app.state.message_manager = None
    app.state.folder_manager = None
    app.state.triage_engine = None
    app.state.scheduler = None
    app.state.anthropic_client = MagicMock() if with_anthropic else None
    return app


async def _make_client(app: FastAPI) -> AsyncClient:
    """Return an httpx AsyncClient for the app."""
    app.router.lifespan_context = _noop_lifespan
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


async def _seed_email_and_suggestion(store: DatabaseStore) -> tuple[str, int]:
    """Seed an email + suggestion, return (email_id, suggestion_id)."""
    email = Email(
        id="chat-ep-001",
        conversation_id="chat-ep-conv",
        subject="Chat Endpoint Test",
        sender_email="sender@ep-test.com",
        sender_name="EP Tester",
        received_at=datetime.now(UTC),
        snippet="Endpoint test email body.",
        current_folder="Inbox",
    )
    await store.save_email(email)
    sid = await store.create_suggestion(
        email_id=email.id,
        suggested_folder="Projects/Test",
        suggested_priority="P2 - Important",
        suggested_action_type="Review",
        confidence=0.85,
        reasoning="Endpoint test reasoning",
    )
    return email.id, sid


# ---------------------------------------------------------------------------
# Tests: Successful round-trip
# ---------------------------------------------------------------------------


async def test_chat_round_trip(store: DatabaseStore, config: AppConfig):
    """POST /api/chat returns reply and actions from ChatAssistant."""
    _email_id, sid = await _seed_email_and_suggestion(store)

    app = _make_app(store, config)
    client = await _make_client(app)

    mock_result = ChatResponse(
        reply="I've reclassified this email.",
        actions_taken=[{"tool_name": "reclassify_email", "input": {}, "result": "ok"}],
    )

    with patch("assistant.chat.assistant.ChatAssistant") as MockAssistant:
        instance = MockAssistant.return_value
        instance.chat = AsyncMock(return_value=mock_result)

        response = await client.post(
            "/api/chat",
            json={
                "suggestion_id": sid,
                "messages": [{"role": "user", "content": "Move this to finance"}],
            },
        )

    await client.aclose()

    assert response.status_code == 200
    data = response.json()
    assert data["reply"] == "I've reclassified this email."
    assert len(data["actions_taken"]) == 1


async def test_chat_no_actions(store: DatabaseStore, config: AppConfig):
    """Chat response with no actions returns empty list."""
    _email_id, sid = await _seed_email_and_suggestion(store)

    app = _make_app(store, config)
    client = await _make_client(app)

    mock_result = ChatResponse(reply="This is a finance email.", actions_taken=[])

    with patch("assistant.chat.assistant.ChatAssistant") as MockAssistant:
        instance = MockAssistant.return_value
        instance.chat = AsyncMock(return_value=mock_result)

        response = await client.post(
            "/api/chat",
            json={
                "suggestion_id": sid,
                "messages": [{"role": "user", "content": "What is this?"}],
            },
        )

    await client.aclose()

    assert response.status_code == 200
    data = response.json()
    assert data["reply"] == "This is a finance email."
    assert data["actions_taken"] == []


# ---------------------------------------------------------------------------
# Tests: Error conditions
# ---------------------------------------------------------------------------


async def test_chat_error_returns_422(store: DatabaseStore, config: AppConfig):
    """ChatAssistant error is returned as 422 with detail."""
    _email_id, sid = await _seed_email_and_suggestion(store)

    app = _make_app(store, config)
    client = await _make_client(app)

    mock_result = ChatResponse(
        reply="",
        error="Suggestion 99999 not found.",
    )

    with patch("assistant.chat.assistant.ChatAssistant") as MockAssistant:
        instance = MockAssistant.return_value
        instance.chat = AsyncMock(return_value=mock_result)

        response = await client.post(
            "/api/chat",
            json={
                "suggestion_id": sid,
                "messages": [{"role": "user", "content": "Hello"}],
            },
        )

    await client.aclose()

    assert response.status_code == 422
    assert "not found" in response.json()["detail"]


async def test_chat_no_anthropic_client_returns_503(store: DatabaseStore, config: AppConfig):
    """503 returned when Anthropic client is not available."""
    app = _make_app(store, config, with_anthropic=False)
    client = await _make_client(app)

    response = await client.post(
        "/api/chat",
        json={
            "suggestion_id": 1,
            "messages": [{"role": "user", "content": "Hello"}],
        },
    )

    await client.aclose()

    assert response.status_code == 503
    assert "Anthropic" in response.json()["detail"]


async def test_chat_invalid_request_body(store: DatabaseStore, config: AppConfig):
    """Missing required fields returns 422 validation error."""
    app = _make_app(store, config)
    client = await _make_client(app)

    response = await client.post(
        "/api/chat",
        json={"messages": [{"role": "user", "content": "Hello"}]},
        # Missing suggestion_id
    )

    await client.aclose()

    assert response.status_code == 422
