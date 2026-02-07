"""Tests for write_config_safely() utility.

Covers atomic writes, round-trip validation, backup creation,
and singleton reset after successful writes.
"""

from pathlib import Path
from typing import Any

import pytest
import yaml

from assistant.config import (
    get_config,
    load_config,
    reset_config,
    write_config_safely,
)
from assistant.config_schema import AppConfig
from assistant.core.errors import ConfigValidationError

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    """Create a temporary config directory with a valid config file."""
    d = tmp_path / "config"
    d.mkdir()
    return d


@pytest.fixture
def config_path(config_dir: Path, sample_config_dict: dict[str, Any]) -> Path:
    """Write a valid config.yaml and return its path."""
    p = config_dir / "config.yaml"
    p.write_text(yaml.dump(sample_config_dict, default_flow_style=False))
    return p


@pytest.fixture
def valid_config(sample_config_dict: dict[str, Any]) -> AppConfig:
    """Return a valid AppConfig instance."""
    return AppConfig(**sample_config_dict)


@pytest.fixture(autouse=True)
def _set_config_env(config_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point config loader at our temp file for every test."""
    monkeypatch.setenv("ASSISTANT_CONFIG_PATH", str(config_path))
    reset_config()


# ---------------------------------------------------------------------------
# Tests: Happy path
# ---------------------------------------------------------------------------


async def test_write_creates_file_with_valid_content(config_path: Path, valid_config: AppConfig):
    """write_config_safely writes valid YAML that can be loaded back."""
    write_config_safely(valid_config)

    # File should exist and be loadable
    loaded = load_config(config_path)
    assert loaded.schema_version == valid_config.schema_version
    assert loaded.auth.client_id == valid_config.auth.client_id


async def test_write_creates_timestamped_backup(config_path: Path, valid_config: AppConfig):
    """A .bak.{timestamp} file is created alongside the config."""
    write_config_safely(valid_config)

    backups = list(config_path.parent.glob("config.yaml.bak.*"))
    assert len(backups) == 1
    assert backups[0].stat().st_size > 0


async def test_write_backup_contains_original_content(config_path: Path, valid_config: AppConfig):
    """The backup should contain the pre-write content."""
    original_content = config_path.read_text()
    write_config_safely(valid_config)

    backups = list(config_path.parent.glob("config.yaml.bak.*"))
    assert backups[0].read_text() == original_content


async def test_write_resets_singleton(config_path: Path, valid_config: AppConfig):
    """After writing, get_config() reloads from disk (not stale cache)."""
    # Prime the singleton
    cfg1 = get_config()
    assert cfg1.projects == []

    # Modify config — add a project
    from assistant.config_schema import ProjectConfig, SignalsConfig

    modified = valid_config.model_copy(deep=True)
    modified.projects.append(
        ProjectConfig(
            name="Test Proj",
            folder="Projects/Test",
            signals=SignalsConfig(subjects=["hello"]),
        )
    )
    write_config_safely(modified)

    # Singleton should now return the updated config
    cfg2 = get_config()
    assert len(cfg2.projects) == 1
    assert cfg2.projects[0].name == "Test Proj"


# ---------------------------------------------------------------------------
# Tests: Validation failures
# ---------------------------------------------------------------------------


async def test_write_rejects_when_round_trip_fails(config_path: Path, valid_config: AppConfig):
    """If round-trip validation fails, the original file is untouched."""
    from unittest.mock import patch

    original_content = config_path.read_text()

    # Simulate a round-trip validation failure by making the AppConfig
    # constructor raise ValidationError during the re-parse step
    from pydantic import ValidationError as PydanticValidationError

    def failing_appconfig(**kwargs):
        raise PydanticValidationError.from_exception_data("AppConfig", [], input_type="python")

    with (
        patch("assistant.config.AppConfig", side_effect=failing_appconfig),
        pytest.raises(ConfigValidationError, match="round-trip"),
    ):
        write_config_safely(valid_config)

    # Original file should be untouched
    assert config_path.read_text() == original_content


# ---------------------------------------------------------------------------
# Tests: Atomic replacement
# ---------------------------------------------------------------------------


async def test_write_uses_atomic_replace(config_path: Path, valid_config: AppConfig):
    """The file should be replaced atomically (no partial writes)."""
    # The config_path should exist before and after
    assert config_path.exists()

    write_config_safely(valid_config)

    assert config_path.exists()
    # No temp files should be left behind
    tmp_files = list(config_path.parent.glob("*.yaml.tmp"))
    assert tmp_files == []


async def test_write_to_explicit_path(tmp_path: Path, valid_config: AppConfig):
    """write_config_safely accepts an explicit path override."""
    custom_path = tmp_path / "custom" / "config.yaml"
    custom_path.parent.mkdir(parents=True)
    custom_path.write_text("placeholder")

    write_config_safely(valid_config, config_path=custom_path)

    loaded = load_config(custom_path)
    assert loaded.auth.client_id == valid_config.auth.client_id


async def test_write_no_backup_when_file_missing(tmp_path: Path, valid_config: AppConfig):
    """If the target file doesn't exist yet, no backup is created."""
    new_path = tmp_path / "brand_new" / "config.yaml"
    new_path.parent.mkdir(parents=True)

    write_config_safely(valid_config, config_path=new_path)

    # File should be created
    assert new_path.exists()
    # No backups since there was nothing to back up
    backups = list(new_path.parent.glob("*.bak.*"))
    assert backups == []
