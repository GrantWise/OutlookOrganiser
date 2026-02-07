"""System prompt template for the classification chat assistant.

Builds a context-rich system prompt anchored to a specific email suggestion,
including thread context, sender history, folder structure, and config state.

Spec reference: Reference/spec/08-classification-chat.md Section 5
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from assistant.classifier.prompts import _build_folder_list, _build_key_contacts

if TYPE_CHECKING:
    from assistant.config_schema import AppConfig
    from assistant.db.store import Email, SenderHistory, SenderProfile, Suggestion


# ---------------------------------------------------------------------------
# System prompt template
# ---------------------------------------------------------------------------

_CHAT_SYSTEM_PROMPT = """\
You are a classification assistant for the Outlook AI email triage system.
The user is reviewing a specific email and wants help with classification,
config changes, or rule creation.

You have tools to make changes. Always confirm with the user before making
config changes (adding projects, areas, or auto-rules). For reclassifications,
you can act immediately — and always reclassify the entire conversation thread
by default, not just the single email. Only use scope "single" if the user
explicitly asks to change just one email in a thread.

Be concise and action-oriented. The user is a busy CEO — don't over-explain.
After making changes, briefly confirm what you did and mention when it takes
effect.

CURRENT EMAIL:
From: {sender_name} <{sender_email}>
Subject: {subject}
Received: {received_at}
Snippet: {snippet}

CURRENT CLASSIFICATION:
Folder: {suggested_folder}
Priority: {suggested_priority}
Action Type: {suggested_action_type}
Confidence: {confidence}
Reasoning: {reasoning}

{thread_section}
{sender_history_section}
{sender_profile_section}

AVAILABLE FOLDERS:
{folder_list}

{projects_signals_section}

CURRENT AUTO-RULES ({rule_count} rules):
{auto_rules_summary}

KEY CONTACTS:
{key_contacts}
"""


def build_chat_system_prompt(
    config: AppConfig,
    email: Email,
    suggestion: Suggestion,
    thread_emails: list[Email],
    sender_history: SenderHistory | None,
    sender_profile: SenderProfile | None,
) -> str:
    """Build the system prompt for a chat session.

    Pre-loads all context into the prompt so Claude does not need fetch tools.

    Args:
        config: Current application configuration.
        email: The email being discussed.
        suggestion: The current suggestion for this email.
        thread_emails: Other emails in the same conversation thread.
        sender_history: Folder distribution for this sender (may be None).
        sender_profile: Sender profile record (may be None).

    Returns:
        Fully assembled system prompt string.
    """
    return _CHAT_SYSTEM_PROMPT.format(
        sender_name=email.sender_name or "Unknown",
        sender_email=email.sender_email,
        subject=email.subject,
        received_at=email.received_at,
        snippet=email.snippet or "(no snippet available)",
        suggested_folder=suggestion.suggested_folder,
        suggested_priority=suggestion.suggested_priority,
        suggested_action_type=suggestion.suggested_action_type,
        confidence=f"{suggestion.confidence:.0%}",
        reasoning=suggestion.reasoning or "No reasoning provided.",
        thread_section=_build_thread_section(thread_emails),
        sender_history_section=_build_sender_history_section(sender_history),
        sender_profile_section=_build_sender_profile_section(sender_profile),
        folder_list=_build_folder_list(config),
        projects_signals_section=_build_projects_signals_section(config),
        rule_count=len(config.auto_rules),
        auto_rules_summary=_build_auto_rules_summary(config),
        key_contacts=_build_key_contacts(config),
    )


# ---------------------------------------------------------------------------
# Section builders
# ---------------------------------------------------------------------------


def _build_thread_section(thread_emails: list[Email]) -> str:
    """Build the thread emails context section."""
    if not thread_emails:
        return "THREAD: This is the only email in this conversation."

    lines = [f"THREAD EMAILS ({len(thread_emails)} other messages in this conversation):"]
    for em in thread_emails[:5]:
        lines.append(
            f"  - From: {em.sender_name or em.sender_email} | "
            f"Subject: {em.subject} | "
            f"Date: {em.received_at} | "
            f"Snippet: {(em.snippet or '')[:150]}"
        )
    if len(thread_emails) > 5:
        lines.append(f"  ... and {len(thread_emails) - 5} more")
    return "\n".join(lines)


def _build_sender_history_section(sender_history: SenderHistory | None) -> str:
    """Build the sender classification history section."""
    if not sender_history or sender_history.total_emails == 0:
        return "SENDER HISTORY: No prior emails from this sender."

    lines = [f"SENDER HISTORY ({sender_history.total_emails} prior emails from this sender):"]
    for folder, count in sorted(
        sender_history.folder_distribution.items(),
        key=lambda x: x[1],
        reverse=True,
    ):
        pct = count / sender_history.total_emails * 100
        lines.append(f"  - {folder}: {count} emails ({pct:.0f}%)")
    return "\n".join(lines)


def _build_sender_profile_section(sender_profile: SenderProfile | None) -> str:
    """Build the sender profile section."""
    if not sender_profile or sender_profile.category == "unknown":
        return "SENDER PROFILE: No profile available."

    parts = [f"SENDER PROFILE: Category: {sender_profile.category}"]
    if sender_profile.default_folder:
        parts.append(f"Default folder: {sender_profile.default_folder}")
    if sender_profile.email_count:
        parts.append(f"Emails seen: {sender_profile.email_count}")
    if sender_profile.auto_rule_candidate:
        parts.append("(Auto-rule candidate)")
    return " | ".join(parts)


def _build_projects_signals_section(config: AppConfig) -> str:
    """Build the projects/areas with their signal keywords."""
    lines = ["PROJECTS AND AREAS WITH SIGNALS:"]

    for project in config.projects:
        signals = []
        if project.signals.subjects:
            signals.append(f"subjects={project.signals.subjects}")
        if project.signals.senders:
            signals.append(f"senders={project.signals.senders}")
        if project.signals.body_keywords:
            signals.append(f"body={project.signals.body_keywords}")
        signal_str = ", ".join(signals) if signals else "no signals"
        lines.append(f"  Project: {project.name} ({project.folder}) — {signal_str}")

    for area in config.areas:
        signals = []
        if area.signals.subjects:
            signals.append(f"subjects={area.signals.subjects}")
        if area.signals.senders:
            signals.append(f"senders={area.signals.senders}")
        if area.signals.body_keywords:
            signals.append(f"body={area.signals.body_keywords}")
        signal_str = ", ".join(signals) if signals else "no signals"
        lines.append(f"  Area: {area.name} ({area.folder}) — {signal_str}")

    return "\n".join(lines)


def _build_auto_rules_summary(config: AppConfig) -> str:
    """Build a summary of current auto-routing rules."""
    if not config.auto_rules:
        return "None configured."

    lines = []
    for rule in config.auto_rules:
        parts = [f"  - {rule.name}:"]
        if rule.match.senders:
            parts.append(f"senders={rule.match.senders}")
        if rule.match.subjects:
            parts.append(f"subjects={rule.match.subjects}")
        parts.append(f"→ {rule.action.folder} ({rule.action.category})")
        lines.append(" ".join(parts))
    return "\n".join(lines)
