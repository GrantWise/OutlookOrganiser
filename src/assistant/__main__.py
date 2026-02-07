"""Entry point for running the assistant as a module.

Usage:
    python -m assistant validate-config
    python -m assistant --help
"""

from dotenv import load_dotenv

load_dotenv()  # Load .env before any other imports that need env vars

from assistant.cli import main  # noqa: E402

if __name__ == "__main__":
    main()
