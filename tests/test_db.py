"""Tests for the database layer.

Tests all CRUD operations for the 7 database tables:
- emails
- suggestions
- waiting_for
- agent_state
- sender_profiles
- llm_request_log
- action_log
"""

from datetime import datetime, timedelta
from pathlib import Path

import aiosqlite
import pytest

from assistant.db import (
    DatabaseStore,
    Email,
    init_database,
    verify_schema,
)


@pytest.fixture
async def db_path(data_dir: Path) -> Path:
    """Create a test database path."""
    return data_dir / "test.db"


@pytest.fixture
async def store(db_path: Path) -> DatabaseStore:
    """Create and initialize a DatabaseStore."""
    store = DatabaseStore(db_path)
    await store.initialize()
    return store


class TestDatabaseInitialization:
    """Tests for database initialization."""

    @pytest.mark.asyncio
    async def test_init_database_creates_file(self, db_path: Path) -> None:
        """Test that init_database creates the database file."""
        assert not db_path.exists()
        await init_database(db_path)
        assert db_path.exists()

    @pytest.mark.asyncio
    async def test_init_database_enables_wal_mode(self, db_path: Path) -> None:
        """Test that WAL mode is enabled."""
        await init_database(db_path)

        async with aiosqlite.connect(db_path) as db:
            cursor = await db.execute("PRAGMA journal_mode")
            row = await cursor.fetchone()
            assert row[0].lower() == "wal"

    @pytest.mark.asyncio
    async def test_init_database_creates_all_tables(self, db_path: Path) -> None:
        """Test that all 8 tables are created."""
        await init_database(db_path)

        async with aiosqlite.connect(db_path) as db:
            cursor = await db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = {row[0] for row in await cursor.fetchall()}

        expected_tables = {
            "emails",
            "suggestions",
            "waiting_for",
            "agent_state",
            "sender_profiles",
            "llm_request_log",
            "action_log",
            "task_sync",
        }
        assert expected_tables.issubset(tables)

    @pytest.mark.asyncio
    async def test_verify_schema_returns_true_for_valid_db(self, db_path: Path) -> None:
        """Test verify_schema with valid database."""
        await init_database(db_path)
        assert await verify_schema(db_path)

    @pytest.mark.asyncio
    async def test_verify_schema_returns_false_for_empty_db(self, db_path: Path) -> None:
        """Test verify_schema with empty database."""
        async with aiosqlite.connect(db_path) as db:
            await db.execute("CREATE TABLE dummy (id INTEGER)")
            await db.commit()

        assert not await verify_schema(db_path)


