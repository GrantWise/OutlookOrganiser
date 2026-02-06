"""SQLite database schema and initialization for the Outlook AI Assistant.

This module defines the database schema with all 7 tables:
- emails: Processed email metadata, classification status
- suggestions: Compound suggestions (folder + priority + action)
- waiting_for: Tracked waiting items
- agent_state: Key-value state persistence
- sender_profiles: Sender categorization for faster routing
- llm_request_log: Claude API call logging for debugging
- action_log: Audit trail of all agent actions

Usage:
    from assistant.db.models import init_database

    # Initialize database (creates tables if not exist)
    await init_database("data/assistant.db")
"""

import stat
from pathlib import Path

import aiosqlite

from assistant.core.errors import DatabaseError
from assistant.core.logging import get_logger

logger = get_logger(__name__)

# Schema version for migrations (increment when schema changes)
SCHEMA_VERSION = 1

# SQL schema definition
SCHEMA_SQL = """
-- MUST be set before creating tables. Persists across connections.
PRAGMA journal_mode=WAL;

-- Track every email the agent has processed
CREATE TABLE IF NOT EXISTS emails (
    id TEXT PRIMARY KEY,                    -- Graph API message ID
    conversation_id TEXT,                   -- Graph API conversation thread ID
    conversation_index TEXT,                -- Graph API conversationIndex (base64, encodes thread depth)
    subject TEXT,
    sender_email TEXT,
    sender_name TEXT,
    received_at DATETIME,
    snippet TEXT,                           -- First 1000 chars of cleaned body
    current_folder TEXT,                    -- Current Outlook folder path
    web_link TEXT,                          -- OWA deep link URL from Graph API webLink field
    importance TEXT DEFAULT 'normal',       -- Sender-set importance: 'low', 'normal', 'high'
    is_read INTEGER DEFAULT 0,              -- 1 if user has read the email
    flag_status TEXT DEFAULT 'notFlagged',  -- 'notFlagged', 'flagged', 'complete'
    has_user_reply INTEGER DEFAULT 0,       -- 1 if user has replied in this thread
    inherited_folder TEXT,                  -- If set, folder was inherited from prior thread classification
    processed_at DATETIME,
    classification_json TEXT,               -- Full Claude classification result
    classification_attempts INTEGER DEFAULT 0,  -- Retry counter for failed classifications
    classification_status TEXT DEFAULT 'pending' -- 'pending', 'classified', 'failed'
);

-- Index for efficient thread inheritance lookups
CREATE INDEX IF NOT EXISTS idx_emails_conversation_id ON emails(conversation_id);

-- Index for efficient sender history lookups
CREATE INDEX IF NOT EXISTS idx_emails_sender ON emails(sender_email);

-- Index for listing emails by received date
CREATE INDEX IF NOT EXISTS idx_emails_received_at ON emails(received_at);

-- Index for finding emails by classification status
CREATE INDEX IF NOT EXISTS idx_emails_classification_status ON emails(classification_status);

-- Composite index for thread inheritance (conversation + received date for ORDER BY)
CREATE INDEX IF NOT EXISTS idx_emails_thread_inheritance
    ON emails(conversation_id, received_at DESC);

-- Track compound suggestions (one row per classification)
CREATE TABLE IF NOT EXISTS suggestions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email_id TEXT REFERENCES emails(id),
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,

    -- Suggested classification (compound — all fields from one classification event)
    suggested_folder TEXT,                  -- e.g., 'Projects/Tradecore Steel'
    suggested_priority TEXT,                -- e.g., 'P2 - Important'
    suggested_action_type TEXT,             -- e.g., 'Needs Reply'
    confidence REAL,                        -- 0.0-1.0 from Claude
    reasoning TEXT,                         -- Claude's one-sentence explanation

    -- User decision (per-field approval/correction)
    status TEXT DEFAULT 'pending',          -- 'pending', 'approved', 'rejected', 'partial'
    approved_folder TEXT,                   -- NULL = pending, or user's chosen folder
    approved_priority TEXT,                 -- NULL = pending, or user's chosen priority
    approved_action_type TEXT,              -- NULL = pending, or user's chosen action type
    resolved_at DATETIME
);

-- Index for finding suggestions by email
CREATE INDEX IF NOT EXISTS idx_suggestions_email_id ON suggestions(email_id);

-- Index for finding pending suggestions
CREATE INDEX IF NOT EXISTS idx_suggestions_status ON suggestions(status);

-- Composite index for suggestion lookups by email + status
CREATE INDEX IF NOT EXISTS idx_suggestions_email_status
    ON suggestions(email_id, status);

-- Track "Waiting For" threads
CREATE TABLE IF NOT EXISTS waiting_for (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email_id TEXT REFERENCES emails(id),
    conversation_id TEXT,                   -- For monitoring thread replies
    waiting_since DATETIME,
    expected_from TEXT,                     -- Email address we're waiting on
    description TEXT,                       -- What we're waiting for
    status TEXT DEFAULT 'waiting',          -- 'waiting', 'received', 'expired'
    nudge_after_hours INTEGER DEFAULT 48,
    resolved_at DATETIME
);

-- Index for finding active waiting items
CREATE INDEX IF NOT EXISTS idx_waiting_for_status ON waiting_for(status);

-- Index for conversation monitoring
CREATE INDEX IF NOT EXISTS idx_waiting_for_conversation ON waiting_for(conversation_id);

-- Agent state persistence (cursors, tokens, counters)
CREATE TABLE IF NOT EXISTS agent_state (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
-- Keys: 'last_processed_timestamp', 'delta_token', 'last_bootstrap_run',
--        'last_digest_run', 'authenticated_user_email',
--        'classification_preferences', 'config_schema_version'

-- Persistent sender profiles for faster classification routing
CREATE TABLE IF NOT EXISTS sender_profiles (
    email TEXT PRIMARY KEY,
    display_name TEXT,
    domain TEXT,
    category TEXT DEFAULT 'unknown',        -- 'key_contact', 'newsletter', 'automated',
                                            -- 'internal', 'client', 'vendor', 'unknown'
    default_folder TEXT,                    -- Most common approved folder for this sender
    email_count INTEGER DEFAULT 0,          -- Total emails processed from this sender
    last_seen DATETIME,
    auto_rule_candidate INTEGER DEFAULT 0,  -- 1 if >90% to single folder with 10+ emails
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_sender_profiles_domain ON sender_profiles(domain);
CREATE INDEX IF NOT EXISTS idx_sender_profiles_category ON sender_profiles(category);

-- LLM request/response log for debugging classification issues
CREATE TABLE IF NOT EXISTS llm_request_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
    task_type TEXT,                         -- 'triage', 'bootstrap', 'digest', 'waiting_for'
    model TEXT,                             -- Model string used (e.g., 'claude-haiku-4-5-20251001')
    email_id TEXT,                          -- NULL for non-email tasks (digest, bootstrap)
    triage_cycle_id TEXT,                   -- Correlation ID for the triage cycle
    prompt_json TEXT,                       -- Full prompt sent to Claude (messages array)
    response_json TEXT,                     -- Full response from Claude
    tool_call_json TEXT,                    -- Extracted tool call result (if applicable)
    input_tokens INTEGER,
    output_tokens INTEGER,
    duration_ms INTEGER,
    error TEXT                              -- NULL on success, error message on failure
);

CREATE INDEX IF NOT EXISTS idx_llm_log_timestamp ON llm_request_log(timestamp);
CREATE INDEX IF NOT EXISTS idx_llm_log_email ON llm_request_log(email_id);
CREATE INDEX IF NOT EXISTS idx_llm_log_triage_cycle ON llm_request_log(triage_cycle_id);

-- Audit log of all agent actions
CREATE TABLE IF NOT EXISTS action_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
    action_type TEXT,                       -- 'classify', 'move', 'categorize', 'suggest', 'bootstrap'
    email_id TEXT,
    details_json TEXT,                      -- Full action details
    triggered_by TEXT                       -- 'auto', 'user_approved', 'bootstrap'
);

CREATE INDEX IF NOT EXISTS idx_action_log_timestamp ON action_log(timestamp);
CREATE INDEX IF NOT EXISTS idx_action_log_email ON action_log(email_id);
"""


