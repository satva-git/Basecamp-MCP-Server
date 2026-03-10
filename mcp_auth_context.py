"""
Request-scoped user ID for multi-user MCP auth.
Set by ASGI middleware from Authorization: Bearer <api_key>; read by tools.
"""

import contextvars

# Context var is inherited by child tasks during the same request.
_mcp_user_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "mcp_user_id", default=None
)


def set_user_id_for_request(user_id: str | None) -> None:
    """Set the current request's user id (from API key resolution)."""
    _mcp_user_id.set(user_id)


def get_user_id_for_request() -> str | None:
    """Get the current request's user id, if set."""
    try:
        return _mcp_user_id.get()
    except LookupError:
        return None

