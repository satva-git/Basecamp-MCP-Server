#!/usr/bin/env python3
"""
Run the Basecamp MCP server over SSE (Server-Sent Events) so it can be hosted
and reached via HTTP. Use this when you want to run the server as a long-lived
process (e.g. on a host or in Docker) and connect clients via URL.

Environment:
  MCP_HOST  - Bind address (default: 0.0.0.0)
  MCP_PORT  - Port (default: 8010)

Example:
  python run_mcp_server_sse.py
  MCP_PORT=8010 python run_mcp_server_sse.py
"""

import os
import sys

# Ensure project root is on path
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# Import after path is set so basecamp_fastmcp registers all tools
from basecamp_fastmcp import mcp
import mcp_auth_context
import user_store


def _auth_middleware(app):
    """
    Resolve Authorization: Bearer <api_key> to user_id and set request-scoped context.
    If no header and exactly one user, use that user (single-user fallback).
    Otherwise require valid Bearer token or return 401.
    """

    async def wrapper(scope, receive, send):
        if scope.get("type") != "http":
            await app(scope, receive, send)
            return
        headers = dict(
            (
                k.decode("latin-1").lower(),
                v.decode("latin-1") if isinstance(v, bytes) else v,
            )
            for k, v in scope.get("headers", [])
        )
        auth = headers.get("authorization") or ""
        token = auth.split() if auth else []
        api_key = token[1] if len(token) == 2 and token[0].lower() == "bearer" else None
        user_id = None
        if api_key:
            user_id = user_store.get_user_by_api_key(api_key)
        if user_id is None and user_store.user_count() <= 1:
            user_id = user_store.get_single_user_id()
        require_auth = (
            os.environ.get("MCP_REQUIRE_AUTH", "").strip().lower()
            in ("1", "true", "yes")
        )
        if user_id is None and (require_auth or user_store.user_count() > 1):
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [[b"content-type", b"application/json"]],
                }
            )
            await send(
                {
                    "type": "http.response.body",
                    "body": b'{"error":"Unauthorized","message":"Missing or invalid API key. Use Authorization: Bearer <your_api_key>."}',
                }
            )
            return
        mcp_auth_context.set_user_id_for_request(user_id)
        try:
            await app(scope, receive, send)
        finally:
            mcp_auth_context.set_user_id_for_request(None)

    return wrapper


def _allow_all_hosts(app, port: int):
    """ASGI middleware that normalizes Host header so requests by IP or hostname are accepted."""
    port_s = str(port)

    async def wrapper(scope, receive, send):
        if scope.get("type") == "http" and "headers" in scope:
            headers = list(scope["headers"])
            new_headers = [(k, v) for k, v in headers if k.lower() != b"host"]
            # Normalize Host to localhost so strict host checks pass when accessing via IP
            new_headers.append((b"host", f"localhost:{port_s}".encode()))
            scope = {**scope, "headers": new_headers}
        await app(scope, receive, send)

    return wrapper


def _friendly_messages_fallback(app):
    """
    If someone GETs /messages in a browser (wrong Content-Type), return a short
    explanation instead of 'Invalid Content-Type header'.
    """

    def has_json_content_type(scope):
        for k, v in scope.get("headers", []):
            if k.lower() == b"content-type":
                v = (
                    v.split(b";")[0].strip().lower()
                    if isinstance(v, bytes)
                    else v.split(";")[0].strip().lower()
                )
                return b"application/json" in (v if isinstance(v, bytes) else v.encode())
        return False

    async def wrapper(scope, receive, send):
        if scope.get("type") != "http":
            await app(scope, receive, send)
            return
        path = (scope.get("path") or "").strip("/")
        method = (scope.get("method") or "GET").upper()

        # GET /messages (or /messages/?session_id=...) without JSON content type => friendly response
        if method == "GET" and path.startswith("messages") and not has_json_content_type(
            scope
        ):
            body = (
                "This is an MCP messages endpoint. Do not open it in a browser.\n\n"
                "Connect an MCP client to the SSE URL first (e.g. /sse) to get a session_id, "
                "then send POST requests to /messages with Content-Type: application/json."
            ).encode()
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [[b"content-type", b"text/plain; charset=utf-8"]],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        await app(scope, receive, send)

    return wrapper


def main():
    try:
        import uvicorn
    except ImportError:
        print("uvicorn is required for SSE hosting. Install it with:", file=sys.stderr)
        print("  pip install uvicorn", file=sys.stderr)
        sys.exit(1)

    # One-time migration: legacy oauth_tokens.json -> one user in SQLite
    api_key = user_store.migrate_legacy_tokens_if_needed()
    if api_key:
        print(
            "Migrated to multi-user. Your API key (save it):",
            api_key[:16] + "...",
            file=sys.stderr,
        )

    host = os.environ.get("MCP_HOST", "0.0.0.0")
    port = int(os.environ.get("MCP_PORT", "8010"))

    # sse_app() returns a Starlette ASGI app; MCP SSE is usually at /
    app = mcp.sse_app(mount_path="/")
    # Resolve Bearer API key to user_id and set request-scoped context for tools
    app = _auth_middleware(app)
    # Friendly response for GET /messages in browser (instead of "Invalid Content-Type header")
    app = _friendly_messages_fallback(app)
    # Wrap so requests to http://192.168.x.x:8010/ etc. don't get "invalid host header"
    app = _allow_all_hosts(app, port)
    print(
        f"Starting Basecamp MCP server (SSE) at http://{host}:{port}", file=sys.stderr
    )
    print(
        f"Connect clients to: http://<this-host>:{port}/", file=sys.stderr
    )
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()

