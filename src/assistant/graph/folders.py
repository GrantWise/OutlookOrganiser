"""Mail folder operations for Microsoft Graph API.

This module provides functions for managing Outlook mail folders including:
- Listing folders (with nested child folders)
- Creating top-level folders
- Creating subfolders
- Finding folders by path
- Building folder path hierarchies

Usage:
    from assistant.graph.client import GraphClient
    from assistant.graph.folders import FolderManager

    client = GraphClient(auth)
    folders = FolderManager(client)

    # List all folders
    all_folders = folders.list_folders()

    # Create a folder
    new_folder = folders.create_folder("Projects")

    # Create a nested subfolder
    subfolder = folders.create_folder("Projects/NewProject")

    # Find a folder by path
    folder = folders.get_folder_by_path("Projects/NewProject")
"""

import threading
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from assistant.core.errors import GraphAPIError
from assistant.core.logging import get_logger

if TYPE_CHECKING:
    from assistant.graph.client import GraphClient

logger = get_logger(__name__)

# Default cache TTL (1 hour)
DEFAULT_CACHE_TTL_SECONDS = 3600


class FolderManager:
    """Manages mail folder operations via Microsoft Graph API.

    Provides a high-level interface for folder operations including:
    - Listing folders with child folder expansion
    - Creating folders at any depth
    - Finding folders by path (e.g., "Projects/Example")
    - Building folder ID to path mappings

    The class caches folder information after listing to avoid repeated API calls.
    Cache has a configurable TTL (default 1 hour) and thread-safe access.
    Call refresh_cache() to force a refresh.

    Attributes:
        client: GraphClient instance for API calls
        _folder_cache: Cached folder data (list of folder dicts)
        _path_to_id: Mapping of folder paths to folder IDs
        _id_to_path: Mapping of folder IDs to folder paths
        _cache_lock: Thread lock for cache access
        _cache_timestamp: When the cache was last refreshed
        _cache_ttl_seconds: Cache TTL in seconds
    """

    def __init__(
        self,
        client: "GraphClient",
        cache_ttl_seconds: int = DEFAULT_CACHE_TTL_SECONDS,
    ):
        """Initialize the FolderManager.

        Args:
            client: GraphClient instance for making API calls
            cache_ttl_seconds: Cache TTL in seconds (default: 3600 = 1 hour)
        """
        self.client = client
        self._folder_cache: list[dict[str, Any]] | None = None
        self._path_to_id: dict[str, str] = {}
        self._id_to_path: dict[str, str] = {}
        self._cache_lock = threading.Lock()
        self._cache_timestamp: datetime | None = None
        self._cache_ttl_seconds = cache_ttl_seconds

    def _is_cache_expired(self) -> bool:
        """Check if the folder cache has expired.

        Returns:
            True if cache is expired or doesn't exist
        """
        if self._cache_timestamp is None:
            return True
        age = datetime.now(UTC) - self._cache_timestamp
        return age > timedelta(seconds=self._cache_ttl_seconds)

    def list_folders(self, force_refresh: bool = False) -> list[dict[str, Any]]:
        """List all mail folders with child folders expanded.

        Returns a flat list of all folders including nested subfolders.
        Uses cached data if available unless force_refresh is True or cache has expired.

        Thread-safe: uses a lock to prevent concurrent cache mutations.

        Args:
            force_refresh: If True, fetch fresh data from API

        Returns:
            List of folder dictionaries with keys:
            - id: Folder ID
            - displayName: Folder name
            - parentFolderId: Parent folder ID (None for root folders)
            - childFolderCount: Number of child folders
            - totalItemCount: Total items in folder
            - unreadItemCount: Unread items in folder

        Example:
            folders = folder_manager.list_folders()
            for folder in folders:
                print(f"{folder['displayName']}: {folder['totalItemCount']} items")
        """
        with self._cache_lock:
            # Check if cache is valid
            if not force_refresh and self._folder_cache is not None:
                if not self._is_cache_expired():
                    return self._folder_cache
                else:
                    logger.info("Folder cache expired, refreshing")

            logger.debug("Fetching mail folders from Graph API")

            # Fetch top-level folders with child folders expanded
            # Note: $expand only goes one level deep, so we recurse for deeper nesting
            response = self.client.get(
                "/me/mailFolders",
                params={
                    "$expand": "childFolders",
                    "$top": 100,  # Get more folders per page
                },
            )

            folders = response.get("value", [])

            # Flatten the folder hierarchy
            all_folders = self._flatten_folders(folders)

            # Build path mappings
            self._build_path_mappings(all_folders)

            # Cache the results with timestamp
            self._folder_cache = all_folders
            self._cache_timestamp = datetime.now(UTC)

            logger.info(
                "Mail folders loaded",
                folder_count=len(all_folders),
                paths=list(self._path_to_id.keys())[:10],  # Log first 10 paths
            )

            return all_folders

    def _flatten_folders(
        self,
        folders: list[dict[str, Any]],
        parent_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Recursively flatten the folder hierarchy.

        Args:
            folders: List of folder dicts (may contain childFolders)
            parent_id: ID of the parent folder (None for root)

        Returns:
            Flat list of all folders
        """
        result: list[dict[str, Any]] = []

        for folder in folders:
            # Add the folder itself (without childFolders to avoid duplication)
            folder_copy = {
                "id": folder["id"],
                "displayName": folder["displayName"],
                "parentFolderId": parent_id,
                "childFolderCount": folder.get("childFolderCount", 0),
                "totalItemCount": folder.get("totalItemCount", 0),
                "unreadItemCount": folder.get("unreadItemCount", 0),
            }
            result.append(folder_copy)

            # Process child folders if present
            child_folders = folder.get("childFolders", [])
            if child_folders:
                result.extend(self._flatten_folders(child_folders, parent_id=folder["id"]))
            elif folder.get("childFolderCount", 0) > 0:
                # Need to fetch child folders separately (expand didn't include them)
                result.extend(self._fetch_child_folders(folder["id"]))

        return result

    def _fetch_child_folders(self, parent_id: str) -> list[dict[str, Any]]:
        """Fetch child folders for a given parent folder.

        Args:
            parent_id: ID of the parent folder

        Returns:
            Flat list of child folders (recursively flattened)
        """
        try:
            response = self.client.get(
                f"/me/mailFolders/{parent_id}/childFolders",
                params={"$expand": "childFolders"},
            )
            child_folders = response.get("value", [])
            return self._flatten_folders(child_folders, parent_id=parent_id)
        except GraphAPIError as e:
            logger.warning(
                "Failed to fetch child folders",
                parent_id=parent_id,
                error=str(e),
            )
            return []

    def _build_path_mappings(self, folders: list[dict[str, Any]]) -> None:
        """Build mappings between folder paths and IDs.

        Creates bidirectional mappings for quick lookup:
        - _path_to_id: "Projects/Example" -> "folder_id"
        - _id_to_path: "folder_id" -> "Projects/Example"

        Args:
            folders: Flat list of all folders
        """
        self._path_to_id = {}
        self._id_to_path = {}

        # Build id -> folder lookup
        id_to_folder = {f["id"]: f for f in folders}

        for folder in folders:
            path = self._build_path(folder, id_to_folder)
            self._path_to_id[path] = folder["id"]
            self._id_to_path[folder["id"]] = path

    def _build_path(
        self,
        folder: dict[str, Any],
        id_to_folder: dict[str, dict[str, Any]],
    ) -> str:
        """Build the full path for a folder by traversing parents.

        Args:
            folder: Folder dictionary
            id_to_folder: Mapping of folder IDs to folder dicts

        Returns:
            Full path like "Projects/Example"
        """
        parts = [folder["displayName"]]
        parent_id = folder.get("parentFolderId")

        # Traverse up the hierarchy
        while parent_id and parent_id in id_to_folder:
            parent = id_to_folder[parent_id]
            parts.insert(0, parent["displayName"])
            parent_id = parent.get("parentFolderId")

        return "/".join(parts)

    def get_folder_by_path(self, path: str) -> dict[str, Any] | None:
        """Find a folder by its path.

        Args:
            path: Folder path like "Projects/Example" or "Inbox"

        Returns:
            Folder dictionary if found, None otherwise

        Example:
            folder = folder_manager.get_folder_by_path("Projects/NewProject")
            if folder:
                print(f"Folder ID: {folder['id']}")
        """
        # Ensure cache is populated
        self.list_folders()

        folder_id = self._path_to_id.get(path)
        if not folder_id:
            return None

        # Return the cached folder data
        for folder in self._folder_cache or []:
            if folder["id"] == folder_id:
                return folder

        return None

    def get_folder_id(self, path: str) -> str | None:
        """Get the folder ID for a given path.

        Args:
            path: Folder path like "Projects/Example"

        Returns:
            Folder ID if found, None otherwise
        """
        # Ensure cache is populated
        self.list_folders()
        return self._path_to_id.get(path)

    def get_folder_path(self, folder_id: str) -> str | None:
        """Get the folder path for a given folder ID.

        Args:
            folder_id: Microsoft Graph folder ID

        Returns:
            Folder path if found, None otherwise
        """
        # Ensure cache is populated
        self.list_folders()
        return self._id_to_path.get(folder_id)

    def create_folder(self, path: str) -> dict[str, Any]:
        """Create a folder at the specified path.

        Creates any missing parent folders as needed. For example,
        creating "Projects/NewProject" will first create "Projects"
        if it doesn't exist.

        Args:
            path: Folder path like "Projects/NewProject"

        Returns:
            Created folder dictionary with id, displayName, etc.

        Raises:
            GraphAPIError: If folder creation fails

        Example:
            folder = folder_manager.create_folder("Projects/NewProject")
            print(f"Created folder with ID: {folder['id']}")
        """
        # Ensure cache is populated
        self.list_folders()

        # Check if folder already exists
        existing = self.get_folder_by_path(path)
        if existing:
            logger.debug("Folder already exists", path=path, id=existing["id"])
            return existing

        # Split path into parts
        parts = path.split("/")
        current_path = ""
        parent_id: str | None = None

        for i, part in enumerate(parts):
            current_path = "/".join(parts[: i + 1])

            # Check if this level exists
            existing_folder = self.get_folder_by_path(current_path)
            if existing_folder:
                parent_id = existing_folder["id"]
                continue

            # Need to create this folder
            if parent_id:
                # Create as subfolder
                folder = self._create_subfolder(parent_id, part)
            else:
                # Create as top-level folder
                folder = self._create_top_level_folder(part)

            parent_id = folder["id"]

            # Update cache atomically
            folder_data = {
                "id": folder["id"],
                "displayName": folder["displayName"],
                "parentFolderId": folder.get("parentFolderId"),
                "childFolderCount": 0,
                "totalItemCount": 0,
                "unreadItemCount": 0,
            }
            with self._cache_lock:
                if self._folder_cache is not None:
                    self._folder_cache.append(folder_data)
                self._path_to_id[current_path] = folder["id"]
                self._id_to_path[folder["id"]] = current_path

        # Return the final created folder
        return self.get_folder_by_path(path) or {"id": parent_id, "displayName": parts[-1]}

    def _create_top_level_folder(self, name: str) -> dict[str, Any]:
        """Create a top-level mail folder.

        Args:
            name: Folder name

        Returns:
            Created folder dictionary

        Raises:
            GraphAPIError: If creation fails
        """
        logger.info("Creating top-level folder", name=name)

        response = self.client.post(
            "/me/mailFolders",
            json={"displayName": name},
        )

        logger.info(
            "Folder created",
            name=name,
            id=response["id"],
        )

        return response

    def _create_subfolder(self, parent_id: str, name: str) -> dict[str, Any]:
        """Create a subfolder under a parent folder.

        Args:
            parent_id: ID of the parent folder
            name: Subfolder name

        Returns:
            Created folder dictionary

        Raises:
            GraphAPIError: If creation fails
        """
        parent_path = self._id_to_path.get(parent_id, parent_id)
        logger.info("Creating subfolder", name=name, parent=parent_path)

        response = self.client.post(
            f"/me/mailFolders/{parent_id}/childFolders",
            json={"displayName": name},
        )

        logger.info(
            "Subfolder created",
            name=name,
            id=response["id"],
            parent=parent_path,
        )

        return response

    def refresh_cache(self) -> None:
        """Force refresh of the folder cache.

        Call this after external folder changes or to ensure fresh data.
        Thread-safe: uses a lock to clear cache before refresh.
        """
        with self._cache_lock:
            self._folder_cache = None
            self._path_to_id = {}
            self._id_to_path = {}
            self._cache_timestamp = None
        self.list_folders(force_refresh=True)

    def get_special_folder_id(self, folder_name: str) -> str | None:
        """Get the ID of a well-known folder.

        Well-known folders have special names: Inbox, SentItems, Drafts,
        DeletedItems, Archive, JunkEmail.

        Args:
            folder_name: Well-known folder name (case-insensitive)

        Returns:
            Folder ID if found, None otherwise
        """
        # Normalize name
        well_known_folders = {
            "inbox": "Inbox",
            "sentitems": "Sent Items",
            "sent items": "Sent Items",
            "drafts": "Drafts",
            "deleteditems": "Deleted Items",
            "deleted items": "Deleted Items",
            "archive": "Archive",
            "junkemail": "Junk Email",
            "junk email": "Junk Email",
        }

        display_name = well_known_folders.get(folder_name.lower())
        if not display_name:
            display_name = folder_name

        return self.get_folder_id(display_name)
