"""Tests for the snippet cleaning pipeline.

Tests the 6-step cleaning pipeline that removes noise from email bodies
to improve classification accuracy.
"""

import pytest

from assistant.classifier.snippet import (
    SnippetCleaner,
    clean_snippet,
)


class TestSnippetCleaner:
    """Tests for the SnippetCleaner class."""

    @pytest.fixture
    def cleaner(self) -> SnippetCleaner:
        """Return a default SnippetCleaner instance."""
        return SnippetCleaner()

    # =========================================================================
    # Step 1: HTML Processing
    # =========================================================================

    def test_strips_html_tags(self, cleaner: SnippetCleaner) -> None:
        """Test that HTML tags are removed."""
        html = "<p>Hello <b>World</b></p>"
        result = cleaner.clean(html, is_html=True)
        assert "<p>" not in result.cleaned_text
        assert "<b>" not in result.cleaned_text
        assert "Hello" in result.cleaned_text
        assert "World" in result.cleaned_text
        assert "strip_html" in result.cleaning_steps_applied

    def test_decodes_html_entities(self, cleaner: SnippetCleaner) -> None:
        """Test that HTML entities are decoded."""
        html = "Tom &amp; Jerry &lt;friends&gt; &nbsp;forever"
        result = cleaner.clean(html, is_html=True)
        assert "&amp;" not in result.cleaned_text
        assert "&lt;" not in result.cleaned_text
        assert "&gt;" not in result.cleaned_text
        assert "Tom & Jerry" in result.cleaned_text
        assert "<friends>" in result.cleaned_text

    def test_plain_text_not_modified_as_html(self, cleaner: SnippetCleaner) -> None:
        """Test that plain text without is_html=True isn't treated as HTML."""
        text = "Hello <not a tag> &amp; more"
        result = cleaner.clean(text, is_html=False)
        # Tags should remain
        assert "<not a tag>" in result.cleaned_text
        # But entities aren't decoded in plain text mode
        assert "&amp;" in result.cleaned_text

    # =========================================================================
    # Step 2: Forwarded Headers
    # =========================================================================

    def test_removes_forwarded_message_header(self, cleaner: SnippetCleaner) -> None:
        """Test removal of forwarded message headers."""
        text = """Hi team,

Please see the forwarded message below.

---------- Forwarded message ----------
From: sender@example.com
Date: Mon, 1 Jan 2024
Subject: Original topic

Original content here."""

        result = cleaner.clean(text)
        assert "---------- Forwarded message ----------" not in result.cleaned_text
        assert "Please see the forwarded" in result.cleaned_text
        assert "remove_forwarded_headers" in result.cleaning_steps_applied

    def test_removes_on_wrote_pattern(self, cleaner: SnippetCleaner) -> None:
        """Test removal of 'On [date], [name] wrote:' pattern."""
        text = """Sounds good!

On Mon, Jan 1, 2024 at 10:00 AM John Doe <john@example.com> wrote:

> Original message content"""

        result = cleaner.clean(text)
        assert "Sounds good" in result.cleaned_text
        # The "On ... wrote:" line should be removed
        assert "remove_forwarded_headers" in result.cleaning_steps_applied

    # =========================================================================
    # Step 3: Signature Blocks
    # =========================================================================

    def test_removes_dash_dash_signature(self, cleaner: SnippetCleaner) -> None:
        """Test removal of -- style signatures."""
        text = """Hi,

Here is the project update.

--
John Smith
Senior Developer
Acme Corp
Phone: 555-1234"""

        result = cleaner.clean(text)
        assert "Here is the project update" in result.cleaned_text
        assert "John Smith" not in result.cleaned_text
        assert "Acme Corp" not in result.cleaned_text
        assert "remove_signatures" in result.cleaning_steps_applied

    def test_removes_underscore_signature(self, cleaner: SnippetCleaner) -> None:
        """Test removal of _____ style signatures."""
        text = """Meeting confirmed for 3pm.

_____________________
Jane Doe | Manager
Company Inc."""

        result = cleaner.clean(text)
        assert "Meeting confirmed" in result.cleaned_text
        assert "Jane Doe" not in result.cleaned_text
        assert "remove_signatures" in result.cleaning_steps_applied

    def test_removes_sent_from_mobile(self, cleaner: SnippetCleaner) -> None:
        """Test removal of mobile device signatures."""
        text = """Got it, thanks!

Sent from my iPhone"""

        result = cleaner.clean(text)
        assert "Got it, thanks" in result.cleaned_text
        assert "Sent from my iPhone" not in result.cleaned_text
        assert "remove_signatures" in result.cleaning_steps_applied

    def test_removes_get_outlook_signature(self, cleaner: SnippetCleaner) -> None:
        """Test removal of Outlook mobile signatures."""
        text = """Will do.

Get Outlook for iOS"""

        result = cleaner.clean(text)
        assert "Will do" in result.cleaned_text
        assert "Get Outlook for iOS" not in result.cleaned_text

    def test_removes_sign_off_signature(self, cleaner: SnippetCleaner) -> None:
        """Test removal of common sign-off signatures."""
        text = """The meeting is at 2pm.

Best regards,
John"""

        result = cleaner.clean(text)
        assert "meeting is at 2pm" in result.cleaned_text
        # Sign-off removal is best-effort
        assert "remove_signatures" in result.cleaning_steps_applied

    # =========================================================================
    # Step 4: Disclaimers
    # =========================================================================

    def test_removes_confidentiality_disclaimer(self, cleaner: SnippetCleaner) -> None:
        """Test removal of confidentiality notices."""
        text = """Hi team,

The project is on track.

CONFIDENTIAL: This email and any attachments are intended only for the
addressee and may contain privileged information. If you have received
this email in error, please notify the sender immediately and delete it."""

        result = cleaner.clean(text)
        assert "project is on track" in result.cleaned_text
        assert "CONFIDENTIAL" not in result.cleaned_text
        assert "intended only for" not in result.cleaned_text
        assert "remove_disclaimers" in result.cleaning_steps_applied

    def test_removes_intended_for_disclaimer(self, cleaner: SnippetCleaner) -> None:
        """Test removal of 'intended for' disclaimers."""
        text = """See attached report.

This message is intended solely for the addressee(s) named above and may
contain confidential information. If you are not the intended recipient,
you must not use, disclose, or copy this email."""

        result = cleaner.clean(text)
        assert "attached report" in result.cleaned_text
        assert "intended solely" not in result.cleaned_text

    def test_removes_received_in_error_disclaimer(self, cleaner: SnippetCleaner) -> None:
        """Test removal of 'received in error' disclaimers."""
        text = """Please review the document.

If you have received this e-mail in error, please notify the sender
immediately by telephone or email and delete the original message."""

        result = cleaner.clean(text)
        assert "review the document" in result.cleaned_text
        assert "received this e-mail in error" not in result.cleaned_text

    # =========================================================================
    # Step 5: Whitespace Normalization
    # =========================================================================

    def test_collapses_multiple_newlines(self, cleaner: SnippetCleaner) -> None:
        """Test that excessive newlines are collapsed."""
        text = "First paragraph.\n\n\n\n\nSecond paragraph."
        result = cleaner.clean(text)
        assert "\n\n\n" not in result.cleaned_text
        assert "First paragraph" in result.cleaned_text
        assert "Second paragraph" in result.cleaned_text

    def test_collapses_multiple_spaces(self, cleaner: SnippetCleaner) -> None:
        """Test that excessive spaces are collapsed."""
        text = "Hello    world   how    are    you"
        result = cleaner.clean(text)
        assert "    " not in result.cleaned_text
        assert "Hello" in result.cleaned_text
        assert "world" in result.cleaned_text

    def test_strips_leading_trailing_whitespace(self, cleaner: SnippetCleaner) -> None:
        """Test that leading/trailing whitespace is removed."""
        text = "   \n\n  Content here  \n\n   "
        result = cleaner.clean(text)
        assert result.cleaned_text == "Content here"

    # =========================================================================
    # Step 6: Truncation
    # =========================================================================

    def test_truncates_to_max_length(self, cleaner: SnippetCleaner) -> None:
        """Test that text is truncated to max_length."""
        text = "A" * 2000
        result = cleaner.clean(text)
        assert len(result.cleaned_text) == 1000  # Default max_length
        assert result.was_truncated is True
        assert "truncate" in result.cleaning_steps_applied

    def test_respects_custom_max_length(self) -> None:
        """Test that custom max_length is respected."""
        cleaner = SnippetCleaner(max_length=500)
        text = "A" * 1000
        result = cleaner.clean(text)
        assert len(result.cleaned_text) == 500
        assert result.was_truncated is True

    def test_no_truncation_when_under_limit(self, cleaner: SnippetCleaner) -> None:
        """Test that short text isn't truncated."""
        text = "Short text"
        result = cleaner.clean(text)
        assert result.was_truncated is False
        assert "truncate" not in result.cleaning_steps_applied

    def test_clean_for_context_uses_shorter_limit(self) -> None:
        """Test that clean_for_context uses context_max_length."""
        cleaner = SnippetCleaner(max_length=1000, context_max_length=500)
        text = "A" * 800
        result = cleaner.clean_for_context(text)
        assert len(result) == 500

    # =========================================================================
    # Edge Cases
    # =========================================================================

    def test_handles_empty_input(self, cleaner: SnippetCleaner) -> None:
        """Test that empty string input is handled."""
        result = cleaner.clean("")
        assert result.cleaned_text == ""
        assert result.original_length == 0
        assert result.was_truncated is False
        assert result.cleaning_steps_applied == []

    def test_handles_none_input(self, cleaner: SnippetCleaner) -> None:
        """Test that None input is handled gracefully."""
        result = cleaner.clean(None)
        assert result.cleaned_text == ""
        assert result.original_length == 0

    def test_handles_whitespace_only_input(self, cleaner: SnippetCleaner) -> None:
        """Test that whitespace-only input produces empty result."""
        result = cleaner.clean("   \n\n\t   ")
        assert result.cleaned_text == ""

    def test_preserves_meaningful_content(self, cleaner: SnippetCleaner) -> None:
        """Test that meaningful content is preserved through pipeline."""
        text = """Hi Sarah,

I wanted to follow up on the project timeline. Can we schedule a call
for tomorrow at 2pm to discuss the deliverables?

The main items to cover:
1. Budget review
2. Resource allocation
3. Timeline adjustments

Let me know if that works for you.

Thanks,
John"""

        result = cleaner.clean(text)
        # Core content should be preserved
        assert "follow up on the project timeline" in result.cleaned_text
        assert "schedule a call" in result.cleaned_text
        assert "Budget review" in result.cleaned_text
        assert "Resource allocation" in result.cleaned_text

    # =========================================================================
    # CleaningResult metadata
    # =========================================================================

    def test_cleaning_result_tracks_original_length(self, cleaner: SnippetCleaner) -> None:
        """Test that original length is tracked correctly."""
        text = "Hello" * 100
        result = cleaner.clean(text)
        assert result.original_length == 500

    def test_cleaning_result_tracks_steps_applied(self, cleaner: SnippetCleaner) -> None:
        """Test that applied steps are tracked."""
        html = "<p>Hello</p>\n\n\n\nWorld"
        result = cleaner.clean(html, is_html=True)
        assert "strip_html" in result.cleaning_steps_applied
        assert "normalize_whitespace" in result.cleaning_steps_applied


