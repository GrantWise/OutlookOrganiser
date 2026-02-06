"""Entry point for running the assistant as a module.

Usage:
    python -m assistant validate-config
    python -m assistant --help
"""

from assistant.cli import main

if __name__ == "__main__":
    main()
