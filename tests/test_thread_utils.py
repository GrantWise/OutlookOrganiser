"""Tests for thread context utilities.

Tests thread inheritance, context fetching, and sender history lookup.
"""

from __future__ import annotations

import base64
from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest

from assistant.engine.thread_utils import (
    InheritanceResult,
    SenderHistoryResult,
    ThreadContext,
    ThreadContextManager,
    ThreadMessage,
    calculate_thread_depth,
    extract_domain,
    normalize_subject,
)

if TYPE_CHECKING:
    from assistant.classifier.snippet import SnippetCleaner
    from assistant.db.store import DatabaseStore, Email, SenderHistory
    from assistant.graph.messages import MessageManager


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def mock_store() -> AsyncMock:
    """Create a mock DatabaseStore."""
    store = AsyncMock()
    store.get_thread_classification = AsyncMock(return_value=None)
    store.get_thread_emails = AsyncMock(return_value=[])
    store.get_sender_history = AsyncMock(
        return_value=MagicMock(
            email="sender@example.com",
            total_emails=0,
            folder_distribution={},
        )
    )
    return store


@pytest.fixture
def mock_message_manager() -> MagicMock:
    """Create a mock MessageManager."""
    manager = MagicMock()
    manager.get_thread_messages = MagicMock(return_value=[])
    return manager


@pytest.fixture
def mock_snippet_cleaner() -> MagicMock:
    """Create a mock SnippetCleaner."""
    cleaner = MagicMock()
    cleaner.clean_for_context = MagicMock(side_effect=lambda text, is_html=False: text[:500] if text else "")
    return cleaner


@pytest.fixture
def thread_manager(
    mock_store: AsyncMock,
    mock_message_manager: MagicMock,
    mock_snippet_cleaner: MagicMock,
) -> ThreadContextManager:
    """Create a ThreadContextManager with mocked dependencies."""
    return ThreadContextManager(
        store=mock_store,
        message_manager=mock_message_manager,
        snippet_cleaner=mock_snippet_cleaner,
    )


def make_mock_email(
    id: str,
    conversation_id: str,
    subject: str,
    sender_email: str,
    snippet: str = "Email content",
    sender_name: str | None = None,
    received_at: datetime | None = None,
    conversation_index: str | None = None,
) -> MagicMock:
    """Create a mock Email object."""
    email = MagicMock()
    email.id = id
    email.conversation_id = conversation_id
    email.subject = subject
    email.sender_email = sender_email
    email.sender_name = sender_name
    email.snippet = snippet
    email.received_at = received_at or datetime.now()
    email.conversation_index = conversation_index
    return email


# =============================================================================
# Test normalize_subject
# =============================================================================


class TestNormalizeSubject:
    """Tests for subject normalization."""

    def test_removes_re_prefix(self) -> None:
        """Test removal of Re: prefix."""
        assert normalize_subject("Re: Project Update") == "project update"
        assert normalize_subject("RE: Project Update") == "project update"

    def test_removes_fwd_prefix(self) -> None:
        """Test removal of Fwd: prefix."""
        assert normalize_subject("Fwd: Project Update") == "project update"
        assert normalize_subject("FWD: Project Update") == "project update"
        assert normalize_subject("FW: Project Update") == "project update"
        assert normalize_subject("Fw: Project Update") == "project update"

    def test_removes_chained_prefixes(self) -> None:
        """Test removal of multiple Re:/Fwd: prefixes."""
        assert normalize_subject("Re: Fwd: Re: Topic") == "topic"
        assert normalize_subject("RE: FW: RE: Topic") == "topic"

    def test_preserves_subject_content(self) -> None:
        """Test that subject content is preserved."""
        assert normalize_subject("Project Update") == "project update"

    def test_handles_empty_subject(self) -> None:
        """Test handling of empty subject."""
        assert normalize_subject("") == ""
        assert normalize_subject("   ") == ""

    def test_returns_lowercase(self) -> None:
        """Test that result is lowercase."""
        assert normalize_subject("URGENT MEETING") == "urgent meeting"


# =============================================================================
# Test extract_domain
# =============================================================================


