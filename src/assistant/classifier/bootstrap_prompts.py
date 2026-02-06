"""Bootstrap-specific prompt templates and YAML response parsing.

Provides prompt templates for the two-pass bootstrap scanner:
- Pass 1: Batch analysis (groups of 50 emails) with Claude Sonnet
- Pass 2: Consolidation (merge all batch results) with Claude Sonnet

Unlike the triage classifier (which uses tool use), bootstrap prompts
expect plain-text YAML responses from Claude.

Spec reference: Reference/spec/04-prompts.md Sections 1-2

Usage:
    from assistant.classifier.bootstrap_prompts import (
        build_batch_analysis_prompt,
        build_consolidation_prompt,
        format_email_for_batch,
        parse_batch_yaml_response,
        parse_consolidated_yaml_response,
    )

    prompt = build_batch_analysis_prompt(
        batch_number=1, total_batches=60,
        email_batch=formatted_emails,
    )
"""

from __future__ import annotations

from typing import Any

import yaml

from assistant.core.logging import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# YAML schemas (shown to Claude as response format examples)
# ---------------------------------------------------------------------------

BATCH_ANALYSIS_YAML_SCHEMA = """\
projects:
  - name: "Project Name"
    folder: "Projects/Project Name"
    signals:
      subjects: ["keyword1", "keyword2"]
      senders: ["*@domain.com"]
      body_keywords: ["keyword3"]
    estimated_volume_percent: 5.0

areas:
  - name: "Area Name"
    folder: "Areas/Area Name"
    signals:
      subjects: ["keyword1"]
      senders: ["*@domain.com"]
      body_keywords: []
    estimated_volume_percent: 10.0

sender_clusters:
  newsletters:
    - "newsletter@example.com"
  automated:
    - "noreply@service.com"
  key_contacts:
    - email: "ceo@partner.com"
      role: "Partner CEO"
  clients:
    - "contact@client.com"
  vendors:
    - "sales@vendor.com"
  internal:
    - "colleague@company.com"

unclassified_percent: 5.0\
"""

CONSOLIDATED_YAML_SCHEMA = """\
projects:
  - name: "Project Name"
    folder: "Projects/Project Name"
    signals:
      subjects: ["keyword1", "keyword2"]
      senders: ["*@domain.com"]
      body_keywords: ["keyword3"]
    priority_default: "P2 - Important"

areas:
  - name: "Area Name"
    folder: "Areas/Area Name"
    signals:
      subjects: ["keyword1"]
      senders: ["*@domain.com"]
      body_keywords: []
    priority_default: "P3 - Urgent Low"

auto_rules:
  - name: "Rule Name"
    match:
      senders: ["notifications@github.com"]
      subjects: []
    action:
      folder: "Reference/Dev Notifications"
      category: "FYI Only"
      priority: "P4 - Low"

key_contacts:
  - email: "ceo@partner.com"
    role: "Partner CEO"
    priority_boost: 1

sender_clusters:
  newsletters:
    - "newsletter@example.com"
  automated:
    - "noreply@service.com"
  clients:
    - "contact@client.com"
  vendors:
    - "sales@vendor.com"
  internal:
    - "colleague@company.com"\
"""


# ---------------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------------

_BATCH_ANALYSIS_TEMPLATE = """\
You are an email analysis assistant analyzing a batch of emails from a \
business executive's Outlook inbox to identify organizational patterns.

The user is a CEO of a manufacturing software company with 350+ customers. \
They manage active implementation projects, ongoing business areas, sales, \
partnerships, and personal matters.

Analyze the following emails and identify:

1. PROJECTS - Active work streams with defined outcomes or deadlines.
   For each: name, suggested folder path (Projects/Name), signal keywords \
(subjects, body terms), key sender domains.

2. AREAS - Ongoing responsibilities that don't have end dates.
   For each: name, suggested folder path (Areas/Name), signal keywords, \
key sender domains.

3. SENDER CLUSTERS - Groups of senders that should be auto-routed:
   - Newsletters and marketing emails (look for patterns: marketing language, \
unsubscribe mentions, bulk-send patterns)
   - Automated notifications (CI/CD, monitoring, calendar, system alerts)
   - Key contacts who should get priority boosts
   - Clients, vendors, and internal colleagues

4. ESTIMATED VOLUME - Rough percentage of total email each category represents.

Respond with ONLY valid YAML (no markdown fences, no explanatory text) \
matching this schema:

{yaml_schema}

Here are the emails to analyze (batch {batch_number} of {total_batches}):

{email_batch}\
"""

