"""To Do task and Outlook category operations for Microsoft Graph API.

This module provides managers for:
- TaskManager: To Do task list discovery/creation, task CRUD with linkedResources
- CategoryManager: Outlook master category list management (read/create/delete)
- Helper functions for mapping classification results to task payloads

Usage:
    from assistant.graph.client import GraphClient
    from assistant.graph.tasks import TaskManager, CategoryManager

    client = GraphClient(auth)
    tasks = TaskManager(client)
    categories = CategoryManager(client)

    # Ensure task list exists
    list_id = tasks.ensure_task_list("AI Assistant")

    # Create a task with linked email
    task = tasks.create_task(list_id, task_payload)

    # Bootstrap framework categories
    existing = categories.get_categories()
    categories.create_category("P1 - Urgent Important", "preset0")
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from assistant.core.logging import get_logger

if TYPE_CHECKING:
    from assistant.config_schema import AgingConfig, AreaConfig
    from assistant.graph.client import GraphClient

logger = get_logger(__name__)

# --- Constants: Framework category color mappings (spec Section 5.6) ---

FRAMEWORK_CATEGORIES: dict[str, str] = {
    "P1 - Urgent Important": "preset0",  # Red
    "P2 - Important": "preset1",  # Orange
    "P3 - Urgent Low": "preset7",  # Blue
    "P4 - Low": "preset14",  # Steel
    "Needs Reply": "preset3",  # Yellow
    "Waiting For": "preset8",  # Purple
    "Delegated": "preset5",  # Green
    "FYI Only": "preset14",  # Steel
    "Scheduled": "preset9",  # Teal
    "Review": "preset2",  # Brown
}

PROJECT_CATEGORY_COLOR = "preset11"  # Mango
AREA_CATEGORY_COLOR = "preset10"  # Lavender

# --- Field mapping constants ---

# Action type -> To Do task status
ACTION_TYPE_TO_STATUS: dict[str, str] = {
    "Waiting For": "waitingOnOthers",
    "Needs Reply": "notStarted",
    "Review": "notStarted",
    "Delegated": "inProgress",
}

# Priority -> To Do task importance
PRIORITY_TO_IMPORTANCE: dict[str, str] = {
    "P1 - Urgent Important": "high",
    "P2 - Important": "high",
    "P3 - Urgent Low": "normal",
    "P4 - Low": "low",
}

# Action type -> task_sync task_type
ACTION_TYPE_TO_TASK_TYPE: dict[str, str] = {
    "Waiting For": "waiting_for",
    "Needs Reply": "needs_reply",
    "Review": "review",
    "Delegated": "delegated",
}

# Title prefixes by action type
ACTION_TYPE_TITLE_PREFIX: dict[str, str] = {
    "Waiting For": "Waiting on {sender} re: {subject}",
    "Needs Reply": "Reply to {sender} re: {subject}",
    "Review": "Review from {sender}: {subject}",
    "Delegated": "Follow up with {sender} re: {subject}",
}

MAX_TASK_TITLE_LENGTH = 255


class TaskManager:
    """Manages Microsoft To Do task operations via Graph API.

    Provides methods for:
    - Discovering or creating the AI Assistant task list
    - Creating tasks with linkedResources pointing to emails
    - Updating and deleting tasks
    - Listing tasks (infrastructure for Phase 2 sync)

    Attributes:
        client: GraphClient instance for API calls
    """

    def __init__(self, client: GraphClient) -> None:
        self._client = client
        self._list_id: str | None = None

    def ensure_task_list(self, list_name: str = "AI Assistant") -> str:
        """Find or create the named To Do task list.

        Caches the list ID on the instance for subsequent calls.

        Args:
            list_name: Display name of the task list

        Returns:
            The task list ID

        Raises:
            GraphAPIError: If list discovery or creation fails
        """
        if self._list_id is not None:
            return self._list_id

        # Try to find existing list
        response = self._client.get(
            "/me/todo/lists",
            params={"$filter": f"displayName eq '{list_name}'"},
        )

        lists = response.get("value", [])
        if lists:
            self._list_id = lists[0]["id"]
            logger.info(
                "Found existing task list",
                list_name=list_name,
                list_id=self._list_id[:20] + "...",
            )
            return self._list_id

        # Create new list
        created = self._client.post(
            "/me/todo/lists",
            json={"displayName": list_name},
        )
        self._list_id = created["id"]
        logger.info(
            "Created task list",
            list_name=list_name,
            list_id=self._list_id[:20] + "...",
        )
        return self._list_id

    def create_task(self, list_id: str, task_payload: dict[str, Any]) -> dict[str, Any]:
        """Create a To Do task with linkedResources inline.

        Args:
            list_id: The task list ID
            task_payload: Full task body including title, status, importance,
                body, categories, linkedResources, dueDateTime, etc.

        Returns:
            The created task dictionary from Graph API

        Raises:
            GraphAPIError: If task creation fails
        """
        response = self._client.post(
            f"/me/todo/lists/{list_id}/tasks",
            json=task_payload,
        )

        task_id = response.get("id", "")
        logger.info(
            "Created To Do task",
            task_id=task_id[:20] + "..." if task_id else "",
            title=response.get("title", "")[:60],
            list_id=list_id[:20] + "...",
        )
        return response

    def update_task(self, list_id: str, task_id: str, updates: dict[str, Any]) -> dict[str, Any]:
        """Update task fields (status, importance, etc.).

        Args:
            list_id: The task list ID
            task_id: The task ID to update
            updates: Dictionary of fields to update

        Returns:
            The updated task dictionary

        Raises:
            GraphAPIError: If update fails
        """
        response = self._client.patch(
            f"/me/todo/lists/{list_id}/tasks/{task_id}",
            json=updates,
        )

        logger.info(
            "Updated To Do task",
            task_id=task_id[:20] + "...",
            fields=list(updates.keys()),
        )
        return response

    def delete_task(self, list_id: str, task_id: str) -> None:
        """Delete a To Do task.

        Args:
            list_id: The task list ID
            task_id: The task ID to delete

        Raises:
            GraphAPIError: If deletion fails
        """
        self._client.delete(f"/me/todo/lists/{list_id}/tasks/{task_id}")

        logger.info(
            "Deleted To Do task",
            task_id=task_id[:20] + "...",
            list_id=list_id[:20] + "...",
        )

    def get_tasks(
        self,
        list_id: str,
        status_filter: str | None = "status ne 'completed'",
    ) -> list[dict[str, Any]]:
        """List tasks from a task list (infrastructure for Phase 2 sync).

        Args:
            list_id: The task list ID
            status_filter: OData filter query (default: exclude completed tasks)

        Returns:
            List of task dictionaries
        """
        params: dict[str, Any] = {
            "$select": (
                "id,title,status,importance,dueDateTime,"
                "completedDateTime,lastModifiedDateTime,categories"
            ),
        }
        if status_filter:
            params["$filter"] = status_filter

        tasks = self._client.paginate(
            f"/me/todo/lists/{list_id}/tasks",
            params=params,
        )

        logger.debug(
            "Listed To Do tasks",
            list_id=list_id[:20] + "...",
            count=len(tasks),
        )
        return tasks


class CategoryManager:
    """Manages Outlook master category list via Graph API.

    Provides methods for:
    - Reading all master categories
    - Creating categories with specified colors
    - Deleting categories by ID

    Categories are shared across email, To Do, calendar, and contacts.
    """

    def __init__(self, client: GraphClient) -> None:
        self._client = client

    def get_categories(self) -> list[dict[str, Any]]:
        """Read all master categories.

        Returns:
            List of category dicts with id, displayName, color fields
        """
        response = self._client.get("/me/outlook/masterCategories")
        categories = response.get("value", [])

        logger.debug("Fetched master categories", count=len(categories))
        return categories

    def create_category(self, name: str, color: str) -> dict[str, Any]:
        """Create a category in the master category list.

        Args:
            name: Category display name (immutable after creation)
            color: Color preset string (e.g., "preset0" for Red)

        Returns:
            The created category dictionary

        Raises:
            GraphAPIError: If creation fails (e.g., duplicate name)
        """
        response = self._client.post(
            "/me/outlook/masterCategories",
            json={"displayName": name, "color": color},
        )

        logger.info(
            "Created master category",
            name=name,
            color=color,
        )
        return response

    def delete_category(self, category_id: str) -> None:
        """Delete a category from the master category list.

        Removing a category from the master list does not remove it from
        resources that already have it applied -- those resources retain
        the category name but it becomes "uncategorized" (no color).

        Args:
            category_id: The category ID (not displayName)

        Raises:
            GraphAPIError: If deletion fails
        """
        self._client.delete(f"/me/outlook/masterCategories/{category_id}")

        logger.info("Deleted master category", category_id=category_id)


# --- Helper functions ---


def derive_taxonomy_name(
    folder: str,
    areas: list[AreaConfig],
) -> str | None:
    """Map a folder path to its area name for taxonomy category.

    Only areas get taxonomy categories -- projects are temporary and
    would accumulate unboundedly. The folder hierarchy already conveys
    the project; areas are permanent cross-cutting concerns.

    Args:
        folder: Folder path from classification (e.g., "Areas/Finance & Accounting")
        areas: Area configs from AppConfig

    Returns:
        The area name if matched, None otherwise

    Example:
        >>> derive_taxonomy_name("Areas/Finance & Accounting", areas)
        "Finance & Accounting"
    """
    for area in areas:
        if area.folder == folder:
            return area.name
    return None


def action_type_to_task_type(action_type: str) -> str:
    """Map an action type string to a task_sync task_type value.

    Args:
        action_type: Classification action type (e.g., "Needs Reply")

    Returns:
        task_sync task_type (e.g., "needs_reply")
    """
    return ACTION_TYPE_TO_TASK_TYPE.get(action_type, action_type.lower().replace(" ", "_"))


def build_task_from_classification(
    email_subject: str,
    sender_name: str,
    snippet: str,
    priority: str,
    action_type: str,
    taxonomy_category: str | None,
    email_id: str,
    web_link: str | None,
    aging_config: AgingConfig,
    received_at: datetime | None = None,
) -> dict[str, Any]:
    """Build a To Do task payload from classification results.

    Maps classification fields to Graph API To Do task fields per the spec:
    - priority -> importance (P1/P2 -> high, P3 -> normal, P4 -> low)
    - action_type -> status (Waiting For -> waitingOnOthers, etc.)
    - Builds title, body, categories, linkedResources, dueDateTime

    Args:
        email_subject: Email subject line
        sender_name: Sender display name
        snippet: Cleaned email snippet (first ~200 chars used in body)
        priority: Priority string (e.g., "P2 - Important")
        action_type: Action type string (e.g., "Needs Reply")
        taxonomy_category: Project/area name for category (or None)
        email_id: Immutable Graph message ID for linkedResource
        web_link: OWA deep link URL for linkedResource webUrl
        aging_config: Aging thresholds for due date calculation
        received_at: Email received time (for due date calculation)

    Returns:
        Dict suitable for POST to /me/todo/lists/{listId}/tasks
    """
    # Build title from action type template
    title_template = ACTION_TYPE_TITLE_PREFIX.get(action_type, "{sender}: {subject}")
    title = title_template.format(sender=sender_name, subject=email_subject)
    if len(title) > MAX_TASK_TITLE_LENGTH:
        title = title[: MAX_TASK_TITLE_LENGTH - 3] + "..."

    # Map priority -> importance
    importance = PRIORITY_TO_IMPORTANCE.get(priority, "normal")

    # Map action type -> task status
    status = ACTION_TYPE_TO_STATUS.get(action_type, "notStarted")

    # Build body content (snippet truncated to 200 chars + classification info)
    body_snippet = snippet[:200] if snippet else ""
    body_content = f"From: {sender_name}\n{body_snippet}\n\nClassified: {priority} | {action_type}"
    if taxonomy_category:
        body_content += f" | {taxonomy_category}"

    # Build categories: priority + taxonomy (not action type -- conveyed via status)
    categories = [priority]
    if taxonomy_category:
        categories.append(taxonomy_category)

    # Build linked resource
    linked_resources = []
    if email_id:
        linked_resource: dict[str, Any] = {
            "applicationName": "Outlook AI Assistant",
            "externalId": email_id,
            "displayName": f"Email: {email_subject[:80]}",
        }
        if web_link:
            linked_resource["webUrl"] = web_link
        linked_resources.append(linked_resource)

    # Build the task payload
    task: dict[str, Any] = {
        "title": title,
        "status": status,
        "importance": importance,
        "body": {
            "content": body_content,
            "contentType": "text",
        },
        "categories": categories,
    }

    if linked_resources:
        task["linkedResources"] = linked_resources

    # Calculate due date from aging config
    if received_at is not None:
        # Use nudge hours for "Waiting For", reply warning for "Needs Reply"
        if action_type == "Waiting For":
            due_hours = aging_config.waiting_for_nudge_hours
        elif action_type == "Needs Reply":
            due_hours = aging_config.needs_reply_warning_hours
        else:
            due_hours = aging_config.needs_reply_warning_hours

        due_dt = received_at + timedelta(hours=due_hours)
        task["dueDateTime"] = {
            "dateTime": due_dt.strftime("%Y-%m-%dT%H:%M:%S.0000000"),
            "timeZone": "UTC",
        }

        # Set reminder at escalation threshold for Waiting For
        if action_type == "Waiting For":
            escalate_dt = received_at + timedelta(hours=aging_config.waiting_for_escalate_hours)
            task["isReminderOn"] = True
            task["reminderDateTime"] = {
                "dateTime": escalate_dt.strftime("%Y-%m-%dT%H:%M:%S.0000000"),
                "timeZone": "UTC",
            }

    return task
