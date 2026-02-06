"""Custom exception types for the Outlook AI Assistant.

All exceptions follow the error message standard from CODING_STANDARDS.md:
- What failed (specific operation or component)
- Where it failed (file, method, context)
- Why it failed (the specific condition)
- How to fix it (actionable guidance)
- Where to learn more (documentation link if available)
"""


class AssistantError(Exception):
    """Base exception for all Outlook AI Assistant errors."""

    pass


class ConfigValidationError(AssistantError):
    """Raised when config.yaml fails Pydantic validation.

    Includes specific field errors with actionable messages.
    """

    pass


class ConfigLoadError(AssistantError):
    """Raised when config.yaml cannot be loaded (file not found, YAML parse error)."""

    pass


class AuthenticationError(AssistantError):
    """Raised when MSAL device code flow fails or tokens cannot be acquired."""

    pass


class GraphAPIError(AssistantError):
    """Raised when Microsoft Graph API returns an error.

    Attributes:
        status_code: HTTP status code from the API
        error_code: Error code from Graph API response (if available)
        message: Error message from Graph API response
    """

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        error_code: str | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code


class RateLimitExceeded(AssistantError):
    """Raised when API rate limits are exceeded and cannot be recovered.

    This is raised when the rate limiter would require an excessive wait time
    (>20 seconds) rather than blocking indefinitely.
    """

    pass


class ClassificationError(AssistantError):
    """Raised when Claude classification fails after retries.

    Attributes:
        email_id: The Graph API message ID that failed classification
        attempts: Number of classification attempts made
    """

    def __init__(self, message: str, email_id: str | None = None, attempts: int = 0):
        super().__init__(message)
        self.email_id = email_id
        self.attempts = attempts


class DatabaseError(AssistantError):
    """Raised when SQLite operations fail."""

    pass
