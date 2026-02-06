"""Tests for the dry-run classification engine.

Tests classification routing (auto-rules vs Claude), distribution
calculation, confusion matrix building, sample selection, and the
read-only guarantee.
"""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from assistant.config_schema import AppConfig
from assistant.db.store import DatabaseStore, Email
from assistant.engine.dry_run import (
    DryRunClassification,
    DryRunEngine,
    DryRunReport,
    FolderDistribution,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_config(sample_config_dict: dict[str, Any]) -> AppConfig:
    """Return a config for dry-run."""
    return AppConfig(**sample_config_dict)


@pytest.fixture
async def store(data_dir: Path) -> DatabaseStore:
    """Return an initialized DatabaseStore."""
    db_path = data_dir / "test_dry_run.db"
    s = DatabaseStore(db_path)
    await s.initialize()
    return s


@pytest.fixture
def mock_classifier() -> MagicMock:
    """Return a mock EmailClassifier."""
    classifier = MagicMock()
    classifier.refresh_system_prompt = AsyncMock()
    classifier.classify_with_auto_rules = MagicMock(return_value=None)
    classifier.classify_with_claude = AsyncMock(return_value=None)
    return classifier


@pytest.fixture
def mock_message_manager() -> MagicMock:
    """Return a mock MessageManager."""
    mgr = MagicMock()
    mgr.list_messages = MagicMock(return_value=[])
    return mgr


@pytest.fixture
def mock_snippet_cleaner() -> MagicMock:
    """Return a mock SnippetCleaner."""
    cleaner = MagicMock()
    result = MagicMock()
    result.cleaned_text = "cleaned"
    cleaner.clean = MagicMock(return_value=result)
    return cleaner


@pytest.fixture
def mock_thread_manager() -> MagicMock:
    """Return a mock ThreadContextManager."""
    return MagicMock()


@pytest.fixture
def engine(
    mock_classifier: MagicMock,
    store: DatabaseStore,
    mock_message_manager: MagicMock,
    mock_snippet_cleaner: MagicMock,
    mock_thread_manager: MagicMock,
    sample_config: AppConfig,
) -> DryRunEngine:
    """Return a DryRunEngine with mocked dependencies."""
    from rich.console import Console

    return DryRunEngine(
        classifier=mock_classifier,
        store=store,
        message_manager=mock_message_manager,
        snippet_cleaner=mock_snippet_cleaner,
        thread_manager=mock_thread_manager,
        config=sample_config,
        console=Console(quiet=True),
    )


def make_email(
    email_id: str = "e1",
    subject: str = "Test Subject",
    sender_email: str = "sender@test.com",
    sender_name: str = "Sender",
    received_at: datetime | None = None,
) -> Email:
    """Create a test Email."""
    return Email(
        id=email_id,
        subject=subject,
        sender_email=sender_email,
        sender_name=sender_name,
        received_at=received_at or datetime.now(UTC),
        snippet="Test snippet",
        importance="normal",
        is_read=False,
        flag_status="notFlagged",
    )


# ---------------------------------------------------------------------------
# Dataclass tests
# ---------------------------------------------------------------------------


class TestDataclasses:
    """Tests for result dataclasses."""

    def test_dry_run_classification_fields(self) -> None:
        """Test DryRunClassification creation."""
        c = DryRunClassification(
            email_id="e1",
            subject="Test",
            sender_email="a@b.com",
            sender_name="A",
            folder="Inbox",
            priority="P2 - Important",
            action_type="Review",
            confidence=0.9,
            reasoning="Test reason",
            method="auto_rule",
        )
        assert c.email_id == "e1"
        assert c.method == "auto_rule"

    def test_folder_distribution_fields(self) -> None:
        """Test FolderDistribution creation."""
        fd = FolderDistribution(folder="Inbox", count=10, percentage=50.0)
        assert fd.folder == "Inbox"
        assert fd.percentage == 50.0

    def test_dry_run_report_defaults(self) -> None:
        """Test DryRunReport default values."""
        report = DryRunReport()
        assert report.total_emails == 0
        assert report.classified_count == 0
        assert report.failed_count == 0
        assert report.auto_ruled_count == 0
        assert report.claude_count == 0
        assert report.folder_distribution == []
        assert report.sample_classifications == []
        assert report.accuracy_report is None
        assert report.duration_seconds == 0.0


# ---------------------------------------------------------------------------
# _build_distribution
# ---------------------------------------------------------------------------


class TestBuildDistribution:
    """Tests for folder distribution calculation."""

    def test_single_folder(self, engine: DryRunEngine) -> None:
        """Test distribution with all emails in one folder."""
        classifications = [
            DryRunClassification(
                email_id=str(i),
                subject="S",
                sender_email="a@b.com",
                sender_name="A",
                folder="Inbox",
                priority="P2 - Important",
                action_type="Review",
                confidence=0.9,
                reasoning="test",
                method="auto_rule",
            )
            for i in range(10)
        ]
        dist = engine._build_distribution(classifications)
        assert len(dist) == 1
        assert dist[0].folder == "Inbox"
        assert dist[0].count == 10
        assert dist[0].percentage == 100.0

    def test_multiple_folders_sorted_by_count(self, engine: DryRunEngine) -> None:
        """Test that folders are sorted by count descending."""
        classifications = [
            DryRunClassification(
                email_id=str(i),
                subject="S",
                sender_email="a@b.com",
                sender_name="A",
                folder="Inbox" if i < 7 else "Archive",
                priority="P2",
                action_type="Review",
                confidence=0.9,
                reasoning="test",
                method="auto_rule",
            )
            for i in range(10)
        ]
        dist = engine._build_distribution(classifications)
        assert len(dist) == 2
        assert dist[0].folder == "Inbox"
        assert dist[0].count == 7
        assert dist[1].folder == "Archive"
        assert dist[1].count == 3

    def test_empty_classifications(self, engine: DryRunEngine) -> None:
        """Test distribution with no classifications."""
        dist = engine._build_distribution([])
        assert dist == []

    def test_percentages_sum_to_100(self, engine: DryRunEngine) -> None:
        """Test that percentages add up to ~100%."""
        classifications = [
            DryRunClassification(
                email_id=str(i),
                subject="S",
                sender_email="a@b.com",
                sender_name="A",
                folder=f"Folder{i % 3}",
                priority="P2",
                action_type="Review",
                confidence=0.9,
                reasoning="test",
                method="auto_rule",
            )
            for i in range(99)
        ]
        dist = engine._build_distribution(classifications)
        total_pct = sum(d.percentage for d in dist)
        assert abs(total_pct - 100.0) < 0.1


# ---------------------------------------------------------------------------
# _classify_email
# ---------------------------------------------------------------------------


class TestClassifyEmail:
    """Tests for single email classification."""

    @pytest.mark.asyncio
    async def test_returns_auto_rule_result(self, engine: DryRunEngine) -> None:
        """Test that auto-rule match is returned first."""
        from assistant.classifier.claude_classifier import ClassificationResult

        auto_result = ClassificationResult(
            folder="Reference/Newsletters",
            priority="P4 - Low",
            action_type="FYI Only",
            confidence=1.0,
            reasoning="Matched newsletter rule",
            method="auto_rule",
        )
        engine._classifier.classify_with_auto_rules.return_value = auto_result

        email = make_email()
        result = await engine._classify_email(email)

        assert result is not None
        assert result.method == "auto_rule"
        assert result.folder == "Reference/Newsletters"
        # Claude should NOT have been called
        engine._classifier.classify_with_claude.assert_not_called()

    @pytest.mark.asyncio
    async def test_falls_back_to_claude(self, engine: DryRunEngine) -> None:
        """Test Claude classification when no auto-rule matches."""
        from assistant.classifier.claude_classifier import ClassificationResult

        engine._classifier.classify_with_auto_rules.return_value = None

        claude_result = ClassificationResult(
            folder="Projects/Alpha",
            priority="P2 - Important",
            action_type="Review",
            confidence=0.85,
            reasoning="Project-related email",
            method="claude_tool_use",
        )
        engine._classifier.classify_with_claude = AsyncMock(return_value=claude_result)

        email = make_email()
        result = await engine._classify_email(email)

        assert result is not None
        assert result.method == "claude_tool_use"
        assert result.folder == "Projects/Alpha"

    @pytest.mark.asyncio
    async def test_returns_none_on_claude_error(self, engine: DryRunEngine) -> None:
        """Test that classification errors return None."""
        from assistant.core.errors import ClassificationError

        engine._classifier.classify_with_auto_rules.return_value = None
        engine._classifier.classify_with_claude = AsyncMock(
            side_effect=ClassificationError("API error", attempts=1)
        )

        email = make_email()
        result = await engine._classify_email(email)
        assert result is None


# ---------------------------------------------------------------------------
# _build_confusion_matrix
# ---------------------------------------------------------------------------


class TestBuildConfusionMatrix:
    """Tests for confusion matrix building."""

    @pytest.mark.asyncio
    async def test_returns_none_with_few_corrections(self, engine: DryRunEngine) -> None:
        """Test that None is returned with <10 resolved suggestions."""
        # Empty database has 0 resolved suggestions
        result = await engine._build_confusion_matrix()
        assert result is None

    @pytest.mark.asyncio
    async def test_calculates_accuracy(self, engine: DryRunEngine, store: DatabaseStore) -> None:
        """Test accuracy calculation with resolved suggestions."""
        # Insert 10+ resolved suggestions
        for i in range(12):
            # Save an email first
            email = Email(id=f"email_{i}", subject=f"Subject {i}")
            await store.save_email(email)

            # Create suggestion
            suggested_folder = "Projects/Alpha"
            approved_folder = "Projects/Alpha" if i < 9 else "Projects/Beta"
            await store.create_suggestion(
                email_id=f"email_{i}",
                suggested_folder=suggested_folder,
                suggested_priority="P2 - Important",
                suggested_action_type="Review",
                confidence=0.9,
                reasoning="Test",
            )

            # Approve it - get the suggestion ID first
            suggestions = await store.get_pending_suggestions(limit=100)
            for s in suggestions:
                if s.email_id == f"email_{i}":
                    await store.approve_suggestion(
                        suggestion_id=s.id,
                        approved_folder=approved_folder,
                        approved_priority="P2 - Important",
                        approved_action_type="Review",
                    )

        result = await engine._build_confusion_matrix()
        assert result is not None
        assert result.total_resolved == 12
        # 9 out of 12 folder matches
        assert result.folder_correct == 9
        assert result.folder_accuracy == 9 / 12


# ---------------------------------------------------------------------------
# _fetch_or_load_emails
# ---------------------------------------------------------------------------


class TestFetchOrLoadEmails:
    """Tests for email loading."""

    @pytest.mark.asyncio
    async def test_prefers_database(self, engine: DryRunEngine, store: DatabaseStore) -> None:
        """Test that database emails are preferred over Graph API."""
        # Insert an email into the database
        email = Email(
            id="db_email_1",
            subject="From DB",
            sender_email="db@test.com",
            received_at=datetime.now(UTC),
        )
        await store.save_email(email)

        result = await engine._fetch_or_load_emails(days=90, limit=None)
        assert len(result) >= 1
        # Graph API should NOT have been called
        engine._message_manager.list_messages.assert_not_called()

    @pytest.mark.asyncio
    async def test_falls_back_to_graph_api(self, engine: DryRunEngine) -> None:
        """Test Graph API fallback when database is empty."""
        engine._message_manager.list_messages.return_value = [
            {
                "id": "graph_msg_1",
                "subject": "From Graph",
                "from": {"emailAddress": {"address": "graph@test.com", "name": "Graph"}},
                "receivedDateTime": "2024-01-15T10:00:00Z",
                "bodyPreview": "test",
                "importance": "normal",
                "isRead": False,
                "flag": {"flagStatus": "notFlagged"},
            }
        ]

        result = await engine._fetch_or_load_emails(days=90, limit=None)
        assert len(result) == 1
        assert result[0].id == "graph_msg_1"

    @pytest.mark.asyncio
    async def test_deduplicates_graph_api_results(self, engine: DryRunEngine) -> None:
        """Test that duplicate messages from Graph API are deduplicated."""
        engine._message_manager.list_messages.return_value = [
            {
                "id": "dup_msg_1",
                "subject": "First",
                "from": {"emailAddress": {"address": "a@b.com", "name": "A"}},
                "receivedDateTime": "2024-01-15T10:00:00Z",
                "bodyPreview": "test",
                "importance": "normal",
                "isRead": False,
                "flag": {"flagStatus": "notFlagged"},
            },
            {
                "id": "dup_msg_2",
                "subject": "Second",
                "from": {"emailAddress": {"address": "c@d.com", "name": "C"}},
                "receivedDateTime": "2024-01-15T11:00:00Z",
                "bodyPreview": "test",
                "importance": "normal",
                "isRead": False,
                "flag": {"flagStatus": "notFlagged"},
            },
            {
                "id": "dup_msg_1",  # Duplicate of first
                "subject": "First Again",
                "from": {"emailAddress": {"address": "a@b.com", "name": "A"}},
                "receivedDateTime": "2024-01-15T10:00:00Z",
                "bodyPreview": "test",
                "importance": "normal",
                "isRead": False,
                "flag": {"flagStatus": "notFlagged"},
            },
        ]

        result = await engine._fetch_or_load_emails(days=90, limit=None)
        assert len(result) == 2
        ids = [e.id for e in result]
        assert "dup_msg_1" in ids
        assert "dup_msg_2" in ids

    @pytest.mark.asyncio
    async def test_respects_limit(self, engine: DryRunEngine, store: DatabaseStore) -> None:
        """Test that limit parameter is respected."""
        for i in range(5):
            email = Email(
                id=f"email_{i}",
                subject=f"Email {i}",
                received_at=datetime.now(UTC),
            )
            await store.save_email(email)

        result = await engine._fetch_or_load_emails(days=90, limit=2)
        assert len(result) == 2


# ---------------------------------------------------------------------------
# run (integration-level with mocks)
# ---------------------------------------------------------------------------


class TestDryRunRun:
    """Integration tests for the full dry-run pipeline."""

    @pytest.mark.asyncio
    async def test_returns_early_on_no_emails(self, engine: DryRunEngine) -> None:
        """Test that dry-run returns early if no emails found."""
        report = await engine.run(days=7, sample=5)
        assert report.total_emails == 0
        assert report.classified_count == 0

    @pytest.mark.asyncio
    async def test_full_pipeline_with_auto_rules(
        self, engine: DryRunEngine, store: DatabaseStore
    ) -> None:
        """Test full pipeline with auto-rule classifications."""
        from assistant.classifier.claude_classifier import ClassificationResult

        # Insert test emails
        for i in range(5):
            email = Email(
                id=f"test_{i}",
                subject=f"Newsletter #{i}",
                sender_email="news@example.com",
                received_at=datetime.now(UTC),
            )
            await store.save_email(email)

        # Mock auto-rules to match everything
        auto_result = ClassificationResult(
            folder="Reference/Newsletters",
            priority="P4 - Low",
            action_type="FYI Only",
            confidence=1.0,
            reasoning="Newsletter rule",
            method="auto_rule",
        )
        engine._classifier.classify_with_auto_rules.return_value = auto_result

        report = await engine.run(days=90, sample=3)

        assert report.total_emails == 5
        assert report.classified_count == 5
        assert report.auto_ruled_count == 5
        assert report.claude_count == 0
        assert report.failed_count == 0
        assert len(report.folder_distribution) == 1
        assert report.folder_distribution[0].folder == "Reference/Newsletters"
        assert len(report.sample_classifications) == 3  # sample=3

    @pytest.mark.asyncio
    async def test_counts_failed_classifications(
        self, engine: DryRunEngine, store: DatabaseStore
    ) -> None:
        """Test that failed classifications are counted."""
        from assistant.core.errors import ClassificationError

        # Insert test emails
        for i in range(3):
            email = Email(
                id=f"fail_{i}",
                subject=f"Fail #{i}",
                received_at=datetime.now(UTC),
            )
            await store.save_email(email)

        # Mock both classifier methods to fail
        engine._classifier.classify_with_auto_rules.return_value = None
        engine._classifier.classify_with_claude = AsyncMock(
            side_effect=ClassificationError("API error", attempts=1)
        )

        report = await engine.run(days=90, sample=5)

        assert report.total_emails == 3
        assert report.classified_count == 0
        assert report.failed_count == 3

    @pytest.mark.asyncio
    async def test_sample_size_capped_at_classified_count(
        self, engine: DryRunEngine, store: DatabaseStore
    ) -> None:
        """Test that sample size doesn't exceed classified count."""
        from assistant.classifier.claude_classifier import ClassificationResult

        # Insert 3 emails
        for i in range(3):
            email = Email(
                id=f"cap_{i}",
                subject=f"Cap #{i}",
                received_at=datetime.now(UTC),
            )
            await store.save_email(email)

        auto_result = ClassificationResult(
            folder="Inbox",
            priority="P2",
            action_type="Review",
            confidence=1.0,
            reasoning="test",
            method="auto_rule",
        )
        engine._classifier.classify_with_auto_rules.return_value = auto_result

        # Request sample=20 but only 3 emails
        report = await engine.run(days=90, sample=20)
        assert len(report.sample_classifications) == 3

    @pytest.mark.asyncio
    async def test_is_read_only(self, engine: DryRunEngine, store: DatabaseStore) -> None:
        """Test that dry-run does not write suggestions to database."""
        from assistant.classifier.claude_classifier import ClassificationResult

        email = Email(
            id="readonly_1",
            subject="Read Only Test",
            received_at=datetime.now(UTC),
        )
        await store.save_email(email)

        auto_result = ClassificationResult(
            folder="Projects/Alpha",
            priority="P2",
            action_type="Review",
            confidence=0.9,
            reasoning="test",
            method="auto_rule",
        )
        engine._classifier.classify_with_auto_rules.return_value = auto_result

        await engine.run(days=90, sample=5)

        # Verify no suggestions were created
        pending = await store.get_pending_suggestions(limit=100)
        assert len(pending) == 0
