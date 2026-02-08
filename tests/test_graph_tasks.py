"""Tests for graph/tasks.py module.

Tests TaskManager, CategoryManager, helper functions (derive_taxonomy_name,
action_type_to_task_type, build_task_from_classification), and constants.
"""

from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

from assistant.config_schema import AgingConfig, AreaConfig
from assistant.graph.tasks import (
    ACTION_TYPE_TO_STATUS,
    AREA_CATEGORY_COLOR,
    FRAMEWORK_CATEGORIES,
    MAX_TASK_TITLE_LENGTH,
    PRIORITY_TO_IMPORTANCE,
    PROJECT_CATEGORY_COLOR,
    CategoryManager,
    TaskManager,
    action_type_to_task_type,
    build_task_from_classification,
    derive_taxonomy_name,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_client() -> MagicMock:
    """Return a mock GraphClient."""
    return MagicMock()


@pytest.fixture
def task_manager(mock_client: MagicMock) -> TaskManager:
    """Return a TaskManager with a mocked GraphClient."""
    return TaskManager(mock_client)


@pytest.fixture
def category_manager(mock_client: MagicMock) -> CategoryManager:
    """Return a CategoryManager with a mocked GraphClient."""
    return CategoryManager(mock_client)


@pytest.fixture
def aging_config() -> AgingConfig:
    """Return a default AgingConfig for testing."""
    return AgingConfig()


# ---------------------------------------------------------------------------
# Constants tests
# ---------------------------------------------------------------------------


class TestConstants:
    """Tests for module constants."""

    def test_framework_categories_has_all_10(self) -> None:
        """Framework categories should have exactly 10 entries."""
        assert len(FRAMEWORK_CATEGORIES) == 10

    def test_framework_categories_contains_all_priorities(self) -> None:
        """All 4 priority levels should be in framework categories."""
        for priority in [
            "P1 - Urgent Important",
            "P2 - Important",
            "P3 - Urgent Low",
            "P4 - Low",
        ]:
            assert priority in FRAMEWORK_CATEGORIES

    def test_framework_categories_contains_all_action_types(self) -> None:
        """All 6 action types should be in framework categories."""
        for action_type in [
            "Needs Reply",
            "Waiting For",
            "Delegated",
            "FYI Only",
            "Scheduled",
            "Review",
        ]:
            assert action_type in FRAMEWORK_CATEGORIES

    def test_project_and_area_colors_differ(self) -> None:
        """Project and area category colors should be different."""
        assert PROJECT_CATEGORY_COLOR != AREA_CATEGORY_COLOR


# ---------------------------------------------------------------------------
# TaskManager tests
# ---------------------------------------------------------------------------


class TestTaskManagerEnsureTaskList:
    """Tests for TaskManager.ensure_task_list()."""

    def test_finds_existing_list(self, task_manager: TaskManager, mock_client: MagicMock) -> None:
        """Should return the existing list ID without creating a new one."""
        mock_client.get.return_value = {
            "value": [{"id": "list-abc-123", "displayName": "AI Assistant"}]
        }

        result = task_manager.ensure_task_list("AI Assistant")

        assert result == "list-abc-123"
        mock_client.get.assert_called_once()
        mock_client.post.assert_not_called()

    def test_creates_new_list_when_not_found(
        self, task_manager: TaskManager, mock_client: MagicMock
    ) -> None:
        """Should create a new list when none exists."""
        mock_client.get.return_value = {"value": []}
        mock_client.post.return_value = {"id": "new-list-xyz", "displayName": "AI Assistant"}

        result = task_manager.ensure_task_list("AI Assistant")

        assert result == "new-list-xyz"
        mock_client.post.assert_called_once_with(
            "/me/todo/lists",
            json={"displayName": "AI Assistant"},
        )

    def test_caches_list_id_on_second_call(
        self, task_manager: TaskManager, mock_client: MagicMock
    ) -> None:
        """Second call should return cached ID without API call."""
        mock_client.get.return_value = {"value": [{"id": "cached-list"}]}

        task_manager.ensure_task_list("AI Assistant")
        result = task_manager.ensure_task_list("AI Assistant")

        assert result == "cached-list"
        assert mock_client.get.call_count == 1  # Only one API call


class TestTaskManagerCRUD:
    """Tests for TaskManager create, update, delete, get operations."""

    def test_create_task(self, task_manager: TaskManager, mock_client: MagicMock) -> None:
        """Should POST a task payload and return the response."""
        payload = {"title": "Test Task", "status": "notStarted"}
        mock_client.post.return_value = {"id": "task-123", "title": "Test Task"}

        result = task_manager.create_task("list-1", payload)

        assert result["id"] == "task-123"
        mock_client.post.assert_called_once_with(
            "/me/todo/lists/list-1/tasks",
            json=payload,
        )

    def test_update_task(self, task_manager: TaskManager, mock_client: MagicMock) -> None:
        """Should PATCH a task with updates."""
        updates = {"importance": "high"}
        mock_client.patch.return_value = {"id": "task-123", "importance": "high"}

        result = task_manager.update_task("list-1", "task-123", updates)

        assert result["importance"] == "high"
        mock_client.patch.assert_called_once_with(
            "/me/todo/lists/list-1/tasks/task-123",
            json=updates,
        )

    def test_delete_task(self, task_manager: TaskManager, mock_client: MagicMock) -> None:
        """Should DELETE a task."""
        task_manager.delete_task("list-1", "task-123")

        mock_client.delete.assert_called_once_with("/me/todo/lists/list-1/tasks/task-123")

    def test_get_tasks(self, task_manager: TaskManager, mock_client: MagicMock) -> None:
        """Should paginate tasks from the API."""
        mock_client.paginate.return_value = [
            {"id": "t1", "title": "Task 1"},
            {"id": "t2", "title": "Task 2"},
        ]

        result = task_manager.get_tasks("list-1")

        assert len(result) == 2
        mock_client.paginate.assert_called_once()

    def test_get_tasks_with_no_filter(
        self, task_manager: TaskManager, mock_client: MagicMock
    ) -> None:
        """Should omit $filter when status_filter is None."""
        mock_client.paginate.return_value = []

        task_manager.get_tasks("list-1", status_filter=None)

        call_args = mock_client.paginate.call_args
        params = (
            call_args[1].get("params") or call_args[0][1]
            if len(call_args[0]) > 1
            else call_args[1].get("params")
        )
        assert "$filter" not in params


# ---------------------------------------------------------------------------
# CategoryManager tests
# ---------------------------------------------------------------------------


class TestCategoryManager:
    """Tests for CategoryManager operations."""

    def test_get_categories(
        self, category_manager: CategoryManager, mock_client: MagicMock
    ) -> None:
        """Should return list of categories from API."""
        mock_client.get.return_value = {
            "value": [
                {"id": "cat-1", "displayName": "P1 - Urgent Important", "color": "preset0"},
                {"id": "cat-2", "displayName": "P2 - Important", "color": "preset1"},
            ]
        }

        result = category_manager.get_categories()

        assert len(result) == 2
        assert result[0]["displayName"] == "P1 - Urgent Important"

    def test_create_category(
        self, category_manager: CategoryManager, mock_client: MagicMock
    ) -> None:
        """Should POST a new category with name and color."""
        mock_client.post.return_value = {
            "id": "cat-new",
            "displayName": "Test Category",
            "color": "preset5",
        }

        result = category_manager.create_category("Test Category", "preset5")

        assert result["displayName"] == "Test Category"
        mock_client.post.assert_called_once_with(
            "/me/outlook/masterCategories",
            json={"displayName": "Test Category", "color": "preset5"},
        )

    def test_delete_category(
        self, category_manager: CategoryManager, mock_client: MagicMock
    ) -> None:
        """Should DELETE a category by ID."""
        category_manager.delete_category("cat-123")

        mock_client.delete.assert_called_once_with("/me/outlook/masterCategories/cat-123")


# ---------------------------------------------------------------------------
# derive_taxonomy_name tests
# ---------------------------------------------------------------------------


class TestDeriveTaxonomyName:
    """Tests for derive_taxonomy_name() helper.

    Only areas produce taxonomy categories -- projects are temporary
    and the folder hierarchy already conveys the project.
    """

    def test_matches_area_folder(self) -> None:
        """Should return area name when folder matches."""
        areas = [
            AreaConfig(name="Finance", folder="Areas/Finance"),
            AreaConfig(name="HR Operations", folder="Areas/HR"),
        ]
        result = derive_taxonomy_name("Areas/Finance", areas)
        assert result == "Finance"

    def test_returns_none_for_project_folder(self) -> None:
        """Should return None for project folders (projects don't get taxonomy categories)."""
        areas = [AreaConfig(name="Finance", folder="Areas/Finance")]
        result = derive_taxonomy_name("Projects/Tradecore Steel", areas)
        assert result is None

    def test_returns_none_for_no_match(self) -> None:
        """Should return None when folder doesn't match any area."""
        areas = [AreaConfig(name="Test Area", folder="Areas/Test")]
        result = derive_taxonomy_name("Archive/Old", areas)
        assert result is None

    def test_empty_areas_list(self) -> None:
        """Should return None with empty area list."""
        result = derive_taxonomy_name("Areas/Finance", [])
        assert result is None


# ---------------------------------------------------------------------------
# action_type_to_task_type tests
# ---------------------------------------------------------------------------


class TestActionTypeToTaskType:
    """Tests for action_type_to_task_type() helper."""

    def test_known_action_types(self) -> None:
        """All known action types should map correctly."""
        assert action_type_to_task_type("Waiting For") == "waiting_for"
        assert action_type_to_task_type("Needs Reply") == "needs_reply"
        assert action_type_to_task_type("Review") == "review"
        assert action_type_to_task_type("Delegated") == "delegated"

    def test_unknown_action_type_falls_back(self) -> None:
        """Unknown action types should be lowercased with spaces replaced."""
        assert action_type_to_task_type("FYI Only") == "fyi_only"
        assert action_type_to_task_type("Custom Type") == "custom_type"


# ---------------------------------------------------------------------------
# build_task_from_classification tests
# ---------------------------------------------------------------------------


class TestBuildTaskFromClassification:
    """Tests for build_task_from_classification() helper."""

    def test_basic_task_payload(self, aging_config: AgingConfig) -> None:
        """Should build a complete task payload with all fields."""
        result = build_task_from_classification(
            email_subject="Project Update",
            sender_name="Alice",
            snippet="Here's the latest update on the project...",
            priority="P2 - Important",
            action_type="Needs Reply",
            taxonomy_category="Website Redesign",
            email_id="msg-abc-123",
            web_link="https://outlook.office.com/mail/id/msg-abc-123",
            aging_config=aging_config,
        )

        assert "title" in result
        assert "Reply to Alice re: Project Update" == result["title"]
        assert result["importance"] == "high"
        assert result["status"] == "notStarted"
        assert "P2 - Important" in result["categories"]
        assert "Website Redesign" in result["categories"]
        assert len(result["linkedResources"]) == 1
        assert result["linkedResources"][0]["externalId"] == "msg-abc-123"

    def test_priority_to_importance_mapping(self, aging_config: AgingConfig) -> None:
        """Should correctly map priorities to To Do importance levels."""
        for priority, expected_importance in PRIORITY_TO_IMPORTANCE.items():
            result = build_task_from_classification(
                email_subject="Test",
                sender_name="Test",
                snippet="",
                priority=priority,
                action_type="Review",
                taxonomy_category=None,
                email_id="msg-1",
                web_link=None,
                aging_config=aging_config,
            )
            assert result["importance"] == expected_importance, (
                f"Priority {priority} should map to {expected_importance}"
            )

    def test_action_type_to_status_mapping(self, aging_config: AgingConfig) -> None:
        """Should correctly map action types to To Do status."""
        for action_type, expected_status in ACTION_TYPE_TO_STATUS.items():
            result = build_task_from_classification(
                email_subject="Test",
                sender_name="Test",
                snippet="",
                priority="P3 - Urgent Low",
                action_type=action_type,
                taxonomy_category=None,
                email_id="msg-1",
                web_link=None,
                aging_config=aging_config,
            )
            assert result["status"] == expected_status, (
                f"Action type {action_type} should map to {expected_status}"
            )

    def test_title_truncation(self, aging_config: AgingConfig) -> None:
        """Title should be truncated to MAX_TASK_TITLE_LENGTH."""
        long_subject = "A" * 300
        result = build_task_from_classification(
            email_subject=long_subject,
            sender_name="Sender",
            snippet="",
            priority="P3 - Urgent Low",
            action_type="Needs Reply",
            taxonomy_category=None,
            email_id="msg-1",
            web_link=None,
            aging_config=aging_config,
        )

        assert len(result["title"]) <= MAX_TASK_TITLE_LENGTH
        assert result["title"].endswith("...")

    def test_title_format_per_action_type(self, aging_config: AgingConfig) -> None:
        """Title should use the correct prefix for each action type."""
        result_waiting = build_task_from_classification(
            email_subject="Status",
            sender_name="Bob",
            snippet="",
            priority="P2 - Important",
            action_type="Waiting For",
            taxonomy_category=None,
            email_id="m1",
            web_link=None,
            aging_config=aging_config,
        )
        assert result_waiting["title"] == "Waiting on Bob re: Status"

        result_delegated = build_task_from_classification(
            email_subject="Report",
            sender_name="Carol",
            snippet="",
            priority="P3 - Urgent Low",
            action_type="Delegated",
            taxonomy_category=None,
            email_id="m2",
            web_link=None,
            aging_config=aging_config,
        )
        assert result_delegated["title"] == "Follow up with Carol re: Report"

    def test_categories_without_taxonomy(self, aging_config: AgingConfig) -> None:
        """Categories should only contain priority when no taxonomy is given."""
        result = build_task_from_classification(
            email_subject="Test",
            sender_name="Test",
            snippet="",
            priority="P1 - Urgent Important",
            action_type="Review",
            taxonomy_category=None,
            email_id="msg-1",
            web_link=None,
            aging_config=aging_config,
        )
        assert result["categories"] == ["P1 - Urgent Important"]

    def test_linked_resource_with_web_link(self, aging_config: AgingConfig) -> None:
        """Linked resource should include webUrl when provided."""
        result = build_task_from_classification(
            email_subject="Test",
            sender_name="Test",
            snippet="",
            priority="P3 - Urgent Low",
            action_type="Review",
            taxonomy_category=None,
            email_id="msg-1",
            web_link="https://outlook.office.com/mail/id/msg-1",
            aging_config=aging_config,
        )
        lr = result["linkedResources"][0]
        assert lr["webUrl"] == "https://outlook.office.com/mail/id/msg-1"

    def test_linked_resource_without_web_link(self, aging_config: AgingConfig) -> None:
        """Linked resource should omit webUrl when not provided."""
        result = build_task_from_classification(
            email_subject="Test",
            sender_name="Test",
            snippet="",
            priority="P3 - Urgent Low",
            action_type="Review",
            taxonomy_category=None,
            email_id="msg-1",
            web_link=None,
            aging_config=aging_config,
        )
        lr = result["linkedResources"][0]
        assert "webUrl" not in lr

    def test_due_date_for_needs_reply(self, aging_config: AgingConfig) -> None:
        """Due date for 'Needs Reply' should use needs_reply_warning_hours."""
        received = datetime(2025, 6, 1, 10, 0, 0)
        result = build_task_from_classification(
            email_subject="Test",
            sender_name="Test",
            snippet="",
            priority="P3 - Urgent Low",
            action_type="Needs Reply",
            taxonomy_category=None,
            email_id="msg-1",
            web_link=None,
            aging_config=aging_config,
            received_at=received,
        )
        expected_due = received + timedelta(hours=aging_config.needs_reply_warning_hours)
        assert "dueDateTime" in result
        assert result["dueDateTime"]["timeZone"] == "UTC"
        assert expected_due.strftime("%Y-%m-%dT%H:%M:%S") in result["dueDateTime"]["dateTime"]

    def test_due_date_and_reminder_for_waiting_for(self, aging_config: AgingConfig) -> None:
        """'Waiting For' should set due date at nudge hours and reminder at escalate hours."""
        received = datetime(2025, 6, 1, 10, 0, 0)
        result = build_task_from_classification(
            email_subject="Test",
            sender_name="Test",
            snippet="",
            priority="P2 - Important",
            action_type="Waiting For",
            taxonomy_category=None,
            email_id="msg-1",
            web_link=None,
            aging_config=aging_config,
            received_at=received,
        )

        # Due date at nudge hours
        expected_due = received + timedelta(hours=aging_config.waiting_for_nudge_hours)
        assert expected_due.strftime("%Y-%m-%dT%H:%M:%S") in result["dueDateTime"]["dateTime"]

        # Reminder at escalate hours
        assert result["isReminderOn"] is True
        expected_reminder = received + timedelta(hours=aging_config.waiting_for_escalate_hours)
        assert (
            expected_reminder.strftime("%Y-%m-%dT%H:%M:%S")
            in (result["reminderDateTime"]["dateTime"])
        )

    def test_no_due_date_without_received_at(self, aging_config: AgingConfig) -> None:
        """Should not include dueDateTime when received_at is not provided."""
        result = build_task_from_classification(
            email_subject="Test",
            sender_name="Test",
            snippet="",
            priority="P3 - Urgent Low",
            action_type="Needs Reply",
            taxonomy_category=None,
            email_id="msg-1",
            web_link=None,
            aging_config=aging_config,
        )
        assert "dueDateTime" not in result

    def test_body_contains_classification_info(self, aging_config: AgingConfig) -> None:
        """Task body should contain sender, snippet, and classification details."""
        result = build_task_from_classification(
            email_subject="Test",
            sender_name="Alice",
            snippet="Hello world snippet",
            priority="P2 - Important",
            action_type="Needs Reply",
            taxonomy_category="Project X",
            email_id="msg-1",
            web_link=None,
            aging_config=aging_config,
        )
        body = result["body"]["content"]
        assert "Alice" in body
        assert "Hello world snippet" in body
        assert "P2 - Important" in body
        assert "Needs Reply" in body
        assert "Project X" in body
