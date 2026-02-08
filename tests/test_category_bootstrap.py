"""Tests for category bootstrap logic.

Tests the framework and taxonomy category creation, duplicate avoidance,
and orphan identification patterns used in bootstrap-categories.
"""

from unittest.mock import MagicMock

import pytest

from assistant.config_schema import AreaConfig
from assistant.graph.tasks import (
    AREA_CATEGORY_COLOR,
    FRAMEWORK_CATEGORIES,
    CategoryManager,
)


@pytest.fixture
def mock_client() -> MagicMock:
    """Return a mock GraphClient."""
    return MagicMock()


@pytest.fixture
def category_manager(mock_client: MagicMock) -> CategoryManager:
    """Return a CategoryManager with a mocked GraphClient."""
    return CategoryManager(mock_client)


class TestFrameworkCategoryBootstrap:
    """Tests for bootstrapping the 10 framework categories."""

    def test_creates_all_framework_categories_when_none_exist(
        self, category_manager: CategoryManager, mock_client: MagicMock
    ) -> None:
        """Should create all 10 framework categories when master list is empty."""
        mock_client.get.return_value = {"value": []}
        mock_client.post.return_value = {"id": "cat-new", "displayName": "test", "color": "preset0"}

        existing = category_manager.get_categories()

        # Simulate bootstrap logic: create each framework category not in existing
        created_count = 0
        for name, color in FRAMEWORK_CATEGORIES.items():
            if not any(c["displayName"] == name for c in existing):
                category_manager.create_category(name, color)
                created_count += 1

        assert created_count == 10
        assert mock_client.post.call_count == 10

    def test_skips_existing_framework_categories(
        self, category_manager: CategoryManager, mock_client: MagicMock
    ) -> None:
        """Should not recreate categories that already exist."""
        mock_client.get.return_value = {
            "value": [
                {"id": "cat-1", "displayName": "P1 - Urgent Important", "color": "preset0"},
                {"id": "cat-2", "displayName": "P2 - Important", "color": "preset1"},
            ]
        }

        existing = category_manager.get_categories()

        created_count = 0
        for name, color in FRAMEWORK_CATEGORIES.items():
            if not any(c["displayName"] == name for c in existing):
                category_manager.create_category(name, color)
                created_count += 1

        # 2 already exist, so only 8 should be created
        assert created_count == 8

    def test_preserves_existing_category_colors(
        self, category_manager: CategoryManager, mock_client: MagicMock
    ) -> None:
        """Existing categories should not be modified (preserves user-customized colors)."""
        mock_client.get.return_value = {
            "value": [
                # User changed P1 color from Red to Green -- we should NOT overwrite
                {"id": "cat-1", "displayName": "P1 - Urgent Important", "color": "preset5"},
            ]
        }

        existing = category_manager.get_categories()

        for name, color in FRAMEWORK_CATEGORIES.items():
            if not any(c["displayName"] == name for c in existing):
                category_manager.create_category(name, color)

        # The PATCH method should never be called (no color updates)
        mock_client.patch.assert_not_called()


class TestTaxonomyCategoryBootstrap:
    """Tests for bootstrapping area taxonomy categories from config.

    Only areas get taxonomy categories -- projects are temporary and
    would accumulate unboundedly in the master category list.
    """

    def test_creates_area_categories(
        self, category_manager: CategoryManager, mock_client: MagicMock
    ) -> None:
        """Should create categories for each area with the area color."""
        mock_client.get.return_value = {"value": []}
        mock_client.post.return_value = {"id": "cat-new"}

        existing = category_manager.get_categories()
        areas = [
            AreaConfig(name="Finance", folder="Areas/Finance"),
            AreaConfig(name="HR", folder="Areas/HR"),
        ]

        for area in areas:
            if not any(c["displayName"] == area.name for c in existing):
                category_manager.create_category(area.name, AREA_CATEGORY_COLOR)

        calls = mock_client.post.call_args_list
        assert len(calls) == 2
        for c in calls:
            payload = c[1]["json"]
            assert payload["color"] == AREA_CATEGORY_COLOR

    def test_skips_existing_area_categories(
        self, category_manager: CategoryManager, mock_client: MagicMock
    ) -> None:
        """Should not duplicate area categories that already exist."""
        mock_client.get.return_value = {
            "value": [{"id": "cat-1", "displayName": "Finance", "color": "preset10"}]
        }

        existing = category_manager.get_categories()
        areas = [
            AreaConfig(name="Finance", folder="Areas/Finance"),
            AreaConfig(name="HR", folder="Areas/HR"),
        ]

        created_count = 0
        for area in areas:
            if not any(c["displayName"] == area.name for c in existing):
                category_manager.create_category(area.name, AREA_CATEGORY_COLOR)
                created_count += 1

        assert created_count == 1  # Only "HR" should be created


class TestOrphanIdentification:
    """Tests for identifying orphan categories during cleanup."""

    def test_identifies_orphan_categories(self) -> None:
        """Should identify categories not matching any framework or taxonomy name."""
        existing_categories = [
            {"id": "c1", "displayName": "P1 - Urgent Important"},
            {"id": "c2", "displayName": "Old Project"},
            {"id": "c3", "displayName": "Random Category"},
            {"id": "c4", "displayName": "Alpha Project"},
        ]

        framework_names = set(FRAMEWORK_CATEGORIES.keys())
        taxonomy_names = {"Alpha Project", "Beta Project"}
        all_known = framework_names | taxonomy_names

        orphans = [c for c in existing_categories if c["displayName"] not in all_known]

        assert len(orphans) == 2
        orphan_names = {o["displayName"] for o in orphans}
        assert orphan_names == {"Old Project", "Random Category"}

    def test_no_orphans_when_all_match(self) -> None:
        """Should return empty list when all categories are known."""
        existing_categories = [
            {"id": "c1", "displayName": "P1 - Urgent Important"},
            {"id": "c2", "displayName": "My Project"},
        ]

        all_known = set(FRAMEWORK_CATEGORIES.keys()) | {"My Project"}

        orphans = [c for c in existing_categories if c["displayName"] not in all_known]

        assert orphans == []