_CONSOLIDATION_TEMPLATE = """\
You are consolidating multiple analysis batches of the same executive's \
inbox into a single unified organizational taxonomy.

Below are {batch_count} separate analyses of different email batches from \
the same mailbox. Each batch independently identified projects, areas, and \
sender clusters. There WILL be duplicates, near-duplicates, and conflicting \
classifications that you must resolve.

Your task:
1. MERGE projects that refer to the same work stream under different names. \
Pick the clearest, most specific name. Combine signal keywords from all mentions.
2. MERGE areas that overlap. Combine signal keywords.
3. RESOLVE sender conflicts: if one batch says a sender is "newsletter" and \
another says "key contact", examine the evidence and pick the correct classification.
4. DEDUPLICATE signal keywords within each project/area.
5. ESTIMATE overall volume percentages based on cross-batch totals.
6. GENERATE auto_rules for sender clusters with high-confidence routing \
(newsletters, automated notifications).
7. IDENTIFY key_contacts with suggested priority_boost values.

Respond with ONLY valid YAML (no markdown fences, no explanatory text) \
matching this schema:

{yaml_schema}

Here are the batch analyses to consolidate:

{all_batch_results}\
"""

_YAML_RETRY_MESSAGE = (
    "Your previous response was not valid YAML. Please respond with ONLY "
    "valid YAML matching the schema. No markdown fences, no explanatory "
    "text before or after the YAML."
)


# ---------------------------------------------------------------------------
# Prompt builders
# ---------------------------------------------------------------------------


def build_batch_analysis_prompt(
    batch_number: int,
    total_batches: int,
    email_batch: str,
) -> str:
    """Build the Pass 1 prompt for a batch of emails.

    Args:
        batch_number: Current batch number (1-indexed)
        total_batches: Total number of batches
        email_batch: Formatted email text (from format_email_for_batch)

    Returns:
        Complete prompt string for Claude
    """
    return _BATCH_ANALYSIS_TEMPLATE.format(
        yaml_schema=BATCH_ANALYSIS_YAML_SCHEMA,
        batch_number=batch_number,
        total_batches=total_batches,
        email_batch=email_batch,
    )


def build_consolidation_prompt(
    batch_count: int,
    all_batch_results: str,
) -> str:
    """Build the Pass 2 prompt for consolidating batch results.

    Args:
        batch_count: Number of batch results being consolidated
        all_batch_results: YAML-formatted string of all batch results

    Returns:
        Complete prompt string for Claude
    """
    return _CONSOLIDATION_TEMPLATE.format(
        yaml_schema=CONSOLIDATED_YAML_SCHEMA,
        batch_count=batch_count,
        all_batch_results=all_batch_results,
    )


def get_yaml_retry_message() -> str:
    """Get the corrective message for YAML parse failures.

    Returns:
        Message to send as follow-up when Claude returns invalid YAML.
    """
    return _YAML_RETRY_MESSAGE


def format_email_for_batch(
    sender_name: str,
    sender_email: str,
    subject: str,
    received_date: str,
    snippet: str,
    current_folder: str | None = None,
) -> str:
    """Format a single email's metadata for inclusion in a batch prompt.

    Produces a compact text block (~5 lines per email) suitable for
    including 50 emails in a single prompt.

    Args:
        sender_name: Sender's display name
        sender_email: Sender's email address
        subject: Email subject line
        received_date: ISO-formatted date string
        snippet: Cleaned body snippet (will be truncated to 200 chars)
        current_folder: Current Outlook folder (optional)

    Returns:
        Formatted email text block
    """
    # Cap snippet at 200 chars for batch efficiency
    truncated_snippet = snippet[:200] if snippet else ""
    if snippet and len(snippet) > 200:
        truncated_snippet += "..."

    parts = [
        f"From: {sender_name} <{sender_email}>",
        f"Subject: {subject}",
        f"Date: {received_date}",
    ]

    if current_folder:
        parts.append(f"Current folder: {current_folder}")

    parts.append(f"Preview: {truncated_snippet}")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# YAML response parsing