class TestEmailOperations:
    """Tests for email CRUD operations."""

    @pytest.mark.asyncio
    async def test_save_and_get_email(self, store: DatabaseStore) -> None:
        """Test saving and retrieving an email."""
        email = Email(
            id="test-email-123",
            conversation_id="conv-456",
            subject="Test Subject",
            sender_email="sender@example.com",
            sender_name="Test Sender",
            received_at=datetime.now(),
            snippet="This is a test email body.",
            current_folder="Inbox",
            classification_status="pending",
        )

        await store.save_email(email)
        retrieved = await store.get_email("test-email-123")

        assert retrieved is not None
        assert retrieved.id == email.id
        assert retrieved.subject == email.subject
        assert retrieved.sender_email == email.sender_email
        assert retrieved.snippet == email.snippet
        assert retrieved.classification_status == "pending"

    @pytest.mark.asyncio
    async def test_get_nonexistent_email_returns_none(self, store: DatabaseStore) -> None:
        """Test that getting a nonexistent email returns None."""
        result = await store.get_email("nonexistent-id")
        assert result is None

    @pytest.mark.asyncio
    async def test_email_exists(self, store: DatabaseStore) -> None:
        """Test email_exists method."""
        email = Email(id="exists-test", subject="Test")
        await store.save_email(email)

        assert await store.email_exists("exists-test")
        assert not await store.email_exists("does-not-exist")

    @pytest.mark.asyncio
    async def test_save_email_upsert(self, store: DatabaseStore) -> None:
        """Test that saving an existing email updates it."""
        email = Email(id="upsert-test", subject="Original Subject")
        await store.save_email(email)

        email.subject = "Updated Subject"
        await store.save_email(email)

        retrieved = await store.get_email("upsert-test")
        assert retrieved is not None
        assert retrieved.subject == "Updated Subject"

    @pytest.mark.asyncio
    async def test_get_emails_by_status(self, store: DatabaseStore) -> None:
        """Test filtering emails by classification status."""
        for i in range(3):
            await store.save_email(Email(id=f"pending-{i}", classification_status="pending"))
        for i in range(2):
            await store.save_email(Email(id=f"classified-{i}", classification_status="classified"))

        pending = await store.get_emails_by_status("pending")
        classified = await store.get_emails_by_status("classified")

        assert len(pending) == 3
        assert len(classified) == 2

    @pytest.mark.asyncio
    async def test_update_classification_status(self, store: DatabaseStore) -> None:
        """Test updating classification status."""
        email = Email(id="status-test", classification_status="pending")
        await store.save_email(email)

        classification = {"folder": "Projects/Test", "confidence": 0.85}
        await store.update_classification_status("status-test", "classified", classification)

        retrieved = await store.get_email("status-test")
        assert retrieved is not None
        assert retrieved.classification_status == "classified"
        assert retrieved.classification_json == classification
        assert retrieved.processed_at is not None

    @pytest.mark.asyncio
    async def test_increment_classification_attempts(self, store: DatabaseStore) -> None:
        """Test incrementing classification attempts."""
        email = Email(id="attempts-test", classification_attempts=0)
        await store.save_email(email)

        count = await store.increment_classification_attempts("attempts-test")
        assert count == 1

        count = await store.increment_classification_attempts("attempts-test")
        assert count == 2

    @pytest.mark.asyncio
    async def test_get_thread_classification(self, store: DatabaseStore) -> None:
        """Test getting prior thread classification for inheritance."""
        # Create email with approved suggestion
        email = Email(id="thread-email-1", conversation_id="thread-123")
        await store.save_email(email)
        await store.create_suggestion(
            email_id="thread-email-1",
            suggested_folder="Projects/Test",
            suggested_priority="P2 - Important",
            suggested_action_type="Review",
            confidence=0.9,
            reasoning="Test",
        )
        await store.approve_suggestion(1, approved_folder="Projects/Test")

        # Check thread inheritance
        result = await store.get_thread_classification("thread-123")
        assert result is not None
        folder, confidence = result
        assert folder == "Projects/Test"
        assert confidence == 0.9


