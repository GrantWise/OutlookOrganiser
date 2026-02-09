"""Preference learning from user corrections.

Analyzes user corrections to email classifications and generates natural
language preferences that are included in future classification prompts.
Also detects new user-applied categories for formalization.

Usage:
    from assistant.classifier.preference_learner import PreferenceLearner

    learner = PreferenceLearner(store, anthropic_client, config)
    result = await learner.check_and_update()
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from assistant.classifier.prompts import PREFERENCE_UPDATE_PROMPT
from assistant.core.logging import get_logger

if TYPE_CHECKING:
    import anthropic

    from assistant.config_schema import AppConfig
    from assistant.db.store import DatabaseStore

logger = get_logger(__name__)


@dataclass(frozen=True)
class PreferenceUpdateResult:
    """Result of preference learning cycle."""

    corrections_analyzed: int
    preferences_before: str
    preferences_after: str
    changed: bool


class PreferenceLearner:
    """Learn classification preferences from user corrections.

    After each batch of corrections, analyzes patterns and updates
    the natural language preferences stored in agent_state.
    """

    def __init__(
        self,
        store: DatabaseStore,
        anthropic_client: anthropic.AsyncAnthropic,
        config: AppConfig,
    ) -> None:
        self._store = store
        self._client = anthropic_client
        self._config = config

    async def check_and_update(self) -> PreferenceUpdateResult | None:
        """Check if enough corrections exist and update preferences if so.

        Returns:
            PreferenceUpdateResult if update was performed, None if skipped
        """
        if not self._config.learning.enabled:
            return None

        learning = self._config.learning
        since = datetime.now() - timedelta(days=learning.lookback_days)
        count = await self._store.get_correction_count_since(since)

        if count < learning.min_corrections_to_update:
            logger.debug(
                "preference_update_skipped",
                corrections=count,
                threshold=learning.min_corrections_to_update,
            )
            return None

        return await self.update_preferences()

    async def update_preferences(self) -> PreferenceUpdateResult:
        """Analyze recent corrections and update classification preferences.

        Steps:
        0. Check cooldown to prevent redundant re-runs (R1)
        1. Fetch corrections from lookback window
        2. Fetch current preferences from agent_state
        3. Send corrections + current preferences to Claude
        4. Validate response
        5. Store updated preferences in agent_state
        6. Return result with change summary
        """
        learning = self._config.learning

        # R1: Check cooldown to prevent redundant re-runs
        last_update = await self._store.get_state("last_preference_update")
        if last_update:
            try:
                last_dt = datetime.fromisoformat(last_update)
                if datetime.now() - last_dt < timedelta(minutes=5):
                    logger.debug("preference_update_cooldown", last_update=last_update)
                    current = await self._store.get_state("classification_preferences") or ""
                    return PreferenceUpdateResult(
                        corrections_analyzed=0,
                        preferences_before=current,
                        preferences_after=current,
                        changed=False,
                    )
            except ValueError:
                pass  # Invalid timestamp, proceed with update

        # 1. Fetch corrections (R5: limit to 100 most recent)
        corrections = await self._store.get_recent_corrections(learning.lookback_days)
        if len(corrections) > 100:
            logger.warning(
                "preference_corrections_truncated",
                total=len(corrections),
                limit=100,
            )
            corrections = corrections[:100]

        if not corrections:
            current = await self._store.get_state("classification_preferences") or ""
            return PreferenceUpdateResult(
                corrections_analyzed=0,
                preferences_before=current,
                preferences_after=current,
                changed=False,
            )

        # 2. Get current preferences
        current_preferences = (
            await self._store.get_state("classification_preferences")
            or "No preferences learned yet."
        )

        # 3. Format corrections for the prompt
        corrections_text = self._format_corrections(corrections)

        prompt = PREFERENCE_UPDATE_PROMPT.format(
            lookback_days=learning.lookback_days,
            corrections_formatted=corrections_text,
            current_preferences=current_preferences,
            max_words=learning.max_preferences_words,
        )

        # 4. Call Claude
        try:
            response = await self._client.messages.create(
                model=self._config.models.triage,
                max_tokens=1024,
                messages=[{"role": "user", "content": prompt}],
            )

            new_preferences = ""
            for block in response.content:
                if block.type == "text":
                    new_preferences += block.text

            new_preferences = new_preferences.strip()

        except Exception as e:
            logger.warning(
                "preference_update_claude_failed",
                error=str(e),
                message="Keeping existing preferences unchanged.",
            )
            return PreferenceUpdateResult(
                corrections_analyzed=len(corrections),
                preferences_before=current_preferences,
                preferences_after=current_preferences,
                changed=False,
            )

        # 5. Validate and truncate if needed
        if not new_preferences:
            logger.warning("preference_update_empty_response")
            return PreferenceUpdateResult(
                corrections_analyzed=len(corrections),
                preferences_before=current_preferences,
                preferences_after=current_preferences,
                changed=False,
            )

        word_count = len(new_preferences.split())
        if word_count > learning.max_preferences_words:
            # Truncate to max words
            words = new_preferences.split()
            new_preferences = " ".join(words[: learning.max_preferences_words])
            logger.warning(
                "preference_update_truncated",
                original_words=word_count,
                max_words=learning.max_preferences_words,
            )

        # 6. Store
        changed = new_preferences != current_preferences
        if changed:
            await self._store.set_state("classification_preferences", new_preferences)
            logger.info(
                "preferences_updated",
                corrections_analyzed=len(corrections),
                word_count=len(new_preferences.split()),
            )

        # R1: Update cooldown timestamp
        await self._store.set_state("last_preference_update", datetime.now().isoformat())

        return PreferenceUpdateResult(
            corrections_analyzed=len(corrections),
            preferences_before=current_preferences,
            preferences_after=new_preferences,
            changed=changed,
        )

    def _format_corrections(self, corrections: list[dict[str, Any]]) -> str:
        """Format corrections list for the preference update prompt.

        Args:
            corrections: List of correction dicts from store

        Returns:
            Formatted string for prompt inclusion
        """
        lines: list[str] = []

        for i, c in enumerate(corrections, 1):
            parts: list[str] = []

            # Show what changed
            if c["suggested_folder"] != c["approved_folder"]:
                parts.append(f"  Folder: {c['suggested_folder']} -> {c['approved_folder']}")
            if c["suggested_priority"] != c["approved_priority"]:
                parts.append(f"  Priority: {c['suggested_priority']} -> {c['approved_priority']}")
            if c["suggested_action_type"] != c["approved_action_type"]:
                parts.append(
                    f"  Action: {c['suggested_action_type']} -> {c['approved_action_type']}"
                )

            if parts:
                # S1: Truncate PII to limit exposure in prompts
                subject = (c.get("subject", "No subject") or "No subject")[:50]
                sender = (c.get("sender_email", "unknown") or "unknown")[:20]
                lines.append(f'Correction {i}: "{subject}" from {sender}')
                lines.extend(parts)
                lines.append("")

        return "\n".join(lines) if lines else "No corrections found."
