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

SuggestionStatus = Literal["pending", "approved", "rejected", "partial"]
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
                    SET status = 'rejected',
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
    ) -> None:
        """Resolve a waiting-for item.

        Args:
            waiting_for_id: The waiting-for ID
            status: Resolution status ('received' or 'expired')
        """
        try:
            async with self._db() as db:
                await db.execute(
                    """
                    UPDATE waiting_for
                    SET status = ?,
                        resolved_at = ?
                    WHERE id = ?
                    """,
                    (status, datetime.now().isoformat(), waiting_for_id),
                )
                await db.commit()

        except aiosqlite.Error as e:
            logger.error(
                "Failed to resolve waiting-for", waiting_for_id=waiting_for_id, error=str(e)
            )
            raise DatabaseError(f"Failed to resolve waiting-for: {e}") from e

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

    async def get_sender_history(self, sender_email: str) -> SenderHistory:
        """Get sender history with folder distribution.

        Args:
            sender_email: Sender email address

        Returns:
            SenderHistory with folder distribution
        """
        try:
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
                    GROUP BY s.approved_folder
                    """,
                    (sender_email.lower(),),
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
