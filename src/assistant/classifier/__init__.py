"""Email classification components.

This package provides email processing and classification functionality:
- Snippet cleaning pipeline for preparing email bodies
- Auto-rules pattern matching for high-confidence routing
- Claude classifier with tool use for AI classification
- Prompt context assembler for building classification prompts
- Bootstrap prompt templates for taxonomy discovery
"""

from assistant.classifier.auto_rules import AutoRuleMatch, AutoRulesEngine
from assistant.classifier.bootstrap_prompts import (
    build_batch_analysis_prompt,
    build_consolidation_prompt,
    format_email_for_batch,
    parse_batch_yaml_response,
    parse_consolidated_yaml_response,
)
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
    # Bootstrap prompts
    "build_batch_analysis_prompt",
    "build_consolidation_prompt",
    "format_email_for_batch",
    "parse_batch_yaml_response",
    "parse_consolidated_yaml_response",
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