class TestExtractDomain:
    """Tests for domain extraction."""

    def test_extracts_domain(self) -> None:
        """Test basic domain extraction."""
        assert extract_domain("user@example.com") == "example.com"

    def test_returns_lowercase(self) -> None:
        """Test that domain is lowercase."""
        assert extract_domain("user@EXAMPLE.COM") == "example.com"

    def test_handles_subdomain(self) -> None:
        """Test handling of subdomains."""
        assert extract_domain("user@mail.example.com") == "mail.example.com"

    def test_handles_invalid_email(self) -> None:
        """Test handling of invalid email."""
        assert extract_domain("not-an-email") == ""
        assert extract_domain("") == ""

    def test_handles_none(self) -> None:
        """Test handling of None input."""
        # extract_domain should handle None gracefully
        assert extract_domain(None) == ""


# =============================================================================
# Test calculate_thread_depth
# =============================================================================


class TestCalculateThreadDepth:
    """Tests for thread depth calculation."""

    def test_original_message_depth_zero(self) -> None:
        """Test that original message has depth 0."""
        # 22 bytes or less = original message
        short_index = base64.b64encode(b"A" * 22).decode()
        assert calculate_thread_depth(short_index) == 0

    def test_first_reply_depth_one(self) -> None:
        """Test that first reply has depth 1."""
        # 22 + 5 bytes = depth 1
        index = base64.b64encode(b"A" * 27).decode()
        assert calculate_thread_depth(index) == 1

    def test_second_reply_depth_two(self) -> None:
        """Test that second reply level has depth 2."""
        # 22 + 10 bytes = depth 2
        index = base64.b64encode(b"A" * 32).decode()
        assert calculate_thread_depth(index) == 2

    def test_handles_empty_index(self) -> None:
        """Test handling of empty index."""
        assert calculate_thread_depth("") == 0
        assert calculate_thread_depth(None) == 0

    def test_handles_invalid_base64(self) -> None:
        """Test handling of invalid base64."""
        assert calculate_thread_depth("not-valid-base64!!!") == 0


# =============================================================================
# Test InheritanceResult
# =============================================================================


class TestInheritanceResult:
    """Tests for InheritanceResult dataclass."""

    def test_inherit_factory(self) -> None:
        """Test the inherit() factory method."""
        result = InheritanceResult.inherit("Projects/X")
        assert result.should_inherit is True
        assert result.inherited_folder == "Projects/X"
        assert result.confidence == 0.95
        assert result.reason != ""

    def test_inherit_custom_confidence(self) -> None:
        """Test inherit() with custom confidence."""
        result = InheritanceResult.inherit("Areas/Y", confidence=0.9)
        assert result.confidence == 0.9

    def test_no_inherit_factory(self) -> None:
        """Test the no_inherit() factory method."""
        result = InheritanceResult.no_inherit("Subject changed")
        assert result.should_inherit is False
        assert result.inherited_folder is None
        assert result.confidence == 0.0
        assert result.reason == "Subject changed"


# =============================================================================
# Test SenderHistoryResult
# =============================================================================