# ---------------------------------------------------------------------------


def _strip_markdown_fences(text: str) -> str:
    """Strip markdown code fences from YAML text.

    Handles both ```yaml and ``` delimiters.

    Args:
        text: Raw text that may contain markdown fences

    Returns:
        Text with fences removed
    """
    stripped = text.strip()

    # Remove opening fence (```yaml or ```)
    if stripped.startswith("```"):
        first_newline = stripped.find("\n")
        if first_newline != -1:
            stripped = stripped[first_newline + 1 :]

    # Remove closing fence
    if stripped.endswith("```"):
        stripped = stripped[: -len("```")]

    return stripped.strip()


def parse_batch_yaml_response(raw_text: str) -> dict[str, Any]:
    """Parse Claude's YAML response from a batch analysis.

    Strips markdown fences if present, parses YAML, and validates
    that required top-level keys exist.

    Args:
        raw_text: Raw text response from Claude

    Returns:
        Parsed dictionary with batch analysis results

    Raises:
        ValueError: If YAML is malformed or missing required keys.
    """
    cleaned = _strip_markdown_fences(raw_text)

    if not cleaned:
        raise ValueError(
            "Empty YAML response from Claude. "
            "Expected batch analysis with projects, areas, and sender_clusters."
        )

    try:
        parsed = yaml.safe_load(cleaned)
    except yaml.YAMLError as e:
        preview = cleaned[:500]
        raise ValueError(
            f"Malformed YAML in batch analysis response. "
            f"YAML error: {e}. Preview: {preview!r}"
        ) from e

    if not isinstance(parsed, dict):
        raise ValueError(
            f"Expected YAML dict, got {type(parsed).__name__}. "
            f"Preview: {str(parsed)[:500]!r}"
        )

    # Validate required top-level keys (lenient: warn but don't fail on missing optional keys)
    required_keys = {"projects", "areas", "sender_clusters"}
    missing = required_keys - set(parsed.keys())
    if missing:
        raise ValueError(
            f"Batch analysis YAML missing required keys: {', '.join(sorted(missing))}. "
            f"Got keys: {', '.join(sorted(parsed.keys()))}"
        )

    # Ensure lists are actually lists (Claude sometimes returns None)
    for key in ("projects", "areas"):
        if parsed.get(key) is None:
            parsed[key] = []

    if parsed.get("sender_clusters") is None:
        parsed["sender_clusters"] = {}

    return parsed


def parse_consolidated_yaml_response(raw_text: str) -> dict[str, Any]:
    """Parse Claude's YAML response from consolidation.

    Same as batch parsing but validates the consolidated schema
    which includes auto_rules and key_contacts.

    Args:
        raw_text: Raw text response from Claude

    Returns:
        Parsed dictionary with consolidated taxonomy

    Raises:
        ValueError: If YAML is malformed or missing required keys.
    """
    cleaned = _strip_markdown_fences(raw_text)

    if not cleaned:
        raise ValueError(
            "Empty YAML response from consolidation. "
            "Expected unified taxonomy with projects, areas, auto_rules, and key_contacts."
        )

    try:
        parsed = yaml.safe_load(cleaned)
    except yaml.YAMLError as e:
        preview = cleaned[:500]
        raise ValueError(
            f"Malformed YAML in consolidation response. "
            f"YAML error: {e}. Preview: {preview!r}"
        ) from e

    if not isinstance(parsed, dict):
        raise ValueError(
            f"Expected YAML dict, got {type(parsed).__name__}. "
            f"Preview: {str(parsed)[:500]!r}"
        )

    # Consolidated schema requires more keys
    required_keys = {"projects", "areas"}
    missing = required_keys - set(parsed.keys())
    if missing:
        raise ValueError(
            f"Consolidated YAML missing required keys: {', '.join(sorted(missing))}. "
            f"Got keys: {', '.join(sorted(parsed.keys()))}"
        )

    # Ensure lists are actually lists
    for key in ("projects", "areas", "auto_rules", "key_contacts"):
        if parsed.get(key) is None:
            parsed[key] = []

    if parsed.get("sender_clusters") is None:
        parsed["sender_clusters"] = {}

    return parsed
