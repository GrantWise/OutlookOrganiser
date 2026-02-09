"""Tests for auto-rules sender affinity, hygiene, and audit (Features 2E + 2F).

Tests cover:
- Rule creation from sender affinity data
- Duplicate detection
- Config append with backup + validation
- Conflict detection (sender/subject overlap)
- Stale rule detection
- Audit report structure
- Match recording in DB
- API endpoint for auto-rule creation
"""

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from assistant.classifier.auto_rules import (
    AutoRuleMatch,
    AutoRulesEngine,
    RulesAuditReport,
    audit_report,
    check_duplicate_rule,
    create_rule_from_sender,
    detect_conflicts,
    detect_stale_rules,
)
from assistant.config import append_auto_rule, load_config, reset_config
from assistant.config_schema import AppConfig, AutoRuleConfig
from assistant.db.store import DatabaseStore

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_rules() -> list[AutoRuleConfig]:
    """Return sample auto-rule configs for testing."""
    return [
        AutoRuleConfig(
            name="newsletters",
            match={"senders": ["*@newsletter.example.com", "*@updates.example.com"]},
            action={
                "folder": "Reference/Newsletters",
                "category": "FYI Only",
                "priority": "P4 - Low",
            },
        ),
        AutoRuleConfig(
            name="ci-alerts",
            match={"senders": ["ci@builds.example.com"], "subjects": ["Build Failed"]},
            action={
                "folder": "Areas/Development",
                "category": "Review",
                "priority": "P3 - Urgent Low",
            },
        ),
        AutoRuleConfig(
            name="invoices",
            match={"subjects": ["invoice", "payment receipt"]},
            action={"folder": "Areas/Finance", "category": "Review", "priority": "P3 - Urgent Low"},
        ),
    ]


@pytest.fixture
async def store(data_dir: Path) -> DatabaseStore:
    """Return an initialized DatabaseStore."""
    db_path = data_dir / "test_auto_rules.db"
    s = DatabaseStore(db_path)
    await s.initialize()
    return s


@pytest.fixture
def config_with_rules(
    sample_config_dict: dict[str, Any],
    sample_rules: list[AutoRuleConfig],
) -> AppConfig:
    """Return a config with sample auto-rules."""
    d = {**sample_config_dict}
    d["auto_rules"] = [r.model_dump() for r in sample_rules]
    return AppConfig(**d)


@pytest.fixture
def config_yaml_path(tmp_path: Path, config_with_rules: AppConfig) -> Path:
    """Write a config file and return its path."""
    config_path = tmp_path / "config.yaml"
    config_dict = config_with_rules.model_dump(mode="python")
    config_path.write_text(yaml.dump(config_dict, default_flow_style=False))
    return config_path


# ---------------------------------------------------------------------------
# Tests: create_rule_from_sender
# ---------------------------------------------------------------------------


def test_create_rule_from_sender_basic():
    """Create a rule dict from sender email."""
    rule = create_rule_from_sender(
        sender_email="alerts@example.com",
        folder="Areas/Operations",
        priority="P3 - Urgent Low",
        action_type="Review",
    )

    assert rule["name"] == "auto-example.com"
    assert rule["match"]["senders"] == ["alerts@example.com"]
    assert rule["action"]["folder"] == "Areas/Operations"
    assert rule["action"]["category"] == "Review"
    assert rule["action"]["priority"] == "P3 - Urgent Low"


def test_create_rule_from_sender_custom_name():
    """Custom rule name overrides auto-generated name."""
    rule = create_rule_from_sender(
        sender_email="alerts@example.com",
        folder="Areas/Operations",
        priority="P3 - Urgent Low",
        action_type="Review",
        rule_name="my-custom-rule",
    )

    assert rule["name"] == "my-custom-rule"


def test_create_rule_from_sender_lowercases_email():
    """Sender email is lowercased in the rule."""
    rule = create_rule_from_sender(
        sender_email="Alerts@EXAMPLE.COM",
        folder="Areas/Operations",
        priority="P3 - Urgent Low",
        action_type="Review",
    )

    assert rule["match"]["senders"] == ["alerts@example.com"]


# ---------------------------------------------------------------------------
# Tests: check_duplicate_rule
# ---------------------------------------------------------------------------


def test_duplicate_detection_exact_match(sample_rules: list[AutoRuleConfig]):
    """Exact sender match is detected as duplicate."""
    result = check_duplicate_rule("ci@builds.example.com", sample_rules)
    assert result is not None
    assert result.name == "ci-alerts"


def test_duplicate_detection_wildcard_match(sample_rules: list[AutoRuleConfig]):
    """Wildcard pattern match is detected as duplicate."""
    result = check_duplicate_rule("someone@newsletter.example.com", sample_rules)
    assert result is not None
    assert result.name == "newsletters"


