"""Tests for task_sync CRUD operations in DatabaseStore.

Tests create, read by email/task, update status, get active, and
the immutable ID migration helpers (get_all_email_ids, update_email_id).
"""

from datetime import datetime
from pathlib import Path

import pytest

from assistant.db.store import DatabaseStore, Email, TaskSync


@pytest.fixture
async def store(data_dir: Path) -> DatabaseStore:
    """Create and initialize a DatabaseStore."""
    db_path = data_dir / "test_task_sync.db"
    s = DatabaseStore(db_path)
    await s.initialize()
    return s


async def _seed_email(store: DatabaseStore, email_id: str = "email-001") -> None:
    """Insert a test email into the database."""
    await store.save_email(Email(id=email_id, subject="Test email"))


class TestCreateTaskSync:
    """Tests for create_task_sync."""

    async def test_creates_record_and_returns_id(self, store: DatabaseStore) -> None:
        """Should insert a task sync record and return its ID."""
        await _seed_email(store, "email-1")

        task_sync_id = await store.create_task_sync(
            email_id="email-1",
            todo_task_id="todo-task-abc",
            todo_list_id="todo-list-xyz",
            task_type="needs_reply",
        )

        assert task_sync_id > 0

    async def test_created_record_has_active_status(self, store: DatabaseStore) -> None:
        """Newly created task sync should default to 'active' status."""
        await _seed_email(store, "email-2")

        task_sync_id = await store.create_task_sync(
            email_id="email-2",
            todo_task_id="todo-task-2",
            todo_list_id="list-2",
            task_type="waiting_for",
        )

        record = await store.get_task_sync_by_email("email-2")
        assert record is not None
        assert record.status == "active"
        assert record.id == task_sync_id


class TestGetTaskSyncByEmail:
    """Tests for get_task_sync_by_email."""

    async def test_returns_record_for_existing_email(self, store: DatabaseStore) -> None:
        """Should return the task sync record for a known email."""
        await _seed_email(store, "email-3")
        await store.create_task_sync("email-3", "todo-3", "list-3", "review")

        record = await store.get_task_sync_by_email("email-3")

        assert record is not None
        assert isinstance(record, TaskSync)
        assert record.email_id == "email-3"
        assert record.todo_task_id == "todo-3"
        assert record.todo_list_id == "list-3"
        assert record.task_type == "review"

    async def test_returns_none_for_missing_email(self, store: DatabaseStore) -> None:
        """Should return None when no task sync exists for the email."""
        result = await store.get_task_sync_by_email("nonexistent-email")
        assert result is None


class TestGetTaskSyncByTask:
    """Tests for get_task_sync_by_task."""

    async def test_returns_record_for_existing_task(self, store: DatabaseStore) -> None:
        """Should return the task sync record for a known To Do task ID."""
        await _seed_email(store, "email-4")
        await store.create_task_sync("email-4", "todo-4", "list-4", "delegated")

        record = await store.get_task_sync_by_task("todo-4")

        assert record is not None
        assert record.todo_task_id == "todo-4"
        assert record.email_id == "email-4"

    async def test_returns_none_for_missing_task(self, store: DatabaseStore) -> None:
        """Should return None when no task sync exists for the task ID."""
        result = await store.get_task_sync_by_task("nonexistent-task")
        assert result is None


