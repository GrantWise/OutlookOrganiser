"""Tests for bootstrap-specific prompt templates and YAML response parsing.

Tests prompt building, email formatting, YAML parsing (valid, fenced,
malformed, missing keys), and the retry message.
"""

import pytest

from assistant.classifier.bootstrap_prompts import (
    BATCH_ANALYSIS_YAML_SCHEMA,
    CONSOLIDATED_YAML_SCHEMA,
    build_batch_analysis_prompt,
    build_consolidation_prompt,
    format_email_for_batch,
    get_yaml_retry_message,
    parse_batch_yaml_response,
    parse_consolidated_yaml_response,
)

# ---------------------------------------------------------------------------
# format_email_for_batch
# ---------------------------------------------------------------------------


class TestFormatEmailForBatch:
    """Tests for format_email_for_batch."""

    def test_includes_all_fields(self) -> None:
        """Test that all fields are present in the output."""
        result = format_email_for_batch(
            sender_name="John Doe",
            sender_email="john@example.com",
            subject="Project Update",
            received_date="2024-01-15T10:00:00Z",
            snippet="Here is the latest status update.",
            current_folder="Inbox",
        )
        assert "From: John Doe <john@example.com>" in result
        assert "Subject: Project Update" in result
        assert "Date: 2024-01-15T10:00:00Z" in result
        assert "Current folder: Inbox" in result
        assert "Preview: Here is the latest status update." in result

    def test_omits_folder_when_none(self) -> None:
        """Test that current folder line is omitted when None."""
        result = format_email_for_batch(
            sender_name="Jane",
            sender_email="jane@test.com",
            subject="Hello",
            received_date="2024-01-01",
            snippet="test",
        )
        assert "Current folder" not in result

    def test_truncates_snippet_at_200_chars(self) -> None:
        """Test that snippets longer than 200 chars are truncated."""
        long_snippet = "A" * 300
        result = format_email_for_batch(
            sender_name="X",
            sender_email="x@y.com",
            subject="S",
            received_date="2024-01-01",
            snippet=long_snippet,
        )
        assert "Preview: " + "A" * 200 + "..." in result

    def test_no_ellipsis_for_short_snippet(self) -> None:
        """Test that short snippets don't get ellipsis."""
        result = format_email_for_batch(
            sender_name="X",
            sender_email="x@y.com",
            subject="S",
            received_date="2024-01-01",
            snippet="Short text",
        )
        assert "Preview: Short text" in result
        assert "..." not in result

    def test_handles_empty_snippet(self) -> None:
        """Test that empty snippet results in empty preview."""
        result = format_email_for_batch(
            sender_name="X",
            sender_email="x@y.com",
            subject="S",
            received_date="2024-01-01",
            snippet="",
        )
        assert "Preview: " in result


# ---------------------------------------------------------------------------
# build_batch_analysis_prompt
# ---------------------------------------------------------------------------


class TestBuildBatchAnalysisPrompt:
    """Tests for build_batch_analysis_prompt."""

    def test_includes_batch_number(self) -> None:
        """Test that batch number is included."""
        prompt = build_batch_analysis_prompt(
            batch_number=3,
            total_batches=10,
            email_batch="email content here",
        )
        assert "batch 3 of 10" in prompt

    def test_includes_yaml_schema(self) -> None:
        """Test that the YAML schema is included."""
        prompt = build_batch_analysis_prompt(
            batch_number=1,
            total_batches=1,
            email_batch="test",
        )
        assert "projects:" in prompt
        assert "areas:" in prompt
        assert "sender_clusters:" in prompt

    def test_includes_email_batch(self) -> None:
        """Test that the email content is included."""
        prompt = build_batch_analysis_prompt(
            batch_number=1,
            total_batches=1,
            email_batch="From: test@example.com\nSubject: Test",
        )
        assert "From: test@example.com" in prompt
        assert "Subject: Test" in prompt

    def test_requests_yaml_only_response(self) -> None:
        """Test that the prompt asks for YAML-only response."""
        prompt = build_batch_analysis_prompt(
            batch_number=1,
            total_batches=1,
            email_batch="test",
        )
        assert "ONLY valid YAML" in prompt


# ---------------------------------------------------------------------------
# build_consolidation_prompt
# ---------------------------------------------------------------------------


