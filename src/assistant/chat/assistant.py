"""Chat assistant orchestrating multi-turn conversations with Claude.

Handles the core chat loop: builds context, calls Claude with tools,
executes tool calls, and returns the final response. Each request is
stateless — the frontend sends the full message history on every call.

Spec reference: Reference/spec/08-classification-chat.md Section 4
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import anthropic

from assistant.chat.prompts import build_chat_system_prompt
from assistant.chat.tools import CHAT_TOOLS, ToolExecutionContext, execute_tool
from assistant.core.logging import get_logger

if TYPE_CHECKING:
    from assistant.config_schema import AppConfig
    from assistant.db.store import DatabaseStore

logger = get_logger(__name__)

# Maximum number of tool-use rounds before stopping to prevent infinite loops
MAX_TOOL_ROUNDS = 5


@dataclass(frozen=True, slots=True)
class ChatResponse:
    """Result of a chat interaction."""

    reply: str
    actions_taken: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None


class ChatAssistant:
    """Orchestrates multi-turn chat with Claude for email classification.

    Each call to chat() is stateless — the frontend maintains message history
    and sends the full conversation on each request.
    """

    def __init__(
        self,
        anthropic_client: anthropic.Anthropic,
        store: DatabaseStore,
        config: AppConfig,
    ) -> None:
        self._client = anthropic_client
        self._store = store
        self._config = config

    async def chat(
        self,
        suggestion_id: int,
        user_messages: list[dict[str, Any]],
        folder_manager: Any | None,
        message_manager: Any | None,
        task_manager: Any | None = None,
        category_manager: Any | None = None,
    ) -> ChatResponse:
        """Process a chat turn: load context, call Claude, execute tools.

        Args:
            suggestion_id: The suggestion being discussed.
            user_messages: Full message history from the frontend.
                Each message is {role: "user"|"assistant", content: str|list}.
            folder_manager: FolderManager for Graph API operations (may be None).
            message_manager: MessageManager for Graph API operations (may be None).
            task_manager: TaskManager for To Do operations (may be None).
            category_manager: CategoryManager for category operations (may be None).

        Returns:
            ChatResponse with the assistant's reply and any actions taken.
        """
        # 1. Load suggestion and email from DB
        suggestion = await self._store.get_suggestion(suggestion_id)
        if not suggestion:
            return ChatResponse(
                reply="",
                error=f"Suggestion {suggestion_id} not found.",
            )

        email = await self._store.get_email(suggestion.email_id)
        if not email:
            return ChatResponse(
                reply="",
                error=f"Email for suggestion {suggestion_id} not found.",
            )

        # 2. Load context for the system prompt
        thread_emails = []
        if email.conversation_id:
            thread_emails = await self._store.get_thread_emails(
                email.conversation_id, exclude_id=email.id, limit=5
            )

        sender_history = await self._store.get_sender_history(email.sender_email)
        sender_profile = await self._store.get_sender_profile(email.sender_email)

        # 3. Build system prompt with all context pre-loaded
        system_prompt = build_chat_system_prompt(
            config=self._config,
            email=email,
            suggestion=suggestion,
            thread_emails=thread_emails,
            sender_history=sender_history,
            sender_profile=sender_profile,
        )

        # 4. Build tool execution context
        tool_ctx = ToolExecutionContext(
            email=email,
            suggestion=suggestion,
            store=self._store,
            folder_manager=folder_manager,
            message_manager=message_manager,
            config=self._config,
            task_manager=task_manager,
            category_manager=category_manager,
        )

        # 5. Multi-turn tool use loop
        messages = list(user_messages)  # Don't mutate the caller's list
        actions_taken: list[dict[str, Any]] = []
        assistant_text = ""

        try:
            for _round in range(MAX_TOOL_ROUNDS):
                response = self._client.messages.create(
                    model=self._config.models.chat,
                    system=system_prompt,
                    messages=messages,
                    tools=CHAT_TOOLS,
                    max_tokens=2048,
                    tool_choice={"type": "auto"},
                )

                # Check for tool calls
                tool_calls = [block for block in response.content if block.type == "tool_use"]

                if not tool_calls:
                    # Text-only response — extract and finish
                    assistant_text = _extract_text(response)
                    break

                # Append the assistant's response (with tool_use blocks)
                messages.append(
                    {
                        "role": "assistant",
                        "content": [_serialize_content_block(b) for b in response.content],
                    }
                )

                # Execute each tool and collect results
                tool_result_blocks = []
                for tc in tool_calls:
                    result_str = await execute_tool(tc.name, tc.input, tool_ctx)
                    actions_taken.append(
                        {
                            "tool_name": tc.name,
                            "input": tc.input,
                            "result": result_str,
                        }
                    )
                    tool_result_blocks.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": tc.id,
                            "content": result_str,
                        }
                    )

                # Append tool results as a user message
                messages.append(
                    {
                        "role": "user",
                        "content": tool_result_blocks,
                    }
                )
            else:
                # Reached MAX_TOOL_ROUNDS without a text-only response
                assistant_text = (
                    "I've completed the requested changes. Let me know if you need anything else."
                )

            # 6. Log the LLM interaction
            await self._log_request(response, actions_taken)

        except anthropic.RateLimitError as e:
            logger.error("chat_rate_limited", error=str(e))
            return ChatResponse(
                reply="",
                error="The AI service is temporarily busy. Please try again in a moment.",
            )
        except anthropic.APIConnectionError as e:
            logger.error("chat_connection_error", error=str(e))
            return ChatResponse(
                reply="",
                error="Could not connect to the AI service. Please check your connection.",
            )
        except anthropic.APIStatusError as e:
            logger.error("chat_api_error", status_code=e.status_code, error=str(e))
            return ChatResponse(
                reply="",
                error=f"AI service error (status {e.status_code}). Please try again.",
            )

        return ChatResponse(
            reply=assistant_text,
            actions_taken=actions_taken,
        )

    async def _log_request(
        self,
        response: anthropic.types.Message,
        actions_taken: list[dict[str, Any]],
    ) -> None:
        """Log the chat LLM request to the database."""
        try:
            from assistant.db.store import LLMLogEntry

            entry = LLMLogEntry(
                timestamp=None,
                task_type="chat",
                model=response.model,
                email_id=None,
                triage_cycle_id=None,
                prompt_json=None,
                response_json=json.dumps([_serialize_content_block(b) for b in response.content])
                if self._config.llm_logging.log_responses
                else None,
                tool_call_json=json.dumps(actions_taken) if actions_taken else None,
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
                duration_ms=None,
                error=None,
            )
            await self._store.log_llm_request(entry)
        except Exception as e:
            # LLM logging failures should never block the chat response
            logger.warning("chat_llm_log_failed", error=str(e))


# ---------------------------------------------------------------------------
# Response helpers
# ---------------------------------------------------------------------------


def _extract_text(response: anthropic.types.Message) -> str:
    """Extract text content from a Claude response."""
    parts = []
    for block in response.content:
        if block.type == "text":
            parts.append(block.text)
    return "\n".join(parts)


def _serialize_content_block(block: Any) -> dict[str, Any]:
    """Serialize an Anthropic content block to a JSON-safe dict.

    This is needed because tool_use blocks must be sent back in subsequent
    messages when continuing a multi-turn tool use conversation.
    """
    if block.type == "text":
        return {"type": "text", "text": block.text}
    if block.type == "tool_use":
        return {
            "type": "tool_use",
            "id": block.id,
            "name": block.name,
            "input": block.input,
        }
    # Fallback for unknown block types
    return {"type": block.type}
