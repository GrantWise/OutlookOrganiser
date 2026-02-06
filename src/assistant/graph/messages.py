"""Email message operations for Microsoft Graph API.

This module provides functions for managing Outlook email messages including:
- Listing messages from folders (with pagination)
- Moving messages between folders
- Setting categories on messages
- Fetching message details
- SentItemsCache for efficient reply state detection

Usage:
    from assistant.graph.client import GraphClient
    from assistant.graph.messages import MessageManager

    client = GraphClient(auth)
    messages = MessageManager(client)

    # List inbox messages
    inbox_messages = messages.list_messages("Inbox")

    # Move a message
    messages.move_message(message_id, destination_folder_id)

    # Set categories
    messages.set_categories(message_id, ["P1 - Urgent Important", "Needs Reply"])
"""

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from assistant.core.errors import ConflictError
from assistant.core.logging import get_logger

if TYPE_CHECKING:
    from assistant.graph.client import GraphClient

logger = get_logger(__name__)

# Maximum retries for optimistic concurrency conflicts
MAX_CONFLICT_RETRIES = 3

# Default select fields for email messages (per spec 05-graph-api.md)
DEFAULT_MESSAGE_FIELDS = (
    "id,conversationId,conversationIndex,subject,from,receivedDateTime,"
    "bodyPreview,parentFolderId,categories,webLink,flag,isRead,importance"
)


