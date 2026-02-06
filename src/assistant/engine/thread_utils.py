"""Thread context utilities for email classification.

This module provides utilities for managing email thread context:
- Thread inheritance: Reuse folder classification from prior messages
- Thread context fetching: Get prior messages for classification
- Sender history: Historical folder patterns for a sender

Usage:
    from assistant.engine.thread_utils import ThreadContextManager

    manager = ThreadContextManager(store, message_manager, snippet_cleaner)

    # Check if we can inherit folder from prior message in thread
    result = await manager.check_thread_inheritance(
        conversation_id="AAQk...",
        current_subject="Re: Project Update",
        current_sender_domain="example.com",
    )
    if result.should_inherit:
        print(f"Inherit folder: {result.inherited_folder}")

    # Get thread context for classification
    context = await manager.get_thread_context(
        conversation_id="AAQk...",
        exclude_message_id="AAMk...",
    )

    # Get sender folder patterns
    history = await manager.get_sender_history("sender@example.com")
    if history.has_strong_pattern():
        print(history.format_for_prompt())
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

import regex

from assistant.core.logging import get_logger

if TYPE_CHECKING:
    from assistant.classifier.snippet import SnippetCleaner
    from assistant.db.store import DatabaseStore
    from assistant.graph.messages import MessageManager

logger = get_logger(__name__)

# Regex timeout for security (used in match operations)
REGEX_TIMEOUT = 1.0

# Subject prefix pattern for normalization (Re:, Fwd:, FW:, etc.)
# Note: timeout is passed at match time (sub, search), not compile time
SUBJECT_PREFIX_PATTERN = regex.compile(r"^(Re:|RE:|Fwd:|FWD:|FW:|Fw:)\s*")

# Default confidence for inherited folders
INHERITANCE_CONFIDENCE = 0.95


@dataclass
class ThreadMessage:
    """A message in a thread for classification context.

    Attributes:
        message_id: Graph API message ID
        sender_email: Sender's email address
        sender_name: Sender's display name (if available)
        subject: Email subject
        received_at: When the message was received
        snippet: Cleaned body snippet (max 500 chars)
    """

    message_id: str
    sender_email: str
    sender_name: str | None
    subject: str
    received_at: datetime
    snippet: str


@dataclass
class ThreadContext:
    """Context for an email thread including prior messages.

    Attributes:
        conversation_id: Graph API conversation ID
        messages: Prior messages in thread (up to 3, newest first)
        thread_depth: Reply depth (0 = original, 1+ = replies)
        unique_domains: All sender domains appearing in thread
    """

    conversation_id: str
    messages: list[ThreadMessage] = field(default_factory=list)
    thread_depth: int = 0
    unique_domains: set[str] = field(default_factory=set)


@dataclass
class InheritanceResult:
    """Result of thread inheritance check.

    Attributes:
        should_inherit: Whether to inherit folder from prior message
        inherited_folder: Folder to inherit (if should_inherit is True)
        confidence: Confidence score (0.95 for inherited)
        reason: Explanation for debugging
    """

    should_inherit: bool
    inherited_folder: str | None
    confidence: float
    reason: str

    @staticmethod
    def inherit(folder: str, confidence: float = INHERITANCE_CONFIDENCE) -> InheritanceResult:
        """Create an inheritance result that inherits the folder.

        Args:
            folder: Folder path to inherit
            confidence: Confidence score (default 0.95)

        Returns:
            InheritanceResult indicating inheritance
        """
        return InheritanceResult(
            should_inherit=True,
            inherited_folder=folder,
            confidence=confidence,
            reason="Thread continues with same topic and participants",
        )

    @staticmethod
    def no_inherit(reason: str) -> InheritanceResult:
        """Create an inheritance result that does not inherit.

        Args:
            reason: Why inheritance doesn't apply

        Returns:
            InheritanceResult indicating no inheritance
        """
        return InheritanceResult(
            should_inherit=False,
            inherited_folder=None,
            confidence=0.0,
            reason=reason,
        )


@dataclass
class SenderHistoryResult:
    """Result of sender history lookup with analysis.

    Attributes:
        sender_email: Sender's email address
        total_emails: Total emails from this sender
        folder_distribution: Count per folder
        dominant_folder: Folder with >80% of emails (if any)
        dominant_percentage: Percentage going to dominant folder
    """

    sender_email: str
    total_emails: int
    folder_distribution: dict[str, int] = field(default_factory=dict)
    dominant_folder: str | None = None
    dominant_percentage: float = 0.0

    def has_strong_pattern(
        self,
        min_emails: int = 5,
        min_percentage: float = 0.8,
    ) -> bool:
        """Check if there's a strong historical pattern.

        Args:
            min_emails: Minimum emails required
            min_percentage: Minimum percentage for dominant folder

        Returns:
            True if pattern is strong enough to inform classification
        """
        return self.total_emails >= min_emails and self.dominant_percentage >= min_percentage

    def format_for_prompt(self) -> str | None:
        """Format sender history for Claude prompt.

        Returns:
            Formatted string for prompt, or None if pattern is weak
        """
        if not self.has_strong_pattern():
            return None

        percentage = int(self.dominant_percentage * 100)
        count = self.folder_distribution.get(self.dominant_folder, 0)

        return (
            f"{percentage}% of emails from this sender are classified to "
            f"{self.dominant_folder} ({count}/{self.total_emails} emails)"
        )


class ThreadContextManager:
    """Manages thread context operations for the triage engine.

    Handles:
    - Thread inheritance checking (should we inherit folder from prior message?)
    - Thread context fetching (get prior messages for Claude)
    - Sender history lookup (historical folder patterns)

    Uses local database first, falls back to Graph API when needed.

    Attributes:
        store: DatabaseStore for local queries
        message_manager: MessageManager for Graph API fallback
        snippet_cleaner: SnippetCleaner for cleaning context snippets
    """

    def __init__(
        self,
        store: DatabaseStore,
        message_manager: MessageManager,
        snippet_cleaner: SnippetCleaner,
    ):
        """Initialize the thread context manager.

        Args:
            store: DatabaseStore for local email queries
            message_manager: MessageManager for Graph API queries
            snippet_cleaner: SnippetCleaner for cleaning context snippets
        """
        self._store = store
        self._message_manager = message_manager
        self._snippet_cleaner = snippet_cleaner

    async def check_thread_inheritance(
        self,
        conversation_id: str,
        current_subject: str,
        current_sender_domain: str,
    ) -> InheritanceResult:
        """Check if folder can be inherited from prior thread classification.

        Logic:
        1. Query for prior approved classification in same conversation
        2. If found, check for significant changes:
           - Subject changed (not just Re:/Fwd:)? -> No inherit
           - New sender domain in thread? -> No inherit
           - Otherwise -> Inherit with confidence 0.95

        Note: Even when inheriting folder, priority and action_type should
        still be classified by Claude (they can change within a thread).

        Args:
            conversation_id: Graph API conversation ID
            current_subject: Current email subject
            current_sender_domain: Domain of current email sender

        Returns:
            InheritanceResult indicating whether to inherit and why
        """
        # Get prior classification from database
        prior = await self._store.get_thread_classification(conversation_id)
        if not prior:
            return InheritanceResult.no_inherit("No prior classification in thread")

        prior_folder, _ = prior

        # Get prior emails to check for significant changes
        prior_emails = await self._store.get_thread_emails(conversation_id, limit=10)
        if not prior_emails:
            return InheritanceResult.no_inherit("No prior emails found in thread")

        # Check subject change (ignoring Re:/Fwd: prefixes)
        normalized_current = normalize_subject(current_subject)
        prior_subjects = {normalize_subject(e.subject or "") for e in prior_emails}

        # If subject is significantly different from all prior subjects
        if prior_subjects and normalized_current not in prior_subjects:
            # Check if it's truly different (not just empty)
            if normalized_current and all(prior_subjects):
                return InheritanceResult.no_inherit(
                    f"Subject changed: '{normalized_current}' not in prior subjects"
                )

        # Check for new sender domain
        prior_domains = {
            extract_domain(e.sender_email) for e in prior_emails if e.sender_email
        }
        current_domain_lower = current_sender_domain.lower()

        if prior_domains and current_domain_lower not in prior_domains:
            return InheritanceResult.no_inherit(
                f"New participant domain: {current_domain_lower} not in {prior_domains}"
            )

        # All checks passed - inherit the folder
        logger.debug(
            "Thread inheritance applied",
            conversation_id=conversation_id[:20] + "...",
            inherited_folder=prior_folder,
        )

        return InheritanceResult.inherit(prior_folder)

    async def get_thread_context(
        self,
        conversation_id: str,
        exclude_message_id: str,
        max_messages: int = 3,
    ) -> ThreadContext:
        """Get thread context for classification.

        Fetches prior messages in the thread:
        1. First checks local database for already-processed messages
        2. Falls back to Graph API if not enough local context

        Args:
            conversation_id: Graph API conversation ID
            exclude_message_id: Current message ID to exclude
            max_messages: Maximum prior messages to return (default 3)

        Returns:
            ThreadContext with prior messages and metadata
        """
        messages: list[ThreadMessage] = []
        unique_domains: set[str] = set()
        thread_depth = 0

        # First, try to get from local database
        local_emails = await self._store.get_thread_emails(
            conversation_id,
            exclude_id=exclude_message_id,
            limit=max_messages + 1,  # Extra for depth calculation
        )

        # Convert local emails to ThreadMessage objects
        for email in local_emails[:max_messages]:
            snippet = self._snippet_cleaner.clean_for_context(
                email.snippet,
                is_html=False,  # Snippets in DB are already plain text
            )

            messages.append(
                ThreadMessage(
                    message_id=email.id,
                    sender_email=email.sender_email or "",
                    sender_name=email.sender_name,
                    subject=email.subject or "",
                    received_at=email.received_at or datetime.now(),
                    snippet=snippet,
                )
            )

            if email.sender_email:
                unique_domains.add(extract_domain(email.sender_email))

            # Calculate thread depth from conversation index
            if email.conversation_index:
                depth = calculate_thread_depth(email.conversation_index)
                thread_depth = max(thread_depth, depth)

        # If we don't have enough messages locally, fetch from Graph API
        if len(messages) < max_messages:
            try:
                api_messages = self._message_manager.get_thread_messages(
                    conversation_id,
                    max_messages=max_messages + 1,  # +1 to exclude current
                )

                # Filter out current message and ones we already have
                local_ids = {m.message_id for m in messages}
                for msg in api_messages:
                    msg_id = msg.get("id", "")
                    if msg_id == exclude_message_id or msg_id in local_ids:
                        continue

                    if len(messages) >= max_messages:
                        break

                    # Extract sender info
                    from_data = msg.get("from", {}).get("emailAddress", {})
                    sender_email = from_data.get("address", "")
                    sender_name = from_data.get("name")

                    # Clean the snippet
                    snippet = self._snippet_cleaner.clean_for_context(
                        msg.get("bodyPreview", ""),
                        is_html=False,
                    )

                    # Parse received time
                    received_str = msg.get("receivedDateTime", "")
                    try:
                        received_at = datetime.fromisoformat(
                            received_str.replace("Z", "+00:00")
                        )
                    except ValueError:
                        received_at = datetime.now()

                    messages.append(
                        ThreadMessage(
                            message_id=msg_id,
                            sender_email=sender_email,
                            sender_name=sender_name,
                            subject=msg.get("subject", ""),
                            received_at=received_at,
                            snippet=snippet,
                        )
                    )

                    if sender_email:
                        unique_domains.add(extract_domain(sender_email))

                    # Calculate thread depth
                    conv_index = msg.get("conversationIndex", "")
                    if conv_index:
                        depth = calculate_thread_depth(conv_index)
                        thread_depth = max(thread_depth, depth)

            except Exception as e:
                # Log but don't fail - empty context is acceptable
                logger.warning(
                    "Failed to fetch thread context from Graph API",
                    conversation_id=conversation_id[:20] + "...",
                    error=str(e),
                )

        return ThreadContext(
            conversation_id=conversation_id,
            messages=messages,
            thread_depth=thread_depth,
            unique_domains=unique_domains,
        )

    async def get_sender_history(self, sender_email: str) -> SenderHistoryResult:
        """Get folder distribution history for a sender.

        Analyzes historical classifications for this sender to identify
        patterns that can inform classification.

        Args:
            sender_email: Sender email address

        Returns:
            SenderHistoryResult with folder distribution and analysis
        """
        # Get raw history from database
        history = await self._store.get_sender_history(sender_email)

        # Find dominant folder (>80% of emails)
        dominant_folder: str | None = None
        dominant_percentage: float = 0.0

        if history.total_emails > 0:
            for folder, count in history.folder_distribution.items():
                percentage = count / history.total_emails
                if percentage > dominant_percentage:
                    dominant_percentage = percentage
                    dominant_folder = folder

        return SenderHistoryResult(
            sender_email=sender_email.lower(),
            total_emails=history.total_emails,
            folder_distribution=history.folder_distribution,
            dominant_folder=dominant_folder,
            dominant_percentage=dominant_percentage,
        )


def normalize_subject(subject: str) -> str:
    """Normalize subject by removing Re:/Fwd: prefixes.

    Args:
        subject: Email subject

    Returns:
        Normalized subject for comparison
    """
    if not subject:
        return ""

    try:
        # Remove all Re:/Fwd: prefixes (can be chained)
        normalized = subject
        while True:
            new_normalized = SUBJECT_PREFIX_PATTERN.sub("", normalized, timeout=REGEX_TIMEOUT)
            if new_normalized == normalized:
                break
            normalized = new_normalized
        return normalized.strip().lower()
    except (regex.error, TimeoutError):
        # Timeout or error - return original stripped and lowercased
        return subject.strip().lower()


def extract_domain(email: str) -> str:
    """Extract domain from email address.

    Args:
        email: Email address

    Returns:
        Lowercase domain, or empty string if invalid
    """
    if not email or "@" not in email:
        return ""
    return email.split("@")[1].lower()


def calculate_thread_depth(conversation_index: str) -> int:
    """Calculate thread depth from conversation index.

    The conversationIndex is a base64-encoded binary value where:
    - First 22 bytes = thread root identifier
    - Each subsequent 5 bytes = one reply level

    Args:
        conversation_index: Base64-encoded conversation index

    Returns:
        Thread depth (0 = original message, 1+ = reply depth)
    """
    if not conversation_index:
        return 0

    try:
        # Decode base64
        decoded = base64.b64decode(conversation_index)
        # First 22 bytes = root, each 5 bytes after = reply level
        if len(decoded) <= 22:
            return 0
        return (len(decoded) - 22) // 5
    except Exception:
        return 0
