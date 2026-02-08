"""Tests for web routes and API endpoints.

Tests the FastAPI application routes using httpx AsyncClient,
covering page rendering, suggestion approval/rejection, config
validation, and health endpoint.
"""

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from assistant.config_schema import AppConfig
from assistant.db.store import DatabaseStore, Email
from assistant.web.app import create_app

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_config(sample_config_dict: dict[str, Any]) -> AppConfig:
    """Return a config for web route testing."""
    return AppConfig(**sample_config_dict)


@pytest.fixture
async def store(data_dir: Path) -> DatabaseStore:
    """Return an initialized DatabaseStore."""
    db_path = data_dir / "test_web.db"
    s = DatabaseStore(db_path)
    await s.initialize()
    return s


@pytest.fixture
def app(store: DatabaseStore, sample_config: AppConfig) -> FastAPI:
    """Create a FastAPI app with test dependencies."""
    test_app = create_app()

    # Override app state with test dependencies
    test_app.state.store = store
    test_app.state.config = sample_config
    test_app.state.message_manager = None
    test_app.state.folder_manager = None
    test_app.state.triage_engine = None
    test_app.state.scheduler = None
    test_app.state.task_manager = None
    test_app.state.category_manager = None

    return test_app


@pytest.fixture
async def client(app: FastAPI) -> AsyncClient:
    """Return an httpx AsyncClient for the test app."""
    # Override lifespan to avoid real initialization
    app.router.lifespan_context = _noop_lifespan
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@asynccontextmanager
async def _noop_lifespan(app: FastAPI):
    """No-op lifespan that preserves existing app.state."""
    yield


async def _seed_email(store: DatabaseStore, email_id: str = "test-001") -> Email:
    """Insert a test email into the database."""
    email = Email(
        id=email_id,
        conversation_id="conv-001",
        subject="Test Email Subject",
        sender_email="sender@example.com",
        sender_name="Test Sender",
        received_at=datetime.now(UTC),
        snippet="This is a test email body snippet for testing.",
        current_folder="Inbox",
        web_link=f"https://outlook.office.com/mail/{email_id}",
        importance="normal",
        is_read=False,
        flag_status="notFlagged",
    )
    await store.save_email(email)
    return email


async def _seed_suggestion(
    store: DatabaseStore,
    email_id: str = "test-001",
    confidence: float = 0.88,
) -> int:
    """Insert a test suggestion and return its ID."""
    return await store.create_suggestion(
        email_id=email_id,
        suggested_folder="Projects/Test",
        suggested_priority="P2 - Important",
        suggested_action_type="Review",
        confidence=confidence,
        reasoning="Test classification reasoning",
    )


# ---------------------------------------------------------------------------
# Tests: Page routes
# ---------------------------------------------------------------------------


async def test_dashboard_returns_200(client: AsyncClient):
    """Dashboard page returns 200."""
    response = await client.get("/")
    assert response.status_code == 200
    assert "Dashboard" in response.text


async def test_review_returns_200(client: AsyncClient):
    """Review page returns 200."""
    response = await client.get("/review")
    assert response.status_code == 200
    assert "Review Queue" in response.text


async def test_review_shows_pending_suggestions(
    client: AsyncClient,
    store: DatabaseStore,
):
    """Review page shows pending suggestions with email data."""
    await _seed_email(store)
    await _seed_suggestion(store)

    response = await client.get("/review")
    assert response.status_code == 200
    assert "Test Email Subject" in response.text
    assert "Projects/Test" in response.text


async def test_review_shows_empty_state(client: AsyncClient):
    """Review page shows empty state when no suggestions exist."""
    response = await client.get("/review")
    assert response.status_code == 200
    assert "No pending suggestions" in response.text


async def test_waiting_returns_200(client: AsyncClient):
    """Waiting page returns 200."""
    response = await client.get("/waiting")
    assert response.status_code == 200
    assert "Waiting For" in response.text


async def test_config_returns_200(client: AsyncClient):
    """Config page returns 200."""
    response = await client.get("/config")
    assert response.status_code == 200
    assert "Configuration" in response.text


async def test_log_returns_200(client: AsyncClient):
    """Activity log page returns 200."""
    response = await client.get("/log")
    assert response.status_code == 200
    assert "Activity Log" in response.text


