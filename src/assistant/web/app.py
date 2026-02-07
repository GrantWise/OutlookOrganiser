"""FastAPI application for the Outlook AI Assistant review UI.

Creates the FastAPI app with:
- Lifespan context manager for dependency initialization and scheduler
- Jinja2 templates with auto-escaping
- Static file serving (CSS, HTMX)
- Page and API routers

The triage engine runs as a background job via APScheduler's
BackgroundScheduler in the same process as uvicorn. The scheduler
thread bridges to the async event loop via run_coroutine_threadsafe.

Usage:
    from assistant.web.app import create_app

    app = create_app()
    # Run with: uvicorn.run(app, host="127.0.0.1", port=8000)
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from assistant.core.logging import get_logger

logger = get_logger(__name__)

# Static files directory
_STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize dependencies on startup, clean up on shutdown.

    On startup:
    1. Load config
    2. Initialize auth, Graph client, message/folder managers
    3. Initialize database
    4. Initialize classifier and triage engine
    5. Start APScheduler

    On shutdown:
    - Stop APScheduler
    """
    from apscheduler.schedulers.background import BackgroundScheduler

    from assistant.auth.msal_auth import GraphAuth
    from assistant.classifier.claude_classifier import EmailClassifier
    from assistant.classifier.snippet import SnippetCleaner
    from assistant.config import get_config
    from assistant.core.errors import AuthenticationError, ConfigLoadError
    from assistant.db.store import DatabaseStore
    from assistant.engine.thread_utils import ThreadContextManager
    from assistant.engine.triage import TriageEngine
    from assistant.graph.client import GraphClient
    from assistant.graph.folders import FolderManager
    from assistant.graph.messages import MessageManager, SentItemsCache

    # 1. Load config
    try:
        config = get_config()
    except (ConfigLoadError, Exception) as e:
        logger.error("config_load_failed", error=str(e))
        # Store None values so app can still start for config editing
        app.state.store = None
        app.state.config = None
        app.state.message_manager = None
        app.state.folder_manager = None
        app.state.triage_engine = None
        app.state.scheduler = None
        app.state.anthropic_client = None
        yield
        return

    app.state.config = config

    # 2. Initialize auth and Graph client
    try:
        auth = GraphAuth(
            client_id=config.auth.client_id,
            tenant_id=config.auth.tenant_id,
            scopes=config.auth.scopes,
            token_cache_path=config.auth.token_cache_path,
        )
        graph_client = GraphClient(auth)
        message_manager = MessageManager(graph_client)
        folder_manager = FolderManager(graph_client)
    except (AuthenticationError, Exception) as e:
        logger.error("auth_init_failed", error=str(e))
        message_manager = None
        folder_manager = None

    app.state.message_manager = message_manager
    app.state.folder_manager = folder_manager

    # 3. Initialize database
    db_path = Path("data/assistant.db")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    store = DatabaseStore(db_path)
    await store.initialize()
    app.state.store = store

    # 4. Initialize classifier and triage engine
    triage_engine = None
    scheduler = None
    app.state.anthropic_client = None

    if message_manager and folder_manager:
        try:
            import anthropic

            anthropic_client = anthropic.Anthropic(max_retries=3)
            app.state.anthropic_client = anthropic_client
            snippet_cleaner = SnippetCleaner(max_length=config.snippet.max_length)
            thread_manager = ThreadContextManager(
                store=store,
                message_manager=message_manager,
                snippet_cleaner=snippet_cleaner,
            )
            classifier = EmailClassifier(
                anthropic_client=anthropic_client,
                store=store,
                config=config,
            )
            sent_cache = SentItemsCache(message_manager)

            triage_engine = TriageEngine(
                classifier=classifier,
                store=store,
                message_manager=message_manager,
                folder_manager=folder_manager,
                snippet_cleaner=snippet_cleaner,
                thread_manager=thread_manager,
                sent_cache=sent_cache,
                config=config,
            )
        except Exception as e:
            logger.error("triage_engine_init_failed", error=str(e))

    app.state.triage_engine = triage_engine

    # 5. Start APScheduler
    if triage_engine:
        loop = asyncio.get_running_loop()

        def _run_triage_sync():
            """Bridge async triage cycle into sync scheduler thread."""
            try:
                future = asyncio.run_coroutine_threadsafe(triage_engine.run_cycle(), loop)
                future.result(timeout=300)  # 5 min max
            except Exception as e:
                logger.error("scheduled_triage_failed", error=str(e))

        scheduler = BackgroundScheduler()
        scheduler.add_job(
            _run_triage_sync,
            "interval",
            minutes=config.triage.interval_minutes,
            id="triage_cycle",
            max_instances=1,
            coalesce=True,
            next_run_time=None,  # Don't run immediately on startup
        )
        scheduler.start()
        logger.info(
            "scheduler_started",
            interval_minutes=config.triage.interval_minutes,
        )

    app.state.scheduler = scheduler

    yield

    # Shutdown
    if scheduler:
        scheduler.shutdown(wait=False)
        logger.info("scheduler_stopped")


def create_app() -> FastAPI:
    """Create and configure the FastAPI application.

    Returns:
        Configured FastAPI instance
    """
    from assistant.web.routes import api_router, page_router

    app = FastAPI(
        title="Outlook AI Assistant",
        description="AI-powered email management review UI",
        version="0.1.0",
        lifespan=lifespan,
    )

    # Mount static files
    _STATIC_DIR.mkdir(parents=True, exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    # Include routers
    app.include_router(page_router)
    app.include_router(api_router)

    return app
