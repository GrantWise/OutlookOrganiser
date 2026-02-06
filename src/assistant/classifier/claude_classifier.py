"""Claude classifier using tool use for structured email classification.

Integrates auto-rules, thread inheritance, and Claude API to classify
emails into the organizational structure. Uses forced tool_choice to
guarantee structured output from Claude.

Error handling strategy:
- Transient errors (429, 5xx, network): Handled by Anthropic SDK (max_retries=3)
- Logical errors (missing fields, bad enums): App-level retry up to 3 attempts
- After 3 total attempts: Mark as failed, include in daily digest

Spec reference: Reference/spec/03-agent-behaviors.md Section 2,
                Reference/spec/04-prompts.md Section 3

Usage:
    from assistant.classifier.claude_classifier import EmailClassifier

    classifier = EmailClassifier(
        anthropic_client=client,
        store=db_store,
        thread_manager=thread_mgr,
        config=app_config,
    )
    result = await classifier.classify(email_data)
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import anthropic

from assistant.classifier.auto_rules import AutoRulesEngine
from assistant.classifier.prompts import (
    CLASSIFY_EMAIL_TOOL,
    VALID_ACTION_TYPES,
    VALID_PRIORITIES,
    ClassificationContext,
    PromptAssembler,
)
from assistant.core.errors import ClassificationError
from assistant.core.logging import get_logger

if TYPE_CHECKING:
    from assistant.config_schema import AppConfig
    from assistant.db.store import DatabaseStore

logger = get_logger(__name__)

# Max classification attempts before marking as failed
MAX_CLASSIFICATION_ATTEMPTS = 3


# ---------------------------------------------------------------------------
# Classification result
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ClassificationResult:
    """Result of email classification.

    Attributes:
        folder: Target folder path
        priority: Priority level
        action_type: Action type
        confidence: Confidence score (0.0-1.0)
        reasoning: One-sentence explanation
        method: How the classification was determined
        waiting_for_detail: Waiting-for info (if action_type is 'Waiting For')
        suggested_new_project: Suggested new project name (if applicable)
        inherited_folder: Whether folder was inherited from thread
    """

    folder: str
    priority: str
    action_type: str
    confidence: float
    reasoning: str
    method: str  # 'auto_rule', 'claude_tool_use', 'claude_inherited'
    waiting_for_detail: dict[str, str] | None = None
    suggested_new_project: str | None = None
    inherited_folder: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict for JSON storage in classification_json."""
        result: dict[str, Any] = {
            "folder": self.folder,
            "priority": self.priority,
            "action_type": self.action_type,
            "confidence": self.confidence,
            "reasoning": self.reasoning,
            "method": self.method,
        }
        if self.waiting_for_detail:
            result["waiting_for_detail"] = self.waiting_for_detail
        if self.suggested_new_project:
            result["suggested_new_project"] = self.suggested_new_project
        if self.inherited_folder:
            result["inherited_folder"] = True
        return result


# ---------------------------------------------------------------------------
# Email classifier
# ---------------------------------------------------------------------------