class TestSuggestionOperations:
    """Tests for suggestion CRUD operations."""

    @pytest.mark.asyncio
    async def test_create_and_get_suggestion(self, store: DatabaseStore) -> None:
        """Test creating and retrieving a suggestion."""
        email = Email(id="sugg-email")
        await store.save_email(email)

        suggestion_id = await store.create_suggestion(
            email_id="sugg-email",
            suggested_folder="Projects/Example",
            suggested_priority="P2 - Important",
            suggested_action_type="Needs Reply",
            confidence=0.85,
            reasoning="Matches project signals",
        )

        suggestion = await store.get_suggestion(suggestion_id)
        assert suggestion is not None
        assert suggestion.email_id == "sugg-email"
        assert suggestion.suggested_folder == "Projects/Example"
        assert suggestion.confidence == 0.85
        assert suggestion.status == "pending"

    @pytest.mark.asyncio
    async def test_get_pending_suggestions(self, store: DatabaseStore) -> None:
        """Test getting pending suggestions."""
        email = Email(id="pending-sugg-email")
        await store.save_email(email)

        for i in range(3):
            await store.create_suggestion(
                email_id="pending-sugg-email",
                suggested_folder=f"Folder-{i}",
                suggested_priority="P2 - Important",
                suggested_action_type="Review",
                confidence=0.8,
                reasoning="Test",
            )

        pending = await store.get_pending_suggestions()
        assert len(pending) == 3

    @pytest.mark.asyncio
    async def test_approve_suggestion(self, store: DatabaseStore) -> None:
        """Test approving a suggestion."""
        email = Email(id="approve-email")
        await store.save_email(email)

        suggestion_id = await store.create_suggestion(
            email_id="approve-email",
            suggested_folder="Projects/Test",
            suggested_priority="P2 - Important",
            suggested_action_type="Review",
            confidence=0.9,
            reasoning="Test",
        )

        await store.approve_suggestion(suggestion_id)

        suggestion = await store.get_suggestion(suggestion_id)
        assert suggestion is not None
        assert suggestion.status == "approved"
        assert suggestion.approved_folder == "Projects/Test"
        assert suggestion.resolved_at is not None

    @pytest.mark.asyncio
    async def test_approve_suggestion_with_correction(self, store: DatabaseStore) -> None:
        """Test approving a suggestion with folder correction."""
        email = Email(id="correct-email")
        await store.save_email(email)

        suggestion_id = await store.create_suggestion(
            email_id="correct-email",
            suggested_folder="Projects/Wrong",
            suggested_priority="P2 - Important",
            suggested_action_type="Review",
            confidence=0.7,
            reasoning="Test",
        )

        await store.approve_suggestion(suggestion_id, approved_folder="Projects/Correct")

        suggestion = await store.get_suggestion(suggestion_id)
        assert suggestion is not None
        assert suggestion.status == "partial"  # Correction status
        assert suggestion.approved_folder == "Projects/Correct"

    @pytest.mark.asyncio
    async def test_reject_suggestion(self, store: DatabaseStore) -> None:
        """Test rejecting a suggestion."""
        email = Email(id="reject-email")
        await store.save_email(email)

        suggestion_id = await store.create_suggestion(
            email_id="reject-email",
            suggested_folder="Projects/Test",
            suggested_priority="P2 - Important",
            suggested_action_type="Review",
            confidence=0.5,
            reasoning="Test",
        )

        await store.reject_suggestion(suggestion_id)

        suggestion = await store.get_suggestion(suggestion_id)
        assert suggestion is not None
        assert suggestion.status == "rejected"
        assert suggestion.resolved_at is not None

    @pytest.mark.asyncio
    async def test_expire_old_suggestions(self, store: DatabaseStore) -> None:
        """Test expiring old suggestions."""
        email = Email(id="expire-email")
        await store.save_email(email)

        # Create suggestion (will be recent)
        await store.create_suggestion(
            email_id="expire-email",
            suggested_folder="Projects/Test",
            suggested_priority="P2 - Important",
            suggested_action_type="Review",
            confidence=0.8,
            reasoning="Test",
        )

        # With 0 days retention, all pending should expire
        # But our suggestion was just created, so it shouldn't expire with 1 day retention
        expired = await store.expire_old_suggestions(1)
        assert expired == 0  # Too recent

        # Manually backdate for testing
        async with store._db() as db:
            old_date = (datetime.now() - timedelta(days=30)).isoformat()
            await db.execute(
                "UPDATE suggestions SET created_at = ? WHERE email_id = 'expire-email'",
                (old_date,),
            )
            await db.commit()

        expired = await store.expire_old_suggestions(14)
        assert expired == 1


