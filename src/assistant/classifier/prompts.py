"""Prompt context assembler and tool definition for Claude classification.

Builds the system prompt, user message, and tool definition used by the
Claude classifier. The system prompt is built once per triage cycle (it
depends on config, not per-email data). The user message is assembled
per-email with conditional context sections.

Spec reference: Reference/spec/04-prompts.md Section 3

Usage:
    from assistant.classifier.prompts import PromptAssembler, CLASSIFY_EMAIL_TOOL

    assembler = PromptAssembler()
    system = assembler.build_system_prompt(config, preferences=None)
    message = assembler.build_user_message(
        sender_name="John", sender_email="john@example.com",
        subject="Re: Project Update", snippet="...",
        ...
    )
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from assistant.config_schema import AppConfig
    from assistant.db.store import SenderProfile
    from assistant.engine.thread_utils import SenderHistoryResult, ThreadContext

# ---------------------------------------------------------------------------
# Tool definition (matches spec 04-prompts.md Section 3)
# ---------------------------------------------------------------------------

CLASSIFY_EMAIL_TOOL: dict[str, Any] = {
    "name": "classify_email",
    "description": "Classify an email into the organizational structure",
    "input_schema": {
        "type": "object",
        "properties": {
            "folder": {
                "type": "string",
                "description": (
                    "Exact folder path from the structure (e.g., 'Projects/Tradecore Steel')"
                ),
            },
            "priority": {
                "type": "string",
                "enum": [
                    "P1 - Urgent Important",
                    "P2 - Important",
                    "P3 - Urgent Low",
                    "P4 - Low",
                ],
            },
            "action_type": {
                "type": "string",
                "enum": [
                    "Needs Reply",
                    "Review",
                    "Delegated",
                    "FYI Only",
                    "Waiting For",
                    "Scheduled",
                ],
            },
            "confidence": {
                "type": "number",
                "minimum": 0.0,
                "maximum": 1.0,
                "description": "Classification confidence score",
            },
            "reasoning": {
                "type": "string",
                "description": "One sentence explaining the classification",
            },
            "waiting_for_detail": {
                "type": ["object", "null"],
                "properties": {
                    "expected_from": {"type": "string"},
                    "description": {"type": "string"},
                },
                "description": ("If action_type is Waiting For, who and what we're waiting for"),
            },
            "suggested_new_project": {
                "type": ["string", "null"],
                "description": (
                    "If the email doesn't fit existing structure, suggest a new project name"
                ),
            },
        },
        "required": ["folder", "priority", "action_type", "confidence", "reasoning"],
    },
}

# Valid enum values for response validation
VALID_PRIORITIES = frozenset(CLASSIFY_EMAIL_TOOL["input_schema"]["properties"]["priority"]["enum"])
VALID_ACTION_TYPES = frozenset(
    CLASSIFY_EMAIL_TOOL["input_schema"]["properties"]["action_type"]["enum"]
)


# ---------------------------------------------------------------------------
# Classification context (per-email data for prompt assembly)
# ---------------------------------------------------------------------------


@dataclass
class ClassificationContext:
    """Per-email context data for prompt assembly.

    Attributes:
        inherited_folder: Folder from thread inheritance (if applicable)
        thread_context: Prior messages in the thread
        sender_history: Historical folder distribution for this sender
        sender_profile: Sender profile from database (if exists)
        thread_depth: Reply depth (0 = original, 1+ = replies)
        has_user_reply: Whether user has already replied in this thread
    """

    inherited_folder: str | None = None
    thread_context: ThreadContext | None = None
    sender_history: SenderHistoryResult | None = None
    sender_profile: SenderProfile | None = None
    thread_depth: int = 0
    has_user_reply: bool = False


# ---------------------------------------------------------------------------
# Prompt assembler
# ---------------------------------------------------------------------------


class PromptAssembler:
    """Builds classification prompts with conditional context sections.

    The system prompt is config-dependent and can be built once per triage
    cycle. The user message is per-email and includes conditional sections
    based on available context.
    """

    def build_system_prompt(
        self,
        config: AppConfig,
        preferences: str | None = None,
    ) -> str:
        """Assemble the system prompt with folder structure and key contacts.

        Args:
            config: Application configuration
            preferences: Learned classification preferences from agent_state
                (or None if not yet available)

        Returns:
            Complete system prompt string
        """
        folder_list = _build_folder_list(config)
        key_contacts = _build_key_contacts(config)
        prefs_text = preferences or "No learned preferences yet."

        return _SYSTEM_PROMPT_TEMPLATE.format(
            folders_from_config=folder_list,
            key_contacts_from_config=key_contacts,
            classification_preferences=prefs_text,
        )

    def build_user_message(
        self,
        sender_name: str,
        sender_email: str,
        subject: str,
        received_datetime: str,
        importance: str,
        is_read: bool,
        flag_status: str,
        snippet: str,
        context: ClassificationContext,
    ) -> str:
        """Assemble the per-email user message with conditional sections.

        Args:
            sender_name: Sender's display name
            sender_email: Sender's email address
            subject: Email subject line
            received_datetime: ISO-formatted received timestamp
            importance: Message importance ('low', 'normal', 'high')
            is_read: Whether the email has been read
            flag_status: Outlook flag status
            snippet: Cleaned body snippet
            context: Classification context with optional sections

        Returns:
            Complete user message string
        """
        parts: list[str] = []

        # Required header section
        parts.append("Classify this email:")
        parts.append("")
        parts.append(f"From: {sender_name} <{sender_email}>")
        parts.append(f"Subject: {subject}")
        parts.append(f"Received: {received_datetime}")
        parts.append(f"Importance: {importance}")
        parts.append(f"Read status: {'Read' if is_read else 'Unread'}")
        parts.append(f"Flag: {flag_status}")
        parts.append(f"Thread depth: {context.thread_depth}")

        # Reply state
        if context.has_user_reply:
            parts.append("Reply state: User has already replied to this thread")
        else:
            parts.append("Reply state: User has NOT replied to this thread")

        # Conditional: inherited folder
        if context.inherited_folder:
            parts.append(
                f"Inherited folder (from thread): {context.inherited_folder} "
                f"(classify priority and action_type only)"
            )

        # Body snippet
        parts.append(f"Body snippet (cleaned): {snippet}")
        parts.append("")

        # Conditional: sender history
        if context.sender_history and context.sender_history.has_strong_pattern():
            formatted = context.sender_history.format_for_prompt()
            if formatted:
                parts.append(f"Sender history: {formatted}")

        # Conditional: sender profile
        if context.sender_profile and context.sender_profile.category != "unknown":
            profile = context.sender_profile
            profile_str = (
                f"Category: {profile.category} | "
                f"Default folder: {profile.default_folder or 'none'} | "
                f"Emails seen: {profile.email_count}"
            )
            parts.append(f"Sender profile: {profile_str}")

        # Conditional: thread context (prior messages)
        parts.append("")
        if context.thread_context and context.thread_context.messages:
            parts.append("Thread context (prior messages, newest first):")
            for i, msg in enumerate(context.thread_context.messages, 1):
                received_str = msg.received_at.strftime("%Y-%m-%d %H:%M")
                parts.append(f"  [{i}] From: {msg.sender_name or 'Unknown'} <{msg.sender_email}>")
                parts.append(f"      Subject: {msg.subject}")
                parts.append(f"      Date: {received_str}")
                parts.append(f"      Snippet: {msg.snippet}")
                parts.append("")
        else:
            parts.append("Thread context (prior messages, newest first):")
            parts.append("No prior messages in this thread.")

        return "\n".join(parts)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _build_folder_list(config: AppConfig) -> str:
    """Build the folder structure string for the system prompt.

    Args:
        config: Application configuration

    Returns:
        Formatted folder list string
    """
    lines: list[str] = []

    if config.projects:
        lines.append("Projects/")
        for project in config.projects:
            lines.append(f"  {project.folder}")

    if config.areas:
        lines.append("Areas/")
        for area in config.areas:
            lines.append(f"  {area.folder}")

    # Always include reference and archive
    lines.append("Reference/")
    lines.append("  Reference/Newsletters")
    lines.append("  Reference/Dev Notifications")
    lines.append("  Reference/Calendar")
    lines.append("  Reference/Industry")
    lines.append("  Reference/Vendor Updates")
    lines.append("Archive/")

    return "\n".join(lines)


def _build_key_contacts(config: AppConfig) -> str:
    """Build the key contacts string for the system prompt.

    Args:
        config: Application configuration

    Returns:
        Formatted key contacts string, or 'None configured'
    """
    if not config.key_contacts:
        return "None configured."

    lines: list[str] = []
    for contact in config.key_contacts:
        boost_desc = (
            f"+{contact.priority_boost} priority level{'s' if contact.priority_boost > 1 else ''}"
        )
        lines.append(f"- {contact.email} ({contact.role}): {boost_desc}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# System prompt template (from spec 04-prompts.md Section 3)
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT_TEMPLATE = """\
You are an email triage assistant for a CEO of a manufacturing software company.
Classify incoming emails using the classify_email tool.