class EmailClassifier:
    """Classifies emails using auto-rules, thread inheritance, and Claude.

    Classification flow:
    1. Check auto-rules -> if match, return immediately
    2. Build context (thread inheritance, sender history, thread context)
    3. Call Claude with forced tool_choice -> parse result
    4. If inherited folder, merge with Claude's priority/action

    Attributes:
        _client: Anthropic API client (configured with max_retries=3)
        _store: Database store for logging and state
        _auto_rules: Auto-rules pattern matching engine
        _prompt_assembler: Prompt context assembler
        _config: Application configuration
    """

    def __init__(
        self,
        anthropic_client: anthropic.Anthropic,
        store: DatabaseStore,
        config: AppConfig,
    ):
        """Initialize the classifier.

        Args:
            anthropic_client: Anthropic API client (should be configured
                with max_retries=3 for transient error handling)
            store: Database store for LLM logging and state queries
            config: Application configuration
        """
        self._client = anthropic_client
        self._store = store
        self._config = config
        self._auto_rules = AutoRulesEngine()
        self._prompt_assembler = PromptAssembler()
        self._system_prompt: str | None = None

    async def refresh_system_prompt(self) -> None:
        """Rebuild the system prompt from current config and preferences.

        Call this at the start of each triage cycle to pick up config
        changes and updated classification preferences.
        """
        preferences = await self._store.get_state("classification_preferences")
        self._system_prompt = self._prompt_assembler.build_system_prompt(self._config, preferences)

    def classify_with_auto_rules(
        self,
        sender_email: str,
        subject: str,
    ) -> ClassificationResult | None:
        """Check if an email matches an auto-rule.

        This is a fast, synchronous check that avoids Claude API calls
        for high-confidence routing patterns.

        Args:
            sender_email: Sender's email address
            subject: Email subject line

        Returns:
            ClassificationResult if a rule matched, None otherwise
        """
        match = self._auto_rules.match(
            sender_email=sender_email,
            subject=subject,
            rules=self._config.auto_rules,
        )

        if not match:
            return None

        return ClassificationResult(
            folder=match.rule.action.folder,
            priority=match.rule.action.priority,
            action_type=match.rule.action.category,
            confidence=1.0,
            reasoning=match.match_reason,
            method="auto_rule",
        )

    async def classify_with_claude(
        self,
        email_id: str,
        sender_name: str,
        sender_email: str,
        subject: str,
        received_datetime: str,
        importance: str,
        is_read: bool,
        flag_status: str,
        snippet: str,
        context: ClassificationContext,
        model: str | None = None,
        triage_cycle_id: str | None = None,
    ) -> ClassificationResult:
        """Classify an email using Claude with forced tool use.

        Handles partial classification when an inherited folder is provided
        in the context. In that case, Claude's folder response is ignored
        and the inherited folder is used instead.

        Args:
            email_id: Graph API message ID (for logging)
            sender_name: Sender's display name
            sender_email: Sender's email address
            subject: Email subject line
            received_datetime: ISO-formatted received timestamp
            importance: Message importance ('low', 'normal', 'high')
            is_read: Whether the email has been read
            flag_status: Outlook flag status
            snippet: Cleaned body snippet
            context: Classification context with optional sections
            model: Override model (defaults to config.models.triage)
            triage_cycle_id: Correlation ID for logging

        Returns:
            ClassificationResult with folder, priority, action_type

        Raises:
            ClassificationError: After MAX_CLASSIFICATION_ATTEMPTS failures
        """
        if self._system_prompt is None:
            await self.refresh_system_prompt()

        model_name = model or self._config.models.triage
        user_message = self._prompt_assembler.build_user_message(
            sender_name=sender_name,
            sender_email=sender_email,
            subject=subject,
            received_datetime=received_datetime,
            importance=importance,
            is_read=is_read,
            flag_status=flag_status,
            snippet=snippet,
            context=context,
        )

        messages = [{"role": "user", "content": user_message}]

        # Attempt classification (app-level retry for logical failures)
        last_error: str | None = None
        for attempt in range(1, MAX_CLASSIFICATION_ATTEMPTS + 1):
            start_time = time.monotonic()
            api_response = None
            tool_call_data: dict[str, Any] | None = None

            try:
                # SDK handles transient retries (429, 5xx, connection errors)
                api_response = self._client.messages.create(
                    model=model_name,
                    max_tokens=1024,
                    system=self._system_prompt,
                    messages=messages,
                    tools=[CLASSIFY_EMAIL_TOOL],
                    tool_choice={"type": "tool", "name": "classify_email"},
                )

                duration_ms = int((time.monotonic() - start_time) * 1000)

                # Extract tool call from response
                tool_call_data = _extract_tool_call(api_response)
                if tool_call_data is None:
                    last_error = "No tool call in response (unexpected with forced tool_choice)"
                    logger.warning(
                        "classification_no_tool_call",
                        email_id=email_id,
                        attempt=attempt,
                    )
                    await self._log_request(
                        model=model_name,
                        messages=messages,
                        response=api_response,
                        tool_call=None,
                        duration_ms=duration_ms,
                        email_id=email_id,
                        triage_cycle_id=triage_cycle_id,
                        error=last_error,
                    )
                    continue

                # Validate the tool call fields
                validation_error = _validate_tool_call(tool_call_data)
                if validation_error:
                    last_error = validation_error
                    logger.warning(
                        "classification_invalid_response",
                        email_id=email_id,
                        attempt=attempt,
                        error=validation_error,
                    )
                    await self._log_request(
                        model=model_name,
                        messages=messages,
                        response=api_response,
                        tool_call=tool_call_data,
                        duration_ms=duration_ms,
                        email_id=email_id,
                        triage_cycle_id=triage_cycle_id,
                        error=validation_error,
                    )
                    continue

                # Success - log and build result
                await self._log_request(
                    model=model_name,
                    messages=messages,
                    response=api_response,
                    tool_call=tool_call_data,
                    duration_ms=duration_ms,
                    email_id=email_id,
                    triage_cycle_id=triage_cycle_id,
                )

                return _build_result(tool_call_data, context)

            except anthropic.RateLimitError as e:
                duration_ms = int((time.monotonic() - start_time) * 1000)
                last_error = f"Rate limited after SDK retries: {e}"
                logger.error(
                    "classification_rate_limited",
                    email_id=email_id,
                    attempt=attempt,
                    error=str(e),
                )
                await self._log_request(
                    model=model_name,
                    messages=messages,
                    response=None,
                    tool_call=None,
                    duration_ms=duration_ms,
                    email_id=email_id,
                    triage_cycle_id=triage_cycle_id,
                    error=last_error,
                )
                # Don't retry rate limits at app level - SDK already retried
                break

            except anthropic.APIConnectionError as e:
                duration_ms = int((time.monotonic() - start_time) * 1000)
                last_error = f"API connection error after SDK retries: {e}"
                logger.error(
                    "classification_connection_error",
                    email_id=email_id,
                    attempt=attempt,
                    error=str(e),
                )
                await self._log_request(
                    model=model_name,
                    messages=messages,
                    response=None,
                    tool_call=None,
                    duration_ms=duration_ms,
                    email_id=email_id,
                    triage_cycle_id=triage_cycle_id,
                    error=last_error,
                )
                # Don't retry connection errors at app level - SDK already retried
                break

            except anthropic.APIStatusError as e:
                duration_ms = int((time.monotonic() - start_time) * 1000)
                last_error = f"API status error {e.status_code}: {e.message}"
                logger.error(
                    "classification_api_error",
                    email_id=email_id,
                    attempt=attempt,
                    status_code=e.status_code,
                    error=str(e),
                )
                await self._log_request(
                    model=model_name,
                    messages=messages,
                    response=None,
                    tool_call=None,
                    duration_ms=duration_ms,
                    email_id=email_id,
                    triage_cycle_id=triage_cycle_id,
                    error=last_error,
                )
                # SDK retries 5xx; other status errors are not retryable
                break

        # All attempts exhausted or non-retryable error
        raise ClassificationError(
            f"Classification failed for email {email_id} after "
            f"{MAX_CLASSIFICATION_ATTEMPTS} attempts. Last error: {last_error}",
            email_id=email_id,
            attempts=MAX_CLASSIFICATION_ATTEMPTS,
        )

    async def _log_request(
        self,
        model: str,
        messages: list[dict[str, Any]],
        response: anthropic.types.Message | None,
        tool_call: dict[str, Any] | None,
        duration_ms: int,
        email_id: str | None = None,
        triage_cycle_id: str | None = None,
        error: str | None = None,
    ) -> None:
        """Log an LLM request to the database.

        Args:
            model: Model used
            messages: Messages sent
            response: API response (if available)
            tool_call: Extracted tool call data (if available)
            duration_ms: Request duration in milliseconds
            email_id: Email being classified
            triage_cycle_id: Correlation ID
            error: Error message (if failed)
        """
        if not self._config.llm_logging.enabled:
            return

        try:
            # Build serializable prompt (system prompt + messages)
            prompt_data: dict[str, Any] = {"messages": messages}
            if self._config.llm_logging.log_prompts and self._system_prompt:
                prompt_data["system"] = self._system_prompt

            # Build serializable response
            response_data: dict[str, Any] | None = None
            input_tokens: int | None = None
            output_tokens: int | None = None

            if response and self._config.llm_logging.log_responses:
                response_data = {
                    "id": response.id,
                    "model": response.model,
                    "stop_reason": response.stop_reason,
                    "content": [_content_block_to_dict(block) for block in response.content],
                }
                input_tokens = response.usage.input_tokens
                output_tokens = response.usage.output_tokens

            await self._store.log_llm_request(
                task_type="triage",
                model=model,
                prompt=prompt_data,
                response=response_data,
                tool_call=tool_call,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                duration_ms=duration_ms,
                email_id=email_id,
                error=error,
            )
        except Exception as e:
            # Logging failures should never block classification
            logger.warning(
                "llm_log_failed",
                error=str(e),
                email_id=email_id,
            )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _extract_tool_call(response: anthropic.types.Message) -> dict[str, Any] | None:
    """Extract the classify_email tool call from the API response.

    Args:
        response: Anthropic API response

    Returns:
        Tool call input dict, or None if no tool call found
    """
    for block in response.content:
        if block.type == "tool_use" and block.name == "classify_email":
            return block.input
    return None


