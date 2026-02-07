"""Configuration loader with hot-reload support.

This module provides configuration loading from YAML with automatic validation
against Pydantic schema and hot-reload capability on file changes.

Usage:
    from assistant.config import get_config, reload_config_if_changed

    # Get current config (singleton)
    config = get_config()

    # Check for changes and reload (call each triage cycle)
    if reload_config_if_changed():
        config = get_config()  # Get updated config
"""

import os
import threading
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from assistant.config_schema import CURRENT_SCHEMA_VERSION, AppConfig
from assistant.core.errors import ConfigLoadError, ConfigValidationError
from assistant.core.logging import get_logger

logger = get_logger(__name__)

# Default config path - can be overridden via environment variable
DEFAULT_CONFIG_PATH = Path("config/config.yaml")

# Global state for config singleton and hot-reload
_config_lock = threading.Lock()
_current_config: AppConfig | None = None
_config_path: Path | None = None
_config_mtime: float = 0.0


def _get_config_path() -> Path:
    """Get the config file path from environment or default."""
    env_path = os.environ.get("ASSISTANT_CONFIG_PATH")
    if env_path:
        return Path(env_path)
    return DEFAULT_CONFIG_PATH


def _format_validation_errors(error: ValidationError) -> str:
    """Format Pydantic validation errors into actionable messages.

    Args:
        error: Pydantic ValidationError

    Returns:
        Formatted error message with specific field errors
    """
    messages = []
    for err in error.errors():
        # Build field path (e.g., "projects.0.folder")
        field_path = ".".join(str(loc) for loc in err["loc"])
        msg = err["msg"]
        err_type = err["type"]

        # Make messages actionable
        if err_type == "missing":
            messages.append(f"  - Missing required field '{field_path}'")
        elif err_type == "string_type":
            messages.append(f"  - Field '{field_path}' must be a string")
        elif err_type == "int_type":
            messages.append(f"  - Field '{field_path}' must be an integer")
        elif err_type == "literal_error":
            messages.append(f"  - Field '{field_path}': {msg}")
        else:
            messages.append(f"  - Field '{field_path}': {msg}")

    return "\n".join(messages)


def _load_yaml(path: Path) -> dict[str, Any]:
    """Load and parse YAML file.

    Args:
        path: Path to YAML file

    Returns:
        Parsed YAML as dictionary

    Raises:
        ConfigLoadError: If file not found or YAML parse error
    """
    if not path.exists():
        raise ConfigLoadError(
            f"Configuration file not found: {path}\n"
            f"Create it by copying config/config.yaml.example to {path}"
        )

    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
            if data is None:
                return {}
            if not isinstance(data, dict):
                raise ConfigLoadError(
                    f"Configuration file must be a YAML mapping, got {type(data).__name__}"
                )
            return data
    except yaml.YAMLError as e:
        raise ConfigLoadError(f"Failed to parse YAML in {path}:\n{e}") from e


def _validate_config(data: dict[str, Any], path: Path) -> AppConfig:
    """Validate config data against Pydantic schema.

    Args:
        data: Parsed YAML data
        path: Path to config file (for error messages)

    Returns:
        Validated AppConfig instance

    Raises:
        ConfigValidationError: If validation fails
    """
    try:
        config = AppConfig(**data)

        # Check schema version
        if config.schema_version > CURRENT_SCHEMA_VERSION:
            raise ConfigValidationError(
                f"Config schema version {config.schema_version} is newer than "
                f"supported version {CURRENT_SCHEMA_VERSION}. "
                "Please upgrade the assistant or downgrade the config."
            )

        return config
    except ValidationError as e:
        error_details = _format_validation_errors(e)
        raise ConfigValidationError(
            f"Configuration validation failed for {path}:\n{error_details}"
        ) from e


