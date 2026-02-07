"""FastAPI dependency injection helpers.

Extracts shared dependencies from app.state for use in route handlers.
All dependencies are initialized during the FastAPI lifespan and stored
on app.state for concurrent access by the web routes and triage engine.

Usage:
    from assistant.web.dependencies import get_store

    @router.get("/")
    async def dashboard(store: DatabaseStore = Depends(get_store)):
        stats = await store.get_stats()
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import Request

if TYPE_CHECKING:
    import anthropic

    from assistant.config_schema import AppConfig
    from assistant.db.store import DatabaseStore
    from assistant.engine.triage import TriageEngine
    from assistant.graph.folders import FolderManager
    from assistant.graph.messages import MessageManager


def get_store(request: Request) -> DatabaseStore:
    """Get the shared DatabaseStore from app state."""
    return request.app.state.store


def get_config(request: Request) -> AppConfig:
    """Get the current AppConfig from app state."""
    return request.app.state.config


def get_message_manager(request: Request) -> MessageManager:
    """Get the MessageManager from app state."""
    return request.app.state.message_manager


def get_folder_manager(request: Request) -> FolderManager:
    """Get the FolderManager from app state."""
    return request.app.state.folder_manager


def get_triage_engine(request: Request) -> TriageEngine:
    """Get the TriageEngine from app state."""
    return request.app.state.triage_engine


def get_anthropic_client(request: Request) -> anthropic.Anthropic:
    """Get the Anthropic client from app state."""
    return request.app.state.anthropic_client
