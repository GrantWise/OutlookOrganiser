"""Pydantic configuration schema for the Outlook AI Assistant.

This module defines the configuration schema that mirrors config.yaml structure.
All configuration is validated against these models on startup and hot-reload.

Usage:
    from assistant.config_schema import AppConfig

    # Validate a config dict
    config = AppConfig(**yaml_data)
"""

from typing import Literal

from pydantic import BaseModel, Field, field_validator

# Current schema version - increment when adding new required fields
CURRENT_SCHEMA_VERSION = 1


class AuthConfig(BaseModel):
    """Azure AD authentication configuration."""

    client_id: str = Field(description="Azure AD Application (client) ID")
    tenant_id: str = Field(
        default="common",
        description="Azure AD Directory (tenant) ID or 'common' for personal accounts",
    )
    scopes: list[str] = Field(
        default=[
            "Mail.ReadWrite",
            "Mail.Send",
            "MailboxSettings.Read",
            "User.Read",
        ],
        description="Microsoft Graph API permission scopes",
    )
    token_cache_path: str = Field(
        default="data/token_cache.json",
        description="Path to MSAL token cache file",
    )

    @field_validator("token_cache_path")
    @classmethod
    def validate_token_cache_path(cls, v: str) -> str:
        """Ensure token cache path doesn't contain path traversal."""
        if not v or not v.strip():
            raise ValueError("Token cache path cannot be empty")
        if ".." in v:
            raise ValueError("Token cache path cannot contain '..' (path traversal)")
        return v


class TriageConfig(BaseModel):
    """Triage engine configuration."""

    interval_minutes: int = Field(
        default=15,
        ge=1,
        le=1440,
        description="How often to check for new mail (minutes)",
    )
    lookback_hours: int = Field(
        default=2,
        ge=1,
        le=168,
        description="On restart, re-check emails from this window (hours)",
    )
    batch_size: int = Field(
        default=20,
        ge=1,
        le=100,
        description="Max emails to process per triage cycle",
    )
    mode: Literal["suggest", "auto"] = Field(
        default="suggest",
        description="Operation mode: 'suggest' for review, 'auto' for autonomous",
    )
    watch_folders: list[str] = Field(
        default=["Inbox"],
        description="Folders to monitor for new emails",
    )


class ModelsConfig(BaseModel):
    """Claude model selection per task type."""

    bootstrap: str = Field(
        default="claude-sonnet-4-5-20250929",
        description="Model for bootstrap batch analysis",
    )
    bootstrap_merge: str = Field(
        default="claude-sonnet-4-5-20250929",
        description="Model for bootstrap consolidation",
    )
    triage: str = Field(
        default="claude-haiku-4-5-20251001",
        description="Model for email classification",
    )
    dry_run: str = Field(
        default="claude-haiku-4-5-20251001",
        description="Model for dry-run classification",
    )
    digest: str = Field(
        default="claude-haiku-4-5-20251001",
        description="Model for daily digest generation",
    )
    waiting_for: str = Field(
        default="claude-haiku-4-5-20251001",
        description="Model for waiting-for extraction",
    )
    chat: str = Field(
        default="claude-sonnet-4-5-20250929",
        description="Model for classification chat assistant",
    )


class SnippetConfig(BaseModel):
    """Email snippet processing configuration."""

    max_length: int = Field(
        default=1000,
        ge=100,
        le=10000,
        description="Maximum characters to extract from email body",
    )
    strip_signatures: bool = Field(
        default=True,
        description="Remove signature blocks (-- or _____ separators)",
    )
    strip_disclaimers: bool = Field(
        default=True,
        description="Remove CONFIDENTIAL/legal disclaimer blocks",
    )
    strip_forwarded_headers: bool = Field(
        default=True,
        description="Remove forwarded message headers",
    )


class SignalsConfig(BaseModel):
    """Pattern signals for project/area matching."""

    subjects: list[str] = Field(
        default_factory=list,
        description="Subject line patterns (case-insensitive)",
    )
    senders: list[str] = Field(
        default_factory=list,
        description="Sender patterns (supports wildcards like *@domain.com)",
    )
    body_keywords: list[str] = Field(
        default_factory=list,
        description="Body content keywords",
    )


# Priority literals matching Outlook category values
Priority = Literal[
    "P1 - Urgent Important",
    "P2 - Important",
    "P3 - Urgent Low",
    "P4 - Low",
]