async def init_database(db_path: str | Path) -> None:
    """Initialize the SQLite database with schema and WAL mode.

    Creates the database file if it doesn't exist, enables WAL mode for
    concurrent access, and creates all tables and indexes.

    Args:
        db_path: Path to the SQLite database file

    Raises:
        DatabaseError: If database initialization fails
    """
    db_path = Path(db_path)

    # Ensure parent directory exists
    db_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        async with aiosqlite.connect(db_path) as db:
            # Enable WAL mode for concurrent access
            await db.execute("PRAGMA journal_mode=WAL")
            journal_mode = await db.execute("PRAGMA journal_mode")
            mode = await journal_mode.fetchone()
            if mode and mode[0].lower() != "wal":
                logger.warning(
                    "WAL mode not enabled",
                    requested="wal",
                    actual=mode[0],
                    db_path=str(db_path),
                )

            # Execute schema SQL using executescript for proper multi-statement handling
            await db.executescript(SCHEMA_SQL)

            await db.commit()

            # Count tables for logging
            cursor = await db.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'")
            table_count = (await cursor.fetchone())[0]

        # Set restrictive permissions on database file (0600 = owner read/write only)
        # This protects PII stored in the database from other users
        db_path.chmod(stat.S_IRUSR | stat.S_IWUSR)

        # Also set permissions on WAL files if they exist
        for suffix in ["-wal", "-shm"]:
            wal_file = db_path.with_suffix(db_path.suffix + suffix)
            if wal_file.exists():
                wal_file.chmod(stat.S_IRUSR | stat.S_IWUSR)

        logger.info(
            "Database initialized",
            db_path=str(db_path),
            schema_version=SCHEMA_VERSION,
            tables_created=table_count,
        )

    except aiosqlite.Error as e:
        logger.error(
            "Database initialization failed",
            db_path=str(db_path),
            error=str(e),
        )
        raise DatabaseError(
            f"Failed to initialize database at {db_path}: {e}. "
            "Check that the directory is writable and the database file is not corrupted."
        ) from e


