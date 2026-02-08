"""Tests for immutable ID migration logic.

Tests the _migrate_to_immutable_ids helper that converts stored
mutable email IDs to immutable format via Graph API.
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from assistant.core.errors import GraphAPIError
from assistant.db.store import DatabaseStore, Email


@pytest.fixture
async def store(data_dir: Path) -> DatabaseStore:
    """Create and initialize a DatabaseStore."""
    db_path = data_dir / "test_immutable.db"
    s = DatabaseStore(db_path)
    await s.initialize()
    return s


@pytest.fixture
def mock_graph_client() -> MagicMock:
    """Return a mock GraphClient."""
    return MagicMock()


class TestImmutableIdMigration:
    """Tests for the immutable ID migration function."""

    async def test_skips_when_already_migrated(
        self, store: DatabaseStore, mock_graph_client: MagicMock
    ) -> None:
        """Should skip migration when agent_state says it's already done."""
        await store.set_state("immutable_ids_migrated", "true")

        from assistant.cli import _migrate_to_immutable_ids

        await _migrate_to_immutable_ids(store, mock_graph_client)

        mock_graph_client.get.assert_not_called()

    async def test_skips_when_no_emails(
        self, store: DatabaseStore, mock_graph_client: MagicMock
    ) -> None:
        """Should set migrated flag and skip when no emails in database."""
        from assistant.cli import _migrate_to_immutable_ids

        await _migrate_to_immutable_ids(store, mock_graph_client)

        state = await store.get_state("immutable_ids_migrated")
        assert state == "true"
        mock_graph_client.get.assert_not_called()

    async def test_migrates_changed_ids(
        self, store: DatabaseStore, mock_graph_client: MagicMock
    ) -> None:
        """Should update email IDs that differ after immutable ID conversion."""
        await store.save_email(Email(id="mutable-id-1", subject="Email 1"))
        await store.save_email(Email(id="mutable-id-2", subject="Email 2"))

        # Simulate: first email gets a new immutable ID, second stays the same
        def mock_get(path, **kwargs):
            if "mutable-id-1" in path:
                return {"id": "immutable-id-1"}
            if "mutable-id-2" in path:
                return {"id": "mutable-id-2"}
            return {"id": path.split("/")[-1]}

        mock_graph_client.get.side_effect = mock_get

        from assistant.cli import _migrate_to_immutable_ids

        await _migrate_to_immutable_ids(store, mock_graph_client)

        # First email should have new ID
        old = await store.get_email("mutable-id-1")
        assert old is None
        new = await store.get_email("immutable-id-1")
        assert new is not None
        assert new.subject == "Email 1"

        # Second email should be unchanged
        same = await store.get_email("mutable-id-2")
        assert same is not None
        assert same.subject == "Email 2"

        # Migration flag should be set
        state = await store.get_state("immutable_ids_migrated")
        assert state == "true"

    async def test_handles_404_gracefully(
        self, store: DatabaseStore, mock_graph_client: MagicMock
    ) -> None:
        """Should skip deleted messages (404) without stopping migration."""
        await store.save_email(Email(id="exists-id", subject="Exists"))
        await store.save_email(Email(id="deleted-id", subject="Deleted"))

        def mock_get(path, **kwargs):
            if "deleted-id" in path:
                raise GraphAPIError("Not Found", status_code=404)
            return {"id": "exists-id"}

        mock_graph_client.get.side_effect = mock_get

        from assistant.cli import _migrate_to_immutable_ids

        await _migrate_to_immutable_ids(store, mock_graph_client)

        # Non-deleted email should still be accessible
        exists = await store.get_email("exists-id")
        assert exists is not None

        # Deleted email should remain (not updated, not removed)
        deleted = await store.get_email("deleted-id")
        assert deleted is not None

        # Migration should still complete
        state = await store.get_state("immutable_ids_migrated")
        assert state == "true"

    async def test_handles_non_404_errors_gracefully(
        self, store: DatabaseStore, mock_graph_client: MagicMock
    ) -> None:
        """Should log warning and skip on non-404 Graph API errors."""
        await store.save_email(Email(id="error-id", subject="Error"))
        await store.save_email(Email(id="ok-id", subject="OK"))

        def mock_get(path, **kwargs):
            if "error-id" in path:
                raise GraphAPIError("Server Error", status_code=500)
            return {"id": "ok-id"}

        mock_graph_client.get.side_effect = mock_get

        from assistant.cli import _migrate_to_immutable_ids

        await _migrate_to_immutable_ids(store, mock_graph_client)

        # Both emails should still exist (error email skipped, not deleted)
        error_email = await store.get_email("error-id")
        assert error_email is not None

        ok_email = await store.get_email("ok-id")
        assert ok_email is not None

        # Migration should still be marked complete
        state = await store.get_state("immutable_ids_migrated")
        assert state == "true"


class TestImmutableIdMigrationWithConsole:
    """Tests for migration with Rich console output."""

    async def test_prints_progress_with_console(
        self, store: DatabaseStore, mock_graph_client: MagicMock
    ) -> None:
        """Should output progress messages when console is provided."""
        await store.save_email(Email(id="console-email", subject="Test"))
        mock_graph_client.get.return_value = {"id": "console-email"}

        mock_console = MagicMock()

        from assistant.cli import _migrate_to_immutable_ids

        await _migrate_to_immutable_ids(store, mock_graph_client, output_console=mock_console)

        # Should have printed at least the "Migrating..." and summary messages
        assert mock_console.print.call_count >= 2

    async def test_prints_already_migrated_with_console(
        self, store: DatabaseStore, mock_graph_client: MagicMock
    ) -> None:
        """Should print 'already migrated' message when flag is set."""
        await store.set_state("immutable_ids_migrated", "true")

        mock_console = MagicMock()

        from assistant.cli import _migrate_to_immutable_ids

        await _migrate_to_immutable_ids(store, mock_graph_client, output_console=mock_console)

        mock_console.print.assert_called_once()
        call_str = str(mock_console.print.call_args)
        assert "already migrated" in call_str.lower()
