"""Rate limiting functionality for API requests.

This module provides a token bucket rate limiter for controlling the rate of API calls
to external services. It includes both a class-based implementation and decorators
for easy application to functions.

Key features:
- Token bucket algorithm for precise rate limiting
- Support for both async and sync functions
- Configurable for different services with different rate limits
- Clear error handling and logging

Standard Rate Limits by Service:
- ms_graph: 10 requests per second (Microsoft Graph API)
- claude_api: 2 requests per second (Claude API - adjust based on tier)
"""

import asyncio
import functools
import threading
import time
from collections.abc import Callable
from typing import Any, cast

from assistant.core.errors import RateLimitExceeded
from assistant.core.logging import get_logger

logger = get_logger(__name__)


class TokenBucket:
    """Token bucket rate limiter implementation.

    This implements a token bucket algorithm where tokens are added at a fixed rate,
    and each request consumes a token. If no tokens are available, the request is
    delayed until a token becomes available.

    Example:
        # Create a rate limiter that allows 1 request per second
        limiter = TokenBucket(rate=1.0, capacity=1)

        # Use in an async function
        async def make_api_call():
            await limiter.consume()  # This will wait if needed
            # Make your API call here
    """

    def __init__(
        self,
        rate: float = 1.0,
        capacity: int = 1,
        initial_tokens: int | None = None,
    ):
        """Initialize a token bucket rate limiter.

        Args:
            rate: Token refill rate per second
            capacity: Maximum number of tokens in the bucket
            initial_tokens: Initial number of tokens in the bucket (defaults to capacity)
        """
        self.rate = rate  # tokens per second
        self.capacity = capacity
        self.tokens = capacity if initial_tokens is None else initial_tokens
        self.last_refill = time.time()
        self.lock = asyncio.Lock()  # For async consume()
        self.sync_lock = threading.Lock()  # For sync wrapper

    async def consume(self, tokens: int = 1) -> bool:
        """Consume tokens from the bucket, waiting if needed.

        Args:
            tokens: Number of tokens to consume

        Returns:
            True if tokens were consumed

        Raises:
            RateLimitExceeded: If tokens cannot be consumed even after waiting
        """
        if tokens > self.capacity:
            logger.error(
                "Attempted to consume more tokens than bucket capacity",
                tokens=tokens,
                capacity=self.capacity,
            )
            raise RateLimitExceeded(
                f"Requested tokens ({tokens}) exceed bucket capacity ({self.capacity})"
            )

        async with self.lock:
            self._refill()

            if self.tokens >= tokens:
                self.tokens -= tokens
                return True

            # Need to wait for tokens to refill
            required_tokens = tokens - self.tokens
            wait_time = required_tokens / self.rate

            if wait_time > 20:  # Arbitrary large wait time limit
                logger.warning(
                    "Rate limit would require excessive wait",
                    wait_time=wait_time,
                    tokens_needed=required_tokens,
                )
                raise RateLimitExceeded(f"Rate limit exceeded, would require {wait_time:.2f}s wait")

        # Release lock during sleep to allow other consumers to check
        logger.debug(
            "Waiting for token bucket refill",
            wait_time=wait_time,
            tokens_needed=required_tokens,
        )
        await asyncio.sleep(wait_time)

        # Reacquire lock for final consume
        async with self.lock:
            self._refill()
            if self.tokens < tokens:
                logger.error(
                    "Failed to get enough tokens even after waiting",
                    tokens=self.tokens,
                    required=tokens,
                )
                raise RateLimitExceeded("Failed to get enough tokens even after waiting")

            self.tokens -= tokens
            return True

    def consume_sync(self, tokens: int = 1) -> bool:
        """Consume tokens synchronously, waiting if needed.

        Thread-safe synchronous version of consume() for use in sync contexts
        (e.g., GraphClient.request() running in a thread).

        Args:
            tokens: Number of tokens to consume

        Returns:
            True if tokens were consumed

        Raises:
            RateLimitExceeded: If tokens cannot be consumed even after waiting
        """
        if tokens > self.capacity:
            logger.error(
                "Attempted to consume more tokens than bucket capacity",
                tokens=tokens,
                capacity=self.capacity,
            )
            raise RateLimitExceeded(
                f"Requested tokens ({tokens}) exceed bucket capacity ({self.capacity})"
            )

        with self.sync_lock:
            self._refill()
            if self.tokens >= tokens:
                self.tokens -= tokens
                return True

            # Need to wait — calculate wait time inside lock
            required_tokens = tokens - self.tokens
            wait_time = required_tokens / self.rate

            if wait_time > 20:
                logger.warning(
                    "Rate limit would require excessive wait",
                    wait_time=wait_time,
                    tokens_needed=required_tokens,
                )
                raise RateLimitExceeded(f"Rate limit exceeded, would require {wait_time:.2f}s wait")

        # Release lock during sleep
        logger.debug(
            "Waiting for token bucket refill (sync)",
            wait_time=wait_time,
            tokens_needed=required_tokens,
        )
        time.sleep(wait_time)

        # Reacquire lock for final consume
        with self.sync_lock:
            self._refill()
            if self.tokens < tokens:
                logger.error(
                    "Failed to get enough tokens even after waiting",
                    tokens=self.tokens,
                    required=tokens,
                )
                raise RateLimitExceeded("Failed to get enough tokens even after waiting")
            self.tokens -= tokens
            return True

    def _refill(self) -> None:
        """Refill tokens based on elapsed time."""
        now = time.time()
        elapsed = now - self.last_refill
        new_tokens = elapsed * self.rate
        self.tokens = min(self.capacity, self.tokens + new_tokens)
        self.last_refill = now


