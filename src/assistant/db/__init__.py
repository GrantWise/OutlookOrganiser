"""Database layer for the Outlook AI Assistant.

This module provides SQLite database access with async operations.

Usage:
    from assistant.db import DatabaseStore, Email, Suggestion

    store = DatabaseStore("data/assistant.db")
    await store.initialize()

    # Save an email
    email = Email(id="abc123", subject="Hello", sender_email="test@example.com")
    await store.save_email(email)

    # Create a suggestion
    suggestion_id = await store.create_suggestion(
        email_id="abc123",
        suggested_folder="Projects/Example",
        suggested_priority="P2 - Important",
        suggested_action_type="Review",
        confidence=0.85,
        reasoning="Matches project signals",
    )
"""

from assistant.db.models import (
    SCHEMA_VERSION,
    get_connection,
    init_database,
    verify_schema,
)
from assistant.db.store import (
    MAX_SNIPPET_LENGTH,
    ActionLogEntry,
    DatabaseStore,
    Email,
    LLMLogEntry,
    SenderHistory,
    SenderProfile,
    Suggestion,
    WaitingFor,
)

__all__ = [
    # Models
    "SCHEMA_VERSION",
    "init_database",
    "get_connection",
    "verify_schema",
    # Store
    "DatabaseStore",
    "MAX_SNIPPET_LENGTH",
    # Dataclasses
    "Email",
    "Suggestion",
    "WaitingFor",
    "SenderProfile",
    "SenderHistory",
    "LLMLogEntry",
    "ActionLogEntry",
]
