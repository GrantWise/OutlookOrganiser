"""Chat assistant tool definitions and execution functions.

Defines the four tools available to the chat assistant:
- reclassify_email: Reclassify current email (and thread) with immediate Graph API move
- add_auto_rule: Add an auto-routing rule to config.yaml
- update_project_signals: Add signal keywords to an existing project or area
- create_project_or_area: Create a new project or area in config.yaml

All config-modifying tools use write_config_safely() for atomic writes with backup.

Spec reference: Reference/spec/08-classification-chat.md Section 6
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from assistant.config import write_config_safely
from assistant.config_schema import (
    AreaConfig,
    AutoRuleAction,
    AutoRuleConfig,
    AutoRuleMatch,
    ProjectConfig,
    SignalsConfig,
)
from assistant.core.logging import get_logger
from assistant.web.routes import execute_email_move

if TYPE_CHECKING:
    from assistant.config_schema import AppConfig
    from assistant.db.store import DatabaseStore, Email, Suggestion

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Tool execution context
# ---------------------------------------------------------------------------


@dataclass
class ToolExecutionContext:
    """Context passed to each tool execution function."""

    email: Email
    suggestion: Suggestion
    store: DatabaseStore
    folder_manager: Any
    message_manager: Any
    config: AppConfig
    task_manager: Any = None
    category_manager: Any = None


# ---------------------------------------------------------------------------
# Tool schemas (Anthropic API format)
# ---------------------------------------------------------------------------

_PRIORITY_ENUM = [
    "P1 - Urgent Important",
    "P2 - Important",
    "P3 - Urgent Low",
    "P4 - Low",
]

_ACTION_TYPE_ENUM = [
    "Needs Reply",
    "Review",
    "Delegated",
    "FYI Only",
    "Waiting For",
    "Scheduled",
]

CHAT_TOOLS: list[dict[str, Any]] = [
    {
        "name": "reclassify_email",
        "description": (
            "Reclassify the current email (and by default all emails in the same "
            "conversation thread) with a new folder, priority, and action type. "
            "This approves the suggestion and moves the email immediately."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "folder": {
                    "type": "string",
                    "description": "Target folder path (e.g., 'Projects/Tradecore Steel')",
                },
                "priority": {
                    "type": "string",
                    "enum": _PRIORITY_ENUM,
                },
                "action_type": {
                    "type": "string",
                    "enum": _ACTION_TYPE_ENUM,
                },
                "scope": {
                    "type": "string",
                    "enum": ["thread", "single"],
                    "description": (
                        "Default 'thread': update all emails in the conversation. "
                        "Use 'single' only if the user explicitly asks to reclassify "
                        "just this one email."
                    ),
                },
                "reasoning": {
                    "type": "string",
                    "description": "Brief explanation for the reclassification",
                },
            },
            "required": ["folder", "priority", "action_type", "reasoning"],
        },
    },
    {
        "name": "add_auto_rule",
        "description": (
            "Add a new auto-routing rule to the configuration. "
            "Auto-rules skip AI classification entirely for matching emails."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Human-readable rule name",
                },
                "senders": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Sender email patterns (e.g., ['*@acme.com'])",
                },
                "subjects": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Subject keyword patterns (optional)",
                },
                "folder": {
                    "type": "string",
                    "description": "Target folder path",
                },
                "category": {
                    "type": "string",
                    "enum": _ACTION_TYPE_ENUM,
                    "description": "Action type / Outlook category to apply",
                },
                "priority": {
                    "type": "string",
                    "enum": _PRIORITY_ENUM,
                },
            },
            "required": ["name", "folder", "category", "priority"],
        },
    },
    {
        "name": "update_project_signals",
        "description": (
            "Add signal keywords or sender patterns to an existing project or area "
            "in the configuration. Signals help the AI classify emails correctly."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "target_name": {
                    "type": "string",
                    "description": "Exact name of the project or area to update",
                },
                "add_subjects": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Subject keywords to add",
                },
                "add_senders": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Sender patterns to add (e.g., '*@newdomain.com')",
                },
                "add_body_keywords": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Body keywords to add",
                },
            },
            "required": ["target_name"],
        },
    },
    {
        "name": "create_project_or_area",
        "description": (
            "Create a new project or area in the configuration with folder path "
            "and signal definitions. The Outlook folder will be auto-created if needed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "type": {
                    "type": "string",
                    "enum": ["project", "area"],
                    "description": "Whether this is a project (has an end date) or area (ongoing)",
                },
                "name": {
                    "type": "string",
                    "description": "Display name (e.g., 'Acme Corp Onboarding')",
                },
                "folder": {
                    "type": "string",
                    "description": "Folder path (must start with 'Projects/' or 'Areas/')",
                },
                "subjects": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Subject signal keywords",
                },
                "senders": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Sender patterns",
                },
                "body_keywords": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Body signal keywords",
                },
                "priority_default": {
                    "type": "string",
                    "enum": _PRIORITY_ENUM,
                    "description": "Default priority for emails in this project/area",
                },
            },
            "required": ["type", "name", "folder"],
        },
    },
]


# ---------------------------------------------------------------------------
# Tool execution functions
# ---------------------------------------------------------------------------


async def execute_reclassify(args: dict[str, Any], ctx: ToolExecutionContext) -> str:
    """Reclassify the current email (and optionally thread) with immediate move."""
    folder = args["folder"]
    priority = args["priority"]
    action_type = args["action_type"]
    scope = args.get("scope", "thread")

    # Determine target emails
    if scope == "thread" and ctx.email.conversation_id:
        thread_emails = await ctx.store.get_thread_emails(ctx.email.conversation_id, limit=50)
        # Include the current email if not in the thread result
        email_ids = {em.id for em in thread_emails}
        if ctx.email.id not in email_ids:
            thread_emails.append(ctx.email)
    else:
        thread_emails = [ctx.email]

    moved_count = 0
    errors = []

    for em in thread_emails:
        # Find or create suggestion for this email
        suggestion = await ctx.store.get_suggestion_by_email_id(em.id)

        if suggestion:
            # Update existing suggestion
            await ctx.store.approve_suggestion(
                suggestion.id,
                approved_folder=folder,
                approved_priority=priority,
                approved_action_type=action_type,
            )
        else:
            # Create new suggestion and approve it using the returned ID directly.
            # This avoids a TOCTOU race where a concurrent request could create
            # a duplicate suggestion between create and re-fetch.
            new_id = await ctx.store.create_suggestion(
                email_id=em.id,
                suggested_folder=folder,
                suggested_priority=priority,
                suggested_action_type=action_type,
                confidence=1.0,
                reasoning=f"Chat reclassification: {args.get('reasoning', '')}",
            )
            await ctx.store.approve_suggestion(
                new_id,
                approved_folder=folder,
                approved_priority=priority,
                approved_action_type=action_type,
            )

        # Execute Graph API move
        if ctx.folder_manager and ctx.message_manager:
            email_data = {
                "subject": em.subject,
                "sender_name": em.sender_name,
                "snippet": em.snippet,
                "web_link": em.web_link,
                "received_at": em.received_at,
            }
            result = execute_email_move(
                email_id=em.id,
                folder=folder,
                priority=priority,
                action_type=action_type,
                folder_manager=ctx.folder_manager,
                message_manager=ctx.message_manager,
                config=ctx.config,
                task_manager=ctx.task_manager,
                email_data=email_data,
            )
            if result["graph_error"]:
                errors.append(f"{em.subject}: {result['graph_error']}")
            else:
                moved_count += 1

            # Record task_sync if task was created
            task_info = result.get("task_info")
            if task_info:
                await ctx.store.create_task_sync(
                    email_id=em.id,
                    todo_task_id=task_info["todo_task_id"],
                    todo_list_id=task_info["todo_list_id"],
                    task_type=task_info["task_type"],
                )
        else:
            moved_count += 1  # Count as success if no Graph managers

        # Update inherited_folder for thread consistency
        await ctx.store.update_email_inherited_folder(em.id, folder)

        # Log action
        await ctx.store.log_action(
            action_type="move",
            email_id=em.id,
            details={
                "folder": folder,
                "priority": priority,
                "action_type": action_type,
                "reasoning": args.get("reasoning", ""),
                "scope": scope,
            },
            triggered_by="chat_assistant",
        )

    summary = f"Reclassified {moved_count} email(s) to {folder}."
    if errors:
        summary += f" Errors: {'; '.join(errors)}"

    logger.info(
        "chat_reclassify_complete",
        email_id=ctx.email.id,
        folder=folder,
        scope=scope,
        moved_count=moved_count,
        error_count=len(errors),
    )
    return summary


async def execute_add_auto_rule(args: dict[str, Any], ctx: ToolExecutionContext) -> str:
    """Add a new auto-routing rule to config.yaml."""
    senders = args.get("senders", [])
    subjects = args.get("subjects", [])

    if not senders and not subjects:
        return "Error: At least one of 'senders' or 'subjects' must be provided."

    # Check for conflicts with existing rules
    for existing in ctx.config.auto_rules:
        overlap_senders = set(senders) & set(existing.match.senders)
        overlap_subjects = {s.lower() for s in subjects} & {
            s.lower() for s in existing.match.subjects
        }
        if overlap_senders or overlap_subjects:
            return (
                f"Warning: Overlapping patterns with existing rule '{existing.name}'. "
                f"Overlapping senders: {list(overlap_senders)}, "
                f"subjects: {list(overlap_subjects)}. "
                f"Rule was NOT added. Please adjust the patterns."
            )

    # Build and validate new rule via Pydantic
    try:
        new_rule = AutoRuleConfig(
            name=args["name"],
            match=AutoRuleMatch(senders=senders, subjects=subjects),
            action=AutoRuleAction(
                folder=args["folder"],
                category=args["category"],
                priority=args["priority"],
            ),
        )
    except (ValueError, TypeError) as e:
        return f"Error: Invalid rule configuration: {e}"

    # Deep copy config and append rule
    new_config = ctx.config.model_copy(deep=True)
    new_config.auto_rules.append(new_rule)

    try:
        write_config_safely(new_config)
    except Exception as e:
        return f"Error: Failed to write config: {e}"

    # Invalidate folder cache so new folders are visible immediately
    if ctx.folder_manager:
        ctx.folder_manager.refresh_cache()

    await ctx.store.log_action(
        action_type="config_change",
        email_id=ctx.email.id,
        details={
            "change_type": "add_auto_rule",
            "rule_name": args["name"],
            "senders": senders,
            "subjects": subjects,
            "folder": args["folder"],
        },
        triggered_by="chat_assistant",
    )

    logger.info("chat_auto_rule_added", rule_name=args["name"])
    return (
        f"Auto-rule '{args['name']}' added successfully. "
        f"It will take effect on the next triage cycle."
    )


async def execute_update_signals(args: dict[str, Any], ctx: ToolExecutionContext) -> str:
    """Add signal keywords to an existing project or area."""
    target_name = args["target_name"]
    add_subjects = args.get("add_subjects", [])
    add_senders = args.get("add_senders", [])
    add_body_keywords = args.get("add_body_keywords", [])

    if not add_subjects and not add_senders and not add_body_keywords:
        return "Error: At least one signal list must be provided."

    # Find the target in projects or areas
    new_config = ctx.config.model_copy(deep=True)
    target = None

    for project in new_config.projects:
        if project.name == target_name:
            target = project
            break

    if not target:
        for area in new_config.areas:
            if area.name == target_name:
                target = area
                break

    if not target:
        available = [p.name for p in ctx.config.projects] + [a.name for a in ctx.config.areas]
        return f"Error: No project or area named '{target_name}'. Available: {', '.join(available)}"

    # Deduplicate and append new signals
    added = {"subjects": [], "senders": [], "body_keywords": []}

    for kw in add_subjects:
        if kw not in target.signals.subjects:
            target.signals.subjects.append(kw)
            added["subjects"].append(kw)

    for sender in add_senders:
        if sender not in target.signals.senders:
            target.signals.senders.append(sender)
            added["senders"].append(sender)

    for kw in add_body_keywords:
        if kw not in target.signals.body_keywords:
            target.signals.body_keywords.append(kw)
            added["body_keywords"].append(kw)

    total_added = sum(len(v) for v in added.values())
    if total_added == 0:
        return f"No new signals to add — all provided values already exist on '{target_name}'."

    try:
        write_config_safely(new_config)
    except Exception as e:
        return f"Error: Failed to write config: {e}"

    # Invalidate folder cache so signal changes take effect immediately
    if ctx.folder_manager:
        ctx.folder_manager.refresh_cache()

    await ctx.store.log_action(
        action_type="config_change",
        email_id=ctx.email.id,
        details={
            "change_type": "update_signals",
            "target_name": target_name,
            "added": added,
        },
        triggered_by="chat_assistant",
    )

    logger.info(
        "chat_signals_updated",
        target_name=target_name,
        added_count=total_added,
    )

    parts = []
    if added["subjects"]:
        parts.append(f"subjects: {added['subjects']}")
    if added["senders"]:
        parts.append(f"senders: {added['senders']}")
    if added["body_keywords"]:
        parts.append(f"body keywords: {added['body_keywords']}")
    return (
        f"Updated '{target_name}' with {total_added} new signal(s): "
        f"{', '.join(parts)}. Changes take effect on the next triage cycle."
    )


async def execute_create_project_or_area(args: dict[str, Any], ctx: ToolExecutionContext) -> str:
    """Create a new project or area in config.yaml."""
    entry_type = args["type"]
    name = args["name"]
    folder = args["folder"]
    priority_default = args.get("priority_default", "P2 - Important")
    subjects = args.get("subjects", [])
    senders = args.get("senders", [])
    body_keywords = args.get("body_keywords", [])

    # Validate folder uses a known PARA top-level category
    valid_prefixes = ("Projects/", "Areas/", "Reference/", "Archive/")
    if not folder.startswith(valid_prefixes):
        return f"Error: Folder must start with one of {', '.join(valid_prefixes)}. Got: '{folder}'"

    # Check for duplicate names
    existing_names = [p.name for p in ctx.config.projects] + [a.name for a in ctx.config.areas]
    if name in existing_names:
        return f"Error: A project or area named '{name}' already exists."

    # Build config entry via Pydantic (validates folder path traversal etc.)
    signals = SignalsConfig(
        subjects=subjects,
        senders=senders,
        body_keywords=body_keywords,
    )

    try:
        if entry_type == "project":
            entry = ProjectConfig(
                name=name,
                folder=folder,
                signals=signals,
                priority_default=priority_default,
            )
        else:
            entry = AreaConfig(
                name=name,
                folder=folder,
                signals=signals,
                priority_default=priority_default,
            )
    except (ValueError, TypeError) as e:
        return f"Error: Invalid configuration: {e}"

    # Deep copy and append
    new_config = ctx.config.model_copy(deep=True)
    if entry_type == "project":
        new_config.projects.append(entry)
    else:
        new_config.areas.append(entry)

    try:
        write_config_safely(new_config)
    except Exception as e:
        return f"Error: Failed to write config: {e}"

    # Invalidate folder cache so the new project/area folder is visible
    if ctx.folder_manager:
        ctx.folder_manager.refresh_cache()

    # Auto-create the Outlook folder if folder manager is available
    if ctx.folder_manager:
        try:
            folder_id = ctx.folder_manager.get_folder_id(folder)
            if not folder_id:
                ctx.folder_manager.create_folder(folder)
                logger.info("chat_created_outlook_folder", folder=folder)
        except Exception as e:
            logger.warning("chat_folder_creation_failed", folder=folder, error=str(e))
            # Non-fatal — folder will be created on first email move

    # Create taxonomy category for new areas only (projects are temporary
    # and the folder hierarchy already conveys the project)
    if ctx.category_manager and entry_type == "area":
        from assistant.graph.tasks import AREA_CATEGORY_COLOR

        try:
            existing_cats = ctx.category_manager.get_categories()
            if not any(c["displayName"] == name for c in existing_cats):
                ctx.category_manager.create_category(name, AREA_CATEGORY_COLOR)
                logger.info("chat_created_taxonomy_category", name=name, color=AREA_CATEGORY_COLOR)
        except Exception as e:
            logger.warning("chat_category_creation_failed", name=name, error=str(e))
            # Non-fatal — category can be created via bootstrap-categories later

    await ctx.store.log_action(
        action_type="config_change",
        email_id=ctx.email.id,
        details={
            "change_type": f"create_{entry_type}",
            "name": name,
            "folder": folder,
            "subjects": subjects,
            "senders": senders,
            "priority_default": priority_default,
        },
        triggered_by="chat_assistant",
    )

    logger.info(
        "chat_project_or_area_created",
        entry_type=entry_type,
        name=name,
        folder=folder,
    )
    return (
        f"Created {entry_type} '{name}' with folder {folder}. "
        f"Changes take effect on the next triage cycle."
    )


# ---------------------------------------------------------------------------
# Tool dispatcher
# ---------------------------------------------------------------------------

_TOOL_HANDLERS: dict[str, Any] = {
    "reclassify_email": execute_reclassify,
    "add_auto_rule": execute_add_auto_rule,
    "update_project_signals": execute_update_signals,
    "create_project_or_area": execute_create_project_or_area,
}


async def execute_tool(
    tool_name: str,
    tool_input: dict[str, Any],
    ctx: ToolExecutionContext,
) -> str:
    """Execute a chat tool by name and return a result string for Claude.

    All tool errors are caught and returned as error strings rather than
    raising exceptions, so Claude can relay the error to the user.

    Args:
        tool_name: Name of the tool to execute.
        tool_input: Arguments dict from Claude's tool call.
        ctx: Execution context with shared dependencies.

    Returns:
        Human-readable result string.
    """
    handler = _TOOL_HANDLERS.get(tool_name)
    if not handler:
        return f"Unknown tool: {tool_name}"

    try:
        return await handler(tool_input, ctx)
    except Exception as e:
        logger.error(
            "chat_tool_execution_failed",
            tool=tool_name,
            error=str(e),
        )
        return f"Tool execution failed: {e}"
