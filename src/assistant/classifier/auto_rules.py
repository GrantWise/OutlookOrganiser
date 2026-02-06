"""Auto-rules pattern matching engine for high-confidence email routing.

Auto-rules bypass Claude classification entirely when a match is found,
reducing API costs and improving response time. Rules are defined in
config.yaml under the `auto_rules` section.

Matching uses fnmatch for sender patterns (glob-style wildcards like
*@domain.com) and case-insensitive substring search for subject patterns.
No regex is used, so there is no ReDoS risk.

Usage:
    from assistant.classifier.auto_rules import AutoRulesEngine

    engine = AutoRulesEngine()
    result = engine.match(email_data, config.auto_rules)
    if result:
        # Apply result.rule.action directly (folder, priority, action_type)
"""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatch
from typing import TYPE_CHECKING

from assistant.core.logging import get_logger

if TYPE_CHECKING:
    from assistant.config_schema import AutoRuleConfig

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class AutoRuleMatch:
    """Result of an auto-rule match.

    Attributes:
        rule: The matched rule configuration
        match_reason: Human-readable explanation of why the rule matched
    """

    rule: AutoRuleConfig
    match_reason: str


class AutoRulesEngine:
    """Pattern matching engine for auto-routing rules.

    Evaluates emails against configured auto-rules in order. Returns the
    first matching rule, or None if no rules match.

    Matching logic:
    - Sender patterns use fnmatch (glob-style: *@domain.com)
    - Subject patterns use case-insensitive substring search
    - When both senders AND subjects are specified, BOTH must match (AND)
    - When only one is specified, that one alone is sufficient
    """

    def match(
        self,
        sender_email: str,
        subject: str,
        rules: list[AutoRuleConfig],
    ) -> AutoRuleMatch | None:
        """Check if an email matches any auto-rule.

        Rules are evaluated in order; the first match wins.

        Args:
            sender_email: Sender's email address
            subject: Email subject line
            rules: Auto-rule configurations from config.yaml

        Returns:
            AutoRuleMatch if a rule matched, None otherwise
        """
        if not rules:
            return None

        sender_lower = sender_email.lower()
        subject_lower = subject.lower()

        for rule in rules:
            has_sender_patterns = bool(rule.match.senders)
            has_subject_patterns = bool(rule.match.subjects)

            # Skip rules with no patterns (misconfigured)
            if not has_sender_patterns and not has_subject_patterns:
                continue

            sender_matched = _match_senders(sender_lower, rule.match.senders)
            subject_matched = _match_subjects(subject_lower, rule.match.subjects)

            # AND logic: both must match when both are specified
            if has_sender_patterns and has_subject_patterns:
                if sender_matched and subject_matched:
                    reason = (
                        f"Rule '{rule.name}': sender matched pattern and subject matched keyword"
                    )
                    logger.debug(
                        "auto_rule_matched",
                        rule=rule.name,
                        sender_domain=sender_email.split("@")[-1],
                        match_type="sender+subject",
                    )
                    return AutoRuleMatch(rule=rule, match_reason=reason)

            # OR logic: only one type specified, that one is sufficient
            elif has_sender_patterns and sender_matched:
                reason = f"Rule '{rule.name}': sender matched pattern"
                logger.debug(
                    "auto_rule_matched",
                    rule=rule.name,
                    sender_domain=sender_email.split("@")[-1],
                    match_type="sender",
                )
                return AutoRuleMatch(rule=rule, match_reason=reason)

            elif has_subject_patterns and subject_matched:
                reason = f"Rule '{rule.name}': subject matched keyword"
                logger.debug(
                    "auto_rule_matched",
                    rule=rule.name,
                    match_type="subject",
                )
                return AutoRuleMatch(rule=rule, match_reason=reason)

        return None


def _match_senders(sender_lower: str, patterns: list[str]) -> bool:
    """Check if sender matches any pattern using fnmatch.

    Args:
        sender_lower: Lowercased sender email
        patterns: Glob patterns (e.g., '*@domain.com', 'user@example.com')

    Returns:
        True if any pattern matches
    """
    return any(fnmatch(sender_lower, pattern.lower()) for pattern in patterns)


def _match_subjects(subject_lower: str, keywords: list[str]) -> bool:
    """Check if subject contains any keyword (case-insensitive substring).

    Args:
        subject_lower: Lowercased subject line
        keywords: Keywords to search for

    Returns:
        True if any keyword is found in the subject
    """
    return any(keyword.lower() in subject_lower for keyword in keywords)