async def get_connection(db_path: str | Path) -> aiosqlite.Connection:
    """Get a database connection with row factory enabled.

    The returned connection uses aiosqlite.Row as the row factory,
    allowing column access by name (row["column_name"]).

    Args:
        db_path: Path to the SQLite database file

    Returns:
        An aiosqlite connection with row factory enabled

    Note:
        The caller is responsible for closing the connection.
        Use `async with` for automatic cleanup.
    """
    db = await aiosqlite.connect(db_path)
    db.row_factory = aiosqlite.Row
    return db


async def verify_schema(db_path: str | Path) -> bool:
    """Verify that the database has the expected schema.

    Checks that all required tables exist.

    Args:
        db_path: Path to the SQLite database file

    Returns:
        True if all tables exist, False otherwise
    """
    required_tables = [
        "emails",
        "suggestions",
        "waiting_for",
        "agent_state",
        "sender_profiles",
        "llm_request_log",
        "action_log",
    ]

    try:
        async with aiosqlite.connect(db_path) as db:
            cursor = await db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            existing_tables = {row[0] for row in await cursor.fetchall()}

            missing = set(required_tables) - existing_tables
            if missing:
                logger.warning(
                    "Missing database tables",
                    missing=list(missing),
                    db_path=str(db_path),
                )
                return False

            return True

    except aiosqlite.Error as e:
        logger.error(
            "Schema verification failed",
            db_path=str(db_path),
            error=str(e),
        )
        return False