class TestWaitingForOperations:
    """Tests for waiting-for CRUD operations."""

    @pytest.mark.asyncio
    async def test_create_and_get_waiting_for(self, store: DatabaseStore) -> None:
        """Test creating and retrieving a waiting-for item."""
        email = Email(id="wait-email", conversation_id="wait-conv")
        await store.save_email(email)

        await store.create_waiting_for(
            email_id="wait-email",
            conversation_id="wait-conv",
            expected_from="responder@example.com",
            description="Waiting for project approval",
        )

        active = await store.get_active_waiting_for()
        assert len(active) == 1
        assert active[0].email_id == "wait-email"
        assert active[0].expected_from == "responder@example.com"

    @pytest.mark.asyncio
    async def test_resolve_waiting_for(self, store: DatabaseStore) -> None:
        """Test resolving a waiting-for item."""
        email = Email(id="resolve-wait-email", conversation_id="resolve-conv")
        await store.save_email(email)

        waiting_id = await store.create_waiting_for(
            email_id="resolve-wait-email",
            conversation_id="resolve-conv",
            expected_from="responder@example.com",
            description="Test",
        )

        await store.resolve_waiting_for(waiting_id, status="received")

        active = await store.get_active_waiting_for()
        assert len(active) == 0

    @pytest.mark.asyncio
    async def test_check_waiting_for_by_conversation(self, store: DatabaseStore) -> None:
        """Test checking for active waiting-for by conversation."""
        email = Email(id="check-wait-email", conversation_id="check-conv")
        await store.save_email(email)

        await store.create_waiting_for(
            email_id="check-wait-email",
            conversation_id="check-conv",
            expected_from="responder@example.com",
            description="Test",
        )

        result = await store.check_waiting_for_by_conversation("check-conv")
        assert result is not None
        assert result.conversation_id == "check-conv"

        result = await store.check_waiting_for_by_conversation("nonexistent-conv")
        assert result is None


class TestAgentStateOperations:
    """Tests for agent state key-value operations."""

    @pytest.mark.asyncio
    async def test_set_and_get_state(self, store: DatabaseStore) -> None:
        """Test setting and getting state values."""
        await store.set_state("test_key", "test_value")

        value = await store.get_state("test_key")
        assert value == "test_value"

    @pytest.mark.asyncio
    async def test_get_nonexistent_state(self, store: DatabaseStore) -> None:
        """Test getting a nonexistent state key."""
        value = await store.get_state("nonexistent_key")
        assert value is None

    @pytest.mark.asyncio
    async def test_update_state(self, store: DatabaseStore) -> None:
        """Test updating an existing state value."""
        await store.set_state("update_key", "original")
        await store.set_state("update_key", "updated")

        value = await store.get_state("update_key")
        assert value == "updated"

    @pytest.mark.asyncio
    async def test_delete_state(self, store: DatabaseStore) -> None:
        """Test deleting a state value."""
        await store.set_state("delete_key", "value")
        await store.delete_state("delete_key")

        value = await store.get_state("delete_key")
        assert value is None


class TestSenderProfileOperations:
    """Tests for sender profile operations."""

    @pytest.mark.asyncio
    async def test_upsert_and_get_sender_profile(self, store: DatabaseStore) -> None:
        """Test upserting and retrieving a sender profile."""
        await store.upsert_sender_profile(
            email="test@example.com",
            display_name="Test User",
            category="client",
        )

        profile = await store.get_sender_profile("test@example.com")
        assert profile is not None
        assert profile.email == "test@example.com"
        assert profile.display_name == "Test User"
        assert profile.domain == "example.com"
        assert profile.category == "client"
        assert profile.email_count == 1

    @pytest.mark.asyncio
    async def test_sender_profile_increment_count(self, store: DatabaseStore) -> None:
        """Test that email count is incremented on upsert."""
        await store.upsert_sender_profile(email="count@example.com")
        await store.upsert_sender_profile(email="count@example.com")
        await store.upsert_sender_profile(email="count@example.com")

        profile = await store.get_sender_profile("count@example.com")
        assert profile is not None
        assert profile.email_count == 3

    @pytest.mark.asyncio
    async def test_sender_profile_case_insensitive(self, store: DatabaseStore) -> None:
        """Test that email addresses are case-insensitive."""
        await store.upsert_sender_profile(email="Test@Example.COM")

        profile = await store.get_sender_profile("test@example.com")
        assert profile is not None

    @pytest.mark.asyncio
    async def test_get_sender_history(self, store: DatabaseStore) -> None:
        """Test getting sender history with folder distribution."""
        # Create emails and approved suggestions for sender
        for i in range(3):
            email = Email(id=f"hist-email-{i}", sender_email="history@example.com")
            await store.save_email(email)
            await store.create_suggestion(
                email_id=f"hist-email-{i}",
                suggested_folder="Projects/A",
                suggested_priority="P2 - Important",
                suggested_action_type="Review",
                confidence=0.8,
                reasoning="Test",
            )
            await store.approve_suggestion(i + 1)

        history = await store.get_sender_history("history@example.com")
        assert history.total_emails == 3
        assert history.folder_distribution.get("Projects/A") == 3

    @pytest.mark.asyncio
    async def test_mark_auto_rule_candidate(self, store: DatabaseStore) -> None:
        """Test marking a sender as an auto-rule candidate."""
        await store.upsert_sender_profile(email="candidate@example.com")
        await store.mark_auto_rule_candidate("candidate@example.com", True)

        candidates = await store.get_auto_rule_candidates()
        assert len(candidates) == 1
        assert candidates[0].email == "candidate@example.com"


