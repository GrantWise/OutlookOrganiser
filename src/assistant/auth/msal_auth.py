"""MSAL device code flow authentication for Microsoft Graph API.

Handles OAuth2 authentication using Microsoft's device code flow, which allows
users to authenticate on any device by visiting a URL and entering a code.

Key features:
- Token cache persistence (file-based, with restricted permissions)
- Silent token acquisition (from cache/refresh token)
- Automatic token refresh via MSAL
- Clear user feedback during device code flow

Usage:
    from assistant.auth.msal_auth import GraphAuth
    from assistant.config import get_config

    config = get_config()
    auth = GraphAuth(
        client_id=config.auth.client_id,
        tenant_id=config.auth.tenant_id,
        scopes=config.auth.scopes,
        token_cache_path=config.auth.token_cache_path,
    )

    # Get access token (prompts for device code if needed)
    token = auth.get_access_token()

    # Get user info
    user_email = auth.get_user_email()
"""

import os
import random
import stat
import time
from pathlib import Path

import msal
import requests
from rich.console import Console
from rich.panel import Panel

from assistant.core.errors import AuthenticationError
from assistant.core.logging import get_logger

logger = get_logger(__name__)
console = Console()

# Retry configuration for MSAL operations
MSAL_MAX_RETRIES = 3
MSAL_RETRY_DELAYS = [1.0, 2.0, 4.0]  # Exponential backoff with jitter