class TestSenderHistoryResult:
    """Tests for SenderHistoryResult dataclass."""

    def test_has_strong_pattern_true(self) -> None:
        """Test has_strong_pattern() returns True when pattern is strong."""
        result = SenderHistoryResult(
            sender_email="sender@example.com",
            total_emails=10,
            folder_distribution={"Projects/X": 9, "Areas/Y": 1},
            dominant_folder="Projects/X",
            dominant_percentage=0.9,
        )
        assert result.has_strong_pattern() is True

    def test_has_strong_pattern_false_low_count(self) -> None:
        """Test has_strong_pattern() returns False with low email count."""
        result = SenderHistoryResult(
            sender_email="sender@example.com",
            total_emails=3,  # Below threshold
            folder_distribution={"Projects/X": 3},
            dominant_folder="Projects/X",
            dominant_percentage=1.0,
        )
        assert result.has_strong_pattern() is False

    def test_has_strong_pattern_false_low_percentage(self) -> None:
        """Test has_strong_pattern() returns False with low percentage."""
        result = SenderHistoryResult(
            sender_email="sender@example.com",
            total_emails=10,
            folder_distribution={"Projects/X": 5, "Areas/Y": 5},
            dominant_folder="Projects/X",
            dominant_percentage=0.5,  # Below threshold
        )
        assert result.has_strong_pattern() is False

    def test_format_for_prompt_with_strong_pattern(self) -> None:
        """Test format_for_prompt() with strong pattern."""
        result = SenderHistoryResult(
            sender_email="sender@example.com",
            total_emails=10,
            folder_distribution={"Projects/X": 9, "Areas/Y": 1},
            dominant_folder="Projects/X",
            dominant_percentage=0.9,
        )
        formatted = result.format_for_prompt()
        assert formatted is not None
        assert "90%" in formatted
        assert "Projects/X" in formatted
        assert "9/10" in formatted

    def test_format_for_prompt_with_weak_pattern(self) -> None:
        """Test format_for_prompt() with weak pattern returns None."""
        result = SenderHistoryResult(
            sender_email="sender@example.com",
            total_emails=2,
            folder_distribution={"Projects/X": 2},
            dominant_folder="Projects/X",
            dominant_percentage=1.0,
        )
        assert result.format_for_prompt() is None


# =============================================================================
# Test ThreadContextManager.check_thread_inheritance
# =============================================================================


class TestCheckThreadInheritance:
    """Tests for thread inheritance checking."""

    @pytest.mark.asyncio
    async def test_no_inherit_when_no_prior_classification(
        self, thread_manager: ThreadContextManager, mock_store: AsyncMock
    ) -> None:
        """Test no inheritance when no prior classification exists."""
        mock_store.get_thread_classification.return_value = None

        result = await thread_manager.check_thread_inheritance(
            conversation_id="conv123",
            current_subject="Project Update",
            current_sender_domain="example.com",
        )

        assert result.should_inherit is False
        assert "No prior classification" in result.reason

    @pytest.mark.asyncio
    async def test_inherits_folder_with_matching_subject(
        self, thread_manager: ThreadContextManager, mock_store: AsyncMock
    ) -> None:
        """Test folder inheritance when prior classification exists."""
        mock_store.get_thread_classification.return_value = ("Projects/X", 0.95)
        mock_store.get_thread_emails.return_value = [
            make_mock_email(
                id="msg1",
                conversation_id="conv123",
                subject="Project Update",
                sender_email="user@example.com",
            ),
        ]

        result = await thread_manager.check_thread_inheritance(
            conversation_id="conv123",
            current_subject="Re: Project Update",
            current_sender_domain="example.com",
        )

        assert result.should_inherit is True
        assert result.inherited_folder == "Projects/X"
        assert result.confidence == 0.95

    @pytest.mark.asyncio
    async def test_no_inherit_when_subject_changed(
        self, thread_manager: ThreadContextManager, mock_store: AsyncMock
    ) -> None:
        """Test no inheritance when subject changes significantly."""
        mock_store.get_thread_classification.return_value = ("Projects/X", 0.95)
        mock_store.get_thread_emails.return_value = [
            make_mock_email(
                id="msg1",
                conversation_id="conv123",
                subject="Original Topic",
                sender_email="user@example.com",
            ),
        ]

        result = await thread_manager.check_thread_inheritance(
            conversation_id="conv123",
            current_subject="Completely Different Subject",
            current_sender_domain="example.com",
        )

        assert result.should_inherit is False
        assert "Subject changed" in result.reason

    @pytest.mark.asyncio
    async def test_no_inherit_when_new_domain_joins(
        self, thread_manager: ThreadContextManager, mock_store: AsyncMock
    ) -> None:
        """Test no inheritance when new sender domain joins thread."""
        mock_store.get_thread_classification.return_value = ("Projects/X", 0.95)
        mock_store.get_thread_emails.return_value = [
            make_mock_email(
                id="msg1",
                conversation_id="conv123",
                subject="Project Update",
                sender_email="user@company-a.com",
            ),
        ]

        result = await thread_manager.check_thread_inheritance(
            conversation_id="conv123",
            current_subject="Re: Project Update",
            current_sender_domain="company-b.com",  # Different domain
        )

        assert result.should_inherit is False
        assert "New participant domain" in result.reason

    @pytest.mark.asyncio
    async def test_inherits_with_re_fwd_prefix(
        self, thread_manager: ThreadContextManager, mock_store: AsyncMock
    ) -> None:
        """Test inheritance works despite Re:/Fwd: prefixes."""
        mock_store.get_thread_classification.return_value = ("Projects/X", 0.95)
        mock_store.get_thread_emails.return_value = [
            make_mock_email(
                id="msg1",
                conversation_id="conv123",
                subject="Re: Fwd: Important Meeting",
                sender_email="user@example.com",
            ),
        ]

        result = await thread_manager.check_thread_inheritance(
            conversation_id="conv123",
            current_subject="RE: FW: Important Meeting",
            current_sender_domain="example.com",
        )

        assert result.should_inherit is True
        assert result.inherited_folder == "Projects/X"


