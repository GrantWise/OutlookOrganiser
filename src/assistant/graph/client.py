"""Base Microsoft Graph API client with retry logic and error handling.

This module provides a robust HTTP client for interacting with the Microsoft
Graph API, including:
- Automatic retry with exponential backoff for transient errors
- Proper handling of rate limits (429 responses)
- Request/response logging for debugging
- Token refresh coordination with MSAL

Usage:
    from assistant.auth.msal_auth import GraphAuth
    from assistant.graph.client import GraphClient

    auth = GraphAuth(client_id, tenant_id, scopes, cache_path)
    client = GraphClient(auth)

    # Make a request
    response = client.get("/me")
    print(response["displayName"])
"""

import random
import time
from typing import Any

import requests

from assistant.auth.msal_auth import GraphAuth
from assistant.core.errors import (
    AuthenticationError,
    ConflictError,
    GraphAPIError,
    RateLimitExceeded,
)
from assistant.core.logging import get_logger
from assistant.core.rate_limiter import get_bucket

logger = get_logger(__name__)

# Microsoft Graph API base URL
GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"

# Default retry configuration
DEFAULT_MAX_RETRIES = 3
DEFAULT_RETRY_DELAYS = [1.0, 2.0, 4.0]  # Exponential backoff delays in seconds

# Rate limiting configuration
# Microsoft Graph API allows 10,000 requests per 10 minutes per app per mailbox
# We use 10 req/sec as a safe default
MS_GRAPH_RATE = 10.0  # requests per second
MS_GRAPH_CAPACITY = 10  # burst capacity