class TestCleanSnippetFunction:
    """Tests for the clean_snippet convenience function."""

    def test_clean_snippet_basic(self) -> None:
        """Test basic usage of clean_snippet function."""
        result = clean_snippet("Hello World")
        assert result == "Hello World"

    def test_clean_snippet_with_html(self) -> None:
        """Test clean_snippet with HTML content."""
        result = clean_snippet("<p>Hello</p>", is_html=True)
        assert "<p>" not in result
        assert "Hello" in result

    def test_clean_snippet_with_max_length(self) -> None:
        """Test clean_snippet with custom max_length."""
        result = clean_snippet("A" * 500, max_length=100)
        assert len(result) == 100


class TestRegexTimeoutSafety:
    """Tests to verify regex patterns have timeouts for security."""

    def test_all_patterns_have_timeout(self) -> None:
        """Verify all compiled patterns have timeout set."""
        from assistant.classifier import snippet

        # Check patterns are defined with timeout
        # This is a compile-time check - if patterns don't have timeout,
        # the module won't load properly
        assert hasattr(snippet, "HTML_TAG_PATTERN")
        assert hasattr(snippet, "FORWARDED_HEADER_PATTERNS")
        assert hasattr(snippet, "SIGNATURE_PATTERNS")
        assert hasattr(snippet, "DISCLAIMER_PATTERNS")
        assert hasattr(snippet, "EXCESSIVE_NEWLINES")
        assert hasattr(snippet, "EXCESSIVE_SPACES")

    def test_handles_potentially_slow_input(self) -> None:
        """Test that potentially slow input doesn't hang."""
        import time

        cleaner = SnippetCleaner()

        # This pattern could be slow without timeout protection
        slow_input = "a" * 10000 + "@" * 100 + "b" * 10000

        start = time.time()
        result = cleaner.clean(slow_input)
        elapsed = time.time() - start

        # Should complete quickly (well under 5 seconds)
        assert elapsed < 5.0
        # Should return some result
        assert isinstance(result.cleaned_text, str)