class ProjectConfig(BaseModel):
    """Project configuration (active with defined outcomes)."""

    name: str = Field(description="Project display name")
    folder: str = Field(description="Outlook folder path (e.g., 'Projects/Example')")
    signals: SignalsConfig = Field(
        default_factory=SignalsConfig,
        description="Pattern signals for matching emails",
    )
    priority_default: Priority = Field(
        default="P2 - Important",
        description="Default priority for matched emails",
    )

    @field_validator("folder")
    @classmethod
    def validate_folder_path(cls, v: str) -> str:
        """Ensure folder path is not empty and doesn't contain traversal."""
        if not v or not v.strip():
            raise ValueError("Folder path cannot be empty")
        if ".." in v:
            raise ValueError("Folder path cannot contain '..' (path traversal)")
        return v


class AreaConfig(BaseModel):
    """Area configuration (ongoing responsibilities, no end date)."""

    name: str = Field(description="Area display name")
    folder: str = Field(description="Outlook folder path (e.g., 'Areas/Example')")
    signals: SignalsConfig = Field(
        default_factory=SignalsConfig,
        description="Pattern signals for matching emails",
    )
    priority_default: Priority = Field(
        default="P3 - Urgent Low",
        description="Default priority for matched emails",
    )

    @field_validator("folder")
    @classmethod
    def validate_folder_path(cls, v: str) -> str:
        """Ensure folder path is not empty and doesn't contain traversal."""
        if not v or not v.strip():
            raise ValueError("Folder path cannot be empty")
        if ".." in v:
            raise ValueError("Folder path cannot contain '..' (path traversal)")
        return v


class AutoRuleMatch(BaseModel):
    """Pattern matching criteria for auto-routing rules."""

    senders: list[str] = Field(
        default_factory=list,
        description="Sender patterns (supports wildcards)",
    )
    subjects: list[str] = Field(
        default_factory=list,
        description="Subject patterns (case-insensitive)",
    )


# Action type literals matching Outlook category values
ActionType = Literal[
    "Needs Reply",
    "Review",
    "Delegated",
    "FYI Only",
    "Waiting For",
    "Scheduled",
]


class AutoRuleAction(BaseModel):
    """Action to take when auto-rule matches."""

    folder: str = Field(description="Target folder path")
    category: ActionType = Field(description="Action type category to apply")
    priority: Priority = Field(description="Priority category to apply")

    @field_validator("folder")
    @classmethod
    def validate_folder_path(cls, v: str) -> str:
        """Ensure folder path is not empty and doesn't contain traversal."""
        if not v or not v.strip():
            raise ValueError("Folder path cannot be empty")
        if ".." in v:
            raise ValueError("Folder path cannot contain '..' (path traversal)")
        return v


class AutoRuleConfig(BaseModel):
    """Auto-routing rule configuration (skips Claude classification)."""

    name: str = Field(description="Rule display name")
    match: AutoRuleMatch = Field(description="Matching criteria")
    action: AutoRuleAction = Field(description="Action to take on match")


class KeyContactConfig(BaseModel):
    """Key contact configuration for priority boosting."""

    email: str = Field(description="Contact email address")
    role: str = Field(description="Contact role description")
    priority_boost: int = Field(
        default=1,
        ge=0,
        le=3,
        description="Priority levels to boost (0-3)",
    )


class AgingConfig(BaseModel):
    """Aging threshold configuration for alerts."""

    needs_reply_warning_hours: int = Field(
        default=24,
        ge=1,
        description="Hours before 'Needs Reply' items show warning",
    )
    needs_reply_critical_hours: int = Field(
        default=48,
        ge=1,
        description="Hours before 'Needs Reply' items show critical",
    )
    waiting_for_nudge_hours: int = Field(
        default=48,
        ge=1,
        description="Hours before suggesting nudge for 'Waiting For' items",
    )
    waiting_for_escalate_hours: int = Field(
        default=96,
        ge=1,
        description="Hours before escalating 'Waiting For' items",
    )