class TestLLMLogOperations:
    """Tests for LLM request log operations."""

    @pytest.mark.asyncio
    async def test_log_llm_request(self, store: DatabaseStore) -> None:
        """Test logging an LLM request."""
        log_id = await store.log_llm_request(
            task_type="triage",
            model="claude-haiku-4-5-20251001",
            prompt=[{"role": "user", "content": "Test prompt"}],
            response={"content": "Test response"},
            input_tokens=100,
            output_tokens=50,
            duration_ms=500,
            email_id="test-email",
        )

        assert log_id > 0

        logs = await store.get_llm_logs(limit=10)
        assert len(logs) == 1
        assert logs[0].task_type == "triage"
        assert logs[0].model == "claude-haiku-4-5-20251001"
        assert logs[0].input_tokens == 100

    @pytest.mark.asyncio
    async def test_get_llm_logs_with_filters(self, store: DatabaseStore) -> None:
        """Test getting LLM logs with filters."""
        await store.log_llm_request(
            task_type="triage",
            model="claude-haiku-4-5-20251001",
            prompt=[],
            email_id="email-1",
        )
        await store.log_llm_request(
            task_type="bootstrap",
            model="claude-sonnet-4-5-20250929",
            prompt=[],
            email_id="email-2",
        )

        # Filter by email_id
        logs = await store.get_llm_logs(email_id="email-1")
        assert len(logs) == 1
        assert logs[0].email_id == "email-1"

    @pytest.mark.asyncio
    async def test_prune_llm_logs(self, store: DatabaseStore) -> None:
        """Test pruning old LLM logs."""
        # Create a log entry
        await store.log_llm_request(
            task_type="triage",
            model="test-model",
            prompt=[],
        )

        # Should not prune recent entries
        deleted = await store.prune_llm_logs(retention_days=30)
        assert deleted == 0

        # Manually backdate the entry
        async with store._db() as db:
            old_date = (datetime.now() - timedelta(days=60)).isoformat()
            await db.execute(
                "UPDATE llm_request_log SET timestamp = ?",
                (old_date,),
            )
            await db.commit()

        # Now it should be pruned
        deleted = await store.prune_llm_logs(retention_days=30)
        assert deleted == 1


class TestActionLogOperations:
    """Tests for action log operations."""

    @pytest.mark.asyncio
    async def test_log_action(self, store: DatabaseStore) -> None:
        """Test logging an action."""
        log_id = await store.log_action(
            action_type="move",
            email_id="test-email",
            details={"from_folder": "Inbox", "to_folder": "Projects/Test"},
            triggered_by="user_approved",
        )

        assert log_id > 0

        logs = await store.get_action_logs(limit=10)
        assert len(logs) == 1
        assert logs[0].action_type == "move"
        assert logs[0].triggered_by == "user_approved"
        assert logs[0].details_json["to_folder"] == "Projects/Test"

    @pytest.mark.asyncio
    async def test_get_action_logs_with_filters(self, store: DatabaseStore) -> None:
        """Test getting action logs with filters."""
        await store.log_action(action_type="move", email_id="email-1")
        await store.log_action(action_type="classify", email_id="email-2")

        # Filter by action_type
        logs = await store.get_action_logs(action_type="move")
        assert len(logs) == 1
        assert logs[0].action_type == "move"

        # Filter by email_id
        logs = await store.get_action_logs(email_id="email-2")
        assert len(logs) == 1
        assert logs[0].email_id == "email-2"


