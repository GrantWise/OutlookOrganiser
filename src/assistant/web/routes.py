"""Web routes for the Outlook AI Assistant review UI.

Contains two routers:
- page_router: HTML page routes (Dashboard, Review, Waiting, Config, Log)
- api_router: JSON/HTMX API endpoints (approve, reject, config, health)

All routes use FastAPI dependency injection to access shared state.

Spec reference: Reference/spec/03-agent-behaviors.md Section 3
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ValidationError

from assistant.config_schema import AppConfig
from assistant.core.errors import DatabaseError, GraphAPIError
from assistant.core.logging import get_logger
from assistant.db.store import DatabaseStore
from assistant.web.dependencies import (
    get_config,
    get_store,
)

logger = get_logger(__name__)

# Template directory
_TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
templates.env.autoescape = True

# Routers
page_router = APIRouter()
api_router = APIRouter(prefix="/api")


# ---------------------------------------------------------------------------
# Pydantic models for API input validation
# ---------------------------------------------------------------------------


class ApproveRequest(BaseModel):
    """Request body for approving a suggestion with optional corrections."""

    folder: str | None = None
    priority: str | None = None
    action_type: str | None = None


class BulkApproveRequest(BaseModel):
    """Request body for bulk approving high-confidence suggestions."""

    min_confidence: float = 0.85


class ConfigUpdateRequest(BaseModel):
    """Request body for updating configuration."""

    yaml_content: str


# ---------------------------------------------------------------------------
# Template context helpers
# ---------------------------------------------------------------------------


def _time_ago(dt: datetime | None) -> str:
    """Format a datetime as a relative time string."""
    if dt is None:
        return "unknown"

    now = datetime.now(UTC)
    # Handle naive datetimes
    if dt.tzinfo is None:
        diff = datetime.now() - dt
    else:
        diff = now - dt

    seconds = int(diff.total_seconds())
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        minutes = seconds // 60
        return f"{minutes}m ago"
    if seconds < 86400:
        hours = seconds // 3600
        return f"{hours}h ago"
    days = seconds // 86400
    return f"{days}d ago"


def _confidence_class(confidence: float | None) -> str:
    """Return CSS class name for confidence color coding."""
    if confidence is None:
        return "confidence-low"
    if confidence >= 0.85:
        return "confidence-high"
    if confidence >= 0.5:
        return "confidence-medium"
    return "confidence-low"


def _priority_class(priority: str | None) -> str:
    """Return CSS class name for priority color coding."""
    if not priority:
        return "priority-p4"
    if priority.startswith("P1"):
        return "priority-p1"
    if priority.startswith("P2"):
        return "priority-p2"
    if priority.startswith("P3"):
        return "priority-p3"
    return "priority-p4"


# Register template filters
templates.env.filters["time_ago"] = _time_ago
templates.env.filters["confidence_class"] = _confidence_class
templates.env.filters["priority_class"] = _priority_class


# ---------------------------------------------------------------------------
# Page routes
# ---------------------------------------------------------------------------


@page_router.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    store: DatabaseStore = Depends(get_store),
    config: AppConfig = Depends(get_config),
):
    """Dashboard page with overview stats and health indicator."""
    stats = await store.get_stats()

    # Get health info
    last_cycle = await store.get_state("last_triage_cycle")
    last_cycle_dt = None
    if last_cycle:
        try:
            last_cycle_dt = datetime.fromisoformat(last_cycle)
        except ValueError:
            pass

    # Compute aging counts
    aging_needs_reply = 0
    overdue_waiting = 0
    try:
        pending = await store.get_pending_suggestions()
        now = datetime.now()
        warning_hours = config.aging.needs_reply_warning_hours
        for s in pending:
            if s.suggested_action_type == "Needs Reply":
                age = now - s.created_at
                if age > timedelta(hours=warning_hours):
                    aging_needs_reply += 1

        waiting_items = await store.get_active_waiting_for()
        for w in waiting_items:
            if w.waiting_since:
                age = now - w.waiting_since
                if age > timedelta(hours=w.nudge_after_hours):
                    overdue_waiting += 1
    except DatabaseError:
        pass

    triage_engine = request.app.state.triage_engine
    degraded_mode = triage_engine.degraded_mode if triage_engine else False

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "stats": stats,
            "aging_needs_reply": aging_needs_reply,
            "overdue_waiting": overdue_waiting,
            "last_cycle": last_cycle_dt,
            "last_cycle_ago": _time_ago(last_cycle_dt),
            "degraded_mode": degraded_mode,
            "interval_minutes": config.triage.interval_minutes,
            "nav_active": "dashboard",
        },
    )


@page_router.get("/review", response_class=HTMLResponse)
async def review_queue(
    request: Request,
    store: DatabaseStore = Depends(get_store),
    config: AppConfig = Depends(get_config),
):
    """Review queue page with pending suggestions."""
    suggestions = await store.get_pending_suggestions(limit=200)

    # Join email data for each suggestion
    items = []
    for s in suggestions:
        email = await store.get_email(s.email_id)
        items.append({"suggestion": s, "email": email})

    # Build folder options for correction dropdowns
    folder_options = []
    for p in config.projects:
        folder_options.append(p.folder)
    for a in config.areas:
        folder_options.append(a.folder)
    folder_options.sort()

    # Get failed classifications
    failed_emails = await store.get_emails_by_status("failed", limit=50)

    return templates.TemplateResponse(
        request,
        "review.html",
        {
            "items": items,
            "folder_options": folder_options,
            "failed_emails": failed_emails,
            "pending_count": len(suggestions),
            "nav_active": "review",
        },
    )


@page_router.get("/waiting", response_class=HTMLResponse)
async def waiting_for(
    request: Request,
    store: DatabaseStore = Depends(get_store),
    config: AppConfig = Depends(get_config),
):
    """Waiting-for tracker page."""
    waiting_items = await store.get_active_waiting_for()

    # Enrich with email data and age status
    items = []
    now = datetime.now()
    for w in waiting_items:
        email = await store.get_email(w.email_id) if w.email_id else None
        age_hours = 0
        age_class = "age-fresh"
        if w.waiting_since:
            age_hours = (now - w.waiting_since).total_seconds() / 3600
            if age_hours > config.aging.waiting_for_escalate_hours:
                age_class = "age-critical"
            elif age_hours > w.nudge_after_hours:
                age_class = "age-overdue"
            elif age_hours > w.nudge_after_hours * 0.75:
                age_class = "age-warning"

        items.append(
            {
                "waiting": w,
                "email": email,
                "age_hours": int(age_hours),
                "age_class": age_class,
            }
        )

    return templates.TemplateResponse(
        request,
        "waiting.html",
        {
            "items": items,
            "nav_active": "waiting",
        },
    )


@page_router.get("/config", response_class=HTMLResponse)
async def config_editor(request: Request):
    """Configuration editor page."""
    config_path = Path("config/config.yaml")
    yaml_content = ""
    if config_path.exists():
        yaml_content = config_path.read_text()

    return templates.TemplateResponse(
        request,
        "config.html",
        {
            "yaml_content": yaml_content,
            "nav_active": "config",
        },
    )


@page_router.get("/log", response_class=HTMLResponse)
async def activity_log(
    request: Request,
    store: DatabaseStore = Depends(get_store),
    action_type: str | None = None,
    days: int = 7,
):
    """Activity log page with filterable entries."""
    logs = await store.get_action_logs(limit=200, action_type=action_type)

    # Enrich with email subjects
    items = []
    for log_entry in logs:
        email_subject = None
        if log_entry.email_id:
            email = await store.get_email(log_entry.email_id)
            if email:
                email_subject = email.subject
        items.append(
            {
                "log": log_entry,
                "email_subject": email_subject,
            }
        )

    return templates.TemplateResponse(
        request,
        "log.html",
        {
            "items": items,
            "current_action_type": action_type,
            "current_days": days,
            "nav_active": "log",
        },
    )


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------


@api_router.post("/suggestions/{suggestion_id}/approve")
async def approve_suggestion(
    suggestion_id: int,
    request: Request,
    body: ApproveRequest | None = None,
    store: DatabaseStore = Depends(get_store),
):
    """Approve a suggestion, optionally with corrections.

    Executes the move via Graph API and sets categories on the message.
    Returns HTMX fragment or JSON depending on HX-Request header.
    """
    # Get suggestion
    suggestion = await store.get_suggestion(suggestion_id)
    if not suggestion:
        raise HTTPException(status_code=404, detail="Suggestion not found")
    if suggestion.status != "pending":
        raise HTTPException(status_code=409, detail="Suggestion already resolved")

    body = body or ApproveRequest()

    # Approve in database
    success = await store.approve_suggestion(
        suggestion_id,
        approved_folder=body.folder,
        approved_priority=body.priority,
        approved_action_type=body.action_type,
    )
    if not success:
        raise HTTPException(status_code=409, detail="Failed to approve suggestion")

    # Refresh suggestion to get approved values
    approved = await store.get_suggestion(suggestion_id)

    # Execute via Graph API
    graph_error = None
    try:
        folder_manager = request.app.state.folder_manager
        message_manager = request.app.state.message_manager

        if folder_manager and message_manager and approved:
            folder_id = folder_manager.get_folder_id(approved.approved_folder)
            if not folder_id:
                # Auto-create missing folder (and parents)
                created = folder_manager.create_folder(approved.approved_folder)
                folder_id = created["id"]
                logger.info(
                    "auto_created_folder",
                    path=approved.approved_folder,
                    folder_id=folder_id[:20] + "...",
                )
            message_manager.move_message(suggestion.email_id, folder_id)

            categories = []
            if approved.approved_priority:
                categories.append(approved.approved_priority)
            if approved.approved_action_type:
                categories.append(approved.approved_action_type)
            if categories:
                message_manager.set_categories(suggestion.email_id, categories)
    except GraphAPIError as e:
        graph_error = str(e)
        logger.error(
            "approve_graph_api_failed",
            suggestion_id=suggestion_id,
            error=str(e),
        )

    # Log action
    await store.log_action(
        action_type="move",
        email_id=suggestion.email_id,
        details={
            "suggestion_id": suggestion_id,
            "folder": approved.approved_folder if approved else None,
            "priority": approved.approved_priority if approved else None,
            "action_type": approved.approved_action_type if approved else None,
            "graph_error": graph_error,
        },
        triggered_by="user_approved",
    )

    # Return HTMX or JSON response
    if request.headers.get("HX-Request"):
        response = Response(content="", media_type="text/html")
        toast_msg = "Approved" if not graph_error else f"Approved (move failed: {graph_error})"
        response.headers["HX-Trigger"] = f'{{"showToast": "{toast_msg}"}}'
        return response

    return {
        "status": "approved",
        "suggestion_id": suggestion_id,
        "graph_error": graph_error,
    }


@api_router.post("/suggestions/{suggestion_id}/reject")
async def reject_suggestion(
    suggestion_id: int,
    request: Request,
    store: DatabaseStore = Depends(get_store),
):
    """Reject a suggestion. Email stays in current folder."""
    suggestion = await store.get_suggestion(suggestion_id)
    if not suggestion:
        raise HTTPException(status_code=404, detail="Suggestion not found")
    if suggestion.status != "pending":
        raise HTTPException(status_code=409, detail="Suggestion already resolved")

    await store.reject_suggestion(suggestion_id)

    await store.log_action(
        action_type="reject",
        email_id=suggestion.email_id,
        details={"suggestion_id": suggestion_id},
        triggered_by="user_approved",
    )

    if request.headers.get("HX-Request"):
        response = Response(content="", media_type="text/html")
        response.headers["HX-Trigger"] = '{"showToast": "Rejected"}'
        return response

    return {"status": "rejected", "suggestion_id": suggestion_id}


@api_router.post("/suggestions/bulk-approve")
async def bulk_approve(
    request: Request,
    body: BulkApproveRequest | None = None,
    store: DatabaseStore = Depends(get_store),
):
    """Approve all pending suggestions above confidence threshold."""
    body = body or BulkApproveRequest()

    suggestions = await store.get_pending_suggestions(limit=500)
    approved_count = 0

    for s in suggestions:
        if s.confidence is not None and s.confidence >= body.min_confidence:
            success = await store.approve_suggestion(s.id)
            if success:
                approved_count += 1

                # Execute via Graph API
                try:
                    approved = await store.get_suggestion(s.id)
                    folder_manager = request.app.state.folder_manager
                    message_manager = request.app.state.message_manager

                    if folder_manager and message_manager and approved:
                        folder_id = folder_manager.get_folder_id(approved.approved_folder)
                        if not folder_id:
                            created = folder_manager.create_folder(approved.approved_folder)
                            folder_id = created["id"]
                        message_manager.move_message(s.email_id, folder_id)

                        categories = []
                        if approved.approved_priority:
                            categories.append(approved.approved_priority)
                        if approved.approved_action_type:
                            categories.append(approved.approved_action_type)
                        if categories:
                            message_manager.set_categories(s.email_id, categories)
                except GraphAPIError as e:
                    logger.warning(
                        "bulk_approve_graph_error",
                        suggestion_id=s.id,
                        error=str(e),
                    )

                await store.log_action(
                    action_type="move",
                    email_id=s.email_id,
                    details={"suggestion_id": s.id, "bulk": True},
                    triggered_by="user_approved",
                )

    if request.headers.get("HX-Request"):
        response = Response(
            content=f"<p>{approved_count} suggestions approved</p>",
            media_type="text/html",
        )
        response.headers["HX-Trigger"] = f'{{"showToast": "{approved_count} approved"}}'
        return response

    return {"approved_count": approved_count}


@api_router.post("/waiting/{waiting_id}/resolve")
async def resolve_waiting(
    waiting_id: int,
    request: Request,
    store: DatabaseStore = Depends(get_store),
):
    """Resolve a waiting-for item."""
    await store.resolve_waiting_for(waiting_id, status="received")

    if request.headers.get("HX-Request"):
        response = Response(content="", media_type="text/html")
        response.headers["HX-Trigger"] = '{"showToast": "Resolved"}'
        return response

    return {"status": "resolved", "waiting_id": waiting_id}


@api_router.get("/config")
async def get_config_api():
    """Return current config.yaml content."""
    config_path = Path("config/config.yaml")
    if not config_path.exists():
        raise HTTPException(status_code=404, detail="Config file not found")

    return {"yaml_content": config_path.read_text()}


@api_router.post("/config")
async def update_config_api(
    request: Request,
    body: ConfigUpdateRequest,
):
    """Validate and save config.yaml content."""
    # Parse YAML
    try:
        yaml_data = yaml.safe_load(body.yaml_content)
    except yaml.YAMLError as e:
        if request.headers.get("HX-Request"):
            return HTMLResponse(
                content=f'<div class="error-message">Invalid YAML: {e}</div>',
                status_code=422,
            )
        raise HTTPException(status_code=422, detail=f"Invalid YAML: {e}") from None

    # Validate against Pydantic schema
    try:
        AppConfig(**yaml_data)
    except ValidationError as e:
        errors = []
        for err in e.errors():
            loc = " -> ".join(str(part) for part in err["loc"])
            errors.append(f"{loc}: {err['msg']}")
        error_html = "<br>".join(errors)

        if request.headers.get("HX-Request"):
            return HTMLResponse(
                content=f'<div class="error-message">{error_html}</div>',
                status_code=422,
            )
        raise HTTPException(status_code=422, detail=errors) from None

    # Write to file
    config_path = Path("config/config.yaml")
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(body.yaml_content)

    logger.info("config_saved", path=str(config_path))

    if request.headers.get("HX-Request"):
        response = HTMLResponse(
            content='<div class="success-message">Configuration saved and reloaded.</div>'
        )
        response.headers["HX-Trigger"] = '{"showToast": "Config saved"}'
        return response

    return {"status": "saved"}


@api_router.get("/health")
async def health_check(
    request: Request,
    store: DatabaseStore = Depends(get_store),
):
    """Health check endpoint for Docker and monitoring."""
    last_cycle = await store.get_state("last_triage_cycle")
    stats = await store.get_stats()

    triage_engine = request.app.state.triage_engine
    degraded = triage_engine.degraded_mode if triage_engine else False

    return {
        "status": "degraded" if degraded else "healthy",
        "last_triage_cycle": last_cycle,
        "pending_suggestions": stats.get("pending_suggestions", 0),
        "degraded_mode": degraded,
        "version": "0.1.0",
    }