class MessageManager:
    """Manages email message operations via Microsoft Graph API.

    Provides methods for:
    - Listing messages from folders
    - Moving messages between folders
    - Setting categories (for priority, action type)
    - Fetching individual message details
    - Thread context queries

    Attributes:
        client: GraphClient instance for API calls
    """

    def __init__(self, client: "GraphClient"):
        """Initialize the MessageManager.

        Args:
            client: GraphClient instance for making API calls
        """
        self.client = client

    def list_messages(
        self,
        folder: str = "Inbox",
        select: str | None = None,
        filter_query: str | None = None,
        order_by: str = "receivedDateTime desc",
        top: int = 50,
        max_items: int | None = None,
        delay_between_pages: float = 0.0,
    ) -> list[dict[str, Any]]:
        """List messages from a mail folder.

        Args:
            folder: Folder name or ID (e.g., "Inbox", "SentItems", or folder ID)
            select: Fields to select (defaults to DEFAULT_MESSAGE_FIELDS)
            filter_query: OData filter query (e.g., "isRead eq false")
            order_by: Sort order (default: receivedDateTime desc)
            top: Items per page (max 50)
            max_items: Maximum total items to fetch (None for all)
            delay_between_pages: Delay between pagination requests (seconds)

        Returns:
            List of message dictionaries

        Example:
            # Get unread messages from inbox
            messages = message_manager.list_messages(
                folder="Inbox",
                filter_query="isRead eq false",
                top=20,
            )

            # Get messages from last 7 days
            messages = message_manager.list_messages(
                folder="Inbox",
                filter_query="receivedDateTime ge 2024-01-01T00:00:00Z",
            )
        """
        # Map well-known folder names to their endpoints
        folder_endpoint = self._get_folder_endpoint(folder)

        params: dict[str, Any] = {
            "$select": select or DEFAULT_MESSAGE_FIELDS,
            "$orderby": order_by,
            "$top": min(top, 50),
        }

        if filter_query:
            params["$filter"] = filter_query

        # Calculate max pages if max_items specified
        max_pages = None
        if max_items:
            max_pages = (max_items + top - 1) // top  # Ceiling division

        logger.debug(
            "Listing messages",
            folder=folder,
            filter=filter_query,
            max_items=max_items,
        )

        messages = self.client.paginate(
            f"/me/mailFolders/{folder_endpoint}/messages",
            params=params,
            page_size=top,
            max_pages=max_pages,
            delay_between_pages=delay_between_pages,
        )

        # Trim to exact max_items if specified
        if max_items and len(messages) > max_items:
            messages = messages[:max_items]

        logger.info(
            "Messages listed",
            folder=folder,
            count=len(messages),
        )

        return messages

    def _get_folder_endpoint(self, folder: str) -> str:
        """Convert folder name to Graph API endpoint path.

        Args:
            folder: Folder name or ID

        Returns:
            Folder endpoint for Graph API
        """
        # Well-known folder names
        well_known = {
            "inbox": "inbox",
            "sentitems": "sentitems",
            "sent items": "sentitems",
            "drafts": "drafts",
            "deleteditems": "deleteditems",
            "deleted items": "deleteditems",
            "archive": "archive",
            "junkemail": "junkemail",
            "junk email": "junkemail",
        }

        folder_lower = folder.lower()
        if folder_lower in well_known:
            return well_known[folder_lower]

        # Assume it's a folder ID
        return folder

    def get_message(
        self,
        message_id: str,
        select: str | None = None,
    ) -> dict[str, Any]:
        """Get a single message by ID.

        Args:
            message_id: Graph API message ID
            select: Fields to select (defaults to DEFAULT_MESSAGE_FIELDS)

        Returns:
            Message dictionary

        Raises:
            GraphAPIError: If message not found or request fails
        """
        params = {"$select": select or DEFAULT_MESSAGE_FIELDS}

        return self.client.get(f"/me/messages/{message_id}", params=params)

    def move_message(
        self,
        message_id: str,
        destination_folder_id: str,
    ) -> dict[str, Any]:
        """Move a message to a different folder.

        This operation is idempotent: if the message is already in the
        destination folder, it returns the current message state without
        making an API call to move it.

        Args:
            message_id: Graph API message ID
            destination_folder_id: ID of the destination folder

        Returns:
            Updated message dictionary

        Raises:
            GraphAPIError: If move fails

        Example:
            # Move message to a folder
            result = message_manager.move_message(
                message_id="AAMkAGI...",
                destination_folder_id="AAMkAGI...",
            )
        """
        # Check if message is already in destination folder (idempotency check)
        message = self.get_message(message_id, select="id,parentFolderId")
        current_folder_id = message.get("parentFolderId")

        if current_folder_id == destination_folder_id:
            logger.info(
                "Message already in destination folder, skipping move",
                message_id=message_id[:20] + "...",
                folder_id=destination_folder_id[:20] + "...",
            )
            return message

        logger.info(
            "Moving message",
            message_id=message_id[:20] + "...",
            destination_folder_id=destination_folder_id[:20] + "...",
        )

        response = self.client.post(
            f"/me/messages/{message_id}/move",
            json={"destinationId": destination_folder_id},
        )

        logger.info(
            "Message moved",
            message_id=message_id[:20] + "...",
            new_parent_folder_id=response.get("parentFolderId", "")[:20] + "...",
        )

        return response

    def set_categories(
        self,
        message_id: str,
        categories: list[str],
    ) -> dict[str, Any]:
        """Set categories on a message.

        Categories are used for priority (P1-P4) and action type labels.
        This replaces any existing categories on the message.

        Args:
            message_id: Graph API message ID
            categories: List of category strings

        Returns:
            Updated message dictionary

        Raises:
            GraphAPIError: If update fails

        Example:
            message_manager.set_categories(
                message_id="AAMkAGI...",
                categories=["P1 - Urgent Important", "Needs Reply"],
            )
        """
        logger.debug(
            "Setting message categories",
            message_id=message_id[:20] + "...",
            categories=categories,
        )

        response = self.client.patch(
            f"/me/messages/{message_id}",
            json={"categories": categories},
        )

        logger.info(
            "Message categories updated",
            message_id=message_id[:20] + "...",
            categories=categories,
        )

        return response

    def add_categories(
        self,
        message_id: str,
        new_categories: list[str],
    ) -> dict[str, Any]:
        """Add categories to a message without removing existing ones.

        Uses optimistic concurrency with ETags to handle race conditions.
        If the message is modified by another client between read and write,
        this method will retry with fresh data (up to MAX_CONFLICT_RETRIES times).

        Args:
            message_id: Graph API message ID
            new_categories: Categories to add

        Returns:
            Updated message dictionary

        Raises:
            ConflictError: If max retries exceeded due to concurrent modifications
            GraphAPIError: If the update fails for other reasons
        """
        for attempt in range(MAX_CONFLICT_RETRIES):
            # Get current categories with ETag for optimistic concurrency
            message = self.client.get(
                f"/me/messages/{message_id}",
                params={"$select": "categories"},
            )
            existing = message.get("categories", [])
            etag = message.get("@odata.etag")

            # Merge categories (preserve order, no duplicates)
            merged = list(existing)
            for cat in new_categories:
                if cat not in merged:
                    merged.append(cat)

            # If no changes needed, return early
            if merged == existing:
                logger.debug(
                    "Categories already present, no update needed",
                    message_id=message_id[:20] + "...",
                )
                return message

            try:
                # Update with ETag for optimistic concurrency
                logger.debug(
                    "Updating message categories with ETag",
                    message_id=message_id[:20] + "...",
                    categories=merged,
                    attempt=attempt + 1,
                )

                response = self.client.patch(
                    f"/me/messages/{message_id}",
                    json={"categories": merged},
                    if_match=etag,
                )

                logger.info(
                    "Message categories updated",
                    message_id=message_id[:20] + "...",
                    categories=merged,
                )
                return response

            except ConflictError:
                if attempt < MAX_CONFLICT_RETRIES - 1:
                    logger.warning(
                        "Category update conflict, retrying with fresh data",
                        message_id=message_id[:20] + "...",
                        attempt=attempt + 1,
                        max_retries=MAX_CONFLICT_RETRIES,
                    )
                    continue
                raise ConflictError(
                    f"Failed to update categories after {MAX_CONFLICT_RETRIES} attempts "
                    "due to concurrent modifications. The message is being modified by "
                    "another client.",
                    resource_id=message_id,
                ) from None

        # Should not reach here, but just in case
        raise ConflictError(
            f"Failed to update categories after {MAX_CONFLICT_RETRIES} attempts",
            resource_id=message_id,
        )

    def get_thread_messages(
        self,
        conversation_id: str,
        max_messages: int = 4,
        select: str | None = None,
    ) -> list[dict[str, Any]]:
        """Get messages in a conversation thread.

        Useful for fetching thread context before classification.

        Args:
            conversation_id: Outlook conversation ID
            max_messages: Maximum messages to return (default: 4)
            select: Fields to select

        Returns:
            List of messages in the thread, ordered by receivedDateTime desc

        Example:
            thread = message_manager.get_thread_messages(
                conversation_id="AAQkAGI...",
                max_messages=4,
            )
        """
        params = {
            "$filter": f"conversationId eq '{conversation_id}'",
            "$orderby": "receivedDateTime desc",
            "$top": max_messages,
            "$select": select or "id,subject,from,receivedDateTime,bodyPreview",
        }

        response = self.client.get("/me/messages", params=params)

        messages = response.get("value", [])

        logger.debug(
            "Thread messages fetched",
            conversation_id=conversation_id[:20] + "...",
            count=len(messages),
        )

        return messages

    def check_reply_state(
        self,
        conversation_id: str,
        sent_cache: "SentItemsCache | None" = None,
    ) -> dict[str, Any] | None:
        """Check if the user has replied to a conversation.

        Prefers using the SentItemsCache for efficiency (O(1) lookup vs O(1) API call).
        Falls back to direct Graph API query if cache is not provided.

        For best performance, pass a SentItemsCache that's refreshed at the start
        of each triage cycle.

        Args:
            conversation_id: Outlook conversation ID
            sent_cache: Optional SentItemsCache for efficient lookup

        Returns:
            Dict with receivedDateTime if reply found, None otherwise
        """
        # Prefer cache for efficiency (avoids N+1 API calls)
        if sent_cache is not None:
            if sent_cache.has_replied(conversation_id):
                reply_time = sent_cache.get_last_reply_time(conversation_id)
                if reply_time:
                    return {"receivedDateTime": reply_time.isoformat()}
                return {"receivedDateTime": "unknown"}
            return None

        # Fallback to direct API call (less efficient)
        logger.debug(
            "check_reply_state: no cache provided, using direct API call",
            conversation_id=conversation_id[:20] + "...",
        )
        params = {
            "$filter": f"conversationId eq '{conversation_id}'",
            "$orderby": "receivedDateTime desc",
            "$top": 1,
            "$select": "receivedDateTime",
        }

        response = self.client.get("/me/mailFolders/sentitems/messages", params=params)

        messages = response.get("value", [])
        if messages:
            return messages[0]

        return None