FOLDER STRUCTURE:
{folders_from_config}

PRIORITY LEVELS:
- P1 - Urgent Important: Needs action today. Client escalations, deadlines, \
blockers, executive requests.
- P2 - Important: Needs action this week. Strategic work, key decisions, \
planning, important relationships.
- P3 - Urgent Low: Quick action or delegate. Routine requests, standard replies, \
operational tasks.
- P4 - Low: Archive or defer. FYI, informational, newsletters, automated.

ACTION TYPES:
- Needs Reply: The user needs to respond to this email AND has not already replied.
- Review: The user needs to review an attachment, document, or decision.
- Delegated: This should be forwarded to someone else.
- FYI Only: Informational, no action required.
- Waiting For: The user previously sent something and is awaiting a response.
- Scheduled: Action planned for a specific date.

KEY CONTACTS (priority boost):
{key_contacts_from_config}

CLASSIFICATION HINTS:
- Use the thread context to understand short replies (e.g., "Sounds good" only \
makes sense in the context of the preceding message).
- The sender's importance flag (high/normal/low) is a useful signal: senders \
rarely mark emails as "high importance" without reason.
- If a sender history is provided, treat it as a strong prior for the folder \
assignment, but override it if the email content clearly indicates a different topic.
- If an inherited_folder is provided, the folder has already been determined by \
thread inheritance. Focus your classification on priority and action_type only.
- Thread depth indicates how deep in a reply chain this email is. Very deep \
threads (depth > 5) are more likely FYI/informational unless the latest message \
introduces a new request.
- If a sender profile is provided, treat it as context for classification. A sender \
categorized as 'newsletter' or 'automated' is a strong signal for P4/FYI Only. A \
sender categorized as 'client' or 'executive' warrants higher priority. If the \
sender's default_folder is set with high email_count, treat it similarly to \
sender_history as a strong prior for folder assignment.

LEARNED PREFERENCES (from user correction history):
{classification_preferences}
(These preferences reflect patterns the user has established through corrections. \
Treat them as strong guidance -- they represent the user's actual intent when the \
standard signals were ambiguous or misleading.)\
"""
