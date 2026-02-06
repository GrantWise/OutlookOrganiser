"""Pytest fixtures and configuration for Outlook AI Assistant tests.

Provides common fixtures for configuration, database, and mocking.
"""

import os
from pathlib import Path
from typing import Any, Generator

import pytest

from assistant.config import reset_config
from assistant.config_schema import AppConfig


@pytest.fixture(autouse=True)
def reset_config_singleton() -> Generator[None, None, None]:
    """Reset the config singleton before each test."""
    reset_config()
    yield
    reset_config()


@pytest.fixture
def temp_config_dir(tmp_path: Path) -> Path:
    """Create a temporary config directory."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    return config_dir


@pytest.fixture
def sample_config_yaml() -> str:
    """Return a minimal valid config.yaml content."""
    return """
schema_version: 1

auth:
  client_id: "test-client-id"
  tenant_id: "test-tenant-id"

timezone: "America/New_York"

triage:
  interval_minutes: 15
  batch_size: 20
  mode: "suggest"
  watch_folders: ["Inbox"]

projects: []
areas: []
auto_rules: []
"""


@pytest.fixture
def sample_config_dict() -> dict[str, Any]:
    """Return a minimal valid config as a dictionary."""
    return {
        "schema_version": 1,
        "auth": {
            "client_id": "test-client-id",
            "tenant_id": "test-tenant-id",
        },
        "timezone": "America/New_York",
        "triage": {
            "interval_minutes": 15,
            "batch_size": 20,
            "mode": "suggest",
            "watch_folders": ["Inbox"],
        },
        "projects": [],
        "areas": [],
        "auto_rules": [],
    }


@pytest.fixture
def sample_config(sample_config_dict: dict[str, Any]) -> AppConfig:
    """Return a minimal valid AppConfig instance."""
    return AppConfig(**sample_config_dict)


@pytest.fixture
def config_file(temp_config_dir: Path, sample_config_yaml: str) -> Path:
    """Create a temporary config file with valid content."""
    config_path = temp_config_dir / "config.yaml"
    config_path.write_text(sample_config_yaml)
    return config_path


@pytest.fixture
def set_config_env(config_file: Path) -> Generator[None, None, None]:
    """Set the ASSISTANT_CONFIG_PATH environment variable."""
    old_value = os.environ.get("ASSISTANT_CONFIG_PATH")
    os.environ["ASSISTANT_CONFIG_PATH"] = str(config_file)
    yield
    if old_value is None:
        del os.environ["ASSISTANT_CONFIG_PATH"]
    else:
        os.environ["ASSISTANT_CONFIG_PATH"] = old_value


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    """Create a temporary data directory."""
    data = tmp_path / "data"
    data.mkdir()
    return data
