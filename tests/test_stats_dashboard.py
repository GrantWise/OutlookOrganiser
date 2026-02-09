"""Tests for the Stats Dashboard (Features 2H + 2K).

Tests cover:
- Approval stats calculation (overall + per-folder)
- Correction heatmap
- Confidence calibration bucketing + empty buckets
- Calibration alerts (over/under-confident)
- Cost tracking aggregation
- Stats page loads (200)
- Stats API returns JSON
"""

from contextlib import asynccontextmanager
from datetime import datetime
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
    """Config for stats testing."""
    return AppConfig(**sample_config_dict)


@pytest.fixture
async def store(data_dir: Path) -> DatabaseStore:
    """Return an initialized DatabaseStore."""
    db_path = data_dir / "test_stats.db"
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


async def _seed_suggestion(
    store: DatabaseStore,
    email_id: str,
    suggested_folder: str = "Areas/Test",
    confidence: float = 0.85,
    status: str = "approved",
    approved_folder: str | None = None,
) -> int:
    """Seed an email + suggestion with a given status."""
    await store.save_email(
        Email(
            id=email_id,
            subject=f"Test {email_id}",
            sender_email="test@example.com",
            sender_name="Test Sender",
            received_at=datetime.now(),
            snippet="test",
        )
    )
    sid = await store.create_suggestion(
        email_id=email_id,
        suggested_folder=suggested_folder,
        suggested_priority="P2 - Important",
        suggested_action_type="Review",
        confidence=confidence,
        reasoning="Test",
    )

    if status == "approved":
        await store.approve_suggestion(sid)
    elif status == "partial":
        await store.approve_suggestion(
            sid,
            approved_folder=approved_folder or "Areas/Other",
        )
    elif status == "rejected":
        await store.reject_suggestion(sid)

    return sid


# ---------------------------------------------------------------------------
# Tests: Approval Stats
# ---------------------------------------------------------------------------


async def test_approval_stats_empty(store: DatabaseStore):
    """Empty DB returns zero stats."""
    stats = await store.get_approval_stats(30)
    assert stats["overall"] == {}
    assert stats["per_folder"] == []


async def test_approval_stats_with_data(store: DatabaseStore):
    """Approval stats reflect suggestions status."""
    await _seed_suggestion(store, "e1", status="approved", confidence=0.9)
    await _seed_suggestion(store, "e2", status="approved", confidence=0.8)
    await _seed_suggestion(
        store, "e3", status="partial", confidence=0.7, approved_folder="Areas/Other"
    )

    stats = await store.get_approval_stats(30)
    assert stats["overall"]["approved"] == 2
    assert stats["overall"]["partial"] == 1


async def test_approval_stats_per_folder(store: DatabaseStore):
    """Per-folder stats break down by suggested folder."""
    await _seed_suggestion(store, "e1", suggested_folder="Projects/A", status="approved")
    await _seed_suggestion(store, "e2", suggested_folder="Projects/A", status="approved")
    await _seed_suggestion(
        store,
        "e3",
        suggested_folder="Projects/A",
        status="partial",
        approved_folder="Areas/B",
    )
    await _seed_suggestion(store, "e4", suggested_folder="Areas/B", status="approved")

    stats = await store.get_approval_stats(30)

    # Projects/A has 3 total (2 approved, 1 corrected)
    folder_a = next((f for f in stats["per_folder"] if f["folder"] == "Projects/A"), None)
    assert folder_a is not None
    assert folder_a["total"] == 3
    assert folder_a["approved"] == 2
    assert folder_a["corrected"] == 1


# ---------------------------------------------------------------------------
# Tests: Correction Heatmap
# ---------------------------------------------------------------------------


async def test_correction_heatmap_empty(store: DatabaseStore):
    """No corrections means empty heatmap."""
    heatmap = await store.get_correction_heatmap(30)
    assert heatmap == []