class TestBuildConsolidationPrompt:
    """Tests for build_consolidation_prompt."""

    def test_includes_batch_count(self) -> None:
        """Test that batch count is included."""
        prompt = build_consolidation_prompt(
            batch_count=5,
            all_batch_results="batch results",
        )
        assert "5 separate analyses" in prompt

    def test_includes_consolidated_schema(self) -> None:
        """Test that the consolidated schema is included."""
        prompt = build_consolidation_prompt(
            batch_count=1,
            all_batch_results="test",
        )
        assert "auto_rules:" in prompt
        assert "key_contacts:" in prompt

    def test_includes_batch_results(self) -> None:
        """Test that batch results are included."""
        prompt = build_consolidation_prompt(
            batch_count=1,
            all_batch_results="--- Batch 1 ---\nprojects:\n  - name: Test",
        )
        assert "--- Batch 1 ---" in prompt

    def test_instructs_deduplication(self) -> None:
        """Test that the prompt asks for deduplication."""
        prompt = build_consolidation_prompt(
            batch_count=2,
            all_batch_results="test",
        )
        assert "MERGE" in prompt
        assert "DEDUPLICATE" in prompt


# ---------------------------------------------------------------------------
# get_yaml_retry_message
# ---------------------------------------------------------------------------


class TestGetYamlRetryMessage:
    """Tests for get_yaml_retry_message."""

    def test_returns_corrective_message(self) -> None:
        """Test that retry message asks for valid YAML."""
        msg = get_yaml_retry_message()
        assert "not valid YAML" in msg
        assert "ONLY valid YAML" in msg
        assert "No markdown fences" in msg


# ---------------------------------------------------------------------------
# parse_batch_yaml_response
# ---------------------------------------------------------------------------


class TestParseBatchYamlResponse:
    """Tests for parse_batch_yaml_response."""

    def test_parses_valid_yaml(self) -> None:
        """Test parsing of well-formed YAML batch response."""
        raw = """\
projects:
  - name: "Alpha Project"
    folder: "Projects/Alpha"
areas:
  - name: "Finance"
    folder: "Areas/Finance"
sender_clusters:
  newsletters:
    - "news@example.com"
"""
        result = parse_batch_yaml_response(raw)
        assert len(result["projects"]) == 1
        assert result["projects"][0]["name"] == "Alpha Project"
        assert len(result["areas"]) == 1
        assert result["areas"][0]["name"] == "Finance"
        assert "news@example.com" in result["sender_clusters"]["newsletters"]

    def test_strips_markdown_yaml_fences(self) -> None:
        """Test that markdown ```yaml fences are stripped."""
        raw = """\
```yaml
projects:
  - name: "Beta"
areas: []
sender_clusters: {}
```"""
        result = parse_batch_yaml_response(raw)
        assert result["projects"][0]["name"] == "Beta"

    def test_strips_plain_markdown_fences(self) -> None:
        """Test that plain ``` fences are stripped."""
        raw = """\
```
projects: []
areas: []
sender_clusters: {}
```"""
        result = parse_batch_yaml_response(raw)
        assert result["projects"] == []

    def test_raises_on_empty_response(self) -> None:
        """Test that empty response raises ValueError."""
        with pytest.raises(ValueError, match="Empty YAML"):
            parse_batch_yaml_response("")

    def test_raises_on_whitespace_only_response(self) -> None:
        """Test that whitespace-only response raises ValueError."""
        with pytest.raises(ValueError, match="Empty YAML"):
            parse_batch_yaml_response("   \n\n  ")

    def test_raises_on_malformed_yaml(self) -> None:
        """Test that malformed YAML raises ValueError."""
        raw = 'projects:\n  - name: "unclosed quote\n  invalid: {{'
        with pytest.raises(ValueError, match="Malformed YAML"):
            parse_batch_yaml_response(raw)

    def test_raises_on_non_dict_response(self) -> None:
        """Test that YAML producing a non-dict raises ValueError."""
        raw = "- item1\n- item2"
        with pytest.raises(ValueError, match="Expected YAML dict"):
            parse_batch_yaml_response(raw)

    def test_raises_on_missing_required_keys(self) -> None:
        """Test that missing required keys raises ValueError."""
        raw = "projects:\n  - name: Test\nareas: []"
        with pytest.raises(ValueError, match="missing required keys.*sender_clusters"):
            parse_batch_yaml_response(raw)

    def test_normalizes_none_projects_to_empty_list(self) -> None:
        """Test that null projects/areas become empty lists."""
        raw = """\
projects: null
areas: null
sender_clusters: {}
"""
        result = parse_batch_yaml_response(raw)
        assert result["projects"] == []
        assert result["areas"] == []

    def test_normalizes_none_sender_clusters_to_empty_dict(self) -> None:
        """Test that null sender_clusters becomes empty dict."""
        raw = """\
projects: []
areas: []
sender_clusters: null
"""
        result = parse_batch_yaml_response(raw)
        assert result["sender_clusters"] == {}


