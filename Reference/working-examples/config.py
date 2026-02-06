"""Configuration settings for the unified application.

This module provides configuration settings for the unified application that
combines the email ingestion and client identification services.
"""

from typing import Optional, Dict, Any, List
from pydantic import Field, validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuration settings for the unified application."""
    
    # Application settings
    app_name: str = "Support Email System"
    app_version: str = "0.1.0"
    debug: bool = False
    environment: str = "development"
    
    # Database settings
    DB_USER: str = Field(..., description="Database username")
    DB_PASSWORD: str = Field(..., description="Database password")
    DB_HOST: str = Field(..., description="Database host")
    DB_PORT: str = Field(default="5432", description="Database port")
    DB_NAME: str = Field(..., description="Database name")
    database_url: str = Field(default="", description="Database connection URL")

    # API settings
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    
    # Email ingestion service settings
    MICROSOFT_GRAPH_CLIENT_ID: str = Field(default="", description="Microsoft Graph API client ID")
    MICROSOFT_GRAPH_CLIENT_SECRET: str = Field(default="", description="Microsoft Graph API client secret")
    MICROSOFT_GRAPH_TENANT_ID: str = Field(default="", description="Microsoft Graph API tenant ID")
    MICROSOFT_GRAPH_MAILBOX: str = Field(default="", description="Microsoft Graph mailbox to process")
    
    # Email processing settings
    EMAIL_BATCH_SIZE: int = Field(default=50, description="Number of emails to process in each batch")
    EMAIL_MAX_PER_REQUEST: int = Field(default=100, description="Maximum emails per API request")
    EMAIL_SYNC_INTERVAL_MINUTES: int = Field(default=5, description="Email sync interval in minutes")
    AUTO_SYNC_ON_STARTUP: bool = Field(default=False, description="Automatically start email sync task on application startup")

    # Email content cleaning settings (PII processing)
    EMAIL_CLEANING_ENABLED: bool = Field(
        default=True,
        description="Enable email content cleaning to remove signatures, disclaimers, and quoted text before LLM processing"
    )
    EMAIL_CLEANING_LOG_STATS: bool = Field(
        default=True,
        description="Log content cleaning statistics for monitoring token savings"
    )

    # Client identification service settings
    CLIENT_ID_SERVICE_NAME: str = Field(default="client_identification", description="Client identification service name")
    CLIENT_ID_SERVICE_VERSION: str = Field(default="1.0.0", description="Client identification service version")
    
    # Client identification API settings
    ABSTRACT_API_KEY: str = Field(default="", description="Abstract API key for company enrichment")
    ABSTRACT_API_URL: str = Field(
        default="https://companyenrichment.abstractapi.com/v2",
        description="Abstract API endpoint URL"
    )
    PDL_API_KEY: str = Field(default="", description="People Data Labs API key")
    PDL_API_URL: str = Field(
        default="https://api.peopledatalabs.com/v5/company/enrich",
        description="PDL API endpoint URL"
    )
    
    # Client identification enrichment settings
    ENRICHMENT_PROVIDERS: List[str] = Field(
        default=["ABSTRACT", "PDL"],
        description="List of enrichment providers in order of priority"
    )
    ENRICHMENT_CACHE_TTL: int = Field(
        default=86400,  # 24 hours
        description="Time-to-live for enrichment cache entries in seconds"
    )
    
    # Client identification rate limiting settings
    CLIENT_ID_RATE_LIMIT_REQUESTS: int = Field(default=100, description="Number of requests per rate limit window")
    CLIENT_ID_RATE_LIMIT_WINDOW_SECONDS: int = Field(default=60, description="Rate limit window in seconds")
    CLIENT_ID_REQUEST_DELAY_SECONDS: float = Field(default=1.0, description="Delay between requests in seconds")
    CLIENT_ID_MAX_RETRY_ATTEMPTS: int = Field(default=5, description="Maximum number of retry attempts")
    CLIENT_ID_RETRY_BASE_DELAY: float = Field(default=2.0, description="Base delay for exponential backoff")
    
    # Client identification participant processing settings
    CLIENT_ID_PARTICIPANT_BATCH_SIZE: int = Field(default=100, description="Number of participants to process in each batch")
    CLIENT_ID_PAGINATION_CURSOR_ENABLED: bool = Field(default=True, description="Whether to use cursor-based pagination")
    CLIENT_ID_MIN_CONFIDENCE: float = Field(default=0.7, description="Minimum confidence for client identification")

    # LLM API settings (for classification and other services)
    ANTHROPIC_API_KEY: str = Field(default="", description="Anthropic API key for Claude models")

    # Conversation Analysis Settings
    CONVERSATION_CONTEXT_ENABLED: bool = Field(
        default=True,
        description="Enable conversation-aware analysis (classification, sentiment, prioritization, topic extraction)"
    )
    CONVERSATION_HISTORY_LIMIT: int = Field(
        default=10,
        description="Maximum number of emails to include in conversation history for analysis"
    )
    CONVERSATION_HISTORY_DAYS: int = Field(
        default=30,
        description="Maximum age (in days) of emails to include in conversation history"
    )
    ESCALATION_DETECTION_ENABLED: bool = Field(
        default=True,
        description="Enable automatic detection of escalation patterns in conversations"
    )
    TONE_EVOLUTION_TRACKING_ENABLED: bool = Field(
        default=True,
        description="Enable tracking of sentiment/tone evolution across conversation"
    )
    PATTERN_DETECTION_ENABLED: bool = Field(
        default=True,
        description="Enable pattern detection (unanswered follow-ups, topic shifts, etc.)"
    )
    CONVERSATION_CACHE_ENABLED: bool = Field(
        default=True,
        description="Enable caching of conversation history during batch processing"
    )
    CONVERSATION_CACHE_TTL_SECONDS: int = Field(
        default=300,
        description="Time-to-live for conversation history cache entries in seconds (5 minutes default)"
    )

    # Logging settings
    log_level: str = "INFO"
    log_file_path: str = Field(default="logs/unified_app.log", description="Path to log file")
    
    # Metrics
    metrics_enabled: bool = True
    metrics_port: int = 9090
    metrics_path: str = "/metrics"
    
    @validator("database_url", pre=True)
    def build_database_url(cls, v: str, values: Dict[str, Any]) -> str:
        """Build database URL from individual components if not provided.
        
        Args:
            v: The database URL value
            values: Other field values
            
        Returns:
            str: The constructed database URL
        """
        if v:
            return v
            
        # Build URL from components
        db_user = values.get("DB_USER", "")
        db_password = values.get("DB_PASSWORD", "")
        db_host = values.get("DB_HOST", "")
        db_port = values.get("DB_PORT", "5432")
        db_name = values.get("DB_NAME", "")
        
        return f"postgresql+psycopg2://{db_user}:{db_password}@{db_host}:{db_port}/{db_name}"
    
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        populate_by_name=True,
        extra="allow"
    )


def get_settings() -> Settings:
    """Get the application settings.
    
    Returns:
        Settings: The application settings
    """
    return Settings() 