"""Email body snippet cleaning pipeline.

This module provides the 6-step snippet cleaning pipeline for preparing
email body text for classification. The pipeline removes noise (signatures,
disclaimers, forwarded headers) to improve classification accuracy.

CRITICAL SECURITY NOTE:
All regex operations use the `regex` library with timeout parameter on
match operations to prevent ReDoS (Regular Expression Denial of Service)
attacks from malicious email content.

Usage:
    from assistant.classifier.snippet import SnippetCleaner, clean_snippet

    cleaner = SnippetCleaner()
    result = cleaner.clean(email_body, is_html=True)
    print(result.cleaned_text)

    # Or use convenience function
    cleaned = clean_snippet(email_body, is_html=True)
"""

from __future__ import annotations

import html
from dataclasses import dataclass, field

import regex

from assistant.core.logging import get_logger

logger = get_logger(__name__)

# Default limits (from spec 03-agent-behaviors.md Section 6)
DEFAULT_MAX_LENGTH = 1000  # Primary snippet for classification
DEFAULT_CONTEXT_MAX_LENGTH = 500  # Thread context snippets

# Regex timeout in seconds (CRITICAL: all operations MUST use this)
REGEX_TIMEOUT = 1.0


# =============================================================================
# Compiled Regex Patterns
# Note: timeout is passed at match time (search, sub, etc.), not compile time
# =============================================================================

# Step 1: HTML processing
HTML_TAG_PATTERN = regex.compile(r"<[^>]+>")

# Step 2: Forwarded message headers
FORWARDED_HEADER_PATTERNS = [
    # Standard forwarded message delimiter
    regex.compile(
        r"^-{5,}\s*Forwarded message\s*-{5,}.*?(?=\n\n|\Z)",
        regex.MULTILINE | regex.IGNORECASE | regex.DOTALL,
    ),
    # "On [date], [name] wrote:" pattern (quoted reply header)
    regex.compile(
        r"^On .+? wrote:\s*$",
        regex.MULTILINE,
    ),
    # Outlook-style "From: ... Sent: ... To: ... Subject: ..." block
    regex.compile(
        r"^From:\s+.+?\nSent:\s+.+?\nTo:\s+.+?\nSubject:\s+.+?(?=\n\n|\Z)",
        regex.MULTILINE | regex.DOTALL,
    ),
]

# Step 3: Signature blocks
SIGNATURE_PATTERNS = [
    # Classic "-- " signature delimiter (must be at start of line)
    regex.compile(
        r"^--\s*\n.*",
        regex.MULTILINE | regex.DOTALL,
    ),
    # Underscore line signature delimiter
    regex.compile(
        r"^_{5,}.*",
        regex.MULTILINE | regex.DOTALL,
    ),
    # Mobile device signatures
    regex.compile(
        r"^Sent from my (iPhone|iPad|Android|Galaxy|Pixel|mobile).*$",
        regex.MULTILINE | regex.IGNORECASE,
    ),
    regex.compile(
        r"^Get Outlook for (iOS|Android).*$",
        regex.MULTILINE | regex.IGNORECASE,
    ),
    # Common sign-off patterns at end of email
    regex.compile(
        r"\n(Best regards?|Kind regards?|Regards|Thanks|Thank you|Cheers|"
        r"Sincerely|Best wishes|Warm regards)[,\s]*\n.{0,200}$",
        regex.IGNORECASE,
    ),
]

# Step 4: Legal disclaimers
DISCLAIMER_PATTERNS = [
    # Confidentiality notice
    regex.compile(
        r"(?:CONFIDENTIAL|PRIVILEGED|CONFIDENTIALITY).*?"
        r"(?:intended (?:only |solely )?for|addressee|recipient).*?(?=\n\n|\Z)",
        regex.IGNORECASE | regex.DOTALL,
    ),
    # "This email is intended for" pattern
    regex.compile(
        r"This (?:e-?mail|message|communication) is intended "
        r"(?:only |solely )?for.*?(?=\n\n|\Z)",
        regex.IGNORECASE | regex.DOTALL,
    ),
    # "If you received this in error" pattern
    regex.compile(
        r"If you (?:have )?received? this (?:e-?mail|message) in error.*?(?=\n\n|\Z)",
        regex.IGNORECASE | regex.DOTALL,
    ),
    # Explicit disclaimer header
    regex.compile(
        r"^DISCLAIMER:.*",
        regex.MULTILINE | regex.IGNORECASE | regex.DOTALL,
    ),
    # Legal notice block
    regex.compile(
        r"^LEGAL NOTICE:.*",
        regex.MULTILINE | regex.IGNORECASE | regex.DOTALL,
    ),
]