class TestDashboardStats:
    """Tests for dashboard statistics."""

    @pytest.mark.asyncio
    async def test_get_stats(self, store: DatabaseStore) -> None:
        """Test getting dashboard statistics."""
        # Create some test data
        await store.save_email(Email(id="stat-1", classification_status="pending"))
        await store.save_email(Email(id="stat-2", classification_status="classified"))
        await store.save_email(Email(id="stat-3", classification_status="classified"))

        stats = await store.get_stats()

        assert stats["emails_by_status"]["pending"] == 1
        assert stats["emails_by_status"]["classified"] == 2
        assert "pending_suggestions" in stats
        assert "active_waiting_for" in stats
        assert "total_senders" in stats


class TestBatchOperations:
    """Tests for batch operations (optimized for bootstrap)."""

    @pytest.mark.asyncio
    async def test_save_emails_batch(self, store: DatabaseStore) -> None:
        """Test batch saving multiple emails in a single transaction."""
        emails = [
            Email(
                id=f"batch-email-{i}",
                subject=f"Batch Subject {i}",
                sender_email=f"sender{i}@example.com",
                classification_status="pending",
            )
            for i in range(10)
        ]

        count = await store.save_emails_batch(emails)
        assert count == 10

        # Verify all emails were saved
        for i in range(10):
            email = await store.get_email(f"batch-email-{i}")
            assert email is not None
            assert email.subject == f"Batch Subject {i}"

    @pytest.mark.asyncio
    async def test_save_emails_batch_empty_list(self, store: DatabaseStore) -> None:
        """Test batch save with empty list returns 0."""
        count = await store.save_emails_batch([])
        assert count == 0

    @pytest.mark.asyncio
    async def test_save_emails_batch_upsert(self, store: DatabaseStore) -> None:
        """Test batch save updates existing emails."""
        # First save
        emails = [Email(id="batch-upsert", subject="Original")]
        await store.save_emails_batch(emails)

        # Update via batch
        emails = [Email(id="batch-upsert", subject="Updated")]
        count = await store.save_emails_batch(emails)
        assert count == 1

        email = await store.get_email("batch-upsert")
        assert email is not None
        assert email.subject == "Updated"

    @pytest.mark.asyncio
    async def test_get_sender_histories_batch(self, store: DatabaseStore) -> None:
        """Test batch sender history lookup for multiple senders."""
        # Create emails and suggestions for two senders
        senders = ["batch1@example.com", "batch2@example.com"]

        for i, sender in enumerate(senders):
            for j in range(2):
                email_id = f"batch-hist-{i}-{j}"
                email = Email(id=email_id, sender_email=sender)
                await store.save_email(email)
                suggestion_id = await store.create_suggestion(
                    email_id=email_id,
                    suggested_folder=f"Projects/Sender{i}",
                    suggested_priority="P2 - Important",
                    suggested_action_type="Review",
                    confidence=0.8,
                    reasoning="Test",
                )
                await store.approve_suggestion(suggestion_id)

        # Get batch histories
        histories = await store.get_sender_histories_batch(senders)

        assert len(histories) == 2
        assert histories["batch1@example.com"].total_emails == 2
        assert histories["batch2@example.com"].total_emails == 2
        assert "Projects/Sender0" in histories["batch1@example.com"].folder_distribution
        assert "Projects/Sender1" in histories["batch2@example.com"].folder_distribution

    @pytest.mark.asyncio
    async def test_get_sender_histories_batch_empty(self, store: DatabaseStore) -> None:
        """Test batch sender history with empty list returns empty dict."""
        histories = await store.get_sender_histories_batch([])
        assert histories == {}

    @pytest.mark.asyncio
    async def test_get_sender_histories_batch_no_history(self, store: DatabaseStore) -> None:
        """Test batch sender history for senders with no history."""
        histories = await store.get_sender_histories_batch(["unknown@example.com"])
        assert len(histories) == 1
        assert histories["unknown@example.com"].total_emails == 0
        assert histories["unknown@example.com"].folder_distribution == {}