class GraphAuth:
    """Handles Microsoft Graph API authentication via MSAL device code flow.

    This class manages the OAuth2 device code flow authentication, token caching,
    and automatic refresh. It uses MSAL (Microsoft Authentication Library) for
    robust handling of tokens.

    Attributes:
        client_id: Azure AD Application (client) ID
        tenant_id: Azure AD Directory (tenant) ID or 'common' for personal accounts
        scopes: List of Microsoft Graph API permission scopes
        token_cache_path: Path to the token cache file

    Security notes:
        - Token cache file is created with mode 600 (owner read/write only)
        - Refresh tokens in the cache are sensitive and should be protected
    """

    def __init__(
        self,
        client_id: str,
        tenant_id: str,
        scopes: list[str],
        token_cache_path: str,
    ):
        """Initialize the Graph authentication handler.

        Args:
            client_id: Azure AD Application (client) ID
            tenant_id: Azure AD Directory (tenant) ID or 'common'
            scopes: Microsoft Graph API permission scopes
            token_cache_path: Path to store the token cache file

        Raises:
            ValueError: If client_id is empty or invalid
        """
        if not client_id or not client_id.strip():
            raise ValueError(
                "client_id is required. "
                "Register an app in Azure Portal: https://portal.azure.com → "
                "Microsoft Entra ID → App registrations → New registration"
            )

        self.client_id = client_id
        self.tenant_id = tenant_id
        self.scopes = scopes
        self.token_cache_path = Path(token_cache_path)
        self.cache = msal.SerializableTokenCache()

        # Load existing cache if available
        self._load_cache()

        # Initialize MSAL PublicClientApplication
        authority = f"https://login.microsoftonline.com/{tenant_id}"
        self.app = msal.PublicClientApplication(
            client_id=self.client_id,
            authority=authority,
            token_cache=self.cache,
        )

        logger.debug(
            "GraphAuth initialized",
            client_id=client_id[:8] + "...",  # Log partial ID for debugging
            tenant_id=tenant_id[:8] + "...",
            scopes=scopes,
        )

    def get_access_token(self) -> str:
        """Get a valid access token, refreshing or re-authenticating as needed.

        This method tries the following in order:
        1. Silent acquisition from cache/refresh token
        2. Device code flow for interactive authentication

        Returns:
            Access token string for Microsoft Graph API

        Raises:
            AuthenticationError: If authentication fails after all attempts
        """
        # 1. Try to get token silently (from cache or via refresh token)
        accounts = self.app.get_accounts()
        if accounts:
            logger.debug(
                "Attempting silent token acquisition",
                account_count=len(accounts),
                username=accounts[0].get("username", "unknown"),
            )
            result = self._acquire_token_silent_with_retry(accounts[0])
            if result and "access_token" in result:
                self._save_cache()
                logger.debug("Token acquired silently (from cache/refresh)")
                return result["access_token"]

            # Log why silent acquisition failed
            if result:
                logger.debug(
                    "Silent acquisition failed",
                    error=result.get("error"),
                    description=result.get("error_description"),
                )

        # 2. Fall back to device code flow
        logger.info("Initiating device code flow authentication")
        return self._device_code_flow()

    def _acquire_token_silent_with_retry(self, account: dict) -> dict | None:
        """Acquire token silently with retry logic for transient network errors.

        Args:
            account: MSAL account dict to acquire token for

        Returns:
            Token result dict from MSAL, or None if no cached token available
        """
        last_error: Exception | None = None

        for attempt in range(MSAL_MAX_RETRIES):
            try:
                return self.app.acquire_token_silent(
                    scopes=self.scopes,
                    account=account,
                )
            except requests.exceptions.RequestException as e:
                last_error = e
                if attempt < MSAL_MAX_RETRIES - 1:
                    delay = MSAL_RETRY_DELAYS[attempt]
                    jitter = delay * 0.2 * (2 * random.random() - 1)
                    actual_delay = delay + jitter
                    logger.warning(
                        "Silent token acquisition failed, retrying",
                        attempt=attempt + 1,
                        max_retries=MSAL_MAX_RETRIES,
                        delay=actual_delay,
                        error=str(e),
                    )
                    time.sleep(actual_delay)

        logger.error(
            "Silent token acquisition failed after retries",
            max_retries=MSAL_MAX_RETRIES,
            error=str(last_error),
        )
        # Return None to fall through to device code flow
        return None

    def _device_code_flow(self) -> str:
        """Run the device code flow for interactive authentication.

        Displays a prompt to the user with the verification URL and code,
        then waits for authentication to complete.

        Includes retry logic with exponential backoff for transient network errors
        during device flow initiation.

        Returns:
            Access token string

        Raises:
            AuthenticationError: If device code flow fails after retries
        """
        # Retry device flow initiation for transient network errors
        flow = self._initiate_device_flow_with_retry()

        if "user_code" not in flow:
            error_msg = flow.get("error_description", "Unknown error during flow initiation")
            logger.error("Device code flow initiation failed", error=error_msg)
            raise AuthenticationError(
                f"Failed to initiate device code flow: {error_msg}. "
                "Check that 'Allow public client flows' is enabled in Azure Portal: "
                "App registrations → Your app → Authentication → Advanced settings"
            )

        # Display authentication instructions with rich formatting
        self._display_auth_prompt(
            verification_uri=flow["verification_uri"],
            user_code=flow["user_code"],
        )

        # Wait for user to complete authentication (with retry for network issues)
        result = self._acquire_token_with_retry(flow)

        if "access_token" not in result:
            error = result.get("error", "unknown_error")
            error_desc = result.get("error_description", "Authentication failed")

            # Provide helpful error messages based on common errors
            if error == "authorization_pending":
                logger.error("Authentication timed out waiting for user")
                raise AuthenticationError(
                    "Authentication timed out. Please try again and complete the "
                    "sign-in process within the time limit."
                )
            elif error == "authorization_declined":
                logger.error("User declined authentication")
                raise AuthenticationError(
                    "Authentication was declined. Please try again and accept "
                    "the permission request."
                )
            elif "AADSTS7000218" in error_desc:
                logger.error("Public client flow not enabled")
                raise AuthenticationError(
                    "Device code flow is not enabled for this application. "
                    "In Azure Portal: App registrations → Your app → Authentication → "
                    "Advanced settings → Set 'Allow public client flows' to Yes"
                )
            else:
                logger.error(
                    "Device code flow authentication failed",
                    error=error,
                    description=error_desc,
                )
                raise AuthenticationError(f"Authentication failed: {error_desc}")

        self._save_cache()
        logger.info(
            "Authentication successful",
            username=result.get("id_token_claims", {}).get("preferred_username", "unknown"),
        )
        return result["access_token"]

    def _initiate_device_flow_with_retry(self) -> dict:
        """Initiate device flow with retry logic for transient network errors.

        Returns:
            Device flow dict from MSAL

        Raises:
            AuthenticationError: If all retries fail
        """
        last_error: Exception | None = None

        for attempt in range(MSAL_MAX_RETRIES):
            try:
                return self.app.initiate_device_flow(scopes=self.scopes)
            except requests.exceptions.RequestException as e:
                last_error = e
                if attempt < MSAL_MAX_RETRIES - 1:
                    delay = MSAL_RETRY_DELAYS[attempt]
                    # Add jitter (±20%)
                    jitter = delay * 0.2 * (2 * random.random() - 1)
                    actual_delay = delay + jitter
                    logger.warning(
                        "Device flow initiation failed, retrying",
                        attempt=attempt + 1,
                        max_retries=MSAL_MAX_RETRIES,
                        delay=actual_delay,
                        error=str(e),
                    )
                    time.sleep(actual_delay)

        raise AuthenticationError(
            f"Failed to initiate device code flow after {MSAL_MAX_RETRIES} attempts: {last_error}. "
            "Check your network connection and try again."
        ) from last_error

    def _acquire_token_with_retry(self, flow: dict) -> dict:
        """Acquire token by device flow with retry logic for transient errors.

        Note: This only retries on network errors, not on user-interaction errors
        like timeout or declined authorization.

        Args:
            flow: Device flow dict from initiate_device_flow

        Returns:
            Token result dict from MSAL
        """
        last_error: Exception | None = None

        for attempt in range(MSAL_MAX_RETRIES):
            try:
                return self.app.acquire_token_by_device_flow(flow)
            except requests.exceptions.RequestException as e:
                last_error = e
                if attempt < MSAL_MAX_RETRIES - 1:
                    delay = MSAL_RETRY_DELAYS[attempt]
                    jitter = delay * 0.2 * (2 * random.random() - 1)
                    actual_delay = delay + jitter
                    logger.warning(
                        "Token acquisition failed, retrying",
                        attempt=attempt + 1,
                        max_retries=MSAL_MAX_RETRIES,
                        delay=actual_delay,
                        error=str(e),
                    )
                    time.sleep(actual_delay)

        # Return error dict for consistent handling
        return {
            "error": "network_error",
            "error_description": f"Network error after {MSAL_MAX_RETRIES} retries: {last_error}",
        }

    def _display_auth_prompt(self, verification_uri: str, user_code: str) -> None:
        """Display authentication instructions to the user.

        Args:
            verification_uri: URL where user should authenticate
            user_code: Code the user needs to enter
        """
        panel_content = (
            f"To authenticate, open a browser and go to:\n\n"
            f"  [bold blue]{verification_uri}[/bold blue]\n\n"
            f"Enter this code: [bold green]{user_code}[/bold green]\n\n"
            f"Waiting for authentication..."
        )

        console.print()
        console.print(
            Panel(
                panel_content,
                title="Microsoft Authentication Required",
                border_style="bright_blue",
            )
        )
        console.print()

    def _load_cache(self) -> None:
        """Load the token cache from disk if it exists."""
        if self.token_cache_path.exists():
            try:
                cache_content = self.token_cache_path.read_text()
                self.cache.deserialize(cache_content)
                logger.debug("Token cache loaded", path=str(self.token_cache_path))
            except (OSError, ValueError) as e:
                # OSError: file read errors, permission issues
                # ValueError: MSAL cache deserialization errors (invalid JSON/format)
                logger.warning(
                    "Failed to load token cache, will re-authenticate",
                    path=str(self.token_cache_path),
                    error=str(e),
                )

    def _save_cache(self) -> None:
        """Save the token cache to disk with restricted permissions.

        The cache file is created with mode 600 (owner read/write only)
        to protect the sensitive refresh tokens it contains.
        """
        if not self.cache.has_state_changed:
            return

        try:
            # Ensure parent directory exists
            self.token_cache_path.parent.mkdir(parents=True, exist_ok=True)

            # Write cache content
            cache_content = self.cache.serialize()
            self.token_cache_path.write_text(cache_content)

            # Set restrictive permissions (owner read/write only)
            os.chmod(self.token_cache_path, stat.S_IRUSR | stat.S_IWUSR)

            logger.debug("Token cache saved", path=str(self.token_cache_path))
        except OSError as e:
            # OSError covers: file write errors, permission issues, disk full, etc.
            logger.error(
                "Failed to save token cache",
                path=str(self.token_cache_path),
                error=str(e),
            )
            # Don't raise - token will just need to be re-acquired next time

    def get_accounts(self) -> list[dict]:
        """Get the list of cached accounts.

        Returns:
            List of account dictionaries with user info
        """
        return self.app.get_accounts()

    def clear_cache(self) -> None:
        """Clear the token cache and delete the cache file.

        Use this to force re-authentication or when revoking access.
        """
        # Remove all accounts from MSAL cache
        for account in self.app.get_accounts():
            self.app.remove_account(account)

        # Delete cache file if it exists
        if self.token_cache_path.exists():
            try:
                self.token_cache_path.unlink()
                logger.info("Token cache cleared", path=str(self.token_cache_path))
            except OSError as e:
                logger.warning(
                    "Failed to delete token cache file",
                    path=str(self.token_cache_path),
                    error=str(e),
                )