def load_config(path: Path | None = None) -> AppConfig:
    """Load and validate configuration from YAML file.

    This function always loads fresh from disk. For cached access with
    hot-reload support, use get_config() instead.

    Args:
        path: Optional path to config file. If not provided, uses
              ASSISTANT_CONFIG_PATH env var or default.

    Returns:
        Validated AppConfig instance

    Raises:
        ConfigLoadError: If file cannot be loaded
        ConfigValidationError: If validation fails
    """
    config_path = path or _get_config_path()

    logger.debug("Loading configuration", path=str(config_path))

    data = _load_yaml(config_path)
    config = _validate_config(data, config_path)

    logger.info(
        "Configuration loaded successfully",
        path=str(config_path),
        schema_version=config.schema_version,
        projects_count=len(config.projects),
        areas_count=len(config.areas),
        auto_rules_count=len(config.auto_rules),
    )

    return config


def get_config() -> AppConfig:
    """Get the current configuration singleton.

    On first call, loads configuration from disk. Subsequent calls return
    the cached config. Use reload_config_if_changed() to check for updates.

    Thread-safe: protected by _config_lock for concurrent access from
    APScheduler triage thread and uvicorn async thread.

    Returns:
        Current AppConfig instance

    Raises:
        ConfigLoadError: If file cannot be loaded
        ConfigValidationError: If validation fails
    """
    global _current_config, _config_path, _config_mtime

    with _config_lock:
        if _current_config is None:
            _config_path = _get_config_path()
            _current_config = load_config(_config_path)
            _config_mtime = _config_path.stat().st_mtime

        return _current_config


def reload_config_if_changed() -> bool:
    """Check if config file has changed and reload if so.

    Call this at the start of each triage cycle to pick up config changes
    without requiring a restart.

    Thread-safe: protected by _config_lock for concurrent access from
    APScheduler triage thread and uvicorn async thread.

    Returns:
        True if config was reloaded, False if unchanged

    Behavior:
        - If config file unchanged: returns False
        - If config file changed and valid: updates singleton, returns True
        - If config file changed but invalid: keeps old config, logs WARNING, returns False
    """
    global _current_config, _config_path, _config_mtime

    with _config_lock:
        if _config_path is None:
            # Config hasn't been loaded yet, nothing to reload
            return False

        try:
            current_mtime = _config_path.stat().st_mtime
        except OSError as e:
            logger.warning(
                "Failed to check config file mtime",
                path=str(_config_path),
                error=str(e),
            )
            return False

        if current_mtime <= _config_mtime:
            # File hasn't changed
            return False

        # File has changed, attempt reload
        logger.info(
            "Configuration file changed, attempting reload",
            path=str(_config_path),
        )

        try:
            new_config = load_config(_config_path)
            _current_config = new_config
            _config_mtime = current_mtime

            logger.info(
                "Configuration reloaded successfully",
                path=str(_config_path),
                schema_version=new_config.schema_version,
            )
            return True

        except (ConfigLoadError, ConfigValidationError) as e:
            # Keep the old config, log the error
            logger.warning(
                "Configuration reload failed, keeping previous config",
                path=str(_config_path),
                error=str(e),
            )
            # Update mtime so we don't keep trying to reload on every check
            _config_mtime = current_mtime
            return False


def validate_config_file(path: Path | None = None) -> tuple[bool, str]:
    """Validate a config file without loading it into the singleton.

    Useful for CLI validation commands and testing.

    Args:
        path: Path to config file. If not provided, uses default.

    Returns:
        Tuple of (is_valid, message)
    """
    config_path = path or _get_config_path()

    try:
        config = load_config(config_path)
        return (
            True,
            f"Configuration valid (schema version {config.schema_version})\n"
            f"  - {len(config.projects)} projects\n"
            f"  - {len(config.areas)} areas\n"
            f"  - {len(config.auto_rules)} auto-rules\n"
            f"  - {len(config.key_contacts)} key contacts",
        )
    except ConfigLoadError as e:
        return (False, f"Load error: {e}")
    except ConfigValidationError as e:
        return (False, f"Validation error: {e}")


def reset_config() -> None:
    """Reset the config singleton. Primarily for testing."""
    global _current_config, _config_path, _config_mtime
    with _config_lock:
        _current_config = None
        _config_path = None
        _config_mtime = 0.0
