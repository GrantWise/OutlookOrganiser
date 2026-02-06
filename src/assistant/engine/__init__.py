"""Email processing engines.

This package provides the core processing engines:
- Thread context utilities for classification support
- (Future) Bootstrap scanner for initial taxonomy discovery
- (Future) Triage engine for scheduled email classification
- (Future) Dry-run engine for testing classification
"""

from assistant.engine.thread_utils import (
    InheritanceResult,
    SenderHistoryResult,
    ThreadContext,
    ThreadContextManager,
    ThreadMessage,
    calculate_thread_depth,
    extract_domain,
    normalize_subject,
)

__all__ = [
    "InheritanceResult",
    "SenderHistoryResult",
    "ThreadContext",
    "ThreadContextManager",
    "ThreadMessage",
    "calculate_thread_depth",
    "extract_domain",
    "normalize_subject",
]