# =============================================================================
# Test ThreadContextManager.get_thread_context
# =============================================================================


class TestGetThreadContext:
    """Tests for thread context fetching."""

    @pytest.mark.asyncio
    async def test_returns_empty_context_when_no_prior_messages(
        self, thread_manager: ThreadContextManager, mock_store: AsyncMock
    ) -> None:
        """Test empty context when no prior messages exist."""
        mock_store.get_thread_emails.return_value = []

        context = await thread_manager.get_thread_context(
            conversation_id="conv123",
            exclude_message_id="current_msg",
        )

        assert context.conversation_id == "conv123"
        assert context.messages == []
        assert context.thread_depth == 0

    @pytest.mark.asyncio
    async def test_fetches_from_local_db_first(
        self,
        thread_manager: ThreadContextManager,
        mock_store: AsyncMock,
        mock_message_manager: MagicMock,
    ) -> None:
        """Test that local database is checked before Graph API."""
        mock_store.get_thread_emails.return_value = [
            make_mock_email(
                id="msg1",
                conversation_id="conv123",
                subject="Prior message",
                sender_email="user@example.com",
                snippet="Prior content",
            ),
            make_mock_email(
                id="msg2",
                conversation_id="conv123",
                subject="Earlier message",
                sender_email="other@example.com",
                snippet="Earlier content",
            ),
            make_mock_email(
                id="msg3",
                conversation_id="conv123",
                subject="Even earlier",
                sender_email="third@example.com",
                snippet="Third content",
            ),
        ]

        context = await thread_manager.get_thread_context(
            conversation_id="conv123",
            exclude_message_id="current_msg",
            max_messages=3,
        )

        # Should have 3 messages from local DB
        assert len(context.messages) == 3
        # Graph API should not be called since we have enough local messages
        mock_message_manager.get_thread_messages.assert_not_called()

    @pytest.mark.asyncio
    async def test_falls_back_to_graph_api(
        self,
        thread_manager: ThreadContextManager,
        mock_store: AsyncMock,
        mock_message_manager: MagicMock,
    ) -> None:
        """Test fallback to Graph API when local DB has insufficient data."""
        # Only 1 message in local DB
        mock_store.get_thread_emails.return_value = [
            make_mock_email(
                id="msg1",
                conversation_id="conv123",
                subject="Prior message",
                sender_email="user@example.com",
                snippet="Prior content",
            ),
        ]

        # 2 more messages from API
        mock_message_manager.get_thread_messages.return_value = [
            {
                "id": "msg2",
                "subject": "API message 1",
                "from": {"emailAddress": {"address": "api1@example.com", "name": "API User"}},
                "bodyPreview": "API content 1",
                "receivedDateTime": "2024-01-01T10:00:00Z",
                "conversationIndex": "",
            },
            {
                "id": "msg3",
                "subject": "API message 2",
                "from": {"emailAddress": {"address": "api2@example.com", "name": "API User 2"}},
                "bodyPreview": "API content 2",
                "receivedDateTime": "2024-01-01T09:00:00Z",
                "conversationIndex": "",
            },
        ]

        context = await thread_manager.get_thread_context(
            conversation_id="conv123",
            exclude_message_id="current_msg",
            max_messages=3,
        )

        # Should have messages from both sources
        assert len(context.messages) == 3
        mock_message_manager.get_thread_messages.assert_called_once()

    @pytest.mark.asyncio
    async def test_excludes_current_message(
        self, thread_manager: ThreadContextManager, mock_store: AsyncMock
    ) -> None:
        """Test that current message is excluded from context."""
        mock_store.get_thread_emails.return_value = [
            make_mock_email(
                id="msg1",
                conversation_id="conv123",
                subject="Prior message",
                sender_email="user@example.com",
            ),
        ]

        context = await thread_manager.get_thread_context(
            conversation_id="conv123",
            exclude_message_id="msg1",  # Exclude the only message
        )

        # The excluded message should be in the mock return, but
        # we're testing the exclude_id parameter is passed correctly
        mock_store.get_thread_emails.assert_called_with(
            "conv123",
            exclude_id="msg1",
            limit=4,  # max_messages + 1
        )

    @pytest.mark.asyncio
    async def test_collects_unique_domains(
        self, thread_manager: ThreadContextManager, mock_store: AsyncMock
    ) -> None:
        """Test that unique sender domains are collected."""
        mock_store.get_thread_emails.return_value = [
            make_mock_email(
                id="msg1",
                conversation_id="conv123",
                subject="Message 1",
                sender_email="user1@domain-a.com",
            ),
            make_mock_email(
                id="msg2",
                conversation_id="conv123",
                subject="Message 2",
                sender_email="user2@domain-b.com",
            ),
            make_mock_email(
                id="msg3",
                conversation_id="conv123",
                subject="Message 3",
                sender_email="user3@domain-a.com",  # Same domain as msg1
            ),
        ]

        context = await thread_manager.get_thread_context(
            conversation_id="conv123",
            exclude_message_id="current_msg",
        )

        assert "domain-a.com" in context.unique_domains
        assert "domain-b.com" in context.unique_domains
        assert len(context.unique_domains) == 2  # Only 2 unique domains