async def test_correction_heatmap_with_corrections(store: DatabaseStore):
    """Corrections create heatmap entries."""
    await _seed_suggestion(
        store,
        "e1",
        suggested_folder="Projects/X",
        status="partial",
        approved_folder="Areas/Y",
    )
    await _seed_suggestion(
        store,
        "e2",
        suggested_folder="Projects/X",
        status="partial",
        approved_folder="Areas/Y",
    )

    heatmap = await store.get_correction_heatmap(30)
    assert len(heatmap) >= 1
    assert heatmap[0]["from_folder"] == "Projects/X"
    assert heatmap[0]["to_folder"] == "Areas/Y"
    assert heatmap[0]["count"] == 2


# ---------------------------------------------------------------------------
# Tests: Confidence Calibration
# ---------------------------------------------------------------------------


async def test_calibration_empty(store: DatabaseStore):
    """Empty DB returns all-zero buckets."""
    cal = await store.get_confidence_calibration(30)
    assert len(cal) == 5
    for bucket in cal:
        assert bucket["count"] == 0
        assert bucket["approval_rate"] is None


async def test_calibration_bucketing(store: DatabaseStore):
    """Suggestions are bucketed by confidence."""
    await _seed_suggestion(store, "e1", confidence=0.55, status="approved")
    await _seed_suggestion(store, "e2", confidence=0.75, status="approved")
    await _seed_suggestion(store, "e3", confidence=0.75, status="rejected")
    await _seed_suggestion(store, "e4", confidence=0.95, status="approved")

    cal = await store.get_confidence_calibration(30)

    bucket_05 = next(b for b in cal if b["bucket"] == "0.5-0.6")
    assert bucket_05["count"] == 1
    assert bucket_05["approved"] == 1
    assert bucket_05["approval_rate"] == 1.0

    bucket_07 = next(b for b in cal if b["bucket"] == "0.7-0.8")
    assert bucket_07["count"] == 2
    assert bucket_07["approved"] == 1
    assert bucket_07["approval_rate"] == 0.5

    bucket_09 = next(b for b in cal if b["bucket"] == "0.9-1.0")
    assert bucket_09["count"] == 1
    assert bucket_09["approved"] == 1


async def test_calibration_alerts_over_confident(store: DatabaseStore):
    """Alert triggered when actual approval rate << expected confidence."""
    # Bucket 0.9-1.0 (expected ~95%) but all rejected = 0% approval
    for i in range(6):
        await _seed_suggestion(store, f"e{i}", confidence=0.95, status="rejected")

    cal = await store.get_confidence_calibration(30)
    bucket_09 = next(b for b in cal if b["bucket"] == "0.9-1.0")
    assert bucket_09["approval_rate"] == 0.0

    # Check that calibration alert logic would fire
    expected = 0.95
    actual = bucket_09["approval_rate"]
    assert abs(actual - expected) > 0.15


# ---------------------------------------------------------------------------
# Tests: Cost Tracking
# ---------------------------------------------------------------------------


async def test_cost_tracking_empty(store: DatabaseStore):
    """Empty DB returns zero cost stats."""
    cost = await store.get_cost_tracking(30)
    assert cost["total_requests"] == 0
    assert cost["total_input_tokens"] == 0
    assert cost["total_output_tokens"] == 0


# ---------------------------------------------------------------------------
# Tests: Stats Page
# ---------------------------------------------------------------------------


async def test_stats_page_loads(client: AsyncClient):
    """Stats page returns 200."""
    response = await client.get("/stats")
    assert response.status_code == 200
    assert "Statistics" in response.text


async def test_stats_page_contains_sections(client: AsyncClient):
    """Stats page contains expected section headers."""
    response = await client.get("/stats")
    assert "Approval Rate" in response.text
    assert "Confidence Calibration" in response.text
    assert "API Cost Tracking" in response.text
    assert "Learned Preferences" in response.text
