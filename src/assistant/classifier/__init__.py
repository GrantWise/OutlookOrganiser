"""Email classification components.

This package provides email processing and classification functionality:
- Snippet cleaning pipeline for preparing email bodies
- Auto-rules pattern matching for high-confidence routing
- Claude classifier with tool use for AI classification
- Prompt context assembler for building classification prompts
"""

from assistant.classifier.auto_rules import AutoRuleMatch, AutoRulesEngine
from assistant.classifier.claude_classifier import ClassificationResult, EmailClassifier
from assistant.classifier.prompts import (
    CLASSIFY_EMAIL_TOOL,
    ClassificationContext,
    PromptAssembler,
)
from assistant.classifier.snippet import (
    CleaningResult,
    SnippetCleaner,
    clean_snippet,
)

__all__ = [
    # Auto-rules
    "AutoRuleMatch",
    "AutoRulesEngine",
    # Claude classifier
    "ClassificationResult",
    "EmailClassifier",
    # Prompts
    "CLASSIFY_EMAIL_TOOL",
    "ClassificationContext",
    "PromptAssembler",
    # Snippet cleaning
    "CleaningResult",
    "SnippetCleaner",
    "clean_snippet",
]