def test_duplicate_detection_no_match(sample_rules: list[AutoRuleConfig]):
    """Non-matching sender returns None."""
    result = check_duplicate_rule("new@unique-domain.com", sample_rules)
    assert result is None


# ---------------------------------------------------------------------------
# Tests: detect_conflicts
# ---------------------------------------------------------------------------


def test_detect_conflicts_sender_overlap():
    """Overlapping sender patterns to different folders are conflicts."""
    rules = [
        AutoRuleConfig(
            name="rule-a",
            match={"senders": ["*@example.com"]},
            action={"folder": "Folder/A", "category": "FYI Only", "priority": "P4 - Low"},
        ),
        AutoRuleConfig(
            name="rule-b",
            match={"senders": ["user@example.com"]},
            action={"folder": "Folder/B", "category": "Review", "priority": "P3 - Urgent Low"},
        ),
    ]

    conflicts = detect_conflicts(rules)

    assert len(conflicts) == 1
    assert conflicts[0].rule_a == "rule-a"
    assert conflicts[0].rule_b == "rule-b"
    assert conflicts[0].overlap_type == "sender"


def test_detect_conflicts_subject_overlap():
    """Overlapping subject keywords to different folders are conflicts."""
    rules = [
        AutoRuleConfig(
            name="rule-a",
            match={"subjects": ["invoice"]},
            action={"folder": "Folder/A", "category": "FYI Only", "priority": "P4 - Low"},
        ),
        AutoRuleConfig(
            name="rule-b",
            match={"subjects": ["invoice reminder"]},
            action={"folder": "Folder/B", "category": "Review", "priority": "P3 - Urgent Low"},
        ),
    ]

    conflicts = detect_conflicts(rules)

    assert len(conflicts) == 1
    assert conflicts[0].overlap_type == "subject"


def test_detect_conflicts_same_folder_not_flagged():
    """Rules routing to the same folder are not conflicts."""
    rules = [
        AutoRuleConfig(
            name="rule-a",
            match={"senders": ["*@example.com"]},
            action={"folder": "Same/Folder", "category": "FYI Only", "priority": "P4 - Low"},
        ),
        AutoRuleConfig(
            name="rule-b",
            match={"senders": ["user@example.com"]},
            action={"folder": "Same/Folder", "category": "Review", "priority": "P3 - Urgent Low"},
        ),
    ]

    conflicts = detect_conflicts(rules)
    assert len(conflicts) == 0


def test_detect_conflicts_no_overlap():
    """Non-overlapping rules produce no conflicts."""
    rules = [
        AutoRuleConfig(
            name="rule-a",
            match={"senders": ["*@alpha.com"]},
            action={"folder": "Folder/A", "category": "FYI Only", "priority": "P4 - Low"},
        ),
        AutoRuleConfig(
            name="rule-b",
            match={"senders": ["*@beta.com"]},
            action={"folder": "Folder/B", "category": "Review", "priority": "P3 - Urgent Low"},
        ),
    ]

    conflicts = detect_conflicts(rules)
    assert len(conflicts) == 0


# ---------------------------------------------------------------------------
# Tests: detect_stale_rules
# ---------------------------------------------------------------------------


def test_detect_stale_rules_zero_matches(sample_rules: list[AutoRuleConfig]):
    """Rules with zero matches are flagged as stale."""
    match_counts: dict[str, dict[str, Any]] = {}  # No matches recorded

    stale = detect_stale_rules(sample_rules, match_counts, threshold_days=30)

    assert len(stale) == len(sample_rules)
    assert "newsletters" in stale
    assert "ci-alerts" in stale


def test_detect_stale_rules_recent_match_not_flagged(sample_rules: list[AutoRuleConfig]):
    """Rules with recent matches are not flagged."""
    recent = (datetime.now() - timedelta(days=5)).isoformat()
    match_counts = {
        "newsletters": {"match_count": 15, "last_match_at": recent},
        "ci-alerts": {"match_count": 3, "last_match_at": recent},
        "invoices": {"match_count": 8, "last_match_at": recent},
    }

    stale = detect_stale_rules(sample_rules, match_counts, threshold_days=30)
    assert len(stale) == 0


def test_detect_stale_rules_old_match_flagged(sample_rules: list[AutoRuleConfig]):
    """Rules with matches beyond threshold are flagged."""
    old = (datetime.now() - timedelta(days=60)).isoformat()
    recent = (datetime.now() - timedelta(days=5)).isoformat()
    match_counts = {
        "newsletters": {"match_count": 15, "last_match_at": old},
        "ci-alerts": {"match_count": 3, "last_match_at": recent},
        "invoices": {"match_count": 8, "last_match_at": old},
    }

    stale = detect_stale_rules(sample_rules, match_counts, threshold_days=30)

    assert "newsletters" in stale
    assert "invoices" in stale
    assert "ci-alerts" not in stale


