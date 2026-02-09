"""Database store with CRUD operations for all tables.

This module provides the DatabaseStore class that encapsulates all database
operations for the Outlook AI Assistant. It uses aiosqlite for async access
and provides type-safe operations with dataclasses.

Usage:
    from assistant.db.store import DatabaseStore

    store = DatabaseStore("data/assistant.db")

    # Email operations
    await store.save_email(email_data)
    email = await store.get_email("message_id")

    # Suggestion operations
    suggestion_id = await store.create_suggestion(suggestion_data)
    await store.approve_suggestion(suggestion_id, approved_folder="Projects/X")
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import aiosqlite

from assistant.core.errors import DatabaseError
from assistant.core.logging import get_correlation_id, get_logger
from assistant.db.models import init_database

logger = get_logger(__name__)

# Maximum snippet length (security limit to prevent full email body storage)
MAX_SNIPPET_LENGTH = 1000

# Type aliases
Priority = Literal[
    "P1 - Urgent Important",
    "P2 - Important",
    "P3 - Urgent Low",
    "P4 - Low",
]

ActionType = Literal[
    "Needs Reply",
    "Review",
    "Delegated",
    "FYI Only",
    "Waiting For",
    "Scheduled",
]

SuggestionStatus = Literal["pending", "approved", "rejected", "partial", "auto_approved", "expired"]
ClassificationStatus = Literal["pending", "classified", "failed"]
WaitingStatus = Literal["waiting", "received", "expired"]
SenderCategory = Literal[
    "key_contact", "newsletter", "automated", "internal", "client", "vendor", "unknown"
]


@dataclass
class Email:
    """Email record from the database."""

    id: str
    conversation_id: str | None = None
    conversation_index: str | None = None
    subject: str | None = None
    sender_email: str | None = None
    sender_name: str | None = None
    received_at: datetime | None = None
    snippet: str | None = None
    current_folder: str | None = None
    web_link: str | None = None
    importance: str = "normal"
    is_read: bool = False
    flag_status: str = "notFlagged"
    has_user_reply: bool = False
    inherited_folder: str | None = None
    processed_at: datetime | None = None
    classification_json: dict[str, Any] | None = None
    classification_attempts: int = 0
    classification_status: ClassificationStatus = "pending"


@dataclass
class Suggestion:
    """Suggestion record from the database."""

    id: int
    email_id: str
    created_at: datetime
    suggested_folder: str | None = None
    suggested_priority: str | None = None
    suggested_action_type: str | None = None
    confidence: float | None = None
    reasoning: str | None = None
    status: SuggestionStatus = "pending"
    approved_folder: str | None = None
    approved_priority: str | None = None
    approved_action_type: str | None = None
    resolved_at: datetime | None = None


@dataclass
class WaitingFor:
    """Waiting-for record from the database."""

    id: int
    email_id: str
    conversation_id: str | None = None
    waiting_since: datetime | None = None
    expected_from: str | None = None
    description: str | None = None
    status: WaitingStatus = "waiting"
    nudge_after_hours: int = 48
    resolved_at: datetime | None = None


@dataclass
class SenderProfile:
    """Sender profile record from the database."""

    email: str
    display_name: str | None = None
    domain: str | None = None
    category: SenderCategory = "unknown"
    default_folder: str | None = None
    email_count: int = 0
    last_seen: datetime | None = None
    auto_rule_candidate: bool = False
    updated_at: datetime | None = None


@dataclass
class LLMLogEntry:
    """LLM request log entry from the database."""

    id: int
    timestamp: datetime
    task_type: str | None = None
    model: str | None = None
    email_id: str | None = None
    triage_cycle_id: str | None = None
    prompt_json: dict[str, Any] | None = None
    response_json: dict[str, Any] | None = None
    tool_call_json: dict[str, Any] | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    duration_ms: int | None = None
    error: str | None = None


@dataclass
class ActionLogEntry:
    """Action log entry from the database."""

    id: int
    timestamp: datetime
    action_type: str
    email_id: str | None = None
    details_json: dict[str, Any] | None = None
    triggered_by: str | None = None


@dataclass(frozen=True)
class TaskSync:
    """Task sync record mapping To Do tasks to emails."""

    id: int
    email_id: str
    todo_task_id: str
    todo_list_id: str
    task_type: str
    created_at: datetime
    synced_at: datetime | None = None
    status: str = "active"


TaskSyncStatus = Literal["active", "completed", "deleted"]


@dataclass
class SenderHistory:
    """Sender history with folder distribution."""

    email: str
    total_emails: int
    folder_distribution: dict[str, int] = field(default_factory=dict)


class DatabaseStore:
    """Database store for all Outlook AI Assistant data.

    This class provides async CRUD operations for all database tables.
    It handles connection management, JSON serialization, and type conversion.

    Attributes:
        db_path: Path to the SQLite database file
        _initialized: Whether the database has been initialized
    """

    def __init__(self, db_path: str | Path):
        """Initialize the database store.

        Args:
            db_path: Path to the SQLite database file
        """
        self.db_path = Path(db_path)
        self._initialized = False

    async def initialize(self) -> None:
        """Initialize the database, creating tables if needed.

        This must be called before any other operations.
        """
        await init_database(self.db_path)
        self._initialized = True

    @asynccontextmanager
    async def _db(self) -> AsyncIterator[aiosqlite.Connection]:
        """Get a configured database connection.

        Sets all required PRAGMAs for reliability and performance:
        - busy_timeout: 10s to handle concurrent access from triage + web UI
        - foreign_keys: ON to enforce referential integrity
        - synchronous: NORMAL (safe with WAL, faster writes)
        - cache_size: 64MB for better read performance
        - temp_store: MEMORY for faster temp operations

        Usage:
            async with self._db() as db:
                await db.execute(...)
        """
        async with aiosqlite.connect(self.db_path) as db:
            # Reliability PRAGMAs
            await db.execute("PRAGMA busy_timeout = 10000")
            await db.execute("PRAGMA foreign_keys = ON")

            # Performance PRAGMAs (safe with WAL mode)
            await db.execute("PRAGMA synchronous = NORMAL")
            await db.execute("PRAGMA cache_size = -64000")  # 64MB
            await db.execute("PRAGMA temp_store = MEMORY")

            db.row_factory = aiosqlite.Row
            yield db

    async def checkpoint_wal(self) -> None:
        """Run a WAL checkpoint to keep WAL file size bounded.

        Uses TRUNCATE mode to reset the WAL file after checkpointing.
        Safe to call periodically (e.g., at the end of each triage cycle).
        """
        try:
            async with self._db() as db:
                await db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            logger.debug("wal_checkpoint_complete")
        except aiosqlite.Error as e:
            logger.warning("wal_checkpoint_failed", error=str(e))

    # =========================================================================
    # Email Operations
    # =========================================================================

    async def save_email(self, email: Email) -> None:
        """Save or update an email record.

        Args:
            email: Email dataclass to save

        Raises:
            DatabaseError: If the operation fails
        """
        try:
            # Truncate snippet as defense-in-depth (security limit)
            snippet = email.snippet
            if snippet and len(snippet) > MAX_SNIPPET_LENGTH:
                snippet = snippet[:MAX_SNIPPET_LENGTH]
                logger.warning(
                    "Truncated oversized snippet",
                    email_id=email.id,
                    original_length=len(email.snippet),
                )

            async with self._db() as db:
                classification_json = (
                    json.dumps(email.classification_json) if email.classification_json else None
                )

                await db.execute(
                    """
                    INSERT INTO emails (
                        id, conversation_id, conversation_index, subject,
                        sender_email, sender_name, received_at, snippet,
                        current_folder, web_link, importance, is_read,
                        flag_status, has_user_reply, inherited_folder,
                        processed_at, classification_json, classification_attempts,
                        classification_status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        conversation_id = excluded.conversation_id,
                        conversation_index = excluded.conversation_index,
                        subject = excluded.subject,
                        sender_email = excluded.sender_email,
                        sender_name = excluded.sender_name,
                        received_at = excluded.received_at,
                        snippet = excluded.snippet,
                        current_folder = excluded.current_folder,
                        web_link = excluded.web_link,
                        importance = excluded.importance,
                        is_read = excluded.is_read,
                        flag_status = excluded.flag_status,
                        has_user_reply = excluded.has_user_reply,
                        inherited_folder = excluded.inherited_folder,
                        processed_at = excluded.processed_at,
                        classification_json = excluded.classification_json,
                        classification_attempts = excluded.classification_attempts,
                        classification_status = excluded.classification_status
                    """,
                    (
                        email.id,
                        email.conversation_id,
                        email.conversation_index,
                        email.subject,
                        email.sender_email,
                        email.sender_name,
                        email.received_at.isoformat() if email.received_at else None,
                        snippet,  # Use truncated snippet
                        email.current_folder,
                        email.web_link,
                        email.importance,
                        1 if email.is_read else 0,
                        email.flag_status,
                        1 if email.has_user_reply else 0,
                        email.inherited_folder,
                        email.processed_at.isoformat() if email.processed_at else None,
                        classification_json,
                        email.classification_attempts,
                        email.classification_status,
                    ),
                )
                await db.commit()

                logger.debug("Email saved", email_id=email.id)

        except aiosqlite.Error as e:
            logger.error("Failed to save email", email_id=email.id, error=str(e))
            raise DatabaseError(f"Failed to save email {email.id}: {e}") from e

    async def save_emails_batch(self, emails: list[Email]) -> int:
        """Save multiple emails in a single transaction.

        Optimized for bootstrap when processing 1000+ emails.
        All emails are saved in one transaction for 10-50x speedup.

        Args:
            emails: List of Email dataclasses to save

        Returns:
            Number of emails saved
        """
        if not emails:
            return 0

        try:
            async with self._db() as db:
                for email in emails:
                    # Truncate snippet as defense-in-depth
                    snippet = email.snippet
                    if snippet and len(snippet) > MAX_SNIPPET_LENGTH:
                        snippet = snippet[:MAX_SNIPPET_LENGTH]

                    classification_json = (
                        json.dumps(email.classification_json) if email.classification_json else None
                    )

                    await db.execute(
                        """
                        INSERT INTO emails (
                            id, conversation_id, conversation_index, subject,
                            sender_email, sender_name, received_at, snippet,
                            current_folder, web_link, importance, is_read,
                            flag_status, has_user_reply, inherited_folder,
                            processed_at, classification_json, classification_attempts,
                            classification_status
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(id) DO UPDATE SET
                            conversation_id = excluded.conversation_id,
                            conversation_index = excluded.conversation_index,
                            subject = excluded.subject,
                            sender_email = excluded.sender_email,
                            sender_name = excluded.sender_name,
                            received_at = excluded.received_at,
                            snippet = excluded.snippet,
                            current_folder = excluded.current_folder,
                            web_link = excluded.web_link,
                            importance = excluded.importance,
                            is_read = excluded.is_read,
                            flag_status = excluded.flag_status,
                            has_user_reply = excluded.has_user_reply,
                            inherited_folder = excluded.inherited_folder,
                            processed_at = excluded.processed_at,
                            classification_json = excluded.classification_json,
                            classification_attempts = excluded.classification_attempts,
                            classification_status = excluded.classification_status
                        """,
                        (
                            email.id,
                            email.conversation_id,
                            email.conversation_index,
                            email.subject,
                            email.sender_email,
                            email.sender_name,
                            email.received_at.isoformat() if email.received_at else None,
                            snippet,
                            email.current_folder,
                            email.web_link,
                            email.importance,
                            1 if email.is_read else 0,
                            email.flag_status,
                            1 if email.has_user_reply else 0,
                            email.inherited_folder,
                            email.processed_at.isoformat() if email.processed_at else None,
                            classification_json,
                            email.classification_attempts,
                            email.classification_status,
                        ),
                    )

                # Single commit for all emails
                await db.commit()
                logger.debug("Batch saved emails", count=len(emails))
                return len(emails)

        except aiosqlite.Error as e:
            logger.error("Failed to batch save emails", count=len(emails), error=str(e))
            raise DatabaseError(f"Failed to batch save emails: {e}") from e

    async def get_email(self, email_id: str) -> Email | None:
        """Get an email by ID.

        Args:
            email_id: The Graph API message ID

        Returns:
            Email dataclass or None if not found
        """
        try:
            async with self._db() as db:
                cursor = await db.execute("SELECT * FROM emails WHERE id = ?", (email_id,))
                row = await cursor.fetchone()

                if not row:
                    return None

                return self._row_to_email(row)

        except aiosqlite.Error as e:
            logger.error("Failed to get email", email_id=email_id, error=str(e))
            raise DatabaseError(f"Failed to get email {email_id}: {e}") from e

    async def get_emails_batch(self, email_ids: list[str]) -> dict[str, Email]:
        """Get multiple emails by ID in a single query.

        Eliminates N+1 query patterns when enriching suggestions, waiting-for
        items, or action logs with email data.

        Args:
            email_ids: List of Graph API message IDs

        Returns:
            Dict mapping email_id to Email dataclass (missing IDs are omitted)
        """
        if not email_ids:
            return {}

        try:
            async with self._db() as db:
                placeholders = ",".join("?" * len(email_ids))
                cursor = await db.execute(
                    f"SELECT * FROM emails WHERE id IN ({placeholders})",
                    email_ids,
                )
                rows = await cursor.fetchall()
                return {row["id"]: self._row_to_email(row) for row in rows}

        except aiosqlite.Error as e:
            logger.error("Failed to get emails batch", count=len(email_ids), error=str(e))
            raise DatabaseError(f"Failed to get emails batch: {e}") from e

    async def email_exists(self, email_id: str) -> bool:
        """Check if an email exists in the database.

        Args:
            email_id: The Graph API message ID

        Returns:
            True if the email exists
        """
        try:
            async with self._db() as db:
                cursor = await db.execute("SELECT 1 FROM emails WHERE id = ?", (email_id,))
                row = await cursor.fetchone()
                return row is not None

        except aiosqlite.Error as e:
            logger.error("Failed to check email existence", email_id=email_id, error=str(e))
            raise DatabaseError(f"Failed to check email existence: {e}") from e

    async def has_suggestion(self, email_id: str) -> bool:
        """Check if an email already has any suggestion.

        Args:
            email_id: The Graph API message ID

        Returns:
            True if any suggestion (pending, approved, or rejected) exists
        """
        try:
            async with self._db() as db:
                cursor = await db.execute(
                    "SELECT 1 FROM suggestions WHERE email_id = ? LIMIT 1",
                    (email_id,),
                )
                row = await cursor.fetchone()
                return row is not None

        except aiosqlite.Error as e:
            logger.error("Failed to check suggestion existence", email_id=email_id, error=str(e))
            raise DatabaseError(f"Failed to check suggestion existence: {e}") from e

    async def get_suggestion_by_email_id(self, email_id: str) -> Suggestion | None:
        """Get the most recent suggestion for an email.

        Args:
            email_id: The Graph API message ID

        Returns:
            Most recent Suggestion dataclass or None if not found
        """
        try:
            async with self._db() as db:
                cursor = await db.execute(
                    """
                    SELECT * FROM suggestions
                    WHERE email_id = ?
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    (email_id,),
                )
                row = await cursor.fetchone()
                if not row:
                    return None
                return self._row_to_suggestion(row)

        except aiosqlite.Error as e:
            logger.error(
                "Failed to get suggestion by email_id",
                email_id=email_id,
                error=str(e),
            )
            raise DatabaseError(f"Failed to get suggestion by email_id: {e}") from e

    async def get_thread_classification(self, conversation_id: str) -> tuple[str, str] | None:
        """Get the most recent classification for a thread.

        Used for thread inheritance - if a prior email in the thread
        was classified, we can inherit that folder.

        Args:
            conversation_id: The Graph API conversation ID

        Returns:
            Tuple of (folder, confidence) or None if no prior classification
        """
        try:
            async with self._db() as db:
                cursor = await db.execute(
                    """
                    SELECT s.approved_folder, s.confidence
                    FROM emails e
                    JOIN suggestions s ON e.id = s.email_id
                    WHERE e.conversation_id = ?
                    AND s.status = 'approved'
                    AND s.approved_folder IS NOT NULL
                    ORDER BY e.received_at DESC
                    LIMIT 1
                    """,
                    (conversation_id,),
                )
                row = await cursor.fetchone()

                if not row:
                    return None

                return (row["approved_folder"], row["confidence"])

        except aiosqlite.Error as e:
            logger.error(
                "Failed to get thread classification",
                conversation_id=conversation_id,
                error=str(e),
            )
            raise DatabaseError(f"Failed to get thread classification: {e}") from e

    async def get_emails_by_status(
        self,
        status: ClassificationStatus,
        limit: int = 100,
    ) -> list[Email]:
        """Get emails by classification status.

        Args:
            status: Classification status to filter by
            limit: Maximum number of emails to return

        Returns:
            List of Email dataclasses
        """
        try:
            async with self._db() as db:
                cursor = await db.execute(
                    """
                    SELECT * FROM emails
                    WHERE classification_status = ?
                    ORDER BY received_at DESC
                    LIMIT ?
                    """,
                    (status, limit),
                )
                rows = await cursor.fetchall()
                return [self._row_to_email(row) for row in rows]

        except aiosqlite.Error as e:
            logger.error("Failed to get emails by status", status=status, error=str(e))
            raise DatabaseError(f"Failed to get emails by status: {e}") from e

    async def get_thread_emails(
        self,
        conversation_id: str,
        exclude_id: str | None = None,
        limit: int = 4,
    ) -> list[Email]:
        """Get emails in a conversation thread from local database.

        Used for thread context fetching - checks local database before
        falling back to Graph API. Returns emails ordered by received_at
        descending (most recent first).

        Args:
            conversation_id: Graph API conversation ID
            exclude_id: Email ID to exclude (typically the current email)
            limit: Maximum number of emails to return

        Returns:
            List of Email dataclasses in the thread
        """
        try:
            async with self._db() as db:
                if exclude_id:
                    cursor = await db.execute(
                        """
                        SELECT * FROM emails
                        WHERE conversation_id = ?
                        AND id != ?
                        ORDER BY received_at DESC
                        LIMIT ?
                        """,
                        (conversation_id, exclude_id, limit),
                    )
                else:
                    cursor = await db.execute(
                        """
                        SELECT * FROM emails
                        WHERE conversation_id = ?
                        ORDER BY received_at DESC
                        LIMIT ?
                        """,
                        (conversation_id, limit),
                    )
                rows = await cursor.fetchall()
                return [self._row_to_email(row) for row in rows]

        except aiosqlite.Error as e:
            logger.error(
                "Failed to get thread emails",
                conversation_id=conversation_id,
                error=str(e),
            )
            raise DatabaseError(f"Failed to get thread emails: {e}") from e

    async def update_classification_status(
        self,
        email_id: str,
        status: ClassificationStatus,
        classification_json: dict[str, Any] | None = None,
    ) -> None:
        """Update the classification status of an email.

        Args:
            email_id: The email ID
            status: New classification status
            classification_json: Optional classification result
        """
        try:
            async with self._db() as db:
                if classification_json:
                    await db.execute(
                        """
                        UPDATE emails
                        SET classification_status = ?,
                            classification_json = ?,
                            processed_at = ?
                        WHERE id = ?
                        """,
                        (
                            status,
                            json.dumps(classification_json),
                            datetime.now().isoformat(),
                            email_id,
                        ),
                    )
                else:
                    await db.execute(
                        """
                        UPDATE emails
                        SET classification_status = ?,
                            processed_at = ?
                        WHERE id = ?
                        """,
                        (status, datetime.now().isoformat(), email_id),
                    )
                await db.commit()

        except aiosqlite.Error as e:
            logger.error("Failed to update classification status", email_id=email_id, error=str(e))
            raise DatabaseError(f"Failed to update classification status: {e}") from e

    async def increment_classification_attempts(self, email_id: str) -> int:
        """Increment the classification attempt counter for an email.

        Uses RETURNING clause for atomic update (SQLite 3.35+).

        Args:
            email_id: The email ID

        Returns:
            New attempt count, or 0 if email not found
        """
        try:
            async with self._db() as db:
                cursor = await db.execute(
                    """
                    UPDATE emails
                    SET classification_attempts = classification_attempts + 1
                    WHERE id = ?
                    RETURNING classification_attempts
                    """,
                    (email_id,),
                )
                row = await cursor.fetchone()
                await db.commit()

                if not row:
                    logger.warning("Email not found for attempt increment", email_id=email_id)
                    return 0

                return row[0]

        except aiosqlite.Error as e:
            logger.error("Failed to increment attempts", email_id=email_id, error=str(e))
            raise DatabaseError(f"Failed to increment attempts: {e}") from e

    def _row_to_email(self, row: aiosqlite.Row) -> Email:
        """Convert a database row to an Email dataclass."""
        classification_json = None
        if row["classification_json"]:
            try:
                classification_json = json.loads(row["classification_json"])
            except json.JSONDecodeError:
                pass

        return Email(
            id=row["id"],
            conversation_id=row["conversation_id"],
            conversation_index=row["conversation_index"],
            subject=row["subject"],
            sender_email=row["sender_email"],
            sender_name=row["sender_name"],
            received_at=datetime.fromisoformat(row["received_at"]) if row["received_at"] else None,
            snippet=row["snippet"],
            current_folder=row["current_folder"],
            web_link=row["web_link"],
            importance=row["importance"],
            is_read=bool(row["is_read"]),
            flag_status=row["flag_status"],
            has_user_reply=bool(row["has_user_reply"]),
            inherited_folder=row["inherited_folder"],
            processed_at=datetime.fromisoformat(row["processed_at"])
            if row["processed_at"]
            else None,
            classification_json=classification_json,
            classification_attempts=row["classification_attempts"],
            classification_status=row["classification_status"],
        )

    # =========================================================================
    # Suggestion Operations
    # =========================================================================

    async def create_suggestion(
        self,
        email_id: str,
        suggested_folder: str,
        suggested_priority: str,
        suggested_action_type: str,
        confidence: float,
        reasoning: str,
    ) -> int:
        """Create a new suggestion for an email.

        Args:
            email_id: The email ID this suggestion is for
            suggested_folder: Suggested folder path
            suggested_priority: Suggested priority
            suggested_action_type: Suggested action type
            confidence: Confidence score (0.0-1.0)
            reasoning: One-sentence explanation

        Returns:
            The new suggestion ID
        """
        try:
            async with self._db() as db:
                cursor = await db.execute(
                    """
                    INSERT INTO suggestions (
                        email_id, suggested_folder, suggested_priority,
                        suggested_action_type, confidence, reasoning
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        email_id,
                        suggested_folder,
                        suggested_priority,
                        suggested_action_type,
                        confidence,
                        reasoning,
                    ),
                )
                await db.commit()

                suggestion_id = cursor.lastrowid
                logger.debug(
                    "Suggestion created",
                    suggestion_id=suggestion_id,
                    email_id=email_id,
                    folder=suggested_folder,
                )
                return suggestion_id

        except aiosqlite.Error as e:
            logger.error("Failed to create suggestion", email_id=email_id, error=str(e))
            raise DatabaseError(f"Failed to create suggestion: {e}") from e

    async def get_suggestion(self, suggestion_id: int) -> Suggestion | None:
        """Get a suggestion by ID.

        Args:
            suggestion_id: The suggestion ID

        Returns:
            Suggestion dataclass or None if not found
        """
        try:
            async with self._db() as db:
                cursor = await db.execute(
                    "SELECT * FROM suggestions WHERE id = ?", (suggestion_id,)
                )
                row = await cursor.fetchone()

                if not row:
                    return None

                return self._row_to_suggestion(row)

        except aiosqlite.Error as e:
            logger.error("Failed to get suggestion", suggestion_id=suggestion_id, error=str(e))
            raise DatabaseError(f"Failed to get suggestion: {e}") from e

    async def get_pending_suggestions(self, limit: int = 100) -> list[Suggestion]:
        """Get all pending suggestions.

        Args:
            limit: Maximum number of suggestions to return

        Returns:
            List of pending Suggestion dataclasses
        """
        try:
            async with self._db() as db:
                cursor = await db.execute(
                    """
                    SELECT * FROM suggestions
                    WHERE status = 'pending'
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (limit,),
                )
                rows = await cursor.fetchall()
                return [self._row_to_suggestion(row) for row in rows]

        except aiosqlite.Error as e:
            logger.error("Failed to get pending suggestions", error=str(e))
            raise DatabaseError(f"Failed to get pending suggestions: {e}") from e

    async def approve_suggestion(
        self,
        suggestion_id: int,
        approved_folder: str | None = None,
        approved_priority: str | None = None,
        approved_action_type: str | None = None,
    ) -> bool:
        """Approve a suggestion, optionally with corrections.

        Uses a single atomic UPDATE to prevent race conditions.
        If no corrections are provided, uses the suggested values via COALESCE.

        Args:
            suggestion_id: The suggestion ID
            approved_folder: Override folder (or None to use suggested)
            approved_priority: Override priority (or None to use suggested)
            approved_action_type: Override action type (or None to use suggested)

        Returns:
            True if suggestion was found and updated, False if not found or already resolved
        """
        try:
            async with self._db() as db:
                # Single atomic UPDATE with COALESCE for defaults
                # Status is 'partial' if any approved value differs from suggested
                cursor = await db.execute(
                    """
                    UPDATE suggestions
                    SET status = CASE
                        WHEN (? IS NOT NULL AND ? != suggested_folder)
                             OR (? IS NOT NULL AND ? != suggested_priority)
                             OR (? IS NOT NULL AND ? != suggested_action_type)
                        THEN 'partial'
                        ELSE 'approved'
                    END,
                    approved_folder = COALESCE(?, suggested_folder),
                    approved_priority = COALESCE(?, suggested_priority),
                    approved_action_type = COALESCE(?, suggested_action_type),
                    resolved_at = ?
                    WHERE id = ? AND status = 'pending'
                    RETURNING id, status, approved_folder
                    """,
                    (
                        approved_folder,
                        approved_folder,
                        approved_priority,
                        approved_priority,
                        approved_action_type,
                        approved_action_type,
                        approved_folder,
                        approved_priority,
                        approved_action_type,
                        datetime.now().isoformat(),
                        suggestion_id,
                    ),
                )
                row = await cursor.fetchone()
                await db.commit()

                if not row:
                    logger.warning(
                        "Suggestion not found or already resolved",
                        suggestion_id=suggestion_id,
                    )
                    return False

                logger.info(
                    "Suggestion approved",
                    suggestion_id=suggestion_id,
                    status=row["status"],
                    folder=row["approved_folder"],
                )
                return True

        except aiosqlite.Error as e:
            logger.error("Failed to approve suggestion", suggestion_id=suggestion_id, error=str(e))
            raise DatabaseError(f"Failed to approve suggestion: {e}") from e

    async def reject_suggestion(self, suggestion_id: int) -> None:
        """Reject a suggestion.

        Args:
            suggestion_id: The suggestion ID
        """
        try:
            async with self._db() as db:
                await db.execute(
                    """
                    UPDATE suggestions
                    SET status = 'rejected',
                        resolved_at = ?
                    WHERE id = ?
                    """,
                    (datetime.now().isoformat(), suggestion_id),
                )
                await db.commit()

                logger.info("Suggestion rejected", suggestion_id=suggestion_id)

        except aiosqlite.Error as e:
            logger.error("Failed to reject suggestion", suggestion_id=suggestion_id, error=str(e))
            raise DatabaseError(f"Failed to reject suggestion: {e}") from e

    async def get_suggestions_by_conversation(self, conversation_id: str) -> list[Suggestion]:
        """Get all suggestions for emails in a conversation thread.

        Returns suggestions of any status (pending, approved, rejected, partial).

        Args:
            conversation_id: The Outlook conversation ID

        Returns:
            List of Suggestion dataclasses ordered by email received_at DESC
        """
        try:
            async with self._db() as db:
                cursor = await db.execute(
                    """
                    SELECT s.* FROM suggestions s
                    JOIN emails e ON s.email_id = e.id
                    WHERE e.conversation_id = ?
                    ORDER BY e.received_at DESC
                    """,
                    (conversation_id,),
                )
                rows = await cursor.fetchall()
                return [self._row_to_suggestion(row) for row in rows]

        except aiosqlite.Error as e:
            logger.error(
                "Failed to get suggestions by conversation",
                conversation_id=conversation_id,
                error=str(e),
            )
            raise DatabaseError(f"Failed to get suggestions by conversation: {e}") from e

    async def get_pending_suggestions_by_sender(self, sender_email: str) -> list[Suggestion]:
        """Get pending suggestions for emails from a specific sender.

        Args:
            sender_email: The sender's email address

        Returns:
            List of pending Suggestion dataclasses ordered by received_at DESC
        """
        try:
            async with self._db() as db:
                cursor = await db.execute(
                    """
                    SELECT s.* FROM suggestions s
                    JOIN emails e ON s.email_id = e.id
                    WHERE e.sender_email = ? AND s.status = 'pending'
                    ORDER BY e.received_at DESC
                    """,
                    (sender_email.lower(),),
                )
                rows = await cursor.fetchall()
                return [self._row_to_suggestion(row) for row in rows]

        except aiosqlite.Error as e:
            logger.error(
                "Failed to get pending suggestions by sender",
                sender_email=sender_email,
                error=str(e),
            )
            raise DatabaseError(f"Failed to get pending suggestions by sender: {e}") from e

    async def get_recent_corrections(self, days: int) -> list[dict[str, Any]]:
        """Get recent user corrections (where approved values differ from suggested).

        Corrections are suggestions where:
        - status is 'partial' (user modified at least one field)
        - resolved within the lookback window

        Each row includes both suggested and approved values plus email metadata
        for context in preference learning.

        Args:
            days: Number of days to look back

        Returns:
            List of dicts with correction details
        """
        try:
            cutoff = datetime.now() - timedelta(days=days)

            async with self._db() as db:
                cursor = await db.execute(
                    """
                    SELECT
                        s.id,
                        s.email_id,
                        e.subject,
                        e.sender_email,
                        s.suggested_folder,
                        s.suggested_priority,
                        s.suggested_action_type,
                        s.approved_folder,
                        s.approved_priority,
                        s.approved_action_type,
                        s.confidence,
                        s.resolved_at
                    FROM suggestions s
                    JOIN emails e ON s.email_id = e.id
                    WHERE s.status = 'partial'
                    AND s.resolved_at >= ?
                    ORDER BY s.resolved_at DESC
                    """,
                    (cutoff.isoformat(),),
                )
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]

        except aiosqlite.Error as e:
            logger.error("Failed to get recent corrections", days=days, error=str(e))
            raise DatabaseError(f"Failed to get recent corrections: {e}") from e

    async def get_correction_count_since(self, since: datetime) -> int:
        """Count corrections since a given timestamp.

        Args:
            since: Count corrections after this time

        Returns:
            Number of corrections
        """
        try:
            async with self._db() as db:
                cursor = await db.execute(
                    """
                    SELECT COUNT(*) FROM suggestions
                    WHERE status = 'partial'
                    AND resolved_at >= ?
                    """,
                    (since.isoformat(),),
                )
                row = await cursor.fetchone()
                return row[0] if row else 0

        except aiosqlite.Error as e:
            logger.error("Failed to count corrections", error=str(e))
            raise DatabaseError(f"Failed to count corrections: {e}") from e

    async def update_email_inherited_folder(self, email_id: str, folder: str) -> None:
        """Update the inherited_folder field on an email record.

        Used when chat reclassification changes a thread's folder, ensuring
        future thread inheritance uses the corrected classification.

        Args:
            email_id: The Graph API message ID
            folder: The new folder path to set as inherited
        """
        try:
            async with self._db() as db:
                await db.execute(
                    "UPDATE emails SET inherited_folder = ? WHERE id = ?",
                    (folder, email_id),
                )
                await db.commit()

        except aiosqlite.Error as e:
            logger.error(
                "Failed to update email inherited_folder",
                email_id=email_id,
                folder=folder,
                error=str(e),
            )
            raise DatabaseError(f"Failed to update email inherited_folder: {e}") from e

    async def expire_old_suggestions(self, days: int) -> int:
        """Expire pending suggestions older than the specified days.

        Args:
            days: Number of days after which to expire suggestions

        Returns:
            Number of suggestions expired
        """
        try:
            cutoff = datetime.now() - timedelta(days=days)

            async with self._db() as db:
                cursor = await db.execute(
                    """
                    UPDATE suggestions
                    SET status = 'expired',
                        resolved_at = ?
                    WHERE status = 'pending'
                    AND created_at < ?
                    """,
                    (datetime.now().isoformat(), cutoff.isoformat()),
                )
                await db.commit()

                expired = cursor.rowcount
                if expired:
                    logger.info("Expired old suggestions", count=expired, days=days)
                return expired

        except aiosqlite.Error as e:
            logger.error("Failed to expire suggestions", error=str(e))
            raise DatabaseError(f"Failed to expire suggestions: {e}") from e

    async def get_auto_approvable_suggestions(
        self,
        min_confidence: float,
        min_age_hours: int,
    ) -> list[Suggestion]:
        """Find pending suggestions eligible for auto-approval.

        Suggestions must be pending for at least min_age_hours. P1 suggestions
        are never auto-approved regardless of confidence (always require human
        review).

        Does NOT update status — caller must mark approved after Graph API
        moves succeed (C4: DB-after-Graph pattern).

        Args:
            min_confidence: Minimum confidence score for auto-approval
            min_age_hours: Hours a suggestion must be pending before auto-approving

        Returns:
            List of eligible Suggestion objects
        """
        try:
            cutoff = datetime.now() - timedelta(hours=min_age_hours)

            async with self._db() as db:
                cursor = await db.execute(
                    """
                    SELECT * FROM suggestions
                    WHERE status = 'pending'
                    AND confidence >= ?
                    AND created_at < ?
                    AND suggested_priority != 'P1 - Urgent Important'
                    """,
                    (min_confidence, cutoff.isoformat()),
                )
                rows = await cursor.fetchall()

                if not rows:
                    return []

                return [self._row_to_suggestion(row) for row in rows]

        except aiosqlite.Error as e:
            logger.error("Failed to query auto-approvable suggestions", error=str(e))
            raise DatabaseError(f"Failed to query auto-approvable suggestions: {e}") from e

    async def mark_suggestion_auto_approved(self, suggestion_id: int) -> bool:
        """Mark a single suggestion as auto-approved after Graph API move succeeds.

        C4: Only called after the Graph API move has already succeeded,
        ensuring DB state always reflects actual email location.

        Args:
            suggestion_id: The suggestion ID to approve

        Returns:
            True if the suggestion was updated, False if already resolved
        """
        try:
            async with self._db() as db:
                cursor = await db.execute(
                    """
                    UPDATE suggestions
                    SET status = 'auto_approved',
                        approved_folder = suggested_folder,
                        approved_priority = suggested_priority,
                        approved_action_type = suggested_action_type,
                        resolved_at = ?
                    WHERE id = ? AND status = 'pending'
                    """,
                    (datetime.now().isoformat(), suggestion_id),
                )
                await db.commit()
                return cursor.rowcount > 0

        except aiosqlite.Error as e:
            logger.error(
                "Failed to mark suggestion auto-approved",
                suggestion_id=suggestion_id,
                error=str(e),
            )
            raise DatabaseError(
                f"Failed to mark suggestion {suggestion_id} auto-approved: {e}"
            ) from e

    async def revert_suggestion_to_pending(self, suggestion_id: int) -> None:
        """Revert an auto-approved suggestion back to pending.

        Used when Graph API move fails for an auto-approved suggestion.

        Args:
            suggestion_id: ID of the suggestion to revert
        """
        try:
            async with self._db() as db:
                await db.execute(
                    """
                    UPDATE suggestions
                    SET status = 'pending',
                        approved_folder = NULL,
                        approved_priority = NULL,
                        approved_action_type = NULL,
                        resolved_at = NULL
                    WHERE id = ?
                    """,
                    (suggestion_id,),
                )
                await db.commit()
        except aiosqlite.Error as e:
            logger.error(
                "Failed to revert suggestion to pending",
                suggestion_id=suggestion_id,
                error=str(e),
            )
            raise DatabaseError(f"Failed to revert suggestion {suggestion_id}: {e}") from e

    async def update_email_folder(self, email_id: str, folder: str) -> None:
        """Update the current_folder of an email.

        Used when delta queries detect a folder change for an existing email.

        Args:
            email_id: Graph API message ID
            folder: New folder path
        """
        try:
            async with self._db() as db:
                await db.execute(
                    "UPDATE emails SET current_folder = ? WHERE id = ?",
                    (folder, email_id),
                )
                await db.commit()
        except aiosqlite.Error as e:
            logger.error(
                "Failed to update email folder",
                email_id=email_id[:20],
                folder=folder,
                error=str(e),
            )
            raise DatabaseError(f"Failed to update email folder: {e}") from e

    def _row_to_suggestion(self, row: aiosqlite.Row) -> Suggestion:
        """Convert a database row to a Suggestion dataclass."""
        return Suggestion(
            id=row["id"],
            email_id=row["email_id"],
            created_at=datetime.fromisoformat(row["created_at"])
            if row["created_at"]
            else datetime.now(),
            suggested_folder=row["suggested_folder"],
            suggested_priority=row["suggested_priority"],
            suggested_action_type=row["suggested_action_type"],
            confidence=row["confidence"],
            reasoning=row["reasoning"],
            status=row["status"],
            approved_folder=row["approved_folder"],
            approved_priority=row["approved_priority"],
            approved_action_type=row["approved_action_type"],
            resolved_at=datetime.fromisoformat(row["resolved_at"]) if row["resolved_at"] else None,
        )

    # =========================================================================
    # Waiting-For Operations
    # =========================================================================

    async def create_waiting_for(
        self,
        email_id: str,
        conversation_id: str,
        expected_from: str,
        description: str,
        nudge_after_hours: int = 48,
    ) -> int:
        """Create a new waiting-for tracker.

        Args:
            email_id: The email ID
            conversation_id: The conversation ID to monitor
            expected_from: Email address we're waiting on
            description: What we're waiting for
            nudge_after_hours: Hours before suggesting a nudge

        Returns:
            The new waiting-for ID
        """
        try:
            async with self._db() as db:
                cursor = await db.execute(
                    """
                    INSERT INTO waiting_for (
                        email_id, conversation_id, waiting_since,
                        expected_from, description, nudge_after_hours
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        email_id,
                        conversation_id,
                        datetime.now().isoformat(),
                        expected_from,
                        description,
                        nudge_after_hours,
                    ),
                )
                await db.commit()
                return cursor.lastrowid

        except aiosqlite.Error as e:
            logger.error("Failed to create waiting-for", email_id=email_id, error=str(e))
            raise DatabaseError(f"Failed to create waiting-for: {e}") from e

    async def get_active_waiting_for(self) -> list[WaitingFor]:
        """Get all active waiting-for items.

        Returns:
            List of active WaitingFor dataclasses
        """
        try:
            async with self._db() as db:
                cursor = await db.execute(
                    """
                    SELECT * FROM waiting_for
                    WHERE status = 'waiting'
                    ORDER BY waiting_since ASC
                    """
                )
                rows = await cursor.fetchall()
                return [self._row_to_waiting_for(row) for row in rows]

        except aiosqlite.Error as e:
            logger.error("Failed to get active waiting-for", error=str(e))
            raise DatabaseError(f"Failed to get active waiting-for: {e}") from e

    async def resolve_waiting_for(
        self,
        waiting_for_id: int,
        status: WaitingStatus = "received",
    ) -> bool:
        """Resolve a waiting-for item (idempotent).

        H5: Only updates items that are still in 'waiting' status. Returns
        False if the item was already resolved, preventing double resolution.

        Args:
            waiting_for_id: The waiting-for ID
            status: Resolution status ('received' or 'expired')

        Returns:
            True if the item was actually resolved, False if already resolved
        """
        try:
            async with self._db() as db:
                cursor = await db.execute(
                    """
                    UPDATE waiting_for
                    SET status = ?,
                        resolved_at = ?
                    WHERE id = ? AND status = 'waiting'
                    """,
                    (status, datetime.now().isoformat(), waiting_for_id),
                )
                await db.commit()
                return cursor.rowcount > 0

        except aiosqlite.Error as e:
            logger.error(
                "Failed to resolve waiting-for", waiting_for_id=waiting_for_id, error=str(e)
            )
            raise DatabaseError(f"Failed to resolve waiting-for: {e}") from e

    async def extend_waiting_for_deadline(
        self,
        waiting_for_id: int,
        additional_hours: int,
    ) -> None:
        """Extend a waiting-for item's nudge deadline.

        Args:
            waiting_for_id: The waiting-for ID
            additional_hours: Hours to add to the nudge_after_hours
        """
        try:
            async with self._db() as db:
                await db.execute(
                    """
                    UPDATE waiting_for
                    SET nudge_after_hours = nudge_after_hours + ?
                    WHERE id = ? AND status = 'waiting'
                    """,
                    (additional_hours, waiting_for_id),
                )
                await db.commit()
        except aiosqlite.Error as e:
            logger.error(
                "Failed to extend waiting-for deadline",
                waiting_for_id=waiting_for_id,
                error=str(e),
            )
            raise DatabaseError(f"Failed to extend waiting-for deadline: {e}") from e

    async def check_waiting_for_by_conversation(self, conversation_id: str) -> WaitingFor | None:
        """Check if there's an active waiting-for for a conversation.

        Args:
            conversation_id: The conversation ID

        Returns:
            Active WaitingFor or None
        """
        try:
            async with self._db() as db:
                cursor = await db.execute(
                    """
                    SELECT * FROM waiting_for
                    WHERE conversation_id = ?
                    AND status = 'waiting'
                    ORDER BY waiting_since DESC
                    LIMIT 1
                    """,
                    (conversation_id,),
                )
                row = await cursor.fetchone()

                if not row:
                    return None

                return self._row_to_waiting_for(row)

        except aiosqlite.Error as e:
            logger.error(
                "Failed to check waiting-for", conversation_id=conversation_id, error=str(e)
            )
            raise DatabaseError(f"Failed to check waiting-for: {e}") from e

    def _row_to_waiting_for(self, row: aiosqlite.Row) -> WaitingFor:
        """Convert a database row to a WaitingFor dataclass."""
        return WaitingFor(
            id=row["id"],
            email_id=row["email_id"],
            conversation_id=row["conversation_id"],
            waiting_since=datetime.fromisoformat(row["waiting_since"])
            if row["waiting_since"]
            else None,
            expected_from=row["expected_from"],
            description=row["description"],
            status=row["status"],
            nudge_after_hours=row["nudge_after_hours"],
            resolved_at=datetime.fromisoformat(row["resolved_at"]) if row["resolved_at"] else None,
        )

    # =========================================================================
    # Agent State Operations
    # =========================================================================

    async def get_state(self, key: str) -> str | None:
        """Get an agent state value.

        Args:
            key: State key

        Returns:
            State value or None if not found
        """
        try:
            async with self._db() as db:
                cursor = await db.execute("SELECT value FROM agent_state WHERE key = ?", (key,))
                row = await cursor.fetchone()
                return row["value"] if row else None

        except aiosqlite.Error as e:
            logger.error("Failed to get state", key=key, error=str(e))
            raise DatabaseError(f"Failed to get state: {e}") from e

    async def set_state(self, key: str, value: str) -> None:
        """Set an agent state value.

        Args:
            key: State key
            value: State value
        """
        try:
            async with self._db() as db:
                await db.execute(
                    """
                    INSERT INTO agent_state (key, value, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        value = excluded.value,
                        updated_at = excluded.updated_at
                    """,
                    (key, value, datetime.now().isoformat()),
                )
                await db.commit()

        except aiosqlite.Error as e:
            logger.error("Failed to set state", key=key, error=str(e))
            raise DatabaseError(f"Failed to set state: {e}") from e

    async def delete_state(self, key: str) -> None:
        """Delete an agent state value.

        Args:
            key: State key
        """
        try:
            async with self._db() as db:
                await db.execute("DELETE FROM agent_state WHERE key = ?", (key,))
                await db.commit()

        except aiosqlite.Error as e:
            logger.error("Failed to delete state", key=key, error=str(e))
            raise DatabaseError(f"Failed to delete state: {e}") from e

    # =========================================================================
    # Auto-Rule Match Tracking (Phase 2 - Feature 2F)
    # =========================================================================

    async def record_auto_rule_match(self, rule_name: str) -> None:
        """Record that an auto-rule matched an email.

        Upserts the match count for tracking rule health and stale detection.

        Args:
            rule_name: Name of the auto-rule that matched
        """
        try:
            async with self._db() as db:
                await db.execute(
                    """
                    INSERT INTO auto_rule_matches (rule_name, match_count, last_match_at, updated_at)
                    VALUES (?, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    ON CONFLICT(rule_name) DO UPDATE SET
                        match_count = match_count + 1,
                        last_match_at = CURRENT_TIMESTAMP,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (rule_name,),
                )
                await db.commit()
        except aiosqlite.Error as e:
            # Non-fatal — don't break triage for tracking failures
            logger.warning("record_auto_rule_match_failed", rule_name=rule_name, error=str(e))

    async def get_auto_rule_match_counts(self) -> dict[str, dict[str, Any]]:
        """Get match counts and last match times for all tracked auto-rules.

        Returns:
            Dict mapping rule_name -> {match_count, last_match_at}
        """
        try:
            async with self._db() as db:
                cursor = await db.execute(
                    "SELECT rule_name, match_count, last_match_at FROM auto_rule_matches"
                )
                rows = await cursor.fetchall()
                return {
                    row["rule_name"]: {
                        "match_count": row["match_count"],
                        "last_match_at": row["last_match_at"],
                    }
                    for row in rows
                }
        except aiosqlite.Error as e:
            logger.error("get_auto_rule_match_counts_failed", error=str(e))
            raise DatabaseError(f"Failed to get auto-rule match counts: {e}") from e

    # =========================================================================
    # Sender Profile Operations
    # =========================================================================

    async def upsert_sender_profile(
        self,
        email: str,
        display_name: str | None = None,
        category: SenderCategory = "unknown",
        increment_count: bool = True,
    ) -> None:
        """Insert or update a sender profile.

        Args:
            email: Sender email address
            display_name: Sender display name
            category: Sender category
            increment_count: Whether to increment email count
        """
        try:
            # Extract domain from email
            domain = email.split("@")[1].lower() if "@" in email else None
            now = datetime.now().isoformat()

            async with self._db() as db:
                # Single statement with conditional increment via CASE expression
                await db.execute(
                    """
                    INSERT INTO sender_profiles (
                        email, display_name, domain, category, email_count,
                        last_seen, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(email) DO UPDATE SET
                        display_name = COALESCE(excluded.display_name, display_name),
                        category = CASE WHEN excluded.category != 'unknown'
                                       THEN excluded.category ELSE category END,
                        email_count = CASE WHEN ? THEN email_count + 1 ELSE email_count END,
                        last_seen = excluded.last_seen,
                        updated_at = excluded.updated_at
                    """,
                    (
                        email.lower(),
                        display_name,
                        domain,
                        category,
                        1 if increment_count else 0,  # Initial count on insert
                        now,
                        now,
                        increment_count,  # For CASE expression on update
                    ),
                )
                await db.commit()

        except aiosqlite.Error as e:
            logger.error("Failed to upsert sender profile", email=email, error=str(e))
            raise DatabaseError(f"Failed to upsert sender profile: {e}") from e

    async def upsert_sender_profiles_batch(
        self,
        profiles: list[dict[str, str | int | bool | None]],
    ) -> int:
        """Upsert multiple sender profiles in a single transaction.

        Optimized for bootstrap when populating 500+ sender profiles.
        Combines upsert, auto_rule_candidate marking, and default_folder
        update into a single statement per profile, all in one transaction.

        Expected dict keys:
            email (str): Sender email address (required)
            display_name (str | None): Sender display name
            category (str): SenderCategory value (default: "unknown")
            email_count (int): Total email count to set (not increment)
            auto_rule_candidate (bool): Whether sender is a candidate
            default_folder (str | None): Most common folder for sender

        Args:
            profiles: List of profile dicts

        Returns:
            Number of profiles upserted
        """
        if not profiles:
            return 0

        try:
            async with self._db() as db:
                now = datetime.now().isoformat()

                for profile in profiles:
                    addr = str(profile["email"]).lower()
                    domain = addr.split("@")[1].lower() if "@" in addr else None
                    display_name = profile.get("display_name")
                    category = profile.get("category", "unknown")
                    email_count = profile.get("email_count", 1)
                    is_candidate = profile.get("auto_rule_candidate", False)
                    default_folder = profile.get("default_folder")

                    await db.execute(
                        """
                        INSERT INTO sender_profiles (
                            email, display_name, domain, category, email_count,
                            auto_rule_candidate, default_folder, last_seen, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(email) DO UPDATE SET
                            display_name = COALESCE(excluded.display_name, display_name),
                            category = CASE WHEN excluded.category != 'unknown'
                                           THEN excluded.category ELSE category END,
                            email_count = excluded.email_count,
                            auto_rule_candidate = excluded.auto_rule_candidate,
                            default_folder = COALESCE(excluded.default_folder, default_folder),
                            last_seen = excluded.last_seen,
                            updated_at = excluded.updated_at
                        """,
                        (
                            addr,
                            display_name,
                            domain,
                            category,
                            email_count,
                            1 if is_candidate else 0,
                            default_folder,
                            now,
                            now,
                        ),
                    )

                await db.commit()
                logger.debug("Batch upserted sender profiles", count=len(profiles))
                return len(profiles)

        except aiosqlite.Error as e:
            logger.error(
                "Failed to batch upsert sender profiles",
                count=len(profiles),
                error=str(e),
            )
            raise DatabaseError(f"Failed to batch upsert sender profiles: {e}") from e

    async def get_sender_profile(self, email: str) -> SenderProfile | None:
        """Get a sender profile by email.

        Args:
            email: Sender email address

        Returns:
            SenderProfile or None if not found
        """
        try:
            async with self._db() as db:
                cursor = await db.execute(
                    "SELECT * FROM sender_profiles WHERE email = ?",
                    (email.lower(),),
                )
                row = await cursor.fetchone()

                if not row:
                    return None

                return self._row_to_sender_profile(row)

        except aiosqlite.Error as e:
            logger.error("Failed to get sender profile", email=email, error=str(e))
            raise DatabaseError(f"Failed to get sender profile: {e}") from e

    async def get_sender_history(
        self,
        sender_email: str,
        since_days: int = 180,
    ) -> SenderHistory:
        """Get sender history with folder distribution.

        Args:
            sender_email: Sender email address
            since_days: Only consider emails from the last N days (default 180)

        Returns:
            SenderHistory with folder distribution
        """
        try:
            cutoff = (datetime.now() - timedelta(days=since_days)).isoformat()

            async with self._db() as db:
                # Get total count and folder distribution from approved suggestions
                cursor = await db.execute(
                    """
                    SELECT s.approved_folder, COUNT(*) as count
                    FROM emails e
                    JOIN suggestions s ON e.id = s.email_id
                    WHERE e.sender_email = ?
                    AND s.status IN ('approved', 'partial')
                    AND s.approved_folder IS NOT NULL
                    AND (e.received_at >= ? OR e.received_at IS NULL)
                    GROUP BY s.approved_folder
                    """,
                    (sender_email.lower(), cutoff),
                )
                rows = await cursor.fetchall()

                distribution = {}
                total = 0
                for row in rows:
                    folder = row["approved_folder"]
                    count = row["count"]
                    distribution[folder] = count
                    total += count

                return SenderHistory(
                    email=sender_email.lower(),
                    total_emails=total,
                    folder_distribution=distribution,
                )

        except aiosqlite.Error as e:
            logger.error("Failed to get sender history", sender_email=sender_email, error=str(e))
            raise DatabaseError(f"Failed to get sender history: {e}") from e

    async def get_sender_histories_batch(
        self, sender_emails: list[str]
    ) -> dict[str, SenderHistory]:
        """Get sender histories for multiple senders in one query.

        Optimized for bootstrap when analyzing many senders.
        Reduces N+1 query pattern to a single query.

        Args:
            sender_emails: List of sender email addresses

        Returns:
            Dict mapping lowercase email to SenderHistory
        """
        if not sender_emails:
            return {}

        try:
            async with self._db() as db:
                placeholders = ",".join("?" * len(sender_emails))
                cursor = await db.execute(
                    f"""
                    SELECT LOWER(e.sender_email) as sender, s.approved_folder, COUNT(*) as count
                    FROM emails e
                    JOIN suggestions s ON e.id = s.email_id
                    WHERE LOWER(e.sender_email) IN ({placeholders})
                    AND s.status IN ('approved', 'partial')
                    AND s.approved_folder IS NOT NULL
                    GROUP BY LOWER(e.sender_email), s.approved_folder
                    """,
                    [email.lower() for email in sender_emails],
                )
                rows = await cursor.fetchall()

                # Initialize result with empty histories for all requested senders
                result: dict[str, SenderHistory] = {
                    email.lower(): SenderHistory(
                        email=email.lower(),
                        total_emails=0,
                        folder_distribution={},
                    )
                    for email in sender_emails
                }

                # Populate from query results
                for row in rows:
                    sender = row["sender"]
                    if sender in result:
                        result[sender].folder_distribution[row["approved_folder"]] = row["count"]
                        result[sender].total_emails += row["count"]

                return result

        except aiosqlite.Error as e:
            logger.error("Failed to get sender histories batch", error=str(e))
            raise DatabaseError(f"Failed to get sender histories batch: {e}") from e

    async def update_sender_default_folder(self, email: str, default_folder: str) -> None:
        """Update the default folder for a sender.

        Args:
            email: Sender email address
            default_folder: Most common folder for this sender
        """
        try:
            async with self._db() as db:
                await db.execute(
                    """
                    UPDATE sender_profiles
                    SET default_folder = ?,
                        updated_at = ?
                    WHERE email = ?
                    """,
                    (default_folder, datetime.now().isoformat(), email.lower()),
                )
                await db.commit()

        except aiosqlite.Error as e:
            logger.error("Failed to update sender default folder", email=email, error=str(e))
            raise DatabaseError(f"Failed to update sender default folder: {e}") from e

    async def mark_auto_rule_candidate(self, email: str, is_candidate: bool) -> None:
        """Mark a sender as an auto-rule candidate.

        Args:
            email: Sender email address
            is_candidate: Whether sender qualifies for auto-rule
        """
        try:
            async with self._db() as db:
                await db.execute(
                    """
                    UPDATE sender_profiles
                    SET auto_rule_candidate = ?,
                        updated_at = ?
                    WHERE email = ?
                    """,
                    (1 if is_candidate else 0, datetime.now().isoformat(), email.lower()),
                )
                await db.commit()

        except aiosqlite.Error as e:
            logger.error("Failed to mark auto-rule candidate", email=email, error=str(e))
            raise DatabaseError(f"Failed to mark auto-rule candidate: {e}") from e

    async def get_auto_rule_candidates(self) -> list[SenderProfile]:
        """Get all senders that are auto-rule candidates.

        Returns:
            List of SenderProfile dataclasses
        """
        try:
            async with self._db() as db:
                cursor = await db.execute(
                    """
                    SELECT * FROM sender_profiles
                    WHERE auto_rule_candidate = 1
                    ORDER BY email_count DESC
                    """
                )
                rows = await cursor.fetchall()
                return [self._row_to_sender_profile(row) for row in rows]

        except aiosqlite.Error as e:
            logger.error("Failed to get auto-rule candidates", error=str(e))
            raise DatabaseError(f"Failed to get auto-rule candidates: {e}") from e

    def _row_to_sender_profile(self, row: aiosqlite.Row) -> SenderProfile:
        """Convert a database row to a SenderProfile dataclass."""
        return SenderProfile(
            email=row["email"],
            display_name=row["display_name"],
            domain=row["domain"],
            category=row["category"],
            default_folder=row["default_folder"],
            email_count=row["email_count"],
            last_seen=datetime.fromisoformat(row["last_seen"]) if row["last_seen"] else None,
            auto_rule_candidate=bool(row["auto_rule_candidate"]),
            updated_at=datetime.fromisoformat(row["updated_at"]) if row["updated_at"] else None,
        )

    # =========================================================================
    # LLM Request Log Operations
    # =========================================================================

    async def log_llm_request(
        self,
        task_type: str,
        model: str,
        prompt: dict[str, Any] | list[dict[str, Any]],
        response: dict[str, Any] | None = None,
        tool_call: dict[str, Any] | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        duration_ms: int | None = None,
        email_id: str | None = None,
        error: str | None = None,
    ) -> int:
        """Log an LLM request for debugging.

        Args:
            task_type: Type of task ('triage', 'bootstrap', 'digest', 'waiting_for')
            model: Model string used
            prompt: The prompt sent to Claude
            response: The response from Claude
            tool_call: Extracted tool call result
            input_tokens: Input token count
            output_tokens: Output token count
            duration_ms: Request duration in milliseconds
            email_id: Associated email ID (if applicable)
            error: Error message (if failed)

        Returns:
            The log entry ID
        """
        try:
            triage_cycle_id = get_correlation_id()

            async with self._db() as db:
                cursor = await db.execute(
                    """
                    INSERT INTO llm_request_log (
                        task_type, model, email_id, triage_cycle_id,
                        prompt_json, response_json, tool_call_json,
                        input_tokens, output_tokens, duration_ms, error
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        task_type,
                        model,
                        email_id,
                        triage_cycle_id,
                        json.dumps(prompt),
                        json.dumps(response) if response else None,
                        json.dumps(tool_call) if tool_call else None,
                        input_tokens,
                        output_tokens,
                        duration_ms,
                        error,
                    ),
                )
                await db.commit()
                return cursor.lastrowid

        except aiosqlite.Error as e:
            logger.error("Failed to log LLM request", task_type=task_type, error=str(e))
            raise DatabaseError(f"Failed to log LLM request: {e}") from e

    async def get_llm_logs(
        self,
        limit: int = 100,
        email_id: str | None = None,
        triage_cycle_id: str | None = None,
    ) -> list[LLMLogEntry]:
        """Get LLM request logs with optional filters.

        Args:
            limit: Maximum number of entries to return
            email_id: Filter by email ID
            triage_cycle_id: Filter by triage cycle ID

        Returns:
            List of LLMLogEntry dataclasses
        """
        try:
            async with self._db() as db:
                query = "SELECT * FROM llm_request_log WHERE 1=1"
                params: list[Any] = []

                if email_id:
                    query += " AND email_id = ?"
                    params.append(email_id)

                if triage_cycle_id:
                    query += " AND triage_cycle_id = ?"
                    params.append(triage_cycle_id)

                query += " ORDER BY timestamp DESC LIMIT ?"
                params.append(limit)

                cursor = await db.execute(query, params)
                rows = await cursor.fetchall()
                return [self._row_to_llm_log(row) for row in rows]

        except aiosqlite.Error as e:
            logger.error("Failed to get LLM logs", error=str(e))
            raise DatabaseError(f"Failed to get LLM logs: {e}") from e

    async def prune_llm_logs(self, retention_days: int) -> int:
        """Delete LLM logs older than the retention period.

        Args:
            retention_days: Number of days to retain logs

        Returns:
            Number of entries deleted
        """
        try:
            cutoff = datetime.now() - timedelta(days=retention_days)

            async with self._db() as db:
                cursor = await db.execute(
                    "DELETE FROM llm_request_log WHERE timestamp < ?",
                    (cutoff.isoformat(),),
                )
                await db.commit()

                deleted = cursor.rowcount
                if deleted:
                    logger.info(
                        "Pruned LLM logs",
                        deleted=deleted,
                        retention_days=retention_days,
                    )
                return deleted

        except aiosqlite.Error as e:
            logger.error("Failed to prune LLM logs", error=str(e))
            raise DatabaseError(f"Failed to prune LLM logs: {e}") from e

    def _row_to_llm_log(self, row: aiosqlite.Row) -> LLMLogEntry:
        """Convert a database row to an LLMLogEntry dataclass."""
        prompt_json = None
        if row["prompt_json"]:
            try:
                prompt_json = json.loads(row["prompt_json"])
            except json.JSONDecodeError:
                pass

        response_json = None
        if row["response_json"]:
            try:
                response_json = json.loads(row["response_json"])
            except json.JSONDecodeError:
                pass

        tool_call_json = None
        if row["tool_call_json"]:
            try:
                tool_call_json = json.loads(row["tool_call_json"])
            except json.JSONDecodeError:
                pass

        return LLMLogEntry(
            id=row["id"],
            timestamp=datetime.fromisoformat(row["timestamp"])
            if row["timestamp"]
            else datetime.now(),
            task_type=row["task_type"],
            model=row["model"],
            email_id=row["email_id"],
            triage_cycle_id=row["triage_cycle_id"],
            prompt_json=prompt_json,
            response_json=response_json,
            tool_call_json=tool_call_json,
            input_tokens=row["input_tokens"],
            output_tokens=row["output_tokens"],
            duration_ms=row["duration_ms"],
            error=row["error"],
        )

    # =========================================================================
    # Action Log Operations
    # =========================================================================

    async def log_action(
        self,
        action_type: str,
        email_id: str | None = None,
        details: dict[str, Any] | None = None,
        triggered_by: str = "auto",
    ) -> int:
        """Log an agent action for audit trail.

        Args:
            action_type: Type of action ('classify', 'move', 'categorize', 'suggest', 'bootstrap')
            email_id: Associated email ID (if applicable)
            details: Action details dictionary
            triggered_by: Who triggered the action ('auto', 'user_approved', 'bootstrap')

        Returns:
            The log entry ID
        """
        try:
            async with self._db() as db:
                cursor = await db.execute(
                    """
                    INSERT INTO action_log (
                        action_type, email_id, details_json, triggered_by
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        action_type,
                        email_id,
                        json.dumps(details) if details else None,
                        triggered_by,
                    ),
                )
                await db.commit()
                return cursor.lastrowid

        except aiosqlite.Error as e:
            logger.error("Failed to log action", action_type=action_type, error=str(e))
            raise DatabaseError(f"Failed to log action: {e}") from e

    async def get_action_logs(
        self,
        limit: int = 100,
        email_id: str | None = None,
        action_type: str | None = None,
    ) -> list[ActionLogEntry]:
        """Get action logs with optional filters.

        Args:
            limit: Maximum number of entries to return
            email_id: Filter by email ID
            action_type: Filter by action type

        Returns:
            List of ActionLogEntry dataclasses
        """
        try:
            async with self._db() as db:
                query = "SELECT * FROM action_log WHERE 1=1"
                params: list[Any] = []

                if email_id:
                    query += " AND email_id = ?"
                    params.append(email_id)

                if action_type:
                    query += " AND action_type = ?"
                    params.append(action_type)

                query += " ORDER BY timestamp DESC LIMIT ?"
                params.append(limit)

                cursor = await db.execute(query, params)
                rows = await cursor.fetchall()
                return [self._row_to_action_log(row) for row in rows]

        except aiosqlite.Error as e:
            logger.error("Failed to get action logs", error=str(e))
            raise DatabaseError(f"Failed to get action logs: {e}") from e

    def _row_to_action_log(self, row: aiosqlite.Row) -> ActionLogEntry:
        """Convert a database row to an ActionLogEntry dataclass."""
        details_json = None
        if row["details_json"]:
            try:
                details_json = json.loads(row["details_json"])
            except json.JSONDecodeError:
                pass

        return ActionLogEntry(
            id=row["id"],
            timestamp=datetime.fromisoformat(row["timestamp"])
            if row["timestamp"]
            else datetime.now(),
            action_type=row["action_type"],
            email_id=row["email_id"],
            details_json=details_json,
            triggered_by=row["triggered_by"],
        )

    # =========================================================================
    # Task Sync Operations (Phase 1.5)
    # =========================================================================

    async def create_task_sync(
        self,
        email_id: str,
        todo_task_id: str,
        todo_list_id: str,
        task_type: str,
    ) -> int:
        """Create a task sync record mapping a To Do task to an email.

        Args:
            email_id: Graph message ID (immutable)
            todo_task_id: Graph To Do task ID
            todo_list_id: Graph To Do list ID
            task_type: Task type ('waiting_for', 'needs_reply', 'review', 'delegated')

        Returns:
            The new task_sync ID
        """
        try:
            async with self._db() as db:
                cursor = await db.execute(
                    """
                    INSERT INTO task_sync (
                        email_id, todo_task_id, todo_list_id, task_type
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (email_id, todo_task_id, todo_list_id, task_type),
                )
                await db.commit()

                task_sync_id = cursor.lastrowid
                logger.debug(
                    "Task sync created",
                    task_sync_id=task_sync_id,
                    email_id=email_id,
                    todo_task_id=todo_task_id,
                )
                return task_sync_id

        except aiosqlite.Error as e:
            logger.error("Failed to create task sync", email_id=email_id, error=str(e))
            raise DatabaseError(f"Failed to create task sync: {e}") from e

    async def get_task_sync_by_email(self, email_id: str) -> TaskSync | None:
        """Get the task sync record for an email.

        Args:
            email_id: Graph message ID

        Returns:
            TaskSync or None if no task mapping exists
        """
        try:
            async with self._db() as db:
                cursor = await db.execute(
                    "SELECT * FROM task_sync WHERE email_id = ? ORDER BY created_at DESC LIMIT 1",
                    (email_id,),
                )
                row = await cursor.fetchone()
                if not row:
                    return None
                return self._row_to_task_sync(row)

        except aiosqlite.Error as e:
            logger.error("Failed to get task sync by email", email_id=email_id, error=str(e))
            raise DatabaseError(f"Failed to get task sync by email: {e}") from e

    async def get_task_sync_by_task(self, todo_task_id: str) -> TaskSync | None:
        """Get the task sync record for a To Do task.

        Args:
            todo_task_id: Graph To Do task ID

        Returns:
            TaskSync or None if no mapping exists
        """
        try:
            async with self._db() as db:
                cursor = await db.execute(
                    "SELECT * FROM task_sync WHERE todo_task_id = ?",
                    (todo_task_id,),
                )
                row = await cursor.fetchone()
                if not row:
                    return None
                return self._row_to_task_sync(row)

        except aiosqlite.Error as e:
            logger.error("Failed to get task sync by task", todo_task_id=todo_task_id, error=str(e))
            raise DatabaseError(f"Failed to get task sync by task: {e}") from e

    async def update_task_sync_status(
        self,
        task_sync_id: int,
        status: str,
        synced_at: datetime | None = None,
    ) -> None:
        """Update the status of a task sync record.

        Args:
            task_sync_id: The task_sync ID
            status: New status ('active', 'completed', 'deleted')
            synced_at: Optional timestamp of last sync
        """
        try:
            async with self._db() as db:
                await db.execute(
                    """
                    UPDATE task_sync
                    SET status = ?, synced_at = ?
                    WHERE id = ?
                    """,
                    (
                        status,
                        synced_at.isoformat() if synced_at else datetime.now().isoformat(),
                        task_sync_id,
                    ),
                )
                await db.commit()

        except aiosqlite.Error as e:
            logger.error(
                "Failed to update task sync status",
                task_sync_id=task_sync_id,
                error=str(e),
            )
            raise DatabaseError(f"Failed to update task sync status: {e}") from e

    async def get_active_task_syncs(self) -> list[TaskSync]:
        """Get all active task sync records (for Phase 2 sync cycle).

        Returns:
            List of active TaskSync records
        """
        try:
            async with self._db() as db:
                cursor = await db.execute(
                    """
                    SELECT * FROM task_sync
                    WHERE status = 'active'
                    ORDER BY created_at DESC
                    """
                )
                rows = await cursor.fetchall()
                return [self._row_to_task_sync(row) for row in rows]

        except aiosqlite.Error as e:
            logger.error("Failed to get active task syncs", error=str(e))
            raise DatabaseError(f"Failed to get active task syncs: {e}") from e

    def _row_to_task_sync(self, row: aiosqlite.Row) -> TaskSync:
        """Convert a database row to a TaskSync dataclass."""
        return TaskSync(
            id=row["id"],
            email_id=row["email_id"],
            todo_task_id=row["todo_task_id"],
            todo_list_id=row["todo_list_id"],
            task_type=row["task_type"],
            created_at=datetime.fromisoformat(row["created_at"])
            if row["created_at"]
            else datetime.now(),
            synced_at=datetime.fromisoformat(row["synced_at"]) if row["synced_at"] else None,
            status=row["status"],
        )

    # =========================================================================
    # Immutable ID Migration Operations (Phase 1.5)
    # =========================================================================

    async def get_all_email_ids(self) -> list[str]:
        """Get all email IDs from the emails table.

        Used during the one-time mutable-to-immutable ID migration.

        Returns:
            List of all email IDs
        """
        try:
            async with self._db() as db:
                cursor = await db.execute("SELECT id FROM emails")
                rows = await cursor.fetchall()
                return [row["id"] for row in rows]

        except aiosqlite.Error as e:
            logger.error("Failed to get all email IDs", error=str(e))
            raise DatabaseError(f"Failed to get all email IDs: {e}") from e

    async def update_email_id(self, old_id: str, new_id: str) -> None:
        """Update an email ID and all foreign key references.

        Used during the one-time mutable-to-immutable ID migration.
        Updates the emails table PK and all FK references in suggestions,
        waiting_for, action_log, llm_request_log, and task_sync.

        Args:
            old_id: Current (mutable) email ID
            new_id: New (immutable) email ID
        """
        try:
            async with self._db() as db:
                # Disable FK enforcement for the atomic ID swap.
                # PRAGMA foreign_keys must run outside a transaction,
                # so commit any pending work first.
                await db.commit()
                await db.execute("PRAGMA foreign_keys = OFF")

                # Update primary key first, then all FK references
                await db.execute(
                    "UPDATE emails SET id = ? WHERE id = ?",
                    (new_id, old_id),
                )
                await db.execute(
                    "UPDATE suggestions SET email_id = ? WHERE email_id = ?",
                    (new_id, old_id),
                )
                await db.execute(
                    "UPDATE waiting_for SET email_id = ? WHERE email_id = ?",
                    (new_id, old_id),
                )
                await db.execute(
                    "UPDATE action_log SET email_id = ? WHERE email_id = ?",
                    (new_id, old_id),
                )
                await db.execute(
                    "UPDATE llm_request_log SET email_id = ? WHERE email_id = ?",
                    (new_id, old_id),
                )
                await db.execute(
                    "UPDATE task_sync SET email_id = ? WHERE email_id = ?",
                    (new_id, old_id),
                )
                await db.commit()

                # Re-enable FK enforcement
                await db.execute("PRAGMA foreign_keys = ON")

                logger.debug(
                    "Email ID updated",
                    old_id=old_id[:20] + "...",
                    new_id=new_id[:20] + "...",
                )

        except aiosqlite.Error as e:
            logger.error(
                "Failed to update email ID",
                old_id=old_id[:20] + "...",
                error=str(e),
            )
            raise DatabaseError(f"Failed to update email ID: {e}") from e

    # =========================================================================
    # Dashboard/Stats Operations
    # =========================================================================

    async def get_stats(self) -> dict[str, Any]:
        """Get dashboard statistics.

        Returns:
            Dictionary with various counts and stats
        """
        try:
            async with self._db() as db:
                stats = {}

                # Email counts by status
                cursor = await db.execute(
                    """
                    SELECT classification_status, COUNT(*) as count
                    FROM emails
                    GROUP BY classification_status
                    """
                )
                stats["emails_by_status"] = {
                    row["classification_status"]: row["count"] for row in await cursor.fetchall()
                }

                # Pending suggestions count
                cursor = await db.execute(
                    "SELECT COUNT(*) as count FROM suggestions WHERE status = 'pending'"
                )
                row = await cursor.fetchone()
                stats["pending_suggestions"] = row["count"] if row else 0

                # Active waiting-for count
                cursor = await db.execute(
                    "SELECT COUNT(*) as count FROM waiting_for WHERE status = 'waiting'"
                )
                row = await cursor.fetchone()
                stats["active_waiting_for"] = row["count"] if row else 0

                # Total senders tracked
                cursor = await db.execute("SELECT COUNT(*) as count FROM sender_profiles")
                row = await cursor.fetchone()
                stats["total_senders"] = row["count"] if row else 0

                # Auto-rule candidates
                cursor = await db.execute(
                    "SELECT COUNT(*) as count FROM sender_profiles WHERE auto_rule_candidate = 1"
                )
                row = await cursor.fetchone()
                stats["auto_rule_candidates"] = row["count"] if row else 0

                # Recent actions (last 24 hours)
                yesterday = (datetime.now() - timedelta(days=1)).isoformat()
                cursor = await db.execute(
                    "SELECT COUNT(*) as count FROM action_log WHERE timestamp > ?",
                    (yesterday,),
                )
                row = await cursor.fetchone()
                stats["actions_last_24h"] = row["count"] if row else 0

                return stats

        except aiosqlite.Error as e:
            logger.error("Failed to get stats", error=str(e))
            raise DatabaseError(f"Failed to get stats: {e}") from e

    async def get_overdue_replies(
        self,
        warning_hours: int = 24,
        critical_hours: int = 48,
    ) -> list[dict[str, Any]]:
        """Get emails with action_type 'Needs Reply' past warning threshold.

        Args:
            warning_hours: Hours before warning level
            critical_hours: Hours before critical level

        Returns:
            List of overdue email dicts with level ('warning' or 'critical')
        """
        try:
            async with self._db() as db:
                warning_cutoff = (datetime.now() - timedelta(hours=warning_hours)).isoformat()
                cursor = await db.execute(
                    """
                    SELECT e.id, e.subject, e.sender_email, e.sender_name,
                           e.received_at, s.suggested_action_type, s.approved_action_type
                    FROM emails e
                    JOIN suggestions s ON s.email_id = e.id
                    WHERE (s.approved_action_type = 'Needs Reply'
                           OR (s.approved_action_type IS NULL
                               AND s.suggested_action_type = 'Needs Reply'))
                    AND s.status IN ('approved', 'partial')
                    AND e.received_at < ?
                    ORDER BY e.received_at ASC
                    """,
                    (warning_cutoff,),
                )
                rows = await cursor.fetchall()

                results = []
                for row in rows:
                    received = row["received_at"]
                    if received:
                        age_hours = (
                            datetime.now() - datetime.fromisoformat(received)
                        ).total_seconds() / 3600
                        level = "critical" if age_hours >= critical_hours else "warning"
                    else:
                        level = "warning"

                    results.append(
                        {
                            "id": row["id"],
                            "subject": row["subject"],
                            "sender_email": row["sender_email"],
                            "sender_name": row["sender_name"],
                            "received_at": received,
                            "level": level,
                        }
                    )

                return results

        except aiosqlite.Error as e:
            logger.error("Failed to get overdue replies", error=str(e))
            raise DatabaseError(f"Failed to get overdue replies: {e}") from e

    async def get_processing_stats(self, since: datetime) -> dict[str, Any]:
        """Get processing statistics since a given time.

        Args:
            since: Start time for stats window

        Returns:
            Dict with counts for classifications, auto-rules, failures
        """
        try:
            async with self._db() as db:
                # Use space separator to match SQLite's CURRENT_TIMESTAMP format
                since_str = since.strftime("%Y-%m-%d %H:%M:%S")

                # Count by action type from action_log
                cursor = await db.execute(
                    """
                    SELECT action_type, triggered_by, COUNT(*) as count
                    FROM action_log
                    WHERE timestamp > ?
                    GROUP BY action_type, triggered_by
                    """,
                    (since_str,),
                )
                rows = await cursor.fetchall()

                stats: dict[str, int] = {
                    "classified": 0,
                    "auto_ruled": 0,
                    "auto_approved": 0,
                    "user_approved": 0,
                    "rejected": 0,
                    "failed": 0,
                }
                for row in rows:
                    action = row["action_type"]
                    triggered = row["triggered_by"]
                    count = row["count"]

                    if action == "classify" and triggered == "auto":
                        stats["auto_ruled"] += count
                    elif action == "classify":
                        stats["classified"] += count
                    elif action == "move" and triggered == "auto_approved":
                        stats["auto_approved"] += count
                    elif action == "move" and triggered == "user_approved":
                        stats["user_approved"] += count
                    elif action == "reject":
                        stats["rejected"] += count

                # Count failed classifications
                cursor = await db.execute(
                    """
                    SELECT COUNT(*) as count FROM emails
                    WHERE classification_status = 'failed'
                    AND processed_at > ?
                    """,
                    (since_str,),
                )
                row = await cursor.fetchone()
                stats["failed"] = row["count"] if row else 0

                return stats

        except aiosqlite.Error as e:
            logger.error("Failed to get processing stats", error=str(e))
            raise DatabaseError(f"Failed to get processing stats: {e}") from e

    async def get_approval_stats(self, days: int = 30) -> dict[str, Any]:
        """Get approval/correction rates overall and per-folder.

        Args:
            days: Lookback window in days

        Returns:
            Dict with overall and per-folder approval stats
        """
        try:
            async with self._db() as db:
                cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")

                # Overall approval stats
                cursor = await db.execute(
                    """
                    SELECT status, COUNT(*) as count
                    FROM suggestions
                    WHERE resolved_at > ? OR (status = 'pending' AND created_at > ?)
                    GROUP BY status
                    """,
                    (cutoff, cutoff),
                )
                rows = await cursor.fetchall()
                overall = {row["status"]: row["count"] for row in rows}

                # Per-folder correction rates
                cursor = await db.execute(
                    """
                    SELECT suggested_folder,
                           COUNT(*) as total,
                           SUM(CASE WHEN status = 'approved' THEN 1 ELSE 0 END) as approved,
                           SUM(CASE WHEN status = 'partial' THEN 1 ELSE 0 END) as corrected
                    FROM suggestions
                    WHERE resolved_at > ?
                    GROUP BY suggested_folder
                    ORDER BY total DESC
                    LIMIT 20
                    """,
                    (cutoff,),
                )
                per_folder = [
                    {
                        "folder": row["suggested_folder"],
                        "total": row["total"],
                        "approved": row["approved"],
                        "corrected": row["corrected"],
                        "approval_rate": row["approved"] / row["total"] if row["total"] > 0 else 0,
                    }
                    for row in await cursor.fetchall()
                ]

                return {"overall": overall, "per_folder": per_folder}

        except aiosqlite.Error as e:
            logger.error("Failed to get approval stats", error=str(e))
            raise DatabaseError(f"Failed to get approval stats: {e}") from e

    async def get_correction_heatmap(self, days: int = 30) -> list[dict[str, Any]]:
        """Get most common suggested -> approved transitions.

        Args:
            days: Lookback window in days

        Returns:
            List of correction transition dicts
        """
        try:
            async with self._db() as db:
                cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
                cursor = await db.execute(
                    """
                    SELECT suggested_folder, approved_folder, COUNT(*) as count
                    FROM suggestions
                    WHERE status = 'partial'
                    AND resolved_at > ?
                    AND suggested_folder != approved_folder
                    GROUP BY suggested_folder, approved_folder
                    ORDER BY count DESC
                    LIMIT 20
                    """,
                    (cutoff,),
                )
                return [
                    {
                        "from_folder": row["suggested_folder"],
                        "to_folder": row["approved_folder"],
                        "count": row["count"],
                    }
                    for row in await cursor.fetchall()
                ]

        except aiosqlite.Error as e:
            logger.error("Failed to get correction heatmap", error=str(e))
            raise DatabaseError(f"Failed to get correction heatmap: {e}") from e

    async def get_confidence_calibration(self, days: int = 30) -> list[dict[str, Any]]:
        """Get confidence calibration data (predicted vs actual by bucket).

        Buckets: 0.5-0.6, 0.6-0.7, 0.7-0.8, 0.8-0.9, 0.9-1.0

        Args:
            days: Lookback window in days

        Returns:
            List of calibration bucket dicts
        """
        try:
            async with self._db() as db:
                cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
                cursor = await db.execute(
                    """
                    SELECT confidence, status
                    FROM suggestions
                    WHERE resolved_at > ?
                    AND confidence IS NOT NULL
                    AND status IN ('approved', 'partial', 'rejected')
                    """,
                    (cutoff,),
                )
                rows = await cursor.fetchall()

                # Build buckets
                buckets = {
                    "0.5-0.6": {"count": 0, "approved": 0},
                    "0.6-0.7": {"count": 0, "approved": 0},
                    "0.7-0.8": {"count": 0, "approved": 0},
                    "0.8-0.9": {"count": 0, "approved": 0},
                    "0.9-1.0": {"count": 0, "approved": 0},
                }

                for row in rows:
                    conf = row["confidence"]
                    if conf < 0.5:
                        continue
                    elif conf < 0.6:
                        bucket = "0.5-0.6"
                    elif conf < 0.7:
                        bucket = "0.6-0.7"
                    elif conf < 0.8:
                        bucket = "0.7-0.8"
                    elif conf < 0.9:
                        bucket = "0.8-0.9"
                    else:
                        bucket = "0.9-1.0"

                    buckets[bucket]["count"] += 1
                    if row["status"] == "approved":
                        buckets[bucket]["approved"] += 1

                result = []
                for bucket_name, data in buckets.items():
                    approval_rate = data["approved"] / data["count"] if data["count"] > 0 else None
                    result.append(
                        {
                            "bucket": bucket_name,
                            "count": data["count"],
                            "approved": data["approved"],
                            "approval_rate": approval_rate,
                        }
                    )

                return result

        except aiosqlite.Error as e:
            logger.error("Failed to get confidence calibration", error=str(e))
            raise DatabaseError(f"Failed to get confidence calibration: {e}") from e

    async def get_cost_tracking(self, days: int = 30) -> dict[str, Any]:
        """Get token usage from llm_request_log.

        Args:
            days: Lookback window in days

        Returns:
            Dict with token usage stats
        """
        try:
            async with self._db() as db:
                cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
                cursor = await db.execute(
                    """
                    SELECT
                        COUNT(*) as total_requests,
                        COALESCE(SUM(input_tokens), 0) as total_input_tokens,
                        COALESCE(SUM(output_tokens), 0) as total_output_tokens,
                        COALESCE(AVG(duration_ms), 0) as avg_duration_ms,
                        SUM(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END) as errors
                    FROM llm_request_log
                    WHERE timestamp > ?
                    """,
                    (cutoff,),
                )
                row = await cursor.fetchone()

                return {
                    "total_requests": row["total_requests"] if row else 0,
                    "total_input_tokens": row["total_input_tokens"] if row else 0,
                    "total_output_tokens": row["total_output_tokens"] if row else 0,
                    "avg_duration_ms": int(row["avg_duration_ms"]) if row else 0,
                    "errors": row["errors"] if row else 0,
                }

        except aiosqlite.Error as e:
            logger.error("Failed to get cost tracking", error=str(e))
            raise DatabaseError(f"Failed to get cost tracking: {e}") from e

    async def list_sender_profiles(
        self,
        category: str | None = None,
        sort_by: str = "email_count",
        sort_order: str = "desc",
        limit: int = 50,
        offset: int = 0,
    ) -> list[SenderProfile]:
        """List sender profiles with optional filtering and sorting.

        Args:
            category: Filter by category (None for all)
            sort_by: Column to sort by
            sort_order: 'asc' or 'desc'
            limit: Max results
            offset: Skip first N results

        Returns:
            List of SenderProfile dataclasses
        """
        try:
            async with self._db() as db:
                # Whitelist sort columns to prevent injection
                allowed_sort = {
                    "email",
                    "display_name",
                    "domain",
                    "category",
                    "email_count",
                    "last_seen",
                    "auto_rule_candidate",
                }
                if sort_by not in allowed_sort:
                    sort_by = "email_count"
                if sort_order not in ("asc", "desc"):
                    sort_order = "desc"

                query = "SELECT * FROM sender_profiles"
                params: list[Any] = []

                if category:
                    query += " WHERE category = ?"
                    params.append(category)

                query += f" ORDER BY {sort_by} {sort_order} LIMIT ? OFFSET ?"
                params.extend([limit, offset])

                cursor = await db.execute(query, params)
                rows = await cursor.fetchall()

                return [self._row_to_sender_profile(row) for row in rows]

        except aiosqlite.Error as e:
            logger.error("Failed to list sender profiles", error=str(e))
            raise DatabaseError(f"Failed to list sender profiles: {e}") from e

    async def update_sender_category(self, email: str, category: SenderCategory) -> None:
        """Update a sender's category.

        Args:
            email: Sender email address
            category: New category value
        """
        try:
            async with self._db() as db:
                await db.execute(
                    """
                    UPDATE sender_profiles
                    SET category = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE email = ?
                    """,
                    (category, email),
                )
                await db.commit()

        except aiosqlite.Error as e:
            logger.error("Failed to update sender category", error=str(e))
            raise DatabaseError(f"Failed to update sender category: {e}") from e

    # =========================================================================
    # Database Maintenance Operations
    # =========================================================================

    async def vacuum(self) -> None:
        """Reclaim deleted space and defragment database.

        Should be run periodically after pruning operations (e.g., prune_llm_logs).
        Note: VACUUM requires exclusive access and may take time on large databases.
        """
        try:
            async with self._db() as db:
                await db.execute("VACUUM")
            logger.info("Database vacuumed", db_path=str(self.db_path))
        except aiosqlite.Error as e:
            logger.error("Failed to vacuum database", error=str(e))
            raise DatabaseError(f"Failed to vacuum database: {e}") from e

    async def analyze(self) -> None:
        """Update query planner statistics.

        Should be run after bulk inserts (e.g., bootstrap) or significant data changes
        to ensure optimal query plans.
        """
        try:
            async with self._db() as db:
                await db.execute("ANALYZE")
            logger.info("Database analyzed", db_path=str(self.db_path))
        except aiosqlite.Error as e:
            logger.error("Failed to analyze database", error=str(e))
            raise DatabaseError(f"Failed to analyze database: {e}") from e

    # =========================================================================
    # Dry-Run Support Operations
    # =========================================================================

    async def get_emails_by_date_range(
        self,
        days: int,
        limit: int = 10000,
        status: str | None = None,
    ) -> list[Email]:
        """Get emails from the last N days, optionally filtered by classification status.

        Used by dry-run to load previously bootstrapped emails, and by backlog
        processing to find pending emails accumulated during degraded mode.

        Args:
            days: Number of days to look back
            limit: Maximum number of emails to return
            status: Optional classification_status filter (e.g., 'pending')

        Returns:
            List of Email dataclasses ordered by received_at ASC (FIFO for backlog)
        """
        try:
            cutoff = datetime.now() - timedelta(days=days)

            if status:
                query = """
                    SELECT * FROM emails
                    WHERE received_at >= ?
                    AND classification_status = ?
                    ORDER BY received_at ASC
                    LIMIT ?
                """
                params: tuple[Any, ...] = (cutoff.isoformat(), status, limit)
            else:
                query = """
                    SELECT * FROM emails
                    WHERE received_at >= ?
                    ORDER BY received_at DESC
                    LIMIT ?
                """
                params = (cutoff.isoformat(), limit)

            async with self._db() as db:
                cursor = await db.execute(query, params)
                rows = await cursor.fetchall()
                return [self._row_to_email(row) for row in rows]

        except aiosqlite.Error as e:
            logger.error("Failed to get emails by date range", days=days, error=str(e))
            raise DatabaseError(f"Failed to get emails by date range: {e}") from e

    async def get_resolved_suggestions(self) -> list[Suggestion]:
        """Get all resolved suggestions (approved or partial).

        Used by dry-run confusion matrix to compare suggested vs approved values.

        Returns:
            List of Suggestion dataclasses with resolved_at set
        """
        try:
            async with self._db() as db:
                cursor = await db.execute(
                    """
                    SELECT * FROM suggestions
                    WHERE status IN ('approved', 'partial')
                    AND resolved_at IS NOT NULL
                    ORDER BY resolved_at DESC
                    """
                )
                rows = await cursor.fetchall()
                return [self._row_to_suggestion(row) for row in rows]

        except aiosqlite.Error as e:
            logger.error("Failed to get resolved suggestions", error=str(e))
            raise DatabaseError(f"Failed to get resolved suggestions: {e}") from e
