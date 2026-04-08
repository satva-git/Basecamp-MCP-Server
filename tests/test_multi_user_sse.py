#!/usr/bin/env python3
"""Tests for multi-user token routing, legacy migration, and SSE auth."""

import asyncio
import json
import os
import tempfile
from unittest.mock import patch, MagicMock, AsyncMock

# Ensure project root is on path
import sys
SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)


def test_token_storage_get_token_with_user_id_delegates_to_user_store():
    """When user_id is set, token_storage.get_token(user_id) uses user_store."""
    import token_storage
    import user_store
    with tempfile.TemporaryDirectory() as tmp:
        with patch.object(user_store, "DATA_DIR", tmp), patch.object(user_store, "DB_PATH", os.path.join(tmp, "test.db")):
            user_id, api_key = user_store.create_user(email="test@example.com")
            user_store.store_token(user_id=user_id, access_token="at", refresh_token="rt", account_id="123")
            out = token_storage.get_token(user_id=user_id)
            assert out is not None
            assert out.get("access_token") == "at"
            assert out.get("account_id") == "123"


def test_token_storage_get_token_without_user_id_uses_legacy_file():
    """When user_id is None, token_storage.get_token() reads from legacy file."""
    import token_storage
    with tempfile.TemporaryDirectory() as tmp:
        token_file = os.path.join(tmp, "oauth_tokens.json")
        with patch.object(token_storage, "TOKEN_FILE", token_file):
            with open(token_file, "w") as f:
                json.dump({
                    "basecamp": {
                        "access_token": "legacy_at",
                        "refresh_token": "legacy_rt",
                        "account_id": "999",
                        "expires_at": "2099-01-01T00:00:00",
                        "updated_at": "2020-01-01T00:00:00",
                    }
                }, f)
            out = token_storage.get_token(user_id=None)
            assert out is not None
            assert out.get("access_token") == "legacy_at"


def test_legacy_migration_creates_one_user_and_copies_token():
    """When no users exist but legacy oauth_tokens.json has a token, migration creates one user."""
    import user_store
    import token_storage
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "basecamp_mcp.db")
        token_file = os.path.join(tmp, "oauth_tokens.json")
        os.makedirs(tmp, exist_ok=True)
        with open(token_file, "w") as f:
            json.dump({
                "basecamp": {
                    "access_token": "migrated_at",
                    "refresh_token": "migrated_rt",
                    "account_id": "456",
                    "expires_at": "2099-06-01T12:00:00",
                    "updated_at": "2020-01-01T00:00:00",
                }
            }, f)
        with patch.object(user_store, "DATA_DIR", tmp), patch.object(user_store, "DB_PATH", db_path):
            with patch.object(token_storage, "TOKEN_FILE", token_file):
                # Ensure no users (fresh DB)
                assert user_store.user_count() == 0
                api_key = user_store.migrate_legacy_tokens_if_needed()
                assert api_key is not None
                assert user_store.user_count() == 1
                single_id = user_store.get_single_user_id()
                assert single_id is not None
                token_data = user_store.get_token(single_id)
                assert token_data is not None
                assert token_data.get("access_token") == "migrated_at"
                assert token_data.get("account_id") == "456"


def test_legacy_migration_skipped_when_users_exist():
    """Migration does nothing when at least one user already exists."""
    import user_store
    import token_storage
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "basecamp_mcp.db")
        token_file = os.path.join(tmp, "oauth_tokens.json")
        with open(token_file, "w") as f:
            json.dump({"basecamp": {"access_token": "x", "refresh_token": "y", "account_id": "1"}}, f)
        with patch.object(user_store, "DATA_DIR", tmp), patch.object(user_store, "DB_PATH", db_path):
            user_store.create_user(email="existing@example.com")
            assert user_store.user_count() == 1
            with patch.object(token_storage, "TOKEN_FILE", token_file):
                api_key = user_store.migrate_legacy_tokens_if_needed()
                assert api_key is None
                assert user_store.user_count() == 1


def test_get_basecamp_client_uses_request_user_id_when_set():
    """_get_basecamp_client uses token for request-scoped user_id when mcp_auth_context is set."""
    from basecamp_fastmcp import _get_basecamp_client, _get_request_user_id
    import basecamp_fastmcp
    import user_store
    import token_storage
    with tempfile.TemporaryDirectory() as tmp:
        with patch.object(user_store, "DATA_DIR", tmp), patch.object(user_store, "DB_PATH", os.path.join(tmp, "test.db")):
            user_id, _ = user_store.create_user(email="u@example.com")
            user_store.store_token(user_id=user_id, access_token="at_123", refresh_token="rt_123", account_id="999")
            with patch.object(basecamp_fastmcp, "mcp_auth_context", MagicMock()) as m:
                m.get_user_id_for_request.return_value = user_id
                with patch("basecamp_fastmcp.auth_manager.ensure_authenticated", return_value=True):
                    with patch.dict(os.environ, {"USER_AGENT": "Test"}):
                        client = _get_basecamp_client()
                        assert client is not None
                        assert client.access_token == "at_123"
                        assert client.account_id == "999"