async def test_log_with_action_type_filter(client: AsyncClient, store: DatabaseStore):
    """Activity log filters by action type."""
    await store.log_action(action_type="move", email_id="e1")
    await store.log_action(action_type="reject", email_id="e2")

    response = await client.get("/log?action_type=move")
    assert response.status_code == 200
    assert "move" in response.text


# ---------------------------------------------------------------------------
# Tests: API - Approve
# ---------------------------------------------------------------------------


async def test_approve_suggestion_json(client: AsyncClient, store: DatabaseStore):
    """Approving a suggestion returns JSON and updates status."""
    await _seed_email(store)
    sid = await _seed_suggestion(store)

    response = await client.post(f"/api/suggestions/{sid}/approve")
    assert response.status_code == 200

    data = response.json()
    assert data["status"] == "approved"
    assert data["suggestion_id"] == sid

    # Verify in database
    suggestion = await store.get_suggestion(sid)
    assert suggestion.status == "approved"


async def test_approve_suggestion_htmx(client: AsyncClient, store: DatabaseStore):
    """Approving via HTMX returns empty HTML with toast trigger."""
    await _seed_email(store)
    sid = await _seed_suggestion(store)

    response = await client.post(
        f"/api/suggestions/{sid}/approve",
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 200
    assert "HX-Trigger" in response.headers
    assert "Approved" in response.headers["HX-Trigger"]


async def test_approve_with_corrections(client: AsyncClient, store: DatabaseStore):
    """Approving with corrections uses the corrected values."""
    await _seed_email(store)
    sid = await _seed_suggestion(store)

    response = await client.post(
        f"/api/suggestions/{sid}/approve",
        json={
            "folder": "Areas/Finance",
            "priority": "P1 - Urgent",
            "action_type": "Needs Reply",
        },
    )
    assert response.status_code == 200

    suggestion = await store.get_suggestion(sid)
    assert suggestion.approved_folder == "Areas/Finance"
    assert suggestion.approved_priority == "P1 - Urgent"
    assert suggestion.approved_action_type == "Needs Reply"


async def test_approve_nonexistent_returns_404(client: AsyncClient):
    """Approving a non-existent suggestion returns 404."""
    response = await client.post("/api/suggestions/9999/approve")
    assert response.status_code == 404


async def test_approve_already_resolved_returns_409(
    client: AsyncClient,
    store: DatabaseStore,
):
    """Approving an already-resolved suggestion returns 409."""
    await _seed_email(store)
    sid = await _seed_suggestion(store)
    await store.approve_suggestion(sid)

    response = await client.post(f"/api/suggestions/{sid}/approve")
    assert response.status_code == 409


async def test_approve_logs_action(client: AsyncClient, store: DatabaseStore):
    """Approving a suggestion creates an action log entry."""
    await _seed_email(store)
    sid = await _seed_suggestion(store)

    await client.post(f"/api/suggestions/{sid}/approve")

    logs = await store.get_action_logs(limit=10, action_type="move")
    assert len(logs) >= 1
    assert logs[0].triggered_by == "user_approved"


# ---------------------------------------------------------------------------
# Tests: API - Reject
# ---------------------------------------------------------------------------


async def test_reject_suggestion_json(client: AsyncClient, store: DatabaseStore):
    """Rejecting a suggestion returns JSON and updates status."""
    await _seed_email(store)
    sid = await _seed_suggestion(store)

    response = await client.post(f"/api/suggestions/{sid}/reject")
    assert response.status_code == 200

    data = response.json()
    assert data["status"] == "rejected"

    suggestion = await store.get_suggestion(sid)
    assert suggestion.status == "rejected"


async def test_reject_suggestion_htmx(client: AsyncClient, store: DatabaseStore):
    """Rejecting via HTMX returns empty HTML with toast trigger."""
    await _seed_email(store)
    sid = await _seed_suggestion(store)

    response = await client.post(
        f"/api/suggestions/{sid}/reject",
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 200
    assert "HX-Trigger" in response.headers
    assert "Rejected" in response.headers["HX-Trigger"]


async def test_reject_nonexistent_returns_404(client: AsyncClient):
    """Rejecting a non-existent suggestion returns 404."""
    response = await client.post("/api/suggestions/9999/reject")
    assert response.status_code == 404


async def test_reject_logs_action(client: AsyncClient, store: DatabaseStore):
    """Rejecting a suggestion creates an action log entry."""
    await _seed_email(store)
    sid = await _seed_suggestion(store)

    await client.post(f"/api/suggestions/{sid}/reject")

    logs = await store.get_action_logs(limit=10, action_type="reject")
    assert len(logs) >= 1
    assert logs[0].triggered_by == "user_approved"


# ---------------------------------------------------------------------------
# Tests: API - Bulk approve
# ---------------------------------------------------------------------------


async def test_bulk_approve_approves_high_confidence(
    client: AsyncClient,
    store: DatabaseStore,
):
    """Bulk approve approves suggestions above confidence threshold."""
    await _seed_email(store, "e1")
    await _seed_email(store, "e2")
    await _seed_email(store, "e3")

    await _seed_suggestion(store, "e1", confidence=0.95)
    await _seed_suggestion(store, "e2", confidence=0.60)
    await _seed_suggestion(store, "e3", confidence=0.90)

    response = await client.post(
        "/api/suggestions/bulk-approve",
        json={"min_confidence": 0.85},
    )
    assert response.status_code == 200

    data = response.json()
    assert data["approved_count"] == 2  # e1 (0.95) and e3 (0.90)


# ---------------------------------------------------------------------------
# Tests: API - Waiting for
# ---------------------------------------------------------------------------


async def test_resolve_waiting_for(client: AsyncClient, store: DatabaseStore):
    """Resolving a waiting-for item updates its status."""
    await _seed_email(store)
    wid = await store.create_waiting_for(
        email_id="test-001",
        conversation_id="conv-001",
        expected_from="vendor@example.com",
        description="Price quote",
    )

    response = await client.post(f"/api/waiting/{wid}/resolve")
    assert response.status_code == 200


async def test_resolve_waiting_htmx(client: AsyncClient, store: DatabaseStore):
    """Resolving via HTMX returns empty HTML with toast trigger."""
    await _seed_email(store)
    wid = await store.create_waiting_for(
        email_id="test-001",
        conversation_id="conv-001",
        expected_from="vendor@example.com",
        description="Price quote",
    )

    response = await client.post(
        f"/api/waiting/{wid}/resolve",
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 200
    assert "HX-Trigger" in response.headers
    assert "Resolved" in response.headers["HX-Trigger"]


# ---------------------------------------------------------------------------
# Tests: API - Config
# ---------------------------------------------------------------------------


async def test_get_config_api(client: AsyncClient, tmp_path: Path, monkeypatch):
    """Config API returns YAML content."""
    config_path = tmp_path / "config" / "config.yaml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text("test: config")
    monkeypatch.chdir(tmp_path)

    response = await client.get("/api/config")
    assert response.status_code == 200
    assert response.json()["yaml_content"] == "test: config"


async def test_post_config_invalid_yaml(client: AsyncClient, tmp_path: Path, monkeypatch):
    """Posting invalid YAML returns 422."""
    monkeypatch.chdir(tmp_path)

    response = await client.post(
        "/api/config",
        json={"yaml_content": "invalid: yaml: content: [unclosed"},
    )
    assert response.status_code == 422


async def test_post_config_invalid_schema(client: AsyncClient, tmp_path: Path, monkeypatch):
    """Posting valid YAML that fails schema validation returns 422."""
    monkeypatch.chdir(tmp_path)

    response = await client.post(
        "/api/config",
        json={"yaml_content": "invalid_field: true"},
    )
    assert response.status_code == 422


async def test_post_config_valid(client: AsyncClient, tmp_path: Path, monkeypatch):
    """Posting valid config saves the file."""
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)
    monkeypatch.chdir(tmp_path)

    valid_yaml = """
schema_version: 1
auth:
  client_id: "test-client"
  tenant_id: "test-tenant"
timezone: "UTC"
triage:
  interval_minutes: 15
  batch_size: 20
  mode: "suggest"
  watch_folders: ["Inbox"]
projects: []
areas: []
auto_rules: []
"""
    response = await client.post(
        "/api/config",
        json={"yaml_content": valid_yaml},
    )
    assert response.status_code == 200

    saved = (config_dir / "config.yaml").read_text()
    assert "test-client" in saved


# ---------------------------------------------------------------------------
# Tests: API - Health
# ---------------------------------------------------------------------------


async def test_health_endpoint(client: AsyncClient):
    """Health endpoint returns expected JSON structure."""
    response = await client.get("/api/health")
    assert response.status_code == 200

    data = response.json()
    assert "status" in data
    assert "version" in data
    assert "degraded_mode" in data
    assert data["status"] == "healthy"
    assert data["degraded_mode"] is False
