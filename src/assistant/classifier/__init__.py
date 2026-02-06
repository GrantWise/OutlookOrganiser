"""Email classification components.

This package provides email processing and classification functionality:
- Snippet cleaning pipeline for preparing email bodies
- (Future) Auto-rules pattern matching
- (Future) Claude classifier with tool use
"""

from assistant.classifier.snippet import (
    CleaningResult,
    SnippetCleaner,
    clean_snippet,
)

__all__ = [
    "CleaningResult",
    "SnippetCleaner",
    "clean_snippet",
]