class TestUpdateTaskSyncStatus:
    """Tests for update_task_sync_status."""

    async def test_updates_status_to_completed(self, store: DatabaseStore) -> None:
        """Should update status from active to completed."""
        await _seed_email(store, "email-5")
        task_sync_id = await store.create_task_sync("email-5", "todo-5", "list-5", "needs_reply")

        await store.update_task_sync_status(task_sync_id, "completed")

        record = await store.get_task_sync_by_email("email-5")
        assert record is not None
        assert record.status == "completed"
        assert record.synced_at is not None

    async def test_updates_status_to_deleted(self, store: DatabaseStore) -> None:
        """Should update status to deleted."""
        await _seed_email(store, "email-6")
        task_sync_id = await store.create_task_sync("email-6", "todo-6", "list-6", "review")

        await store.update_task_sync_status(task_sync_id, "deleted")

        record = await store.get_task_sync_by_email("email-6")
        assert record is not None
        assert record.status == "deleted"

    async def test_sets_synced_at_when_provided(self, store: DatabaseStore) -> None:
        """Should use the explicitly provided synced_at timestamp."""
        await _seed_email(store, "email-7")
        task_sync_id = await store.create_task_sync("email-7", "todo-7", "list-7", "waiting_for")

        sync_time = datetime(2025, 6, 1, 12, 0, 0)
        await store.update_task_sync_status(task_sync_id, "completed", synced_at=sync_time)

        record = await store.get_task_sync_by_email("email-7")
        assert record is not None
        assert record.synced_at is not None
        assert record.synced_at.year == 2025
        assert record.synced_at.month == 6


class TestGetActiveTaskSyncs:
    """Tests for get_active_task_syncs."""

    async def test_returns_only_active_records(self, store: DatabaseStore) -> None:
        """Should return only records with 'active' status."""
        for i in range(3):
            await _seed_email(store, f"active-{i}")
            await store.create_task_sync(f"active-{i}", f"todo-a{i}", "list", "review")

        # Complete one of them
        await store.update_task_sync_status(1, "completed")

        active = await store.get_active_task_syncs()
        assert len(active) == 2
        assert all(r.status == "active" for r in active)

    async def test_returns_empty_list_when_none_active(self, store: DatabaseStore) -> None:
        """Should return empty list when no active records exist."""
        active = await store.get_active_task_syncs()
        assert active == []


class TestGetAllEmailIds:
    """Tests for get_all_email_ids (immutable ID migration helper)."""

    async def test_returns_all_email_ids(self, store: DatabaseStore) -> None:
        """Should return all email IDs from the emails table."""
        for i in range(5):
            await store.save_email(Email(id=f"email-id-{i}"))

        ids = await store.get_all_email_ids()
        assert len(ids) == 5
        assert set(ids) == {f"email-id-{i}" for i in range(5)}

    async def test_returns_empty_list_when_no_emails(self, store: DatabaseStore) -> None:
        """Should return empty list when no emails exist."""
        ids = await store.get_all_email_ids()
        assert ids == []


class TestUpdateEmailId:
    """Tests for update_email_id (immutable ID migration helper)."""

    async def test_updates_email_primary_key(self, store: DatabaseStore) -> None:
        """Should update the email ID in the emails table."""
        await store.save_email(Email(id="old-mutable-id", subject="Test"))

        await store.update_email_id("old-mutable-id", "new-immutable-id")

        # Old ID should not exist
        old = await store.get_email("old-mutable-id")
        assert old is None

        # New ID should exist with same data
        new = await store.get_email("new-immutable-id")
        assert new is not None
        assert new.subject == "Test"

    async def test_updates_suggestion_foreign_keys(self, store: DatabaseStore) -> None:
        """Should update email_id in suggestions table."""
        await store.save_email(Email(id="fk-old-id"))
        suggestion_id = await store.create_suggestion(
            email_id="fk-old-id",
            suggested_folder="Projects/Test",
            suggested_priority="P2 - Important",
            suggested_action_type="Review",
            confidence=0.9,
            reasoning="Test",
        )

        await store.update_email_id("fk-old-id", "fk-new-id")

        suggestion = await store.get_suggestion(suggestion_id)
        assert suggestion is not None
        assert suggestion.email_id == "fk-new-id"

    async def test_updates_task_sync_foreign_keys(self, store: DatabaseStore) -> None:
        """Should update email_id in task_sync table."""
        await store.save_email(Email(id="ts-old-id"))
        await store.create_task_sync("ts-old-id", "todo-1", "list-1", "review")

        await store.update_email_id("ts-old-id", "ts-new-id")

        record = await store.get_task_sync_by_email("ts-new-id")
        assert record is not None
        assert record.email_id == "ts-new-id"

        old_record = await store.get_task_sync_by_email("ts-old-id")
        assert old_record is None