def test_get_basecamp_client_falls_back_to_legacy_when_no_user_id():
    """When mcp_auth_context returns None, _get_basecamp_client uses legacy token_storage."""
    from basecamp_fastmcp import _get_basecamp_client
    import basecamp_fastmcp
    import token_storage
    with tempfile.TemporaryDirectory() as tmp:
        token_file = os.path.join(tmp, "oauth_tokens.json")
        with open(token_file, "w") as f:
            json.dump({
                "basecamp": {
                    "access_token": "legacy_token",
                    "refresh_token": "legacy_refresh",
                    "account_id": "111",
                    "expires_at": "2099-01-01T00:00:00",
                    "updated_at": "2020-01-01T00:00:00",
                }
            }, f)
        with patch.object(token_storage, "TOKEN_FILE", token_file):
            mock_ctx = MagicMock()
            mock_ctx.get_user_id_for_request.return_value = None
            with patch.object(basecamp_fastmcp, "mcp_auth_context", mock_ctx):
                with patch("basecamp_fastmcp.auth_manager.ensure_authenticated", return_value=True):
                    with patch.dict(os.environ, {"USER_AGENT": "Test"}):
                        client = _get_basecamp_client()
                        assert client is not None
                        assert client.access_token == "legacy_token"
                        assert client.account_id == "111"


def test_sse_auth_middleware_returns_401_when_required_and_no_bearer():
    """When MCP_REQUIRE_AUTH=1 and no Authorization header, middleware returns 401."""
    import run_mcp_server_sse

    async def run():
        with patch.dict(os.environ, {"MCP_REQUIRE_AUTH": "1"}):
            with patch.object(run_mcp_server_sse.user_store, "user_count", return_value=0):
                with patch.object(run_mcp_server_sse.user_store, "get_single_user_id", return_value=None):
                    app = AsyncMock()
                    wrapper = run_mcp_server_sse._auth_middleware(app)
                    scope = {
                        "type": "http",
                        "headers": [(b"content-type", b"application/json")],
                    }
                    receive = AsyncMock(return_value=None)
                    send = AsyncMock()
                    await wrapper(scope, receive, send)
                    send.assert_called()
                    calls = [c for c in send.call_args_list if c[0][0].get("type") == "http.response.start"]
                    assert len(calls) >= 1
                    assert calls[0][0][0].get("status") == 401
                    app.assert_not_called()

    asyncio.run(run())


def test_sse_auth_middleware_returns_401_when_multiple_users_and_no_bearer():
    """When multiple users exist and no valid API key, middleware returns 401."""
    import run_mcp_server_sse

    async def run():
        with patch.dict(os.environ, {"MCP_REQUIRE_AUTH": ""}, clear=False):
            with patch.object(run_mcp_server_sse.user_store, "user_count", return_value=2):
                with patch.object(run_mcp_server_sse.user_store, "get_user_by_api_key", return_value=None):
                    with patch.object(run_mcp_server_sse.user_store, "get_single_user_id", return_value=None):
                        app = MagicMock()
                        wrapper = run_mcp_server_sse._auth_middleware(app)
                        scope = {"type": "http", "headers": []}
                        receive = AsyncMock(return_value=None)
                        send = AsyncMock()
                        await wrapper(scope, receive, send)
                        send.assert_called()
                        start_calls = [c for c in send.call_args_list if c[0][0].get("type") == "http.response.start"]
                        assert len(start_calls) >= 1
                        assert start_calls[0][0][0].get("status") == 401

    asyncio.run(run())


def test_sse_auth_middleware_allows_single_user_without_auth():
    """When exactly one user exists and MCP_REQUIRE_AUTH is not set, request proceeds without Bearer."""
    import run_mcp_server_sse

    async def run():
        with patch.dict(os.environ, {"MCP_REQUIRE_AUTH": ""}, clear=False):
            with patch.object(run_mcp_server_sse.user_store, "user_count", return_value=1):
                with patch.object(run_mcp_server_sse.user_store, "get_single_user_id", return_value="user-1"):
                    app = AsyncMock()
                    wrapper = run_mcp_server_sse._auth_middleware(app)
                    scope = {"type": "http", "headers": []}
                    receive = AsyncMock(return_value=None)
                    send = AsyncMock()
                    await wrapper(scope, receive, send)
                    app.assert_called_once()

    asyncio.run(run())


def test_streamable_http_mcp_path_returns_401_without_bearer_when_required():
    """GET /mcp returns 401 when auth is required and no API key (Streamable HTTP + SSE combined app)."""
    import run_mcp_server_sse
    import user_store
    from starlette.testclient import TestClient

    with tempfile.TemporaryDirectory() as tmp:
        with patch.object(user_store, "DATA_DIR", tmp), patch.object(
            user_store, "DB_PATH", os.path.join(tmp, "t.db")
        ):
            with patch.dict(
                os.environ,
                {"MCP_REQUIRE_AUTH": "1", "MCP_ENABLE_STREAMABLE_HTTP": "true"},
                clear=False,
            ):
                with patch.object(run_mcp_server_sse.user_store, "user_count", return_value=0):
                    with patch.object(
                        run_mcp_server_sse.user_store, "get_single_user_id", return_value=None
                    ):
                        app = run_mcp_server_sse.build_mcp_asgi_app(8010)
                        with TestClient(app) as client:
                            r = client.get("/mcp")
                            assert r.status_code == 401
                            body = r.json()
                            assert body.get("error") == "Unauthorized"


def test_streamable_http_disabled_returns_404_for_mcp_path():
    """When MCP_ENABLE_STREAMABLE_HTTP=false, /mcp is not mounted (404)."""
    import run_mcp_server_sse
    import user_store
    from starlette.testclient import TestClient

    with tempfile.TemporaryDirectory() as tmp:
        with patch.object(user_store, "DATA_DIR", tmp), patch.object(
            user_store, "DB_PATH", os.path.join(tmp, "t.db")
        ):
            with patch.dict(os.environ, {"MCP_ENABLE_STREAMABLE_HTTP": "false"}, clear=False):
                app = run_mcp_server_sse.build_mcp_asgi_app(8010)
                with TestClient(app) as client:
                    r = client.get("/mcp")
                    assert r.status_code == 404
