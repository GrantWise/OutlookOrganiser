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
from typing import Any

import yaml
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ValidationError

from assistant.config import write_config_safely
from assistant.config_schema import AppConfig
from assistant.core.errors import (
    ConfigLoadError,
    ConfigValidationError,
    DatabaseError,
    GraphAPIError,
)
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


class ChatRequest(BaseModel):
    """Request body for the chat classification assistant."""

    suggestion_id: int
    messages: list[dict[str, Any]]


# ---------------------------------------------------------------------------
# Shared Graph API operations
# ---------------------------------------------------------------------------


def execute_email_move(
    email_id: str,
    folder: str,
    priority: str | None,
    action_type: str | None,
    folder_manager: Any,
    message_manager: Any,
) -> dict[str, Any]:
    """Move an email to a folder via Graph API and set Outlook categories.

    Resolves the folder ID (auto-creating the folder if it doesn't exist),
    moves the message, and sets priority + action_type as Outlook categories.

    The Graph API client methods are synchronous, matching the existing pattern
    used throughout the codebase.

    Args:
        email_id: The Graph API message ID to move.
        folder: Target folder path (e.g., 'Projects/Tradecore Steel').
        priority: Priority label to set as category (e.g., 'P2 - Important').
        action_type: Action type to set as category (e.g., 'Needs Reply').
        folder_manager: FolderManager instance for folder resolution/creation.
        message_manager: MessageManager instance for move/categorize.

    Returns:
        Dict with 'new_msg_id' (str) and 'graph_error' (str | None).
    """
    graph_error = None
    new_msg_id = email_id

    try:
        folder_id = folder_manager.get_folder_id(folder)
        if not folder_id:
            created = folder_manager.create_folder(folder)
            folder_id = created["id"]
            logger.info(
                "auto_created_folder",
                path=folder,
                folder_id=folder_id[:20] + "...",
            )

        moved_msg = message_manager.move_message(email_id, folder_id)
        new_msg_id = moved_msg.get("id", email_id)

        categories = []
        if priority:
            categories.append(priority)
        if action_type:
            categories.append(action_type)
        if categories:
            message_manager.set_categories(new_msg_id, categories)
    except GraphAPIError as e:
        graph_error = str(e)
        logger.error(
            "execute_email_move_failed",
            email_id=email_id,
            folder=folder,
            error=str(e),
        )

    return {"new_msg_id": new_msg_id, "graph_error": graph_error}


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

    # Batch-fetch all emails in a single query (eliminates N+1)
    email_ids = [s.email_id for s in suggestions]
    emails_by_id = await store.get_emails_batch(email_ids)

    items = []
    for s in suggestions:
        items.append({"suggestion": s, "email": emails_by_id.get(s.email_id)})

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

    # Batch-fetch all emails in a single query (eliminates N+1)
    email_ids = [w.email_id for w in waiting_items if w.email_id]
    emails_by_id = await store.get_emails_batch(email_ids)

    # Enrich with email data and age status
    items = []
    now = datetime.now()
    for w in waiting_items:
        email = emails_by_id.get(w.email_id) if w.email_id else None
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

    # Batch-fetch all emails in a single query (eliminates N+1)
    email_ids = [entry.email_id for entry in logs if entry.email_id]
    emails_by_id = await store.get_emails_batch(email_ids)

    # Enrich with email subjects
    items = []
    for log_entry in logs:
        email_subject = None
        if log_entry.email_id:
            email = emails_by_id.get(log_entry.email_id)
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

    # Parse corrections from request body.
    # HTMX sends form-encoded data (hx-vals), API clients send JSON.
    body = ApproveRequest()
    content_type = request.headers.get("content-type", "")
    try:
        if "application/json" in content_type:
            import json

            raw = await request.body()
            if raw and raw.strip():
                body = ApproveRequest(**json.loads(raw))
        elif "form" in content_type:
            form = await request.form()
            body = ApproveRequest(
                folder=form.get("folder") or None,
                priority=form.get("priority") or None,
                action_type=form.get("action_type") or None,
            )
    except (ValueError, Exception):
        pass  # Use defaults — no corrections

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
    folder_mgr = request.app.state.folder_manager
    message_mgr = request.app.state.message_manager

    if folder_mgr and message_mgr and approved:
        move_result = execute_email_move(
            email_id=suggestion.email_id,
            folder=approved.approved_folder,
            priority=approved.approved_priority,
            action_type=approved.approved_action_type,
            folder_manager=folder_mgr,
            message_manager=message_mgr,
        )
        graph_error = move_result["graph_error"]

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
        validated_config = AppConfig(**yaml_data)
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

    # Write safely with backup and atomic replace
    try:
        write_config_safely(validated_config)
    except (ConfigValidationError, ConfigLoadError) as e:
        if request.headers.get("HX-Request"):
            return HTMLResponse(
                content=f'<div class="error-message">Write failed: {e}</div>',
                status_code=500,
            )
        raise HTTPException(status_code=500, detail=f"Config write failed: {e}") from None

    # Invalidate folder cache so config changes take effect immediately
    folder_mgr = getattr(request.app.state, "folder_manager", None)
    if folder_mgr:
        folder_mgr.refresh_cache()

    if request.headers.get("HX-Request"):
        response = HTMLResponse(
            content='<div class="success-message">Configuration saved and reloaded.</div>'
        )
        response.headers["HX-Trigger"] = '{"showToast": "Config saved"}'
        return response

    return {"status": "saved"}


@api_router.post("/chat")
async def chat_endpoint(
    request: Request,
    body: ChatRequest,
    store: DatabaseStore = Depends(get_store),
    config: AppConfig = Depends(get_config),
):
    """Send a message to the classification chat assistant.

    The frontend maintains message history client-side and sends the full
    conversation on each request. The backend is stateless.

    Returns the assistant's reply and a list of any actions taken (tool calls).
    """
    from assistant.chat.assistant import ChatAssistant

    anthropic_client = request.app.state.anthropic_client
    if not anthropic_client:
        raise HTTPException(status_code=503, detail="Anthropic client not available")

    folder_manager = request.app.state.folder_manager
    message_manager = request.app.state.message_manager

    assistant = ChatAssistant(
        anthropic_client=anthropic_client,
        store=store,
        config=config,
    )

    result = await assistant.chat(
        suggestion_id=body.suggestion_id,
        user_messages=body.messages,
        folder_manager=folder_manager,
        message_manager=message_manager,
    )

    if result.error:
        raise HTTPException(status_code=422, detail=result.error)

    return {
        "reply": result.reply,
        "actions_taken": result.actions_taken,
    }


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
