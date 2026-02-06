"""Authentication module for Microsoft Graph API.

Provides MSAL-based OAuth2 device code flow authentication.

Usage:
    from assistant.auth import GraphAuth

    auth = GraphAuth(
        client_id="your-client-id",
        tenant_id="your-tenant-id",
        scopes=["Mail.ReadWrite", "User.Read"],
        token_cache_path="data/token_cache.json",
    )

    token = auth.get_access_token()
"""

from assistant.auth.msal_auth import GraphAuth

__all__ = ["GraphAuth"]