# =============================================================================
# Test ThreadContextManager.get_sender_history
# =============================================================================


class TestGetSenderHistory:
    """Tests for sender history lookup."""

    @pytest.mark.asyncio
    async def test_returns_empty_history_for_new_sender(
        self, thread_manager: ThreadContextManager, mock_store: AsyncMock
    ) -> None:
        """Test empty history for sender with no prior emails."""
        mock_store.get_sender_history.return_value = MagicMock(
            email="new@example.com",
            total_emails=0,
            folder_distribution={},
        )

        result = await thread_manager.get_sender_history("new@example.com")

        assert result.total_emails == 0
        assert result.folder_distribution == {}
        assert result.dominant_folder is None
        assert result.dominant_percentage == 0.0

    @pytest.mark.asyncio
    async def test_identifies_dominant_folder(
        self, thread_manager: ThreadContextManager, mock_store: AsyncMock
    ) -> None:
        """Test identification of dominant folder."""
        mock_store.get_sender_history.return_value = MagicMock(
            email="sender@example.com",
            total_emails=10,
            folder_distribution={"Projects/X": 8, "Areas/Y": 2},
        )

        result = await thread_manager.get_sender_history("sender@example.com")

        assert result.total_emails == 10
        assert result.dominant_folder == "Projects/X"
        assert result.dominant_percentage == 0.8

    @pytest.mark.asyncio
    async def test_no_dominant_when_evenly_split(
        self, thread_manager: ThreadContextManager, mock_store: AsyncMock
    ) -> None:
        """Test no dominant folder when emails are evenly split."""
        mock_store.get_sender_history.return_value = MagicMock(
            email="sender@example.com",
            total_emails=10,
            folder_distribution={"Projects/X": 5, "Areas/Y": 5},
        )

        result = await thread_manager.get_sender_history("sender@example.com")

        assert result.dominant_percentage == 0.5
        # Should still identify one as "dominant" (first to reach max)
        assert result.dominant_folder in ["Projects/X", "Areas/Y"]
        # But has_strong_pattern should be False
        assert result.has_strong_pattern() is False