# ---------------------------------------------------------------------------
# Tests: audit_report
# ---------------------------------------------------------------------------


def test_audit_report_structure(sample_rules: list[AutoRuleConfig]):
    """Audit report has all expected fields."""
    report = audit_report(sample_rules, {}, max_rules=100, threshold_days=30)

    assert isinstance(report, RulesAuditReport)
    assert report.total_rules == 3
    assert report.max_rules == 100
    assert report.over_limit is False
    assert isinstance(report.conflicts, list)
    assert isinstance(report.stale_rules, list)


def test_audit_report_over_limit():
    """Audit report flags over_limit when rules exceed max."""
    rules = [
        AutoRuleConfig(
            name=f"rule-{i}",
            match={"senders": [f"user{i}@example.com"]},
            action={"folder": "Folder/Test", "category": "FYI Only", "priority": "P4 - Low"},
        )
        for i in range(5)
    ]

    report = audit_report(rules, {}, max_rules=3, threshold_days=30)

    assert report.over_limit is True
    assert report.total_rules == 5
    assert report.max_rules == 3


# ---------------------------------------------------------------------------
# Tests: Match recording in DB
# ---------------------------------------------------------------------------


async def test_record_auto_rule_match(store: DatabaseStore):
    """Recording a match increments the count."""
    await store.record_auto_rule_match("test-rule")
    await store.record_auto_rule_match("test-rule")
    await store.record_auto_rule_match("test-rule")

    counts = await store.get_auto_rule_match_counts()

    assert "test-rule" in counts
    assert counts["test-rule"]["match_count"] == 3
    assert counts["test-rule"]["last_match_at"] is not None


async def test_record_auto_rule_match_multiple_rules(store: DatabaseStore):
    """Different rules have independent counts."""
    await store.record_auto_rule_match("rule-a")
    await store.record_auto_rule_match("rule-a")
    await store.record_auto_rule_match("rule-b")

    counts = await store.get_auto_rule_match_counts()

    assert counts["rule-a"]["match_count"] == 2
    assert counts["rule-b"]["match_count"] == 1


# ---------------------------------------------------------------------------
# Tests: Config append
# ---------------------------------------------------------------------------


def test_append_auto_rule_to_config(config_yaml_path: Path):
    """Appending a rule updates the config file."""
    reset_config()

    new_rule = create_rule_from_sender(
        sender_email="new@vendor.com",
        folder="Areas/Vendors",
        priority="P3 - Urgent Low",
        action_type="Review",
        rule_name="vendor-rule",
    )

    append_auto_rule(new_rule, config_path=config_yaml_path)

    # Reload and verify
    reloaded = load_config(config_yaml_path)
    rule_names = [r.name for r in reloaded.auto_rules]
    assert "vendor-rule" in rule_names


def test_append_auto_rule_creates_backup(config_yaml_path: Path):
    """Appending a rule creates a backup file."""
    reset_config()

    new_rule = create_rule_from_sender(
        sender_email="backup@test.com",
        folder="Areas/Test",
        priority="P3 - Urgent Low",
        action_type="FYI Only",
        rule_name="backup-test-rule",
    )

    append_auto_rule(new_rule, config_path=config_yaml_path)

    # Check backup exists
    backups = list(config_yaml_path.parent.glob("*.bak.*"))
    assert len(backups) >= 1


def test_append_auto_rule_rejects_duplicate(config_yaml_path: Path):
    """Appending a rule with an existing name raises error."""
    from assistant.core.errors import ConfigValidationError

    reset_config()

    # "newsletters" already exists in the config
    duplicate_rule = create_rule_from_sender(
        sender_email="dup@test.com",
        folder="Areas/Test",
        priority="P3 - Urgent Low",
        action_type="FYI Only",
        rule_name="newsletters",
    )

    with pytest.raises(ConfigValidationError, match="already exists"):
        append_auto_rule(duplicate_rule, config_path=config_yaml_path)


# ---------------------------------------------------------------------------
# Tests: AutoRulesEngine match (existing + extended)
# ---------------------------------------------------------------------------


def test_engine_match_returns_rule_info(sample_rules: list[AutoRuleConfig]):
    """Engine match returns AutoRuleMatch with rule and reason."""
    engine = AutoRulesEngine()

    result = engine.match(
        sender_email="spam@newsletter.example.com",
        subject="Weekly digest",
        rules=sample_rules,
    )

    assert result is not None
    assert isinstance(result, AutoRuleMatch)
    assert result.rule.name == "newsletters"
    assert "sender matched" in result.match_reason


def test_engine_no_match_returns_none(sample_rules: list[AutoRuleConfig]):
    """Engine returns None when no rules match."""
    engine = AutoRulesEngine()

    result = engine.match(
        sender_email="unique@unmatched.com",
        subject="Random subject",
        rules=sample_rules,
    )

    assert result is None