# Global token bucket instances for different services
_buckets: dict[str, TokenBucket] = {}


def get_bucket(name: str = "default", rate: float = 1.0, capacity: int = 1) -> TokenBucket:
    """Get or create a token bucket for the given name.

    Args:
        name: Bucket name/identifier
        rate: Token refill rate if creating a new bucket
        capacity: Token capacity if creating a new bucket

    Returns:
        TokenBucket instance
    """
    if name not in _buckets:
        _buckets[name] = TokenBucket(rate=rate, capacity=capacity)

    return _buckets[name]


def rate_limit(
    bucket_name: str = "default",
    tokens: int = 1,
    rate: float = 1.0,
    capacity: int = 1,
    max_calls: int | None = None,
    time_window: int | None = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator to apply rate limiting to a function.

    This decorator can be used on both async and sync functions.

    Example:
        @rate_limit(bucket_name="claude_api", rate=2.0, capacity=2)
        async def call_claude_api():
            # API call that will be rate limited to 2 calls per second

        # Or using max_calls/time_window style:
        @rate_limit(max_calls=10, time_window=60)
        async def call_api():
            # API call that will be rate limited to 10 calls per minute

    Args:
        bucket_name: Name of the token bucket to use
        tokens: Number of tokens to consume per call
        rate: Token refill rate per second
        capacity: Maximum number of tokens in the bucket
        max_calls: Alternative rate limit style - max calls in time window
        time_window: Time window in seconds for max_calls

    Returns:
        Decorated function
    """
    # If using max_calls/time_window style, convert to rate/capacity
    actual_rate = rate
    actual_capacity = capacity
    if max_calls is not None and time_window is not None:
        actual_rate = max_calls / time_window
        actual_capacity = max_calls

    def decorator[F: Callable[..., Any]](func: F) -> F:
        @functools.wraps(func)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            bucket = get_bucket(bucket_name, actual_rate, actual_capacity)
            try:
                await bucket.consume(tokens)
                return await func(*args, **kwargs)
            except Exception as e:
                logger.error(
                    "Error in rate-limited function",
                    function=func.__name__,
                    bucket=bucket_name,
                    error_type=type(e).__name__,
                    exc_info=True,
                )
                raise

        @functools.wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            bucket = get_bucket(bucket_name, actual_rate, actual_capacity)
            try:
                bucket.consume_sync(tokens)
                return func(*args, **kwargs)
            except Exception as e:
                logger.error(
                    "Error in rate-limited function",
                    function=func.__name__,
                    bucket=bucket_name,
                    error_type=type(e).__name__,
                    exc_info=True,
                )
                raise

        # Choose the appropriate wrapper based on whether the function is async
        if asyncio.iscoroutinefunction(func):
            return cast(F, async_wrapper)
        else:
            return cast(F, sync_wrapper)

    return decorator


def with_rate_limit[F: Callable[..., Any]](func: F) -> F:
    """Simple decorator with default MS Graph API rate limiting.

    This is a convenience decorator that uses default settings appropriate
    for Microsoft Graph API (10 requests per second).

    For more control, use the rate_limit decorator directly.

    Args:
        func: Function to decorate

    Returns:
        Decorated function with rate limiting
    """
    # Default settings for MS Graph API
    # Microsoft Graph API allows up to 10 requests per second per app
    return rate_limit(bucket_name="ms_graph", rate=10.0, capacity=10)(func)
