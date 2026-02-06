"""Microsoft Graph API client module.

Provides a robust client for interacting with Microsoft Graph API including:
- Base client with retry logic and error handling
- Folder operations (list, create, find by path)
- Message operations (list, move, set categories)
- Sent items cache for reply state detection

Usage:
    from assistant.auth import GraphAuth
    from assistant.graph import GraphClient, FolderManager, MessageManager

    # Initialize
    auth = GraphAuth(client_id, tenant_id, scopes, cache_path)
    client = GraphClient(auth)
    folders = FolderManager(client)
    messages = MessageManager(client)

    # Use the managers
    inbox_messages = messages.list_messages("Inbox")
    folder = folders.create_folder("Projects/NewProject")
"""

from assistant.graph.client import GraphClient
from assistant.graph.folders import FolderManager
from assistant.graph.messages import MessageManager, SentItemsCache

__all__ = [
    "GraphClient",
    "FolderManager",
    "MessageManager",
    "SentItemsCache",
]
