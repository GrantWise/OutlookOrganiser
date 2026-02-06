"""Microsoft Graph API client for email ingestion.

This module provides a client for interacting with the Microsoft Graph API,
including authentication, rate limiting, and retry logic.
"""

import time
import json
from typing import Optional, Dict, Any, List, AsyncGenerator
from datetime import datetime, timedelta, timezone
import structlog
import requests
from tenacity import retry, stop_after_attempt, wait_exponential
import aiohttp
import asyncio
import urllib.parse
import functools

from backend.app.core.config import get_settings
from backend.app.core.rate_limiter import with_rate_limit
from backend.app.email.metrics import MetricsTracker

logger = structlog.get_logger()

class GraphAPIClient:
    """Client for interacting with Microsoft Graph API.
    
    All operations target the configured shared mailbox (MICROSOFT_GRAPH_SHARED_MAILBOX).
    """
    
    def __init__(self):
        """Initialize the Graph API client."""
        self.config = get_settings()
        self.metrics = MetricsTracker()
        self.session = None
        self.graph_api_url = "https://graph.microsoft.com/v1.0"
        
        # Check if shared mailbox is configured
        self.shared_mailbox = getattr(self.config, "MICROSOFT_GRAPH_SHARED_MAILBOX", None)
        if not self.shared_mailbox:
            logger.warning("MICROSOFT_GRAPH_SHARED_MAILBOX not configured, falling back to MICROSOFT_GRAPH_MAILBOX")
            self.shared_mailbox = self.config.MICROSOFT_GRAPH_MAILBOX
            
        logger.info("Graph API client initialized with shared mailbox", shared_mailbox=self.shared_mailbox)
    
    async def __aenter__(self):
        """Enter async context and create session."""
        if not self.session:
            self.session = aiohttp.ClientSession()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Exit async context and close session."""
        if self.session:
            await self.session.close()
            self.session = None
    
    def _get_access_token(self) -> str:
        """Get access token for Microsoft Graph API.
        
        Returns:
            str: Access token string for authenticating to the API
        """
        url = f"https://login.microsoftonline.com/{self.config.MICROSOFT_GRAPH_TENANT_ID}/oauth2/v2.0/token"
        data = {
            "client_id": self.config.MICROSOFT_GRAPH_CLIENT_ID,
            "client_secret": self.config.MICROSOFT_GRAPH_CLIENT_SECRET,
            "scope": "https://graph.microsoft.com/.default",
            "grant_type": "client_credentials"
        }
        
        response = requests.post(url, data=data)
        response.raise_for_status()
        
        return response.json()["access_token"]
    
    def _handle_rate_limit(self, response: requests.Response, retry_count: int = 0) -> bool:
        """Handle rate limiting by waiting if necessary.
        
        Args:
            response: The response from the API request
            retry_count: Current retry attempt count
            
        Returns:
            bool: Whether to retry the request
        """
        if response.status_code == 429:
            retry_after = int(response.headers.get('Retry-After', 2 ** retry_count))
            logger.warning("Rate limit hit, waiting before retry", wait_time=retry_after)
            self.metrics.increment_rate_limit()
            time.sleep(retry_after)
            return True
        
        if 500 <= response.status_code < 600:
            wait_time = 2 ** retry_count
            logger.warning("Server error, waiting before retry", 
                         status_code=response.status_code, 
                         wait_time=wait_time)
            self.metrics.increment_errors()
            time.sleep(wait_time)
            return True
            
        return False
    
    async def _ensure_session(self):
        """Ensure an aiohttp session exists for making requests."""
        if not self.session:
            self.session = aiohttp.ClientSession()
    
    async def _handle_api_error(self, response, url, params=None):
        """Standardized handling for API errors.
        
        Args:
            response: The API response object
            url: The URL that was requested
            params: The query parameters used in the request
            
        Raises:
            Exception: With standardized error details
        """
        error_text = await response.text()
        formatted_params = urllib.parse.urlencode(params) if params else ""
        error_details = {
            "status": response.status,
            "reason": response.reason,
            "url": url,
            "params": formatted_params,
            "response": error_text[:200]  # Limit response text size
        }
        
        logger.error("Error in Microsoft Graph API request", **error_details)
        self.metrics.increment_errors()
        
        raise Exception(f"Graph API error: status={response.status}, reason='{response.reason}', url='{url}'")
    
    async def _maybe_rate_limited(self, func, safe_rate_limit=False, **kwargs):
        """Conditionally apply rate limiting based on the safe_rate_limit parameter.
        
        Args:
            func: The function to call
            safe_rate_limit: If True, bypass rate limiting
            **kwargs: Arguments to pass to the function
            
        Returns:
            The result of calling the function
        """
        if safe_rate_limit:
            # Skip rate limiting in test scenarios
            return await func(**kwargs)
        else:
            # Apply rate limiting
            rate_limited_func = with_rate_limit(func)
            return await rate_limited_func(**kwargs)
    
    async def _get_emails_stream_impl(
        self,
        top: int = 100,
        skip: int = 0,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        max_emails: Optional[int] = None,
        include_attachments: bool = True,
        batch_size: int = 50,
        **kwargs
    ) -> AsyncGenerator[List[Dict[str, Any]], None]:
        """Implementation of get_emails_stream without rate limiting.
        
        This internal method does the actual work of streaming emails from the API.
        Rate limiting is applied at the get_emails_stream level.
        """
        # Ensure dates are timezone-aware and in UTC
        if start_date and start_date.tzinfo is None:
            start_date = start_date.replace(tzinfo=timezone.utc)
        if end_date and end_date.tzinfo is None:
            end_date = end_date.replace(tzinfo=timezone.utc)
        
        # Prepare authorization headers
        headers = {
            "Authorization": f"Bearer {self._get_access_token()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Prefer": 'outlook.body-content-type="text"'
        }
        
        email_count = 0
        batch = []
        
        # Log which mailbox we're using with detailed information
        logger.info(
            "Streaming emails from shared mailbox", 
            mailbox=self.shared_mailbox,
            start_date=start_date.isoformat() if start_date else None,
            end_date=end_date.isoformat() if end_date else None,
            max_emails=max_emails,
            batch_size=batch_size
        )
        
        try:
            # Use the shared mailbox endpoint
            endpoint = f"{self.graph_api_url}/users/{self.shared_mailbox}/mailfolders/inbox/messages"
            
            # Define fields to retrieve from the API
            select_fields = [
                "id",                    # message_id
                "internetMessageId",     # internet_message_id
                "conversationId",        # conversation_id
                "subject",               # subject
                "body",                  # body content
                "bodyPreview",           # body_preview
                "sentDateTime",          # sent_datetime
                "receivedDateTime",      # received_datetime
                "createdDateTime",       # created_datetime
                "lastModifiedDateTime",  # last_modified_datetime
                "importance",            # importance
                "hasAttachments",        # has_attachments
                "isRead",                # is_read
                "isDraft",               # is_draft
                "webLink",               # web_link
                "from",                  # sender
                "toRecipients",          # to recipients
                "ccRecipients",          # cc recipients
                "bccRecipients",         # bcc recipients
                "replyTo",               # reply to
                "categories",            # categories
                "inferenceClassification", # inference_classification
                "flag",                  # flag
                # "inReplyTo" and "references" fields are not available/valid in MS Graph API
            ]
            
            # Calculate request size based on limits
            request_top = min(top, self.config.EMAIL_MAX_PER_REQUEST)
            if max_emails is not None:
                request_top = min(request_top, max_emails)
            
            # Prepare base query parameters
            params = {
                "$top": request_top,
                "$select": ",".join(select_fields),
                "$orderby": "receivedDateTime desc"
            }
            
            # Add date filtering if dates are provided
            if start_date or end_date:
                filter_parts = []
                
                if start_date:
                    start_utc = start_date.astimezone(timezone.utc)
                    filter_parts.append(f"receivedDateTime ge {start_utc.strftime('%Y-%m-%dT%H:%M:%SZ')}")
                
                if end_date:
                    end_utc = end_date.astimezone(timezone.utc)
                    filter_parts.append(f"receivedDateTime le {end_utc.strftime('%Y-%m-%dT%H:%M:%SZ')}")
                
                params["$filter"] = " and ".join(filter_parts)
            
            # Include attachments when requested
            if include_attachments:
                params["$expand"] = "attachments"
            
            # Initialize pagination variables
            next_link = endpoint
            
            # Ensure we have a session
            await self._ensure_session()
            
            # Process pages until we have all emails or reach the maximum
            while next_link:
                # Check if we've reached max_emails
                if max_emails is not None and email_count >= max_emails:
                    logger.info(f"Reached maximum email limit of {max_emails}, stopping fetch")
                    # Yield any remaining emails in the batch
                    if batch:
                        yield batch
                    break
                
                # Determine URL and params for this request
                if next_link == endpoint:
                    # First request
                    url = endpoint
                    request_params = params
                else:
                    # Using next link URL provided by API
                    url = next_link
                    request_params = None
                
                # Make the request
                async with self.session.get(url, params=request_params, headers=headers) as response:
                    if response.status == 429:
                        retry_after = int(response.headers.get('Retry-After', 60))
                        logger.warning("Rate limit hit, waiting before retry", wait_time=retry_after)
                        self.metrics.increment_rate_limit()
                        await asyncio.sleep(retry_after)
                        continue
                    
                    if response.status >= 400:
                        await self._handle_api_error(response, url, request_params)
                    
                    data = await response.json()
                    
                    # Get emails from response
                    emails = data.get("value", [])
                    
                    # Calculate how many emails we can process in this batch
                    remaining = max_emails - email_count if max_emails is not None else len(emails)
                    emails_to_process = emails[:remaining] if remaining < len(emails) else emails
                    
                    # Process emails and add to batch
                    for email in emails_to_process:
                        email["mailbox"] = self.shared_mailbox
                        batch.append(email)
                        email_count += 1
                        
                        # Yield batch when it reaches the specified size
                        if len(batch) >= batch_size:
                            yield batch
                            batch = []
                    
                    # Get next link if available and we still need more emails
                    if max_emails is not None and email_count >= max_emails:
                        next_link = None
                    else:
                        next_link = data.get("@odata.nextLink")
            
            # Yield any remaining emails in the batch
            if batch:
                yield batch
            
            logger.info(f"Streamed {email_count} emails from shared mailbox: {self.shared_mailbox}")
            
        except Exception as e:
            logger.error(f"Error streaming emails from shared mailbox: {self.shared_mailbox}", error=str(e))
            self.metrics.increment_errors()
            # Yield any remaining emails in the batch before raising the exception
            if batch:
                yield batch
            raise
    
    async def get_emails_stream(
        self,
        top: int = 100,
        skip: int = 0,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        max_emails: Optional[int] = None,
        include_attachments: bool = True,
        batch_size: int = 50,
        safe_rate_limit: bool = False
    ) -> AsyncGenerator[List[Dict[str, Any]], None]:
        """Fetch emails from Microsoft Graph API shared mailbox as an async stream.
        
        This method yields batches of emails as they arrive from the API, reducing
        memory usage and allowing for immediate processing.
        
        Args:
            top: Maximum number of emails to fetch per request
            skip: Number of emails to skip
            start_date: Start date for email sync (timezone-aware or will be converted to UTC)
            end_date: End date for email sync (timezone-aware or will be converted to UTC)
            max_emails: Maximum total number of emails to fetch
            include_attachments: Whether to include attachments in the response
            batch_size: Size of email batches to yield (default 50)
            safe_rate_limit: If True, disables internal rate limiting (for background tasks)
            
        Yields:
            List[Dict[str, Any]]: Batches of emails from the shared mailbox
        """
        # For background tasks (safe_rate_limit=True), use the implementation directly
        # without any rate limiting to avoid event loop issues
        if safe_rate_limit:
            async for batch in self._get_emails_stream_impl(
                top=top,
                skip=skip,
                start_date=start_date,
                end_date=end_date,
                max_emails=max_emails,
                include_attachments=include_attachments,
                batch_size=batch_size
            ):
                yield batch
            return
            
        # Otherwise use the rate-limited implementation for normal API calls
        impl_generator = self._get_emails_stream_impl(
            top=top,
            skip=skip,
            start_date=start_date,
            end_date=end_date,
            max_emails=max_emails,
            include_attachments=include_attachments,
            batch_size=batch_size
        )
        
        # Apply rate limiting to each iteration
        rate_limited_next = with_rate_limit(impl_generator.__anext__)
        while True:
            try:
                batch = await rate_limited_next()
                yield batch
            except StopAsyncIteration:
                break
    
    async def get_emails(
        self,
        top: int = 100,
        skip: int = 0,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        max_emails: Optional[int] = None,
        include_attachments: bool = True,
        safe_rate_limit: bool = False
    ) -> List[Dict[str, Any]]:
        """Fetch emails from Microsoft Graph API shared mailbox.
        
        Uses the streaming implementation internally for improved memory efficiency.
        
        Args:
            top: Maximum number of emails to fetch per request
            skip: Number of emails to skip
            start_date: Start date for email sync (timezone-aware or will be converted to UTC)
            end_date: End date for email sync (timezone-aware or will be converted to UTC)
            max_emails: Maximum total number of emails to fetch
            include_attachments: Whether to include attachments in the response
            safe_rate_limit: If True, disables internal rate limiting (for testing)
            
        Returns:
            List[Dict[str, Any]]: List of emails from the shared mailbox
        """
        all_emails = []
        
        # Use the streaming implementation internally, which already handles rate limiting
        async for batch in self.get_emails_stream(
            top=top,
            skip=skip,
            start_date=start_date,
            end_date=end_date,
            max_emails=max_emails,
            include_attachments=include_attachments,
            safe_rate_limit=safe_rate_limit  # Pass along the rate limit flag
        ):
            all_emails.extend(batch)
        
        return all_emails
    
    @with_rate_limit
    async def get_inbox_info(self) -> Dict[str, Any]:
        """Get information about the shared mailbox inbox.
        
        Returns:
            Dict[str, Any]: Dictionary containing inbox metadata
        """
        headers = {
            "Authorization": f"Bearer {self._get_access_token()}",
            "Content-Type": "application/json",
            "Accept": "application/json"
        }
        
        try:
            logger.info(f"Fetching inbox info for shared mailbox: {self.shared_mailbox}")
            
            # Use the shared mailbox endpoint
            endpoint = f"{self.graph_api_url}/users/{self.shared_mailbox}/mailfolders/inbox"
            
            # Ensure we have a session
            await self._ensure_session()
            
            # Make the request
            async with self.session.get(endpoint, headers=headers) as response:
                if response.status >= 400:
                    await self._handle_api_error(response, endpoint)
                
                data = await response.json()
                
                return {
                    "mailbox": self.shared_mailbox,
                    "mailbox_type": "shared",
                    "total_items": data.get("totalItemCount", 0),
                    "unread_items": data.get("unreadItemCount", 0),
                    "last_checked": datetime.now(timezone.utc).isoformat()
                }
        
        except Exception as e:
            logger.error(f"Error retrieving inbox information for shared mailbox: {self.shared_mailbox}", error=str(e))
            self.metrics.increment_errors()
            raise 