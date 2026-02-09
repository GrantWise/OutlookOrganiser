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
from datetime import datetime
from fnmatch import fnmatch
from typing import TYPE_CHECKING, Any

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


# ---------------------------------------------------------------------------
# Rule conflict and hygiene (Phase 2 - Features 2E + 2F)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RuleConflict:
    """Overlap between two auto-rules."""

    rule_a: str
    rule_b: str
    overlap_type: str  # 'sender' or 'subject'


@dataclass(frozen=True)
class RulesAuditReport:
    """Health report for auto-rules configuration."""

    total_rules: int
    max_rules: int
    conflicts: list[RuleConflict]
    stale_rules: list[str]
    over_limit: bool


def create_rule_from_sender(
    sender_email: str,
    folder: str,
    priority: str,
    action_type: str,
    rule_name: str | None = None,
) -> dict[str, Any]:
    """Create an auto-rule config dict from sender affinity data.

    Args:
        sender_email: Email address to match
        folder: Target folder path
        priority: Priority level
        action_type: Action type category
        rule_name: Optional rule name (auto-generated if None)

    Returns:
        Dict suitable for AutoRuleConfig validation
    """
    if not rule_name:
        domain = sender_email.split("@")[-1] if "@" in sender_email else sender_email
        rule_name = f"auto-{domain}"

    return {
        "name": rule_name,
        "match": {"senders": [sender_email.lower()]},
        "action": {
            "folder": folder,
            "category": action_type,
            "priority": priority,
        },
    }


def check_duplicate_rule(
    sender_email: str,
    rules: list[AutoRuleConfig],
) -> AutoRuleConfig | None:
    """Check if an auto-rule already exists for this sender.

    Args:
        sender_email: Sender email to check
        rules: Existing auto-rules

    Returns:
        The matching rule if found, None otherwise
    """
    sender_lower = sender_email.lower()
    for rule in rules:
        if any(fnmatch(sender_lower, p.lower()) for p in rule.match.senders):
            return rule
    return None


def detect_conflicts(rules: list[AutoRuleConfig]) -> list[RuleConflict]:
    """Detect overlapping patterns across auto-rules.

    Two rules conflict if they share sender patterns or subject keywords
    that could match the same email but route to different folders.

    Args:
        rules: Auto-rules to check

    Returns:
        List of detected conflicts
    """
    conflicts: list[RuleConflict] = []

    for i, rule_a in enumerate(rules):
        for rule_b in rules[i + 1 :]:
            # Skip if they route to the same folder (not a conflict)
            if rule_a.action.folder == rule_b.action.folder:
                continue

            # Check sender overlap
            for sender_a in rule_a.match.senders:
                for sender_b in rule_b.match.senders:
                    if fnmatch(sender_a.lower(), sender_b.lower()) or fnmatch(
                        sender_b.lower(), sender_a.lower()
                    ):
                        conflicts.append(
                            RuleConflict(
                                rule_a=rule_a.name,
                                rule_b=rule_b.name,
                                overlap_type="sender",
                            )
                        )
                        break

            # Check subject overlap
            for subj_a in rule_a.match.subjects:
                for subj_b in rule_b.match.subjects:
                    if subj_a.lower() in subj_b.lower() or subj_b.lower() in subj_a.lower():
                        conflicts.append(
                            RuleConflict(
                                rule_a=rule_a.name,
                                rule_b=rule_b.name,
                                overlap_type="subject",
                            )
                        )
                        break

    return conflicts


def detect_stale_rules(
    rules: list[AutoRuleConfig],
    match_counts: dict[str, dict[str, Any]],
    threshold_days: int = 30,
) -> list[str]:
    """Detect rules with zero matches in the threshold period.

    Args:
        rules: Auto-rules to check
        match_counts: Match data from store.get_auto_rule_match_counts()
        threshold_days: Days of inactivity before flagging

    Returns:
        List of stale rule names
    """
    stale: list[str] = []
    now = datetime.now()

    for rule in rules:
        counts = match_counts.get(rule.name)
        if not counts or counts["match_count"] == 0:
            stale.append(rule.name)
        elif counts["last_match_at"]:
            try:
                last = datetime.fromisoformat(counts["last_match_at"])
                if (now - last).days > threshold_days:
                    stale.append(rule.name)
            except (ValueError, TypeError):
                stale.append(rule.name)

    return stale


def audit_report(
    rules: list[AutoRuleConfig],
    match_counts: dict[str, dict[str, Any]],
    max_rules: int = 100,
    threshold_days: int = 30,
) -> RulesAuditReport:
    """Generate a comprehensive auto-rules health report.

    Args:
        rules: Auto-rules configuration
        match_counts: Match data from store
        max_rules: Maximum recommended rules
        threshold_days: Stale threshold in days

    Returns:
        RulesAuditReport with all findings
    """
    return RulesAuditReport(
        total_rules=len(rules),
        max_rules=max_rules,
        conflicts=detect_conflicts(rules),
        stale_rules=detect_stale_rules(rules, match_counts, threshold_days),
        over_limit=len(rules) > max_rules,
    )