# ---------------------------------------------------------------------------
# parse_consolidated_yaml_response
# ---------------------------------------------------------------------------


class TestParseConsolidatedYamlResponse:
    """Tests for parse_consolidated_yaml_response."""

    def test_parses_valid_consolidated_yaml(self) -> None:
        """Test parsing of well-formed consolidated YAML."""
        raw = """\
projects:
  - name: "Alpha"
    folder: "Projects/Alpha"
areas:
  - name: "Finance"
    folder: "Areas/Finance"
auto_rules:
  - name: "GitHub Notifications"
    match:
      senders: ["notifications@github.com"]
    action:
      folder: "Reference/Dev Notifications"
key_contacts:
  - email: "ceo@partner.com"
    role: "Partner CEO"
    priority_boost: 1
sender_clusters:
  newsletters:
    - "news@example.com"
"""
        result = parse_consolidated_yaml_response(raw)
        assert len(result["projects"]) == 1
        assert len(result["areas"]) == 1
        assert len(result["auto_rules"]) == 1
        assert len(result["key_contacts"]) == 1
        assert "newsletters" in result["sender_clusters"]

    def test_strips_fences_from_consolidated(self) -> None:
        """Test that fences are stripped from consolidated YAML."""
        raw = """\
```yaml
projects: []
areas: []
auto_rules: []
key_contacts: []
sender_clusters: {}
```"""
        result = parse_consolidated_yaml_response(raw)
        assert result["projects"] == []

    def test_raises_on_missing_projects(self) -> None:
        """Test that missing projects key raises ValueError."""
        raw = "areas: []\nauto_rules: []"
        with pytest.raises(ValueError, match="missing required keys.*projects"):
            parse_consolidated_yaml_response(raw)

    def test_raises_on_missing_areas(self) -> None:
        """Test that missing areas key raises ValueError."""
        raw = "projects: []\nauto_rules: []"
        with pytest.raises(ValueError, match="missing required keys.*areas"):
            parse_consolidated_yaml_response(raw)

    def test_normalizes_none_optional_keys(self) -> None:
        """Test that null optional keys are normalized."""
        raw = """\
projects: []
areas: []
auto_rules: null
key_contacts: null
sender_clusters: null
"""
        result = parse_consolidated_yaml_response(raw)
        assert result["auto_rules"] == []
        assert result["key_contacts"] == []
        assert result["sender_clusters"] == {}

    def test_allows_missing_optional_keys(self) -> None:
        """Test that optional keys can be absent entirely."""
        raw = """\
projects:
  - name: "Test"
areas:
  - name: "Area"
"""
        result = parse_consolidated_yaml_response(raw)
        # Optional keys should be normalized to empty defaults
        assert result["auto_rules"] == []
        assert result["key_contacts"] == []
        assert result["sender_clusters"] == {}

    def test_raises_on_empty_response(self) -> None:
        """Test that empty response raises ValueError."""
        with pytest.raises(ValueError, match="Empty YAML"):
            parse_consolidated_yaml_response("")

    def test_raises_on_malformed_yaml(self) -> None:
        """Test that malformed YAML raises ValueError."""
        with pytest.raises(ValueError, match="Malformed YAML"):
            parse_consolidated_yaml_response("{{invalid yaml")


# ---------------------------------------------------------------------------
# YAML schema constants
# ---------------------------------------------------------------------------


class TestYamlSchemaConstants:
    """Tests for YAML schema constant validity."""

    def test_batch_schema_is_valid_yaml(self) -> None:
        """Test that BATCH_ANALYSIS_YAML_SCHEMA is valid YAML."""
        import yaml

        parsed = yaml.safe_load(BATCH_ANALYSIS_YAML_SCHEMA)
        assert isinstance(parsed, dict)
        assert "projects" in parsed
        assert "areas" in parsed
        assert "sender_clusters" in parsed

    def test_consolidated_schema_is_valid_yaml(self) -> None:
        """Test that CONSOLIDATED_YAML_SCHEMA is valid YAML."""
        import yaml

        parsed = yaml.safe_load(CONSOLIDATED_YAML_SCHEMA)
        assert isinstance(parsed, dict)
        assert "projects" in parsed
        assert "areas" in parsed
        assert "auto_rules" in parsed
        assert "key_contacts" in parsed