class DigestConfig(BaseModel):
    """Daily digest configuration."""

    enabled: bool = Field(default=True, description="Enable daily digest generation")
    schedule: str = Field(
        default="08:00",
        description="Local time to generate digest (HH:MM)",
    )
    delivery: Literal["stdout", "email", "file"] = Field(
        default="stdout",
        description="Digest delivery method",
    )
    include_sections: list[str] = Field(
        default=[
            "overdue_replies",
            "aging_waiting_for",
            "new_high_priority",
            "classification_summary",
        ],
        description="Sections to include in digest",
    )

    @field_validator("schedule")
    @classmethod
    def validate_schedule_format(cls, v: str) -> str:
        """Validate schedule is in HH:MM format."""
        import regex  # Use regex library with timeout, not re (per CLAUDE.md)

        if not regex.match(r"^\d{2}:\d{2}$", v, timeout=1):
            raise ValueError("Schedule must be in HH:MM format (e.g., '08:00')")
        hours, minutes = map(int, v.split(":"))
        if hours < 0 or hours > 23:
            raise ValueError("Hours must be 00-23")
        if minutes < 0 or minutes > 59:
            raise ValueError("Minutes must be 00-59")
        return v


class AutoRulesHygieneConfig(BaseModel):
    """Auto-rules maintenance configuration."""

    max_rules: int = Field(
        default=100,
        ge=10,
        le=500,
        description="Warn when auto_rules exceeds this count",
    )
    warn_on_conflicts: bool = Field(
        default=True,
        description="Detect overlapping patterns across rules",
    )
    consolidation_check_days: int = Field(
        default=30,
        ge=7,
        description="Suggest consolidation for rules with few matches in N days",
    )


class SuggestionQueueConfig(BaseModel):
    """Suggestion queue management configuration."""

    expire_after_days: int = Field(
        default=14,
        ge=1,
        le=90,
        description="Auto-expire pending suggestions older than N days",
    )
    auto_approve_confidence: float = Field(
        default=0.95,
        ge=0.5,
        le=1.0,
        description="Confidence threshold for auto-approval (Phase 2)",
    )
    auto_approve_delay_hours: int = Field(
        default=48,
        ge=0,
        description="Hours to wait before auto-approving high-confidence suggestions",
    )


class LLMLoggingConfig(BaseModel):
    """LLM request logging configuration."""

    enabled: bool = Field(default=True, description="Enable LLM request logging")
    retention_days: int = Field(
        default=30,
        ge=1,
        le=365,
        description="Days to retain LLM request logs",
    )
    log_prompts: bool = Field(
        default=True,
        description="Store full prompts (disable to save disk space)",
    )
    log_responses: bool = Field(
        default=True,
        description="Store full responses",
    )


class AppConfig(BaseModel):
    """Root configuration schema for the Outlook AI Assistant.

    This model validates the entire config.yaml structure. On startup and
    hot-reload, the YAML is parsed and validated against this schema.

    If validation fails on startup, the application exits with a clear error.
    If validation fails on hot-reload, the previous valid config is kept.
    """

    schema_version: int = Field(
        default=CURRENT_SCHEMA_VERSION,
        ge=1,
        description="Config schema version for migration tracking",
    )

    # Core configuration sections
    auth: AuthConfig = Field(default_factory=AuthConfig)
    timezone: str = Field(
        default="America/New_York",
        description="Timezone for scheduling and display",
    )
    triage: TriageConfig = Field(default_factory=TriageConfig)
    models: ModelsConfig = Field(default_factory=ModelsConfig)
    snippet: SnippetConfig = Field(default_factory=SnippetConfig)

    # Classification taxonomy
    projects: list[ProjectConfig] = Field(
        default_factory=list,
        description="Active projects with defined outcomes",
    )
    areas: list[AreaConfig] = Field(
        default_factory=list,
        description="Ongoing responsibilities (no end date)",
    )
    auto_rules: list[AutoRuleConfig] = Field(
        default_factory=list,
        description="High-confidence auto-routing rules",
    )
    key_contacts: list[KeyContactConfig] = Field(
        default_factory=list,
        description="Key contacts for priority boosting",
    )

    # Operational settings
    aging: AgingConfig = Field(default_factory=AgingConfig)
    digest: DigestConfig = Field(default_factory=DigestConfig)
    auto_rules_hygiene: AutoRulesHygieneConfig = Field(default_factory=AutoRulesHygieneConfig)
    suggestion_queue: SuggestionQueueConfig = Field(default_factory=SuggestionQueueConfig)
    llm_logging: LLMLoggingConfig = Field(default_factory=LLMLoggingConfig)

    # Optional: user email override (normally auto-detected from Graph API)
    user_email: str | None = Field(
        default=None,
        description="Override auto-detected user email (optional)",
    )