class GraphClient:
    """Microsoft Graph API client with retry logic and error handling.

    This client wraps requests to the Microsoft Graph API with:
    - Automatic token management (via GraphAuth)
    - Retry logic with exponential backoff for 5xx errors
    - Proper handling of 429 rate limit responses
    - Structured logging of requests and errors

    Attributes:
        auth: GraphAuth instance for token management
        base_url: Microsoft Graph API base URL
        max_retries: Maximum number of retry attempts
        retry_delays: List of delay times (seconds) for each retry

    Example:
        client = GraphClient(auth)

        # Simple GET request
        user = client.get("/me")

        # GET with query parameters
        messages = client.get(
            "/me/mailFolders/inbox/messages",
            params={"$top": 10, "$select": "id,subject"}
        )

        # POST request
        result = client.post(
            "/me/messages/{id}/move",
            json={"destinationId": "folder_id"}
        )
    """

    def __init__(
        self,
        auth: GraphAuth,
        base_url: str = GRAPH_BASE_URL,
        max_retries: int = DEFAULT_MAX_RETRIES,
        retry_delays: list[float] | None = None,
    ):
        """Initialize the Graph API client.

        Args:
            auth: GraphAuth instance for obtaining access tokens
            base_url: Microsoft Graph API base URL (default: v1.0 endpoint)
            max_retries: Maximum number of retry attempts for transient errors
            retry_delays: List of delay times in seconds for each retry
        """
        self.auth = auth
        self.base_url = base_url.rstrip("/")
        self.max_retries = max_retries
        self.retry_delays = retry_delays or DEFAULT_RETRY_DELAYS

        # Create a session for connection pooling
        self.session = requests.Session()

        # Initialize rate limiter bucket for MS Graph API
        self._rate_bucket = get_bucket(
            name="ms_graph",
            rate=MS_GRAPH_RATE,
            capacity=MS_GRAPH_CAPACITY,
        )

        logger.debug(
            "GraphClient initialized",
            base_url=self.base_url,
            max_retries=self.max_retries,
        )

    def _get_headers(self) -> dict[str, str]:
        """Get request headers with current access token.

        Returns:
            Dictionary of HTTP headers

        Raises:
            AuthenticationError: If token cannot be acquired
        """
        try:
            token = self.auth.get_access_token()
        except Exception as e:
            logger.error("Failed to get access token", error=str(e))
            raise AuthenticationError(
                f"Cannot authenticate with Microsoft Graph: {e}. "
                "Try running 'python -m assistant validate-config' to check your auth settings."
            ) from e

        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _make_url(self, endpoint: str) -> str:
        """Construct the full URL for an endpoint.

        Args:
            endpoint: API endpoint path (e.g., "/me" or "/me/messages")

        Returns:
            Full URL including base URL
        """
        # Handle both absolute endpoints (/me) and relative ones (me)
        if endpoint.startswith("http"):
            return endpoint  # Already a full URL (e.g., @odata.nextLink)
        if not endpoint.startswith("/"):
            endpoint = "/" + endpoint
        return self.base_url + endpoint

    def _handle_error_response(
        self, response: requests.Response, method: str, endpoint: str
    ) -> None:
        """Handle error responses from the Graph API.

        Args:
            response: The HTTP response object
            method: HTTP method used
            endpoint: API endpoint called

        Raises:
            GraphAPIError: With details from the error response
        """
        try:
            error_data = response.json()
            error_info = error_data.get("error", {})
            error_code = error_info.get("code", "unknown")
            error_message = error_info.get("message", response.text)
        except ValueError:
            error_code = "unknown"
            error_message = response.text or f"HTTP {response.status_code}"

        logger.error(
            "Graph API error",
            method=method,
            endpoint=endpoint,
            status_code=response.status_code,
            error_code=error_code,
            error_message=error_message[:200],  # Truncate long messages
        )

        # Provide specific guidance for common errors
        if response.status_code == 401:
            raise GraphAPIError(
                f"Authentication failed (401): {error_message}. "
                "Your access token may have expired. Try clearing the token cache and re-authenticating.",
                status_code=401,
                error_code=error_code,
            )
        elif response.status_code == 403:
            raise GraphAPIError(
                f"Permission denied (403): {error_message}. "
                "Check that the required API permissions are granted in Azure Portal.",
                status_code=403,
                error_code=error_code,
            )
        elif response.status_code == 404:
            raise GraphAPIError(
                f"Resource not found (404): {error_message}. "
                f"The endpoint '{endpoint}' may be incorrect or the resource doesn't exist.",
                status_code=404,
                error_code=error_code,
            )
        elif response.status_code == 429:
            # Rate limit - extract retry-after if available
            retry_after = response.headers.get("Retry-After", "unknown")
            raise RateLimitExceeded(
                f"Rate limit exceeded (429). Retry after: {retry_after} seconds. "
                "Consider reducing request frequency or adding delays between requests."
            )
        else:
            raise GraphAPIError(
                f"Graph API error ({response.status_code}): {error_message}",
                status_code=response.status_code,
                error_code=error_code,
            )

    def _should_retry(self, response: requests.Response, attempt: int) -> bool:
        """Determine if a request should be retried.

        Args:
            response: The HTTP response object
            attempt: Current attempt number (0-based)

        Returns:
            True if the request should be retried
        """
        if attempt >= self.max_retries:
            return False

        # Retry on server errors (5xx)
        if 500 <= response.status_code < 600:
            return True

        # Retry on rate limit (429) - but respect Retry-After
        if response.status_code == 429:
            return True

        return False

    def _get_retry_delay(self, response: requests.Response, attempt: int) -> float:
        """Get the delay before retrying a request.

        Includes jitter (±20%) to prevent retry storms when multiple clients
        hit rate limits simultaneously.

        Args:
            response: The HTTP response object
            attempt: Current attempt number (0-based)

        Returns:
            Delay in seconds before retrying (with jitter)
        """
        # For 429, respect Retry-After header if present
        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            if retry_after:
                try:
                    base_delay = float(retry_after)
                    # Add jitter even for Retry-After delays
                    jitter = base_delay * 0.2 * (2 * random.random() - 1)
                    return base_delay + jitter
                except ValueError:
                    pass  # Fall through to default

        # Use exponential backoff from configured delays
        if attempt < len(self.retry_delays):
            base_delay = self.retry_delays[attempt]
        else:
            base_delay = self.retry_delays[-1]  # Use last delay for additional retries

        # Add ±20% jitter to prevent retry storms
        jitter = base_delay * 0.2 * (2 * random.random() - 1)
        return base_delay + jitter

    def _consume_rate_limit_token(self) -> None:
        """Consume a rate limit token before making a request.

        Uses the token bucket algorithm to proactively rate limit requests
        to Microsoft Graph API, preventing 429 responses.

        This uses synchronous consumption with the rate limiter's sync_lock.
        """
        bucket = self._rate_bucket
        tokens = 1

        with bucket.sync_lock:
            bucket._refill()
            if bucket.tokens >= tokens:
                bucket.tokens -= tokens
                return

            # Need to wait for tokens
            required_tokens = tokens - bucket.tokens
            wait_time = required_tokens / bucket.rate

            if wait_time > 20:  # Same limit as rate_limiter.py
                logger.warning(
                    "Rate limit would require excessive wait",
                    wait_time=wait_time,
                    tokens_needed=required_tokens,
                )
                raise RateLimitExceeded(f"Rate limit exceeded, would require {wait_time:.2f}s wait")

        # Release lock during sleep
        logger.debug(
            "Rate limiting: waiting for token",
            wait_time=wait_time,
            tokens_needed=required_tokens,
        )
        time.sleep(wait_time)

        # Reacquire lock for final consume
        with bucket.sync_lock:
            bucket._refill()
            if bucket.tokens < tokens:
                raise RateLimitExceeded("Failed to get rate limit token after waiting")
            bucket.tokens -= tokens

    def request(
        self,
        method: str,
        endpoint: str,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        timeout: float = 30.0,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Make an HTTP request to the Graph API with retry logic.

        Args:
            method: HTTP method (GET, POST, PATCH, DELETE)
            endpoint: API endpoint path
            params: URL query parameters
            json: JSON body for POST/PATCH requests
            timeout: Request timeout in seconds
            extra_headers: Additional headers to include (e.g., If-Match for ETags)

        Returns:
            Parsed JSON response as a dictionary

        Raises:
            GraphAPIError: For API errors (4xx, 5xx)
            ConflictError: For 412 Precondition Failed (ETag mismatch)
            RateLimitExceeded: When rate limits cannot be recovered
            AuthenticationError: When authentication fails
        """
        url = self._make_url(endpoint)
        last_response = None

        for attempt in range(self.max_retries + 1):
            try:
                # Apply proactive rate limiting before making request
                self._consume_rate_limit_token()

                headers = self._get_headers()
                # Merge in any extra headers (e.g., If-Match for ETags)
                if extra_headers:
                    headers.update(extra_headers)

                logger.debug(
                    "Graph API request",
                    method=method,
                    endpoint=endpoint,
                    attempt=attempt + 1,
                    params=list(params.keys()) if params else None,
                )

                response = self.session.request(
                    method=method,
                    url=url,
                    headers=headers,
                    params=params,
                    json=json,
                    timeout=timeout,
                )
                last_response = response

                # Success
                if response.status_code < 400:
                    # Handle 204 No Content
                    if response.status_code == 204:
                        return {}
                    return response.json()

                # Handle 412 Precondition Failed (ETag mismatch) - don't retry
                if response.status_code == 412:
                    raise ConflictError(
                        f"Resource was modified by another client. "
                        f"The ETag did not match for {endpoint}. "
                        "Retry the operation with fresh data.",
                        resource_id=endpoint,
                    )

                # Check if we should retry
                if self._should_retry(response, attempt):
                    delay = self._get_retry_delay(response, attempt)
                    logger.warning(
                        "Retrying Graph API request",
                        method=method,
                        endpoint=endpoint,
                        status_code=response.status_code,
                        attempt=attempt + 1,
                        max_retries=self.max_retries,
                        delay=delay,
                    )
                    time.sleep(delay)
                    continue

                # Non-retryable error
                self._handle_error_response(response, method, endpoint)

            except requests.exceptions.Timeout:
                if attempt < self.max_retries:
                    delay = self.retry_delays[min(attempt, len(self.retry_delays) - 1)]
                    logger.warning(
                        "Graph API request timed out, retrying",
                        method=method,
                        endpoint=endpoint,
                        attempt=attempt + 1,
                        delay=delay,
                    )
                    time.sleep(delay)
                    continue
                raise GraphAPIError(
                    f"Request to {endpoint} timed out after {timeout}s and {self.max_retries} retries. "
                    "Microsoft Graph API may be experiencing issues.",
                    status_code=None,
                ) from None

            except requests.exceptions.ConnectionError as e:
                if attempt < self.max_retries:
                    delay = self.retry_delays[min(attempt, len(self.retry_delays) - 1)]
                    logger.warning(
                        "Graph API connection error, retrying",
                        method=method,
                        endpoint=endpoint,
                        attempt=attempt + 1,
                        error=str(e),
                        delay=delay,
                    )
                    time.sleep(delay)
                    continue
                raise GraphAPIError(
                    f"Connection to Microsoft Graph failed: {e}. "
                    "Check your internet connection and try again.",
                    status_code=None,
                ) from e

        # All retries exhausted
        if last_response is not None:
            self._handle_error_response(last_response, method, endpoint)

        raise GraphAPIError(
            f"Request to {endpoint} failed after {self.max_retries} retries",
            status_code=None,
        )

    def get(
        self,
        endpoint: str,
        params: dict[str, Any] | None = None,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        """Make a GET request to the Graph API.

        Args:
            endpoint: API endpoint path
            params: URL query parameters
            timeout: Request timeout in seconds

        Returns:
            Parsed JSON response
        """
        return self.request("GET", endpoint, params=params, timeout=timeout)

    def post(
        self,
        endpoint: str,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        """Make a POST request to the Graph API.

        Args:
            endpoint: API endpoint path
            json: JSON body data
            params: URL query parameters
            timeout: Request timeout in seconds

        Returns:
            Parsed JSON response
        """
        return self.request("POST", endpoint, params=params, json=json, timeout=timeout)

    def patch(
        self,
        endpoint: str,
        json: dict[str, Any] | None = None,
        timeout: float = 30.0,
        if_match: str | None = None,
    ) -> dict[str, Any]:
        """Make a PATCH request to the Graph API.

        Args:
            endpoint: API endpoint path
            json: JSON body data
            timeout: Request timeout in seconds
            if_match: ETag value for optimistic concurrency (If-Match header)

        Returns:
            Parsed JSON response

        Raises:
            ConflictError: If if_match is provided and ETag doesn't match (412)
        """
        extra_headers = None
        if if_match:
            extra_headers = {"If-Match": if_match}
        return self.request(
            "PATCH", endpoint, json=json, timeout=timeout, extra_headers=extra_headers
        )

    def delete(
        self,
        endpoint: str,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        """Make a DELETE request to the Graph API.

        Args:
            endpoint: API endpoint path
            timeout: Request timeout in seconds

        Returns:
            Parsed JSON response (usually empty for DELETE)
        """
        return self.request("DELETE", endpoint, timeout=timeout)

    def get_user_info(self) -> dict[str, Any]:
        """Get the current user's profile information.

        Returns:
            User profile dictionary with keys like:
            - id: User's unique ID
            - displayName: User's display name
            - mail: User's email address
            - userPrincipalName: User's principal name (may be email)

        Raises:
            GraphAPIError: If the request fails
        """
        return self.get("/me", params={"$select": "id,displayName,mail,userPrincipalName"})

    def get_user_email(self) -> str:
        """Get the current user's email address.

        Returns:
            User's email address

        Raises:
            GraphAPIError: If the request fails or email cannot be determined
        """
        user_info = self.get_user_info()

        # Try mail first, fall back to userPrincipalName
        email = user_info.get("mail") or user_info.get("userPrincipalName")

        if not email:
            raise GraphAPIError(
                "Could not determine user email. The Graph API response didn't include "
                "'mail' or 'userPrincipalName'. Check User.Read permission is granted.",
                status_code=None,
            )

        logger.info("User email detected", email=email)
        return email

    def paginate(
        self,
        endpoint: str,
        params: dict[str, Any] | None = None,
        page_size: int = 50,
        max_pages: int | None = None,
        delay_between_pages: float = 0.0,
    ) -> list[dict[str, Any]]:
        """Fetch all pages of a paginated Graph API response.

        Microsoft Graph API returns max 50 items per page with @odata.nextLink
        for pagination. This method follows all nextLink references to collect
        all results.

        Args:
            endpoint: API endpoint path
            params: Initial query parameters
            page_size: Number of items per page (default: 50, max: 50)
            max_pages: Maximum number of pages to fetch (None for unlimited)
            delay_between_pages: Delay in seconds between page requests (for rate limiting)

        Returns:
            List of all items across all pages

        Example:
            # Get all inbox messages
            messages = client.paginate(
                "/me/mailFolders/inbox/messages",
                params={"$select": "id,subject,receivedDateTime"},
            )
        """
        all_items: list[dict[str, Any]] = []
        params = dict(params) if params else {}

        # Set page size if not already specified
        if "$top" not in params:
            params["$top"] = min(page_size, 50)

        next_url: str | None = self._make_url(endpoint)
        page_count = 0

        while next_url:
            if max_pages and page_count >= max_pages:
                logger.debug(
                    "Pagination stopped at max_pages",
                    max_pages=max_pages,
                    items_collected=len(all_items),
                )
                break

            # For first page, use params; for subsequent pages, nextLink includes params
            if page_count == 0:
                response = self.get(endpoint, params=params)
            else:
                response = self.get(next_url)

            # Collect items from this page
            items = response.get("value", [])
            all_items.extend(items)

            logger.debug(
                "Pagination page fetched",
                page=page_count + 1,
                items_on_page=len(items),
                total_items=len(all_items),
            )

            # Check for next page
            next_url = response.get("@odata.nextLink")
            page_count += 1

            # Optional delay between pages (useful for rate limiting during bootstrap)
            if next_url and delay_between_pages > 0:
                time.sleep(delay_between_pages)

        logger.debug(
            "Pagination complete",
            endpoint=endpoint,
            total_pages=page_count,
            total_items=len(all_items),
        )

        return all_items