# Step 5: Whitespace normalization
EXCESSIVE_NEWLINES = regex.compile(r"\n{3,}")
EXCESSIVE_SPACES = regex.compile(r"[ \t]{2,}")


def _safe_sub(pattern: regex.Pattern, repl: str, text: str) -> tuple[str, bool]:
    """Safely perform regex substitution with timeout.

    Args:
        pattern: Compiled regex pattern
        repl: Replacement string
        text: Text to process

    Returns:
        Tuple of (result_text, was_modified)
    """
    try:
        result = pattern.sub(repl, text, timeout=REGEX_TIMEOUT)
        return result, result != text
    except TimeoutError:
        logger.warning(
            "Regex timeout during substitution",
            pattern=pattern.pattern[:50] if len(pattern.pattern) > 50 else pattern.pattern,
        )
        return text, False


@dataclass
class CleaningResult:
    """Result of snippet cleaning with metadata for debugging.

    Attributes:
        cleaned_text: The cleaned snippet text
        original_length: Length of the input text
        was_truncated: Whether the text was truncated to fit max_length
        cleaning_steps_applied: List of cleaning steps that modified the text
    """

    cleaned_text: str
    original_length: int
    was_truncated: bool
    cleaning_steps_applied: list[str] = field(default_factory=list)


class SnippetCleaner:
    """Cleans email body text through a 6-step pipeline.

    The pipeline removes noise that doesn't contribute to classification:
    1. Strip HTML tags, decode entities (if HTML body)
    2. Remove forwarded message headers
    3. Remove signature blocks
    4. Remove legal/confidentiality disclaimers
    5. Collapse excessive whitespace
    6. Truncate to max_length

    All regex operations use timeout to prevent ReDoS attacks.

    Attributes:
        max_length: Maximum length for primary snippets (default 1000)
        context_max_length: Maximum length for thread context (default 500)
    """

    def __init__(
        self,
        max_length: int = DEFAULT_MAX_LENGTH,
        context_max_length: int = DEFAULT_CONTEXT_MAX_LENGTH,
    ):
        """Initialize the snippet cleaner.

        Args:
            max_length: Maximum snippet length for primary classification
            context_max_length: Maximum length for thread context snippets
        """
        self.max_length = max_length
        self.context_max_length = context_max_length

    def clean(self, text: str | None, is_html: bool = False) -> CleaningResult:
        """Clean a snippet through the full 6-step pipeline.

        Args:
            text: Raw email body text or HTML (None is treated as empty)
            is_html: True if text contains HTML (will strip tags and decode)

        Returns:
            CleaningResult with cleaned text and metadata

        Note:
            If a regex times out, the cleaner logs a warning and continues
            with the text cleaned up to that point. This ensures malformed
            emails don't halt the entire triage cycle.
        """
        if not text:
            return CleaningResult(
                cleaned_text="",
                original_length=0,
                was_truncated=False,
                cleaning_steps_applied=[],
            )

        original_length = len(text)
        steps_applied: list[str] = []
        current_text = text

        # Step 1: HTML processing
        if is_html:
            current_text, applied = self._step_strip_html(current_text)
            if applied:
                steps_applied.append("strip_html")

        # Step 2: Forwarded headers
        current_text, applied = self._step_remove_forwarded_headers(current_text)
        if applied:
            steps_applied.append("remove_forwarded_headers")

        # Step 3: Signature blocks
        current_text, applied = self._step_remove_signatures(current_text)
        if applied:
            steps_applied.append("remove_signatures")

        # Step 4: Disclaimers
        current_text, applied = self._step_remove_disclaimers(current_text)
        if applied:
            steps_applied.append("remove_disclaimers")

        # Step 5: Whitespace normalization
        current_text, applied = self._step_normalize_whitespace(current_text)
        if applied:
            steps_applied.append("normalize_whitespace")

        # Step 6: Truncation
        was_truncated = len(current_text) > self.max_length
        if was_truncated:
            current_text = current_text[: self.max_length]
            steps_applied.append("truncate")

        return CleaningResult(
            cleaned_text=current_text,
            original_length=original_length,
            was_truncated=was_truncated,
            cleaning_steps_applied=steps_applied,
        )

    def clean_for_context(self, text: str | None, is_html: bool = False) -> str:
        """Clean a snippet for thread context (shorter limit).

        Args:
            text: Raw email body text or HTML
            is_html: True if text contains HTML

        Returns:
            Cleaned text truncated to context_max_length
        """
        # Use the same pipeline but with shorter max length
        original_max = self.max_length
        self.max_length = self.context_max_length
        try:
            result = self.clean(text, is_html=is_html)
            return result.cleaned_text
        finally:
            self.max_length = original_max

    def _step_strip_html(self, text: str) -> tuple[str, bool]:
        """Step 1: Strip HTML tags and decode entities.

        Args:
            text: Input text with HTML

        Returns:
            Tuple of (cleaned_text, was_modified)
        """
        # Remove HTML tags with timeout
        cleaned, modified = _safe_sub(HTML_TAG_PATTERN, " ", text)
        # Decode HTML entities (&amp; -> &, &nbsp; -> space, etc.)
        cleaned = html.unescape(cleaned)
        return cleaned, modified or cleaned != text

    def _step_remove_forwarded_headers(self, text: str) -> tuple[str, bool]:
        """Step 2: Remove forwarded message headers.

        Args:
            text: Input text

        Returns:
            Tuple of (cleaned_text, was_modified)
        """
        modified = False
        current = text

        for pattern in FORWARDED_HEADER_PATTERNS:
            new_text, pattern_modified = _safe_sub(pattern, "", current)
            if pattern_modified:
                modified = True
                current = new_text

        return current, modified

    def _step_remove_signatures(self, text: str) -> tuple[str, bool]:
        """Step 3: Remove signature blocks.

        Args:
            text: Input text

        Returns:
            Tuple of (cleaned_text, was_modified)
        """
        modified = False
        current = text

        for pattern in SIGNATURE_PATTERNS:
            new_text, pattern_modified = _safe_sub(pattern, "", current)
            if pattern_modified:
                modified = True
                current = new_text

        return current, modified

    def _step_remove_disclaimers(self, text: str) -> tuple[str, bool]:
        """Step 4: Remove legal/confidentiality disclaimers.

        Args:
            text: Input text

        Returns:
            Tuple of (cleaned_text, was_modified)
        """
        modified = False
        current = text

        for pattern in DISCLAIMER_PATTERNS:
            new_text, pattern_modified = _safe_sub(pattern, "", current)
            if pattern_modified:
                modified = True
                current = new_text

        return current, modified

    def _step_normalize_whitespace(self, text: str) -> tuple[str, bool]:
        """Step 5: Collapse excessive whitespace.

        Args:
            text: Input text

        Returns:
            Tuple of (cleaned_text, was_modified)
        """
        # Collapse multiple newlines to double newline
        cleaned, _ = _safe_sub(EXCESSIVE_NEWLINES, "\n\n", text)
        # Collapse multiple spaces/tabs to single space
        cleaned, _ = _safe_sub(EXCESSIVE_SPACES, " ", cleaned)
        # Strip leading/trailing whitespace
        cleaned = cleaned.strip()
        return cleaned, cleaned != text


def clean_snippet(
    text: str | None,
    is_html: bool = False,
    max_length: int = DEFAULT_MAX_LENGTH,
) -> str:
    """Convenience function to clean a snippet.

    Args:
        text: Raw email body text or HTML
        is_html: True if text contains HTML
        max_length: Maximum length for the output

    Returns:
        Cleaned text string
    """
    cleaner = SnippetCleaner(max_length=max_length)
    result = cleaner.clean(text, is_html=is_html)
    return result.cleaned_text
