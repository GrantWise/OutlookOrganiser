"""Triage engine for scheduled email classification.

Polls for new emails at configured intervals, classifies them via the
existing pipeline (auto-rules -> thread inheritance -> Claude), and stores
suggestions for user review in the web UI.

Classification pipeline per email:
1. Skip if already in database
2. Save email to database
3. Check auto-rules -> create auto-approved suggestion if match
4. Check thread inheritance -> inherit folder if applicable
5. Build classification context (thread context, sender history)
6. Call Claude classifier with tool use
7. Store suggestion in database
8. Create waiting-for tracker if action_type is "Waiting For"

Graceful degradation: After MAX_CONSECUTIVE_FAILURES consecutive cycles
where ALL Claude calls fail, switch to auto-rules-only mode and queue
remaining emails as pending for when the API recovers.

Spec reference: Reference/spec/03-agent-behaviors.md Section 2

Usage:
    from assistant.engine.triage import TriageEngine

    engine = TriageEngine(
        classifier=email_classifier,
        store=db_store,
        message_manager=msg_mgr,
        folder_manager=folder_mgr,
        snippet_cleaner=cleaner,
        thread_manager=thread_mgr,
        sent_cache=sent_cache,
        config=app_config,
    )
    result = await engine.run_cycle()
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from assistant.classifier.prompts import ClassificationContext
from assistant.core.errors import ClassificationError, DatabaseError, GraphAPIError
from assistant.core.logging import get_logger, set_correlation_id
from assistant.db.store import Email

if TYPE_CHECKING:
    from assistant.classifier.claude_classifier import ClassificationResult, EmailClassifier
    from assistant.classifier.snippet import SnippetCleaner
    from assistant.config_schema import AppConfig
    from assistant.db.store import DatabaseStore
    from assistant.engine.thread_utils import ThreadContextManager
    from assistant.graph.folders import FolderManager
    from assistant.graph.messages import MessageManager, SentItemsCache

logger = get_logger(__name__)

# Maximum consecutive failed cycles before entering degraded mode
MAX_CONSECUTIVE_FAILURES = 3


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass
class TriageCycleResult:
    """Result of a single triage cycle."""

    cycle_id: str
    duration_ms: int = 0
    emails_fetched: int = 0
    emails_processed: int = 0
    auto_ruled: int = 0
    classified: int = 0
    inherited: int = 0
    skipped: int = 0
    failed: int = 0
    degraded_mode: bool = False
    suggestions_expired: int = 0
    logs_pruned: int = 0


@dataclass
class _ProcessResult:
    """Internal result of processing a single email."""

    method: str  # 'auto_rule', 'claude_tool_use', 'claude_inherited', 'skipped', 'failed'
    suggestion_id: int | None = None


class TriageEngine:
    """Scheduled triage engine that polls for new emails and classifies them.

    Each cycle generates a UUID4 triage_cycle_id for log correlation.
    All log entries within a cycle share this ID for end-to-end tracing.

    Attributes:
        _classifier: EmailClassifier for auto-rules + Claude
        _store: DatabaseStore for persistence
        _message_manager: MessageManager for Graph API email operations
        _folder_manager: FolderManager for folder path resolution
        _snippet_cleaner: SnippetCleaner for cleaning email bodies
        _thread_manager: ThreadContextManager for thread inheritance + context
        _sent_cache: SentItemsCache for reply state detection
        _config: Application configuration
        _consecutive_failures: Count of consecutive all-fail cycles
        _degraded_mode: Whether operating in auto-rules-only mode
    """

    def __init__(
        self,
        classifier: EmailClassifier,
        store: DatabaseStore,
        message_manager: MessageManager,
        folder_manager: FolderManager,
        snippet_cleaner: SnippetCleaner,
        thread_manager: ThreadContextManager,
        sent_cache: SentItemsCache,
        config: AppConfig,
    ):
        self._classifier = classifier
        self._store = store
        self._message_manager = message_manager
        self._folder_manager = folder_manager
        self._snippet_cleaner = snippet_cleaner
        self._thread_manager = thread_manager
        self._sent_cache = sent_cache
        self._config = config
        self._consecutive_failures = 0
        self._degraded_mode = False

    @property
    def degraded_mode(self) -> bool:
        """Whether the engine is in degraded (auto-rules-only) mode."""
        return self._degraded_mode

    def update_config(self, config: AppConfig) -> None:
        """Update the config reference for hot-reload support.

        Args:
            config: New AppConfig instance
        """
        self._config = config

    async def run_cycle(self) -> TriageCycleResult:
        """Execute a single triage cycle.

        Steps:
        1. Generate triage_cycle_id and set as correlation ID
        2. Refresh system prompt for config changes
        3. Refresh sent items cache
        4. Fetch new emails from watched folders
        5. Process each email through classification pipeline
        6. Update last_processed_timestamp
        7. Run maintenance (expire suggestions, prune logs)
        8. Log cycle summary

        Returns:
            TriageCycleResult with counts and timing
        """
        cycle_id = str(uuid.uuid4())
        set_correlation_id(cycle_id)
        start_time = time.monotonic()

        result = TriageCycleResult(cycle_id=cycle_id, degraded_mode=self._degraded_mode)

        logger.info(
            "triage_cycle_start",
            degraded_mode=self._degraded_mode,
            interval_minutes=self._config.triage.interval_minutes,
        )

        try:
            # 1. Refresh classifier system prompt (picks up config changes)
            await self._classifier.refresh_system_prompt()

            # 2. Refresh sent items cache for reply state detection
            try:
                self._sent_cache.refresh(hours=self._config.triage.lookback_hours * 2)
            except GraphAPIError as e:
                logger.warning("sent_cache_refresh_failed", error=str(e))

            # 3. Fetch new emails
            raw_emails = await self._fetch_new_emails()
            result.emails_fetched = len(raw_emails)

            if not raw_emails:
                logger.info("triage_cycle_no_new_emails")
            else:
                # 4. Process each email
                claude_attempted = 0
                claude_failed = 0

                for raw_email in raw_emails[: self._config.triage.batch_size]:
                    process_result = await self._process_email(raw_email, cycle_id)

                    if process_result.method == "skipped":
                        result.skipped += 1
                    elif process_result.method == "auto_rule":
                        result.auto_ruled += 1
                        result.emails_processed += 1
                    elif process_result.method == "claude_inherited":
                        result.inherited += 1
                        result.emails_processed += 1
                        claude_attempted += 1
                    elif process_result.method == "claude_tool_use":
                        result.classified += 1
                        result.emails_processed += 1
                        claude_attempted += 1
                    elif process_result.method == "failed":
                        result.failed += 1
                        result.emails_processed += 1
                        claude_attempted += 1
                        claude_failed += 1

                # 5. Update degraded mode state
                self._update_degraded_mode(claude_attempted, claude_failed)
                result.degraded_mode = self._degraded_mode

            # 6. Update last processed timestamp
            await self._store.set_state(
                "last_processed_timestamp",
                datetime.now(UTC).isoformat(),
            )

            # 7. Store last cycle info for dashboard
            await self._store.set_state(
                "last_triage_cycle",
                datetime.now(UTC).isoformat(),
            )
            await self._store.set_state("last_triage_cycle_id", cycle_id)

            # 8. Run maintenance
            try:
                result.suggestions_expired = await self._store.expire_old_suggestions(
                    self._config.suggestion_queue.expire_after_days
                )
            except DatabaseError as e:
                logger.warning("suggestion_expiry_failed", error=str(e))

            try:
                result.logs_pruned = await self._store.prune_llm_logs(
                    self._config.llm_logging.retention_days
                )
            except DatabaseError as e:
                logger.warning("log_pruning_failed", error=str(e))

        except (GraphAPIError, DatabaseError) as e:
            logger.error("triage_cycle_error", error=str(e), error_type=type(e).__name__)
        finally:
            result.duration_ms = int((time.monotonic() - start_time) * 1000)

            logger.info(
                "triage_cycle_complete",
                duration_ms=result.duration_ms,
                emails_fetched=result.emails_fetched,
                emails_processed=result.emails_processed,
                auto_ruled=result.auto_ruled,
                classified=result.classified,
                inherited=result.inherited,
                skipped=result.skipped,
                failed=result.failed,
                degraded_mode=result.degraded_mode,
                suggestions_expired=result.suggestions_expired,
                logs_pruned=result.logs_pruned,
            )

            set_correlation_id(None)

        return result

    async def _fetch_new_emails(self) -> list[dict[str, Any]]:
        """Fetch new emails from watched folders since last processed time.

        Uses last_processed_timestamp from agent_state, falling back to
        lookback_hours on first run.

        Returns:
            List of raw message dicts from Graph API
        """
        # Determine cutoff time
        last_timestamp = await self._store.get_state("last_processed_timestamp")
        if last_timestamp:
            try:
                cutoff = datetime.fromisoformat(last_timestamp)
            except ValueError:
                logger.warning(
                    "invalid_last_processed_timestamp",
                    value=last_timestamp,
                )
                cutoff = datetime.now(UTC) - timedelta(hours=self._config.triage.lookback_hours)
        else:
            cutoff = datetime.now(UTC) - timedelta(hours=self._config.triage.lookback_hours)

        cutoff_str = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")

        all_messages: list[dict[str, Any]] = []
        seen_ids: set[str] = set()

        for folder in self._config.triage.watch_folders:
            try:
                messages = self._message_manager.list_messages(
                    folder=folder,
                    filter_query=f"receivedDateTime ge {cutoff_str}",
                    order_by="receivedDateTime desc",
                    top=50,
                    max_items=self._config.triage.batch_size * 2,
                )

                # Deduplicate across folders
                for msg in messages:
                    msg_id = msg.get("id", "")
                    if msg_id and msg_id not in seen_ids:
                        all_messages.append(msg)
                        seen_ids.add(msg_id)

            except GraphAPIError as e:
                logger.error(
                    "fetch_folder_failed",
                    folder=folder,
                    error=str(e),
                )

        logger.debug("emails_fetched", count=len(all_messages))
        return all_messages

    async def _process_email(
        self,
        raw_msg: dict[str, Any],
        cycle_id: str,
    ) -> _ProcessResult:
        """Process a single email through the classification pipeline.

        Pipeline:
        1. Check if already in database -> skip
        2. Transform raw message to Email and save
        3. Check auto-rules -> create auto-approved suggestion
        4. If degraded mode -> save as pending, skip Claude
        5. Check thread inheritance -> build context
        6. Call Claude classifier
        7. Store suggestion
        8. Create waiting-for if needed

        Args:
            raw_msg: Raw message dict from Graph API
            cycle_id: Current triage cycle ID

        Returns:
            _ProcessResult with method and optional suggestion_id
        """
        msg_id = raw_msg.get("id", "")
        if not msg_id:
            return _ProcessResult(method="skipped")

        # 1. Skip if already processed
        try:
            if await self._store.email_exists(msg_id):
                return _ProcessResult(method="skipped")
        except DatabaseError as e:
            logger.warning("email_exists_check_failed", email_id=msg_id[:20], error=str(e))
            return _ProcessResult(method="skipped")

        # 2. Transform and save email
        email = self._transform_message(raw_msg)
        try:
            await self._store.save_email(email)
        except DatabaseError as e:
            logger.error("email_save_failed", email_id=msg_id[:20], error=str(e))
            return _ProcessResult(method="failed")

        # 3. Check auto-rules
        auto_result = self._classifier.classify_with_auto_rules(
            sender_email=email.sender_email or "",
            subject=email.subject or "",
        )
        if auto_result:
            return await self._handle_auto_rule(email, auto_result)

        # 4. If degraded mode, queue as pending (no Claude call)
        if self._degraded_mode:
            logger.debug(
                "degraded_mode_skip_claude",
                email_id=msg_id[:20],
            )
            return _ProcessResult(method="skipped")

        # 5-7. Full classification pipeline
        return await self._classify_and_store(email, cycle_id)

    async def _handle_auto_rule(
        self,
        email: Email,
        result: ClassificationResult,
    ) -> _ProcessResult:
        """Handle an auto-rule match by creating an auto-approved suggestion.

        Args:
            email: Email that matched the auto-rule
            result: Classification result from auto-rules

        Returns:
            _ProcessResult with method='auto_rule'
        """
        try:
            suggestion_id = await self._store.create_suggestion(
                email_id=email.id,
                suggested_folder=result.folder,
                suggested_priority=result.priority,
                suggested_action_type=result.action_type,
                confidence=result.confidence,
                reasoning=result.reasoning,
            )

            # Auto-approve since auto-rules are high-confidence
            await self._store.approve_suggestion(suggestion_id)

            # Update classification status
            await self._store.update_classification_status(
                email.id,
                "classified",
                result.to_dict(),
            )

            # Log the action
            await self._store.log_action(
                action_type="classify",
                email_id=email.id,
                details={
                    "method": "auto_rule",
                    "folder": result.folder,
                    "priority": result.priority,
                    "action_type": result.action_type,
                    "reasoning": result.reasoning,
                },
                triggered_by="auto",
            )

            logger.info(
                "email_auto_ruled",
                email_id=email.id[:20] + "...",
                folder=result.folder,
            )

            return _ProcessResult(method="auto_rule", suggestion_id=suggestion_id)

        except DatabaseError as e:
            logger.error(
                "auto_rule_suggestion_failed",
                email_id=email.id[:20],
                error=str(e),
            )
            return _ProcessResult(method="failed")

    async def _classify_and_store(
        self,
        email: Email,
        cycle_id: str,
    ) -> _ProcessResult:
        """Run full classification (thread inheritance + Claude) and store result.

        Args:
            email: Email to classify
            cycle_id: Current triage cycle ID

        Returns:
            _ProcessResult with classification method
        """
        sender_email = email.sender_email or ""
        sender_domain = sender_email.split("@")[1].lower() if "@" in sender_email else ""

        # Check thread inheritance
        inherited_folder: str | None = None
        thread_context = None
        sender_history = None

        if email.conversation_id:
            inheritance = await self._thread_manager.check_thread_inheritance(
                conversation_id=email.conversation_id,
                current_subject=email.subject or "",
                current_sender_domain=sender_domain,
            )
            if inheritance.should_inherit:
                inherited_folder = inheritance.inherited_folder

            # Get thread context for Claude
            thread_context = await self._thread_manager.get_thread_context(
                conversation_id=email.conversation_id,
                exclude_message_id=email.id,
            )

        # Get sender history
        sender_history = await self._thread_manager.get_sender_history(sender_email)

        # Get sender profile
        sender_profile = await self._store.get_sender_profile(sender_email)

        # Check reply state
        has_user_reply = False
        if email.conversation_id:
            reply = self._message_manager.check_reply_state(
                email.conversation_id,
                sent_cache=self._sent_cache,
            )
            has_user_reply = reply is not None

        # Build classification context
        context = ClassificationContext(
            inherited_folder=inherited_folder,
            thread_context=thread_context,
            sender_history=sender_history,
            sender_profile=sender_profile,
            thread_depth=thread_context.thread_depth if thread_context else 0,
            has_user_reply=has_user_reply,
        )

        # Call Claude classifier
        try:
            classification = await self._classifier.classify_with_claude(
                email_id=email.id,
                sender_name=email.sender_name or "",
                sender_email=sender_email,
                subject=email.subject or "",
                received_datetime=(
                    email.received_at.isoformat() if email.received_at else "unknown"
                ),
                importance=email.importance,
                is_read=email.is_read,
                flag_status=email.flag_status,
                snippet=email.snippet or "",
                context=context,
                triage_cycle_id=cycle_id,
            )
        except ClassificationError as e:
            logger.warning(
                "classification_failed",
                email_id=email.id[:20] + "...",
                error=str(e),
            )
            attempts = await self._store.increment_classification_attempts(email.id)
            if attempts >= 3:
                await self._store.update_classification_status(email.id, "failed")
            return _ProcessResult(method="failed")

        # Store suggestion
        try:
            suggestion_id = await self._store.create_suggestion(
                email_id=email.id,
                suggested_folder=classification.folder,
                suggested_priority=classification.priority,
                suggested_action_type=classification.action_type,
                confidence=classification.confidence,
                reasoning=classification.reasoning,
            )

            # Update classification status
            await self._store.update_classification_status(
                email.id,
                "classified",
                classification.to_dict(),
            )

            # Log the action
            await self._store.log_action(
                action_type="suggest",
                email_id=email.id,
                details={
                    "method": classification.method,
                    "folder": classification.folder,
                    "priority": classification.priority,
                    "action_type": classification.action_type,
                    "confidence": classification.confidence,
                    "inherited_folder": classification.inherited_folder,
                },
                triggered_by="auto",
            )

            # Create waiting-for tracker if needed
            await self._create_waiting_for_if_needed(email, classification)

            # Update sender profile
            await self._store.upsert_sender_profile(
                email=sender_email,
                display_name=email.sender_name,
                increment_count=True,
            )

            logger.info(
                "email_classified",
                email_id=email.id[:20] + "...",
                method=classification.method,
                folder=classification.folder,
                confidence=classification.confidence,
            )

            return _ProcessResult(method=classification.method, suggestion_id=suggestion_id)

        except DatabaseError as e:
            logger.error(
                "suggestion_creation_failed",
                email_id=email.id[:20],
                error=str(e),
            )
            return _ProcessResult(method="failed")

    async def _create_waiting_for_if_needed(
        self,
        email: Email,
        result: ClassificationResult,
    ) -> None:
        """Create a waiting-for tracker if action_type is 'Waiting For'.

        Args:
            email: The classified email
            result: Classification result
        """
        if result.action_type != "Waiting For" or not result.waiting_for_detail:
            return

        expected_from = result.waiting_for_detail.get("expected_from", "")
        description = result.waiting_for_detail.get("description", "")

        if not expected_from:
            return

        try:
            await self._store.create_waiting_for(
                email_id=email.id,
                conversation_id=email.conversation_id or "",
                expected_from=expected_from,
                description=description,
                nudge_after_hours=self._config.aging.waiting_for_nudge_hours,
            )
            logger.info(
                "waiting_for_created",
                email_id=email.id[:20] + "...",
                expected_from=expected_from,
            )
        except DatabaseError as e:
            logger.warning(
                "waiting_for_creation_failed",
                email_id=email.id[:20],
                error=str(e),
            )

    def _transform_message(self, raw_msg: dict[str, Any]) -> Email:
        """Transform a raw Graph API message dict to an Email dataclass.

        Args:
            raw_msg: Raw message from Graph API

        Returns:
            Email dataclass with cleaned snippet
        """
        from_data = raw_msg.get("from", {}).get("emailAddress", {})
        sender_email = from_data.get("address", "")
        sender_name = from_data.get("name", "")
        body_preview = raw_msg.get("bodyPreview", "")
        cleaned = self._snippet_cleaner.clean(body_preview, is_html=False)

        received_str = raw_msg.get("receivedDateTime", "")
        received_at = None
        if received_str:
            try:
                received_at = datetime.fromisoformat(received_str.replace("Z", "+00:00"))
            except ValueError:
                logger.warning("invalid_received_datetime", value=received_str)

        flag_data = raw_msg.get("flag", {})
        flag_status = (
            flag_data.get("flagStatus", "notFlagged")
            if isinstance(flag_data, dict)
            else "notFlagged"
        )

        return Email(
            id=raw_msg.get("id", ""),
            conversation_id=raw_msg.get("conversationId"),
            conversation_index=raw_msg.get("conversationIndex"),
            subject=raw_msg.get("subject", ""),
            sender_email=sender_email,
            sender_name=sender_name,
            received_at=received_at,
            snippet=cleaned.cleaned_text,
            current_folder=None,
            web_link=raw_msg.get("webLink"),
            importance=raw_msg.get("importance", "normal"),
            is_read=bool(raw_msg.get("isRead", False)),
            flag_status=flag_status,
            classification_status="pending",
        )

    def _update_degraded_mode(self, claude_attempted: int, claude_failed: int) -> None:
        """Update degraded mode state based on cycle results.

        After MAX_CONSECUTIVE_FAILURES consecutive all-fail cycles,
        enter degraded mode. Reset on first successful Claude call.

        Args:
            claude_attempted: Number of Claude classifications attempted
            claude_failed: Number that failed
        """
        if claude_attempted == 0:
            # No Claude calls this cycle, don't change state
            return

        if claude_failed == claude_attempted:
            # All Claude calls failed
            self._consecutive_failures += 1
            if self._consecutive_failures >= MAX_CONSECUTIVE_FAILURES and not self._degraded_mode:
                self._degraded_mode = True
                logger.warning(
                    "entering_degraded_mode",
                    consecutive_failures=self._consecutive_failures,
                    message=(
                        f"All Claude classifications failed for {self._consecutive_failures} "
                        "consecutive cycles. Switching to auto-rules-only mode. "
                        "Check ANTHROPIC_API_KEY and API status."
                    ),
                )
        else:
            # At least one Claude call succeeded
            if self._degraded_mode:
                logger.info(
                    "exiting_degraded_mode",
                    message="Claude API recovered. Resuming full classification.",
                )
            self._consecutive_failures = 0
            self._degraded_mode = False