class SentItemsCache:
    """Cache for sent items to efficiently detect reply state.

    Instead of making a Graph API call per thread to check if the user
    has replied, this cache batch-fetches recent sent items and stores
    their conversation IDs for quick lookup.

    Usage:
        cache = SentItemsCache(message_manager)

        # Refresh at start of triage cycle
        cache.refresh(hours=24)

        # Check if user has replied to a thread
        if cache.has_replied(conversation_id):
            print("User has replied to this thread")
    """

    def __init__(self, message_manager: MessageManager):
        """Initialize the sent items cache.

        Args:
            message_manager: MessageManager instance for API calls
        """
        self.message_manager = message_manager
        self._conversation_ids: set[str] = set()
        self._last_sent_times: dict[str, datetime] = {}
        self._last_refresh: datetime | None = None
        self._refresh_hours: int = 24

    def refresh(self, hours: int = 24) -> int:
        """Refresh the cache with recent sent items.

        Fetches sent items from the last N hours and caches their
        conversation IDs for quick reply state lookup.

        Args:
            hours: Number of hours of sent items to fetch

        Returns:
            Number of sent items cached
        """
        self._refresh_hours = hours

        # Calculate cutoff time
        cutoff = datetime.now(UTC) - timedelta(hours=hours)
        cutoff_str = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")

        logger.debug("Refreshing sent items cache", hours=hours, cutoff=cutoff_str)

        # Fetch recent sent items
        sent_items = self.message_manager.list_messages(
            folder="SentItems",
            select="conversationId,receivedDateTime",
            filter_query=f"receivedDateTime ge {cutoff_str}",
            order_by="receivedDateTime desc",
        )

        # Build the cache
        self._conversation_ids = set()
        self._last_sent_times = {}

        for item in sent_items:
            conv_id = item.get("conversationId")
            if conv_id:
                self._conversation_ids.add(conv_id)

                # Track the most recent send time per conversation
                received = item.get("receivedDateTime")
                if received:
                    try:
                        dt = datetime.fromisoformat(received.replace("Z", "+00:00"))
                        if (
                            conv_id not in self._last_sent_times
                            or dt > self._last_sent_times[conv_id]
                        ):
                            self._last_sent_times[conv_id] = dt
                    except ValueError:
                        pass  # Skip invalid dates

        self._last_refresh = datetime.now(UTC)

        logger.info(
            "Sent items cache refreshed",
            conversations=len(self._conversation_ids),
            items=len(sent_items),
            hours=hours,
        )

        return len(self._conversation_ids)

    def has_replied(self, conversation_id: str) -> bool:
        """Check if the user has replied to a conversation.

        Args:
            conversation_id: Outlook conversation ID

        Returns:
            True if a sent item exists for this conversation
        """
        return conversation_id in self._conversation_ids

    def get_last_reply_time(self, conversation_id: str) -> datetime | None:
        """Get the time of the most recent reply in a conversation.

        Args:
            conversation_id: Outlook conversation ID

        Returns:
            DateTime of last reply, or None if no reply found
        """
        return self._last_sent_times.get(conversation_id)

    def is_stale(self, max_age_minutes: int = 30) -> bool:
        """Check if the cache needs refreshing.

        Args:
            max_age_minutes: Maximum age in minutes before considered stale

        Returns:
            True if cache is stale and should be refreshed
        """
        if self._last_refresh is None:
            return True

        age = datetime.now(UTC) - self._last_refresh
        return age > timedelta(minutes=max_age_minutes)

    @property
    def conversation_count(self) -> int:
        """Number of conversations in the cache."""
        return len(self._conversation_ids)

    @property
    def last_refresh(self) -> datetime | None:
        """Time of last cache refresh."""
        return self._last_refresh
