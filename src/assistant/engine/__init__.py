"""Email processing engines.

This package provides the core processing engines:
- Thread context utilities for classification support
- Bootstrap scanner for initial taxonomy discovery
- Dry-run engine for testing classification
- (Future) Triage engine for scheduled email classification
"""

from assistant.engine.bootstrap import BootstrapEngine, BootstrapStats
from assistant.engine.dry_run import DryRunEngine, DryRunReport
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
    # Bootstrap
    "BootstrapEngine",
    "BootstrapStats",
    # Dry-run
    "DryRunEngine",
    "DryRunReport",
    # Thread utilities
    "InheritanceResult",
    "SenderHistoryResult",
    "ThreadContext",
    "ThreadContextManager",
    "ThreadMessage",
    "calculate_thread_depth",
    "extract_domain",
    "normalize_subject",
]
