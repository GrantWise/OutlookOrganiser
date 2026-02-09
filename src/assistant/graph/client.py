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
    DeltaTokenExpiredError,
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
            "Prefer": 'IdType="ImmutableId"',
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

        Delegates to TokenBucket.consume_sync() to proactively rate limit
        requests to Microsoft Graph API, preventing 429 responses.
        """
        self._rate_bucket.consume_sync()

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

    # ------------------------------------------------------------------
    # Delta query support (Phase 2 — Feature 2A)
    # ------------------------------------------------------------------

    # Safety limit to prevent runaway pagination on corrupted delta streams
    DELTA_MAX_PAGES = 100

    def get_delta_messages(
        self,
        folder_id: str,
        delta_token: str | None,
        select_fields: str | None = None,
        max_items: int = 200,
        max_pages: int | None = None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Fetch messages using a delta query.

        Delta queries return only messages that have changed since the last sync.
        On the first call (no delta_token), performs a full initial sync and
        returns a delta token for subsequent incremental calls.

        Args:
            folder_id: Graph API folder ID (or well-known name like 'Inbox')
            delta_token: Token from previous delta response, or None for initial sync
            select_fields: Comma-separated list of fields to select
            max_items: Maximum total items to collect across all pages
            max_pages: Maximum pages to fetch (overrides DELTA_MAX_PAGES if smaller)

        Returns:
            Tuple of (messages, new_delta_token). If the delta stream has no more
            changes, messages will be empty and a new token is still returned.

        Raises:
            DeltaTokenExpiredError: If the token has expired (410 Gone)
            GraphAPIError: For other API errors
        """
        all_messages: list[dict[str, Any]] = []

        if delta_token:
            # Incremental sync — use the stored deltaLink directly
            next_url = delta_token
            params: dict[str, Any] | None = None
        else:
            # Initial sync — build the delta query endpoint
            next_url = None
            endpoint = f"/me/mailFolders/{folder_id}/messages/delta"
            params = {}
            if select_fields:
                params["$select"] = select_fields

        new_delta_token: str | None = None
        page_count = 0
        page_limit = min(max_pages, self.DELTA_MAX_PAGES) if max_pages else self.DELTA_MAX_PAGES

        while True:
            if page_count >= page_limit:
                logger.warning(
                    "delta_query_page_limit_reached",
                    pages=page_count,
                    items=len(all_messages),
                    folder_id=folder_id,
                )
                break

            try:
                if next_url:
                    response = self.get(next_url)
                else:
                    response = self.get(endpoint, params=params)
            except GraphAPIError as e:
                if e.status_code == 410:
                    raise DeltaTokenExpiredError(
                        f"Delta token expired for folder '{folder_id}'. "
                        "Performing full sync this cycle.",
                        folder=folder_id,
                    ) from e
                raise

            items = response.get("value", [])
            all_messages.extend(items)
            page_count += 1

            # Check for @odata.nextLink (more pages) vs @odata.deltaLink (done)
            next_link = response.get("@odata.nextLink")
            delta_link = response.get("@odata.deltaLink")

            if next_link:
                next_url = next_link
            elif delta_link:
                new_delta_token = delta_link
                break
            else:
                # No next or delta link — end of stream
                break

            if len(all_messages) >= max_items:
                logger.debug(
                    "delta_query_max_items_reached",
                    max_items=max_items,
                    items=len(all_messages),
                )
                break

        logger.info(
            "delta_query_complete",
            folder_id=folder_id,
            pages=page_count,
            messages=len(all_messages),
            has_token=new_delta_token is not None,
            was_incremental=delta_token is not None,
        )

        return all_messages, new_delta_token

    # ------------------------------------------------------------------
    # Batch request support (Phase 2 — C3 remediation)
    # ------------------------------------------------------------------

    BATCH_MAX_SIZE = 20  # Graph API limit per $batch POST

    def batch_request(
        self,
        operations: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Execute multiple Graph API operations in a single $batch request.

        Each operation dict must contain:
            - id: Unique string to correlate request with response
            - method: HTTP method (GET, POST, PATCH, DELETE)
            - url: Relative URL (e.g., "/me/messages/{id}/move")

        Optional per operation:
            - body: JSON body for POST/PATCH
            - headers: Additional headers (Content-Type added automatically for body)

        If more than 20 operations are provided, they are chunked into
        multiple batch calls automatically.

        Args:
            operations: List of operation dicts

        Returns:
            List of response dicts, each with 'id', 'status', 'body' keys,
            ordered by operation id.

        Raises:
            GraphAPIError: If the batch POST itself fails (not individual ops)
        """
        if not operations:
            return []

        all_responses: list[dict[str, Any]] = []

        # Chunk into groups of BATCH_MAX_SIZE
        for chunk_start in range(0, len(operations), self.BATCH_MAX_SIZE):
            chunk = operations[chunk_start : chunk_start + self.BATCH_MAX_SIZE]

            # Ensure Content-Type header on operations with a body
            requests_payload = []
            for op in chunk:
                req: dict[str, Any] = {
                    "id": op["id"],
                    "method": op["method"],
                    "url": op["url"],
                }
                if "body" in op and op["body"] is not None:
                    req["body"] = op["body"]
                    req["headers"] = op.get("headers", {})
                    req["headers"].setdefault("Content-Type", "application/json")
                elif "headers" in op:
                    req["headers"] = op["headers"]
                requests_payload.append(req)

            # Single rate-limit token per batch call
            self._consume_rate_limit_token()

            logger.info(
                "batch_request_sending",
                operation_count=len(chunk),
                chunk_start=chunk_start,
            )

            response = self.post("/$batch", json={"requests": requests_payload})

            responses = response.get("responses", [])
            all_responses.extend(responses)

            logger.info(
                "batch_request_complete",
                sent=len(chunk),
                received=len(responses),
            )

        # Sort by id to match input order
        all_responses.sort(key=lambda r: r.get("id", ""))

        return all_responses

    def batch_move_messages(
        self,
        moves: list[tuple[str, str]],
    ) -> list[dict[str, Any]]:
        """Move multiple messages to destination folders in a single batch.

        Args:
            moves: List of (message_id, destination_folder_id) tuples

        Returns:
            List of result dicts, each with:
                - id: The message_id
                - success: bool
                - status: HTTP status code
                - body: Response body (moved message or error)
        """
        if not moves:
            return []

        operations = [
            {
                "id": msg_id,
                "method": "POST",
                "url": f"/me/messages/{msg_id}/move",
                "body": {"destinationId": folder_id},
            }
            for msg_id, folder_id in moves
        ]

        raw_responses = self.batch_request(operations)

        results = []
        for resp in raw_responses:
            status = resp.get("status", 0)
            results.append(
                {
                    "id": resp.get("id", ""),
                    "success": 200 <= status < 300,
                    "status": status,
                    "body": resp.get("body", {}),
                }
            )

        succeeded = sum(1 for r in results if r["success"])
        failed = len(results) - succeeded
        logger.info(
            "batch_move_complete",
            total=len(moves),
            succeeded=succeeded,
            failed=failed,
        )

        return results
