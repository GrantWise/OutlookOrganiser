"""Web review UI for the Outlook AI Assistant.

Provides a FastAPI-based web interface for:
- Dashboard with system overview
- Review queue for approving/rejecting classification suggestions
- Waiting-for tracker
- Configuration editor
- Activity log
"""

from assistant.web.app import create_app

__all__ = ["create_app"]