def _validate_tool_call(data: dict[str, Any]) -> str | None:
    """Validate that a tool call contains all required fields with valid values.

    Args:
        data: Tool call input data

    Returns:
        Error message if invalid, None if valid
    """
    # Check required fields
    required = ("folder", "priority", "action_type", "confidence", "reasoning")
    missing = [f for f in required if f not in data]
    if missing:
        return f"Missing required fields: {', '.join(missing)}"

    # Validate enum values
    if data["priority"] not in VALID_PRIORITIES:
        return f"Invalid priority: '{data['priority']}'. Must be one of: {', '.join(sorted(VALID_PRIORITIES))}"

    if data["action_type"] not in VALID_ACTION_TYPES:
        return f"Invalid action_type: '{data['action_type']}'. Must be one of: {', '.join(sorted(VALID_ACTION_TYPES))}"

    # Validate confidence range
    confidence = data.get("confidence")
    if not isinstance(confidence, int | float) or confidence < 0.0 or confidence > 1.0:
        return f"Invalid confidence: {confidence}. Must be a number between 0.0 and 1.0"

    # Validate folder is not empty
    if not data.get("folder", "").strip():
        return "Empty folder path"

    return None


def _build_result(
    tool_call: dict[str, Any],
    context: ClassificationContext,
) -> ClassificationResult:
    """Build a ClassificationResult from validated tool call data.

    When an inherited folder is present in the context, the inherited
    folder takes precedence over Claude's folder suggestion.

    Args:
        tool_call: Validated tool call data
        context: Classification context

    Returns:
        ClassificationResult
    """
    # Handle inherited folder (partial classification)
    if context.inherited_folder:
        folder = context.inherited_folder
        method = "claude_inherited"
        confidence = 0.95  # Inherited folder confidence
        inherited = True
    else:
        folder = tool_call["folder"]
        method = "claude_tool_use"
        confidence = float(tool_call["confidence"])
        inherited = False

    # Extract optional fields
    waiting_for_detail = tool_call.get("waiting_for_detail")
    if isinstance(waiting_for_detail, dict):
        # Ensure it has the expected keys
        waiting_for_detail = {
            k: v
            for k, v in waiting_for_detail.items()
            if k in ("expected_from", "description") and isinstance(v, str)
        }
        if not waiting_for_detail:
            waiting_for_detail = None

    suggested_new_project = tool_call.get("suggested_new_project")
    if not isinstance(suggested_new_project, str) or not suggested_new_project.strip():
        suggested_new_project = None

    return ClassificationResult(
        folder=folder,
        priority=tool_call["priority"],
        action_type=tool_call["action_type"],
        confidence=confidence,
        reasoning=tool_call["reasoning"],
        method=method,
        waiting_for_detail=waiting_for_detail,
        suggested_new_project=suggested_new_project,
        inherited_folder=inherited,
    )


def _content_block_to_dict(block: Any) -> dict[str, Any]:
    """Convert an Anthropic content block to a serializable dict.

    Args:
        block: An Anthropic content block (TextBlock or ToolUseBlock)

    Returns:
        Serializable dictionary
    """
    if block.type == "text":
        return {"type": "text", "text": block.text}
    elif block.type == "tool_use":
        return {
            "type": "tool_use",
            "id": block.id,
            "name": block.name,
            "input": block.input,
        }
    # Fallback for unknown block types
    return {"type": block.type}