class TestUpsertSenderProfilesBatch:
    """Tests for batch sender profile upsert."""

    @pytest.mark.asyncio
    async def test_upserts_multiple_profiles(self, store: DatabaseStore) -> None:
        """Test batch upserting multiple sender profiles."""
        profiles = [
            {
                "email": "alice@example.com",
                "display_name": "Alice",
                "category": "client",
                "email_count": 15,
                "auto_rule_candidate": False,
                "default_folder": None,
            },
            {
                "email": "bob@example.com",
                "display_name": "Bob",
                "category": "newsletter",
                "email_count": 42,
                "auto_rule_candidate": True,
                "default_folder": "Reference/Newsletters",
            },
        ]

        count = await store.upsert_sender_profiles_batch(profiles)
        assert count == 2

        alice = await store.get_sender_profile("alice@example.com")
        assert alice is not None
        assert alice.display_name == "Alice"
        assert alice.category == "client"
        assert alice.email_count == 15

        bob = await store.get_sender_profile("bob@example.com")
        assert bob is not None
        assert bob.category == "newsletter"
        assert bob.email_count == 42
        assert bob.auto_rule_candidate is True
        assert bob.default_folder == "Reference/Newsletters"

    @pytest.mark.asyncio
    async def test_batch_upsert_empty_list(self, store: DatabaseStore) -> None:
        """Test batch upsert with empty list returns 0."""
        count = await store.upsert_sender_profiles_batch([])
        assert count == 0

    @pytest.mark.asyncio
    async def test_batch_upsert_updates_existing(self, store: DatabaseStore) -> None:
        """Test that batch upsert updates existing profiles."""
        # Insert initial profile
        await store.upsert_sender_profile(
            email="update@example.com",
            display_name="Original",
            category="unknown",
        )

        # Batch upsert with new data
        profiles = [
            {
                "email": "update@example.com",
                "display_name": "Updated",
                "category": "client",
                "email_count": 25,
                "auto_rule_candidate": True,
                "default_folder": "Projects/Alpha",
            },
        ]
        await store.upsert_sender_profiles_batch(profiles)

        profile = await store.get_sender_profile("update@example.com")
        assert profile is not None
        assert profile.display_name == "Updated"
        assert profile.category == "client"
        assert profile.email_count == 25
        assert profile.auto_rule_candidate is True
        assert profile.default_folder == "Projects/Alpha"

    @pytest.mark.asyncio
    async def test_batch_upsert_preserves_category_on_unknown(self, store: DatabaseStore) -> None:
        """Test that 'unknown' category doesn't overwrite existing category."""
        # Insert with known category
        await store.upsert_sender_profile(
            email="keep@example.com",
            category="client",
        )

        # Batch upsert with unknown category
        profiles = [
            {
                "email": "keep@example.com",
                "display_name": None,
                "category": "unknown",
                "email_count": 10,
                "auto_rule_candidate": False,
                "default_folder": None,
            },
        ]
        await store.upsert_sender_profiles_batch(profiles)

        profile = await store.get_sender_profile("keep@example.com")
        assert profile is not None
        assert profile.category == "client"  # Preserved

    @pytest.mark.asyncio
    async def test_batch_upsert_case_insensitive(self, store: DatabaseStore) -> None:
        """Test that batch upsert lowercases email addresses."""
        profiles = [
            {
                "email": "CasE@Example.COM",
                "display_name": "Case Test",
                "category": "unknown",
                "email_count": 1,
                "auto_rule_candidate": False,
                "default_folder": None,
            },
        ]
        await store.upsert_sender_profiles_batch(profiles)

        profile = await store.get_sender_profile("case@example.com")
        assert profile is not None
        assert profile.display_name == "Case Test"


