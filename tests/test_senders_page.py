"""Tests for the Senders Page (Feature 2I).

Tests cover:
- Sender list with pagination/filtering
- Sender sort by different columns
- Category/folder updates persist
- Senders page loads (200)
- Sender API endpoints
"""

from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from assistant.config_schema import AppConfig
from assistant.db.store import DatabaseStore
from assistant.web.app import create_app

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_config(sample_config_dict: dict[str, Any]) -> AppConfig:
    """Config for senders testing."""
    return AppConfig(**sample_config_dict)


@pytest.fixture
async def store(data_dir: Path) -> DatabaseStore:
    """Return an initialized DatabaseStore."""
    db_path = data_dir / "test_senders.db"
    s = DatabaseStore(db_path)
    await s.initialize()
    return s


@pytest.fixture
def app(store: DatabaseStore, sample_config: AppConfig) -> FastAPI:
    """Create a FastAPI app with test dependencies."""
    test_app = create_app()
    test_app.state.store = store
    test_app.state.config = sample_config
    test_app.state.message_manager = None
    test_app.state.folder_manager = None
    test_app.state.triage_engine = None
    test_app.state.scheduler = None
    test_app.state.task_manager = None
    test_app.state.category_manager = None
    return test_app


@asynccontextmanager
async def _noop_lifespan(app: FastAPI):
    yield


@pytest.fixture
async def client(app: FastAPI) -> AsyncClient:
    """Return an httpx AsyncClient for the test app."""
    app.router.lifespan_context = _noop_lifespan
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _seed_sender(
    store: DatabaseStore,
    email: str,
    display_name: str = "Test User",
    category: str = "unknown",
    email_count: int = 5,
) -> None:
    """Insert a sender profile directly."""
    async with store._db() as db:
        await db.execute(
            """
            INSERT INTO sender_profiles
                (email, display_name, domain, category, email_count, last_seen)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                email,
                display_name,
                email.split("@")[1] if "@" in email else None,
                category,
                email_count,
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )
        await db.commit()


# ---------------------------------------------------------------------------
# Tests: Sender List
# ---------------------------------------------------------------------------


async def test_list_sender_profiles_empty(store: DatabaseStore):
    """Empty DB returns empty list."""
    senders = await store.list_sender_profiles()
    assert senders == []


async def test_list_sender_profiles_returns_data(store: DatabaseStore):
    """Seeded senders are returned."""
    await _seed_sender(store, "alice@example.com", "Alice", email_count=10)
    await _seed_sender(store, "bob@example.com", "Bob", email_count=5)

    senders = await store.list_sender_profiles()
    assert len(senders) == 2
    # Default sort is email_count desc
    assert senders[0].email == "alice@example.com"
    assert senders[0].email_count == 10


async def test_list_sender_profiles_filter_by_category(store: DatabaseStore):
    """Filter by category returns matching senders."""
    await _seed_sender(store, "news@example.com", category="newsletter")
    await _seed_sender(store, "bot@example.com", category="automated")
    await _seed_sender(store, "vip@example.com", category="key_contact")

    senders = await store.list_sender_profiles(category="newsletter")
    assert len(senders) == 1
    assert senders[0].email == "news@example.com"


async def test_list_sender_profiles_sort_by_email(store: DatabaseStore):
    """Sort by email column."""
    await _seed_sender(store, "charlie@example.com")
    await _seed_sender(store, "alice@example.com")
    await _seed_sender(store, "bob@example.com")

    senders = await store.list_sender_profiles(sort_by="email", sort_order="asc")
    assert senders[0].email == "alice@example.com"
    assert senders[2].email == "charlie@example.com"


async def test_list_sender_profiles_invalid_sort(store: DatabaseStore):
    """Invalid sort column falls back to email_count."""
    await _seed_sender(store, "test@example.com")

    # Should not raise - falls back to email_count
    senders = await store.list_sender_profiles(sort_by="DROP TABLE;--")
    assert len(senders) == 1


async def test_list_sender_profiles_pagination(store: DatabaseStore):
    """Pagination works with limit and offset."""
    for i in range(5):
        await _seed_sender(store, f"user{i}@example.com", email_count=10 - i)

    page1 = await store.list_sender_profiles(limit=2, offset=0)
    page2 = await store.list_sender_profiles(limit=2, offset=2)

    assert len(page1) == 2
    assert len(page2) == 2
    assert page1[0].email != page2[0].email


# ---------------------------------------------------------------------------
# Tests: Update Sender
# ---------------------------------------------------------------------------


async def test_update_sender_category(store: DatabaseStore):
    """Category update persists."""
    await _seed_sender(store, "test@example.com", category="unknown")

    await store.update_sender_category("test@example.com", "key_contact")

    senders = await store.list_sender_profiles()
    assert senders[0].category == "key_contact"


async def test_update_sender_default_folder(store: DatabaseStore):
    """Default folder update persists."""
    await _seed_sender(store, "test@example.com")

    await store.update_sender_default_folder("test@example.com", "Projects/Main")

    senders = await store.list_sender_profiles()
    assert senders[0].default_folder == "Projects/Main"


# ---------------------------------------------------------------------------
# Tests: Senders Page
# ---------------------------------------------------------------------------


async def test_senders_page_loads(client: AsyncClient):
    """Senders page returns 200."""
    response = await client.get("/senders")
    assert response.status_code == 200
    assert "Senders" in response.text


async def test_senders_page_empty_state(client: AsyncClient):
    """Empty senders page shows empty message."""
    response = await client.get("/senders")
    assert "No sender profiles found" in response.text


async def test_senders_page_with_data(client: AsyncClient, store: DatabaseStore):
    """Senders page displays seeded data."""
    await _seed_sender(store, "alice@example.com", "Alice")
    response = await client.get("/senders")
    assert "alice@example.com" in response.text


# ---------------------------------------------------------------------------
# Tests: Sender API Endpoints
# ---------------------------------------------------------------------------


async def test_api_update_sender_category(client: AsyncClient, store: DatabaseStore):
    """API endpoint updates sender category."""
    await _seed_sender(store, "api@example.com", category="unknown")

    response = await client.post(
        "/api/senders/api@example.com/category",
        json={"category": "newsletter"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "updated"

    senders = await store.list_sender_profiles()
    assert senders[0].category == "newsletter"


async def test_api_update_sender_category_invalid(client: AsyncClient):
    """API rejects invalid category."""
    response = await client.post(
        "/api/senders/test@example.com/category",
        json={"category": "invalid_category"},
    )
    assert response.status_code == 422


async def test_api_update_sender_folder(client: AsyncClient, store: DatabaseStore):
    """API endpoint updates sender default folder."""
    await _seed_sender(store, "api@example.com")

    response = await client.post(
        "/api/senders/api@example.com/default-folder",
        json={"folder": "Areas/New"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "updated"


async def test_api_update_sender_folder_empty_rejected(client: AsyncClient):
    """API rejects empty folder."""
    response = await client.post(
        "/api/senders/test@example.com/default-folder",
        json={"folder": "  "},
    )
    assert response.status_code == 422
