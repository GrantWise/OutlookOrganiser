"""Tests for the bootstrap engine.

Tests the two-pass bootstrap scanner including email transformation,
batch splitting, Claude API mocking, config writing, and sender profiling.
"""

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from assistant.config_schema import AppConfig
from assistant.db.store import DatabaseStore, Email
from assistant.engine.bootstrap import (
    BATCH_SIZE,
    MAX_BOOTSTRAP_EMAILS,
    BootstrapEngine,
    BootstrapStats,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_config(sample_config_dict: dict[str, Any]) -> AppConfig:
    """Return a config with bootstrap model set."""
    return AppConfig(**sample_config_dict)


@pytest.fixture
def mock_anthropic() -> MagicMock:
    """Return a mock Anthropic client."""
    client = MagicMock()
    return client


@pytest.fixture
def mock_message_manager() -> MagicMock:
    """Return a mock MessageManager."""
    mgr = MagicMock()
    mgr.list_messages = MagicMock(return_value=[])
    return mgr


@pytest.fixture
def mock_folder_manager() -> MagicMock:
    """Return a mock FolderManager."""
    mgr = MagicMock()
    mgr.get_folder_path = MagicMock(return_value="Inbox")
    return mgr


@pytest.fixture
async def store(data_dir: Path) -> DatabaseStore:
    """Return an initialized DatabaseStore."""
    db_path = data_dir / "test_bootstrap.db"
    s = DatabaseStore(db_path)
    await s.initialize()
    return s


@pytest.fixture
def mock_snippet_cleaner() -> MagicMock:
    """Return a mock SnippetCleaner."""
    cleaner = MagicMock()
    result = MagicMock()
    result.cleaned_text = "cleaned snippet"
    cleaner.clean = MagicMock(return_value=result)
    return cleaner


@pytest.fixture
def engine(
    mock_anthropic: MagicMock,
    mock_message_manager: MagicMock,
    mock_folder_manager: MagicMock,
    store: DatabaseStore,
    mock_snippet_cleaner: MagicMock,
    sample_config: AppConfig,
) -> BootstrapEngine:
    """Return a BootstrapEngine with mocked dependencies."""
    from rich.console import Console

    return BootstrapEngine(
        anthropic_client=mock_anthropic,
        message_manager=mock_message_manager,
        folder_manager=mock_folder_manager,
        store=store,
        snippet_cleaner=mock_snippet_cleaner,
        config=sample_config,
        console=Console(quiet=True),
    )


def make_graph_message(
    msg_id: str = "msg1",
    subject: str = "Test Subject",
    sender_email: str = "sender@example.com",
    sender_name: str = "Sender Name",
    received: str = "2024-01-15T10:00:00Z",
    body_preview: str = "Email body preview text",
    folder_id: str = "folder123",
) -> dict[str, Any]:
    """Create a mock Graph API message dict."""
    return {
        "id": msg_id,
        "conversationId": f"conv_{msg_id}",
        "conversationIndex": None,
        "subject": subject,
        "from": {
            "emailAddress": {
                "address": sender_email,
                "name": sender_name,
            },
        },
        "receivedDateTime": received,
        "bodyPreview": body_preview,
        "parentFolderId": folder_id,
        "webLink": f"https://outlook.com/{msg_id}",
        "importance": "normal",
        "isRead": False,
        "flag": {"flagStatus": "notFlagged"},
    }


def make_claude_response(yaml_text: str) -> MagicMock:
    """Create a mock Anthropic API response."""
    response = MagicMock()
    text_block = MagicMock()
    text_block.type = "text"
    text_block.text = yaml_text
    response.content = [text_block]
    response.usage = MagicMock()
    response.usage.input_tokens = 100
    response.usage.output_tokens = 50
    return response


# ---------------------------------------------------------------------------
# BootstrapStats
# ---------------------------------------------------------------------------


class TestBootstrapStats:
    """Tests for BootstrapStats dataclass."""

    def test_default_values(self) -> None:
        """Test that all defaults are zero."""
        stats = BootstrapStats()
        assert stats.total_emails_fetched == 0
        assert stats.total_batches == 0
        assert stats.batches_succeeded == 0
        assert stats.batches_failed == 0
        assert stats.projects_discovered == 0
        assert stats.areas_discovered == 0
        assert stats.senders_profiled == 0
        assert stats.total_input_tokens == 0
        assert stats.total_output_tokens == 0
        assert stats.duration_seconds == 0.0


# ---------------------------------------------------------------------------
# _transform_emails
# ---------------------------------------------------------------------------


class TestTransformEmails:
    """Tests for email transformation from Graph API dicts."""

    def test_transforms_basic_message(self, engine: BootstrapEngine) -> None:
        """Test basic message transformation."""
        raw = [make_graph_message()]
        emails = engine._transform_emails(raw)

        assert len(emails) == 1
        email = emails[0]
        assert email.id == "msg1"
        assert email.subject == "Test Subject"
        assert email.sender_email == "sender@example.com"
        assert email.sender_name == "Sender Name"
        assert email.snippet == "cleaned snippet"
        assert email.current_folder == "Inbox"
        assert email.classification_status == "pending"

    def test_handles_missing_sender(self, engine: BootstrapEngine) -> None:
        """Test transformation with missing sender data."""
        raw = [{"id": "msg1", "subject": "Test", "bodyPreview": ""}]
        emails = engine._transform_emails(raw)
        assert len(emails) == 1
        assert emails[0].sender_email == ""
        assert emails[0].sender_name == ""

    def test_handles_invalid_date(self, engine: BootstrapEngine) -> None:
        """Test transformation with invalid receivedDateTime."""
        raw = [make_graph_message(received="not-a-date")]
        emails = engine._transform_emails(raw)
        assert emails[0].received_at is None

    def test_handles_missing_flag(self, engine: BootstrapEngine) -> None:
        """Test transformation with missing flag data."""
        raw = [make_graph_message()]
        raw[0]["flag"] = None
        emails = engine._transform_emails(raw)
        assert emails[0].flag_status == "notFlagged"


# ---------------------------------------------------------------------------
# _build_batches
# ---------------------------------------------------------------------------


class TestBuildBatches:
    """Tests for email batch splitting."""

    def test_single_batch(self, engine: BootstrapEngine) -> None:
        """Test that fewer than BATCH_SIZE emails produce one batch."""
        emails = [Email(id=str(i), subject=f"S{i}") for i in range(10)]
        batches = engine._build_batches(emails)
        assert len(batches) == 1
        assert len(batches[0]) == 10

    def test_exact_batch_size(self, engine: BootstrapEngine) -> None:
        """Test that exactly BATCH_SIZE emails produce one batch."""
        emails = [Email(id=str(i), subject=f"S{i}") for i in range(BATCH_SIZE)]
        batches = engine._build_batches(emails)
        assert len(batches) == 1
        assert len(batches[0]) == BATCH_SIZE

    def test_multiple_batches(self, engine: BootstrapEngine) -> None:
        """Test that >BATCH_SIZE emails produce multiple batches."""
        emails = [Email(id=str(i), subject=f"S{i}") for i in range(BATCH_SIZE + 10)]
        batches = engine._build_batches(emails)
        assert len(batches) == 2
        assert len(batches[0]) == BATCH_SIZE
        assert len(batches[1]) == 10

    def test_empty_list(self, engine: BootstrapEngine) -> None:
        """Test that empty list produces no batches."""
        assert engine._build_batches([]) == []


# ---------------------------------------------------------------------------
# _build_sender_category_map
# ---------------------------------------------------------------------------


class TestBuildSenderCategoryMap:
    """Tests for sender category mapping from taxonomy."""

    def test_maps_all_cluster_types(self, engine: BootstrapEngine) -> None:
        """Test mapping for all sender cluster types."""
        taxonomy = {
            "sender_clusters": {
                "newsletters": ["news@example.com"],
                "automated": ["noreply@service.com"],
                "clients": ["client@customer.com"],
                "vendors": ["sales@vendor.com"],
                "internal": ["colleague@company.com"],
                "key_contacts": [
                    {"email": "vip@partner.com", "role": "CEO"},
                ],
            },
        }
        result = engine._build_sender_category_map(taxonomy)
        assert result["news@example.com"] == "newsletter"
        assert result["noreply@service.com"] == "automated"
        assert result["client@customer.com"] == "client"
        assert result["sales@vendor.com"] == "vendor"
        assert result["colleague@company.com"] == "internal"
        assert result["vip@partner.com"] == "key_contact"

    def test_handles_empty_clusters(self, engine: BootstrapEngine) -> None:
        """Test with empty sender_clusters."""
        taxonomy = {"sender_clusters": {}}
        result = engine._build_sender_category_map(taxonomy)
        assert result == {}

    def test_handles_missing_clusters(self, engine: BootstrapEngine) -> None:
        """Test with no sender_clusters key."""
        result = engine._build_sender_category_map({})
        assert result == {}

    def test_lowercases_emails(self, engine: BootstrapEngine) -> None:
        """Test that emails are lowercased for consistent lookup."""
        taxonomy = {
            "sender_clusters": {
                "newsletters": ["News@Example.COM"],
            },
        }
        result = engine._build_sender_category_map(taxonomy)
        assert "news@example.com" in result

    def test_skips_non_string_entries(self, engine: BootstrapEngine) -> None:
        """Test that non-string entries in lists are skipped."""
        taxonomy = {
            "sender_clusters": {
                "newsletters": ["valid@test.com", 123, None],
            },
        }
        result = engine._build_sender_category_map(taxonomy)
        assert "valid@test.com" in result
        assert len(result) == 1


# ---------------------------------------------------------------------------
# _extract_text_response
# ---------------------------------------------------------------------------


class TestExtractTextResponse:
    """Tests for extracting text from Claude responses."""

    def test_extracts_single_text_block(self, engine: BootstrapEngine) -> None:
        """Test extraction of a single text block."""
        response = make_claude_response("yaml content")
        text = engine._extract_text_response(response)
        assert text == "yaml content"

    def test_concatenates_multiple_text_blocks(self, engine: BootstrapEngine) -> None:
        """Test concatenation of multiple text blocks."""
        response = MagicMock()
        block1 = MagicMock()
        block1.type = "text"
        block1.text = "first part"
        block2 = MagicMock()
        block2.type = "text"
        block2.text = "second part"
        response.content = [block1, block2]
        text = engine._extract_text_response(response)
        assert text == "first part\nsecond part"

    def test_skips_non_text_blocks(self, engine: BootstrapEngine) -> None:
        """Test that non-text blocks are ignored."""
        response = MagicMock()
        text_block = MagicMock()
        text_block.type = "text"
        text_block.text = "content"
        tool_block = MagicMock()
        tool_block.type = "tool_use"
        response.content = [tool_block, text_block]
        text = engine._extract_text_response(response)
        assert text == "content"


# ---------------------------------------------------------------------------
# _update_token_stats
# ---------------------------------------------------------------------------


class TestUpdateTokenStats:
    """Tests for token usage tracking."""

    def test_accumulates_tokens(self, engine: BootstrapEngine) -> None:
        """Test that tokens accumulate across calls."""
        stats = BootstrapStats()
        response1 = make_claude_response("test")
        response1.usage.input_tokens = 100
        response1.usage.output_tokens = 50

        response2 = make_claude_response("test")
        response2.usage.input_tokens = 200
        response2.usage.output_tokens = 75

        engine._update_token_stats(stats, response1)
        engine._update_token_stats(stats, response2)

        assert stats.total_input_tokens == 300
        assert stats.total_output_tokens == 125


# ---------------------------------------------------------------------------
# _check_idempotency
# ---------------------------------------------------------------------------


class TestCheckIdempotency:
    """Tests for idempotency checking."""

    @pytest.mark.asyncio
    async def test_force_flag_skips_checks(self, engine: BootstrapEngine) -> None:
        """Test that --force skips all checks."""
        # Should not raise or prompt
        await engine._check_idempotency(force=True)

    @pytest.mark.asyncio
    async def test_exits_when_user_declines_overwrite(
        self, engine: BootstrapEngine, tmp_path: Path
    ) -> None:
        """Test that declining overwrite raises SystemExit."""
        from assistant.engine.bootstrap import PROPOSED_CONFIG_PATH

        with (
            patch.object(type(PROPOSED_CONFIG_PATH), "exists", return_value=True),
            patch("click.confirm", return_value=False),
            pytest.raises(SystemExit),
        ):
            await engine._check_idempotency(force=False)

    @pytest.mark.asyncio
    async def test_continues_when_user_accepts_overwrite(
        self,
        engine: BootstrapEngine,
    ) -> None:
        """Test that accepting overwrite proceeds."""
        from assistant.engine.bootstrap import PROPOSED_CONFIG_PATH

        with (
            patch.object(type(PROPOSED_CONFIG_PATH), "exists", return_value=True),
            patch("click.confirm", return_value=True),
        ):
            await engine._check_idempotency(force=False)


# ---------------------------------------------------------------------------
# _fetch_emails
# ---------------------------------------------------------------------------


class TestFetchEmails:
    """Tests for email fetching with dedup and max_items."""

    def test_deduplicates_by_message_id(self, engine: BootstrapEngine) -> None:
        """Test that duplicate messages are removed by ID."""
        raw = [
            make_graph_message(msg_id="msg1"),
            make_graph_message(msg_id="msg2"),
            make_graph_message(msg_id="msg1"),  # Duplicate
            make_graph_message(msg_id="msg3"),
            make_graph_message(msg_id="msg2"),  # Duplicate
        ]
        engine._message_manager.list_messages.return_value = raw
        result = engine._fetch_emails(days=7)
        assert len(result) == 3
        ids = [m["id"] for m in result]
        assert ids == ["msg1", "msg2", "msg3"]

    def test_no_duplicates_returns_all(self, engine: BootstrapEngine) -> None:
        """Test that unique messages all come through."""
        raw = [make_graph_message(msg_id=f"msg{i}") for i in range(5)]
        engine._message_manager.list_messages.return_value = raw
        result = engine._fetch_emails(days=7)
        assert len(result) == 5

    def test_passes_max_items(self, engine: BootstrapEngine) -> None:
        """Test that MAX_BOOTSTRAP_EMAILS is passed to list_messages."""
        engine._message_manager.list_messages.return_value = []
        engine._fetch_emails(days=30)
        call_kwargs = engine._message_manager.list_messages.call_args
        assert call_kwargs.kwargs.get("max_items") == MAX_BOOTSTRAP_EMAILS


# ---------------------------------------------------------------------------
# _write_proposed_config
# ---------------------------------------------------------------------------


class TestWriteProposedConfig:
    """Tests for config file writing."""

    def test_writes_valid_yaml_file(self, engine: BootstrapEngine, tmp_path: Path) -> None:
        """Test that a valid YAML file is written."""
        import yaml

        taxonomy = {
            "projects": [
                {"name": "Alpha", "folder": "Projects/Alpha", "signals": {"subjects": ["alpha"]}}
            ],
            "areas": [
                {"name": "Finance", "folder": "Areas/Finance", "signals": {"subjects": ["budget"]}}
            ],
            "auto_rules": [
                {
                    "name": "GitHub",
                    "match": {"senders": ["notifications@github.com"]},
                    "action": {
                        "folder": "Reference/Dev",
                        "category": "FYI Only",
                        "priority": "P4 - Low",
                    },
                }
            ],
            "key_contacts": [{"email": "vip@test.com", "role": "CEO", "priority_boost": 2}],
            "sender_clusters": {"newsletters": ["news@test.com"]},
        }

        # Patch the config path to use tmp_path
        output_path = tmp_path / "config.yaml.proposed"
        with patch("assistant.engine.bootstrap.PROPOSED_CONFIG_PATH", output_path):
            result_path = engine._write_proposed_config(taxonomy)

        assert result_path == output_path
        assert output_path.exists()

        # Verify it's valid YAML
        with open(output_path) as f:
            parsed = yaml.safe_load(f)

        assert parsed["schema_version"] == 1
        assert len(parsed["projects"]) == 1
        assert parsed["projects"][0]["name"] == "Alpha"
        assert len(parsed["areas"]) == 1
        assert len(parsed["auto_rules"]) == 1
        assert len(parsed["key_contacts"]) == 1

    def test_skips_invalid_project_entries(self, engine: BootstrapEngine, tmp_path: Path) -> None:
        """Test that project entries without name are skipped."""
        import yaml

        taxonomy = {
            "projects": [{"name": "Valid"}, {"folder": "no-name"}, "not-a-dict"],
            "areas": [],
            "auto_rules": [],
            "key_contacts": [],
            "sender_clusters": {},
        }

        output_path = tmp_path / "config.yaml.proposed"
        with patch("assistant.engine.bootstrap.PROPOSED_CONFIG_PATH", output_path):
            engine._write_proposed_config(taxonomy)

        with open(output_path) as f:
            parsed = yaml.safe_load(f)

        assert len(parsed["projects"]) == 1
        assert parsed["projects"][0]["name"] == "Valid"


# ---------------------------------------------------------------------------
# _populate_sender_profiles
# ---------------------------------------------------------------------------


class TestPopulateSenderProfiles:
    """Tests for sender profile population."""

    @pytest.mark.asyncio
    async def test_counts_emails_per_sender(self, engine: BootstrapEngine) -> None:
        """Test that sender email counts are tracked."""
        emails = [
            Email(id="1", sender_email="alice@test.com", sender_name="Alice"),
            Email(id="2", sender_email="alice@test.com", sender_name="Alice"),
            Email(id="3", sender_email="bob@test.com", sender_name="Bob"),
        ]
        taxonomy = {"sender_clusters": {}}
        stats = BootstrapStats()

        count = await engine._populate_sender_profiles(emails, taxonomy, stats)
        assert count == 2  # Two unique senders

    @pytest.mark.asyncio
    async def test_detects_auto_rule_candidates(self, engine: BootstrapEngine) -> None:
        """Test auto-rule candidate detection (>90% to single folder, 10+ emails)."""
        # Create 10 emails from same sender, 9 to Inbox, 1 to Sent
        emails = [
            Email(
                id=str(i),
                sender_email="consistent@test.com",
                sender_name="Consistent",
                current_folder="Inbox" if i < 9 else "Sent",
            )
            for i in range(10)
        ]
        taxonomy = {"sender_clusters": {}}
        stats = BootstrapStats()

        await engine._populate_sender_profiles(emails, taxonomy, stats)
        assert stats.auto_rule_candidates == 1

    @pytest.mark.asyncio
    async def test_no_auto_rule_for_low_volume(self, engine: BootstrapEngine) -> None:
        """Test that senders with <10 emails are not auto-rule candidates."""
        emails = [
            Email(id=str(i), sender_email="low@test.com", current_folder="Inbox") for i in range(5)
        ]
        taxonomy = {"sender_clusters": {}}
        stats = BootstrapStats()

        await engine._populate_sender_profiles(emails, taxonomy, stats)
        assert stats.auto_rule_candidates == 0

    @pytest.mark.asyncio
    async def test_no_auto_rule_for_scattered_folders(self, engine: BootstrapEngine) -> None:
        """Test that senders with scattered folder distribution are not candidates."""
        # Create 10 emails spread across folders (50/50 split)
        emails = [
            Email(
                id=str(i),
                sender_email="scattered@test.com",
                current_folder=f"Folder{i % 2}",
            )
            for i in range(10)
        ]
        taxonomy = {"sender_clusters": {}}
        stats = BootstrapStats()

        await engine._populate_sender_profiles(emails, taxonomy, stats)
        assert stats.auto_rule_candidates == 0

    @pytest.mark.asyncio
    async def test_skips_emails_without_sender(self, engine: BootstrapEngine) -> None:
        """Test that emails without sender_email are skipped."""
        emails = [
            Email(id="1", sender_email="", sender_name=""),
            Email(id="2", sender_email=None),
        ]
        taxonomy = {"sender_clusters": {}}
        stats = BootstrapStats()

        count = await engine._populate_sender_profiles(emails, taxonomy, stats)
        assert count == 0


# ---------------------------------------------------------------------------
# _call_claude_with_yaml_retry
# ---------------------------------------------------------------------------


class TestCallClaudeWithYamlRetry:
    """Tests for the shared Claude retry helper."""

    @pytest.mark.asyncio
    async def test_returns_parsed_result_on_first_attempt(self, engine: BootstrapEngine) -> None:
        """Test successful parse on first attempt."""
        yaml_text = "projects: []\nareas: []\nsender_clusters: {}"
        engine._client.messages.create = MagicMock(return_value=make_claude_response(yaml_text))

        from assistant.classifier.bootstrap_prompts import parse_batch_yaml_response

        stats = BootstrapStats()
        result = await engine._call_claude_with_yaml_retry(
            model="test-model",
            messages=[{"role": "user", "content": "test"}],
            parse_fn=parse_batch_yaml_response,
            task_type="bootstrap_pass1",
            stats=stats,
            error_context="test batch",
        )

        assert result["projects"] == []
        assert engine._client.messages.create.call_count == 1

    @pytest.mark.asyncio
    async def test_retries_on_malformed_yaml(self, engine: BootstrapEngine) -> None:
        """Test retry with corrective prompt on parse failure."""
        bad_yaml = "not valid yaml {{{"
        good_yaml = "projects: []\nareas: []\nsender_clusters: {}"

        engine._client.messages.create = MagicMock(
            side_effect=[
                make_claude_response(bad_yaml),
                make_claude_response(good_yaml),
            ]
        )

        from assistant.classifier.bootstrap_prompts import parse_batch_yaml_response

        stats = BootstrapStats()
        result = await engine._call_claude_with_yaml_retry(
            model="test-model",
            messages=[{"role": "user", "content": "test"}],
            parse_fn=parse_batch_yaml_response,
            task_type="bootstrap_pass1",
            stats=stats,
            error_context="test batch",
        )

        assert result["projects"] == []
        assert engine._client.messages.create.call_count == 2

    @pytest.mark.asyncio
    async def test_raises_on_api_error(self, engine: BootstrapEngine) -> None:
        """Test ClassificationError on API failure."""
        import anthropic

        from assistant.classifier.bootstrap_prompts import parse_batch_yaml_response
        from assistant.core.errors import ClassificationError

        engine._client.messages.create = MagicMock(
            side_effect=anthropic.APIConnectionError(request=MagicMock())
        )

        stats = BootstrapStats()
        with pytest.raises(ClassificationError, match="test batch"):
            await engine._call_claude_with_yaml_retry(
                model="test-model",
                messages=[{"role": "user", "content": "test"}],
                parse_fn=parse_batch_yaml_response,
                task_type="bootstrap_pass1",
                stats=stats,
                error_context="test batch",
            )

    @pytest.mark.asyncio
    async def test_raises_after_second_parse_failure(self, engine: BootstrapEngine) -> None:
        """Test ClassificationError when both attempts produce bad YAML."""
        from assistant.classifier.bootstrap_prompts import parse_batch_yaml_response
        from assistant.core.errors import ClassificationError

        bad_yaml = "not valid yaml {{{"
        engine._client.messages.create = MagicMock(
            side_effect=[
                make_claude_response(bad_yaml),
                make_claude_response(bad_yaml),
            ]
        )

        stats = BootstrapStats()
        with pytest.raises(ClassificationError, match="failed after retry"):
            await engine._call_claude_with_yaml_retry(
                model="test-model",
                messages=[{"role": "user", "content": "test"}],
                parse_fn=parse_batch_yaml_response,
                task_type="bootstrap_pass1",
                stats=stats,
                error_context="test batch",
            )


# ---------------------------------------------------------------------------
# run (integration-level with mocks)
# ---------------------------------------------------------------------------


class TestBootstrapRun:
    """Integration-level tests for the full bootstrap run."""

    @pytest.mark.asyncio
    async def test_returns_early_on_no_emails(self, engine: BootstrapEngine) -> None:
        """Test that bootstrap returns early if no emails found."""
        engine._message_manager.list_messages.return_value = []

        with patch.object(engine, "_check_idempotency", new_callable=AsyncMock):
            stats = await engine.run(days=7, force=True)

        assert stats.total_emails_fetched == 0
        assert stats.total_batches == 0

    @pytest.mark.asyncio
    async def test_full_pipeline_with_mock_claude(
        self, engine: BootstrapEngine, tmp_path: Path
    ) -> None:
        """Test full bootstrap pipeline with mocked Claude responses."""
        # Mock email fetch
        raw_messages = [
            make_graph_message(msg_id=str(i), sender_email=f"sender{i}@test.com") for i in range(3)
        ]
        engine._message_manager.list_messages.return_value = raw_messages

        # Mock Pass 1 Claude response
        batch_yaml = """\
projects:
  - name: "Test Project"
    folder: "Projects/Test"
areas:
  - name: "Test Area"
    folder: "Areas/Test"
sender_clusters:
  newsletters:
    - "news@test.com"
"""
        # Mock Pass 2 Claude response
        consolidated_yaml = """\
projects:
  - name: "Test Project"
    folder: "Projects/Test"
    signals:
      subjects: ["test"]
areas:
  - name: "Test Area"
    folder: "Areas/Test"
    signals:
      subjects: ["test"]
auto_rules: []
key_contacts: []
sender_clusters:
  newsletters:
    - "news@test.com"
"""
        engine._client.messages.create = MagicMock(
            side_effect=[
                make_claude_response(batch_yaml),
                make_claude_response(consolidated_yaml),
            ]
        )

        output_path = tmp_path / "config.yaml.proposed"
        with (
            patch.object(engine, "_check_idempotency", new_callable=AsyncMock),
            patch("assistant.engine.bootstrap.PROPOSED_CONFIG_PATH", output_path),
        ):
            stats = await engine.run(days=7, force=True)

        assert stats.total_emails_fetched == 3
        assert stats.batches_succeeded == 1
        assert stats.batches_failed == 0
        assert output_path.exists()