class TestSnippetValidation:
    """Tests for snippet length validation."""

    @pytest.mark.asyncio
    async def test_snippet_truncation_single_email(self, store: DatabaseStore) -> None:
        """Test that oversized snippets are truncated."""
        from assistant.db.store import MAX_SNIPPET_LENGTH

        long_snippet = "x" * (MAX_SNIPPET_LENGTH + 500)
        email = Email(id="long-snippet", snippet=long_snippet)
        await store.save_email(email)

        retrieved = await store.get_email("long-snippet")
        assert retrieved is not None
        assert len(retrieved.snippet) == MAX_SNIPPET_LENGTH

    @pytest.mark.asyncio
    async def test_snippet_truncation_batch(self, store: DatabaseStore) -> None:
        """Test that batch save also truncates oversized snippets."""
        from assistant.db.store import MAX_SNIPPET_LENGTH

        long_snippet = "y" * (MAX_SNIPPET_LENGTH + 1000)
        emails = [Email(id="batch-long-snippet", snippet=long_snippet)]
        await store.save_emails_batch(emails)

        retrieved = await store.get_email("batch-long-snippet")
        assert retrieved is not None
        assert len(retrieved.snippet) == MAX_SNIPPET_LENGTH


class TestMaintenanceOperations:
    """Tests for database maintenance operations."""

    @pytest.mark.asyncio
    async def test_vacuum(self, store: DatabaseStore) -> None:
        """Test vacuum operation runs without error."""
        # Create and delete some data to have something to vacuum
        for i in range(5):
            await store.save_email(Email(id=f"vacuum-test-{i}"))

        async with store._db() as db:
            await db.execute("DELETE FROM emails WHERE id LIKE 'vacuum-test-%'")
            await db.commit()

        # Should run without error
        await store.vacuum()

    @pytest.mark.asyncio
    async def test_analyze(self, store: DatabaseStore) -> None:
        """Test analyze operation runs without error."""
        # Create some data for statistics
        for i in range(5):
            await store.save_email(Email(id=f"analyze-test-{i}"))

        # Should run without error
        await store.analyze()


class TestSuggestionReturnValue:
    """Tests for approve_suggestion return value."""

    @pytest.mark.asyncio
    async def test_approve_suggestion_returns_true(self, store: DatabaseStore) -> None:
        """Test that approve_suggestion returns True on success."""
        email = Email(id="return-test-email")
        await store.save_email(email)

        suggestion_id = await store.create_suggestion(
            email_id="return-test-email",
            suggested_folder="Projects/Test",
            suggested_priority="P2 - Important",
            suggested_action_type="Review",
            confidence=0.9,
            reasoning="Test",
        )

        result = await store.approve_suggestion(suggestion_id)
        assert result is True

    @pytest.mark.asyncio
    async def test_approve_suggestion_returns_false_not_found(self, store: DatabaseStore) -> None:
        """Test that approve_suggestion returns False when suggestion not found."""
        result = await store.approve_suggestion(99999)
        assert result is False

    @pytest.mark.asyncio
    async def test_approve_suggestion_returns_false_already_resolved(
        self, store: DatabaseStore
    ) -> None:
        """Test that approve_suggestion returns False when already resolved."""
        email = Email(id="already-resolved-email")
        await store.save_email(email)

        suggestion_id = await store.create_suggestion(
            email_id="already-resolved-email",
            suggested_folder="Projects/Test",
            suggested_priority="P2 - Important",
            suggested_action_type="Review",
            confidence=0.9,
            reasoning="Test",
        )

        # First approval should succeed
        result1 = await store.approve_suggestion(suggestion_id)
        assert result1 is True

        # Second approval should fail (already resolved)
        result2 = await store.approve_suggestion(suggestion_id)
        assert result2 is False
