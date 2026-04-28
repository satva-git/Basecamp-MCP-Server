#!/usr/bin/env python3
"""
FastMCP server for Basecamp integration.

This server implements the MCP (Model Context Protocol) using the official
Anthropic FastMCP framework, replacing the custom JSON-RPC implementation.
"""

import logging
import os
import sys
from typing import Any, Dict, List, Optional
import anyio
import httpx
from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_headers
from fastmcp.exceptions import ToolError

# Import existing business logic
from basecamp_client import BasecampClient
from search_utils import BasecampSearch
from dotenv import load_dotenv

# Determine project root (directory containing this script)
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
DOTENV_PATH = os.path.join(PROJECT_ROOT, '.env')
load_dotenv(DOTENV_PATH)

# Set up logging to file AND stderr (following MCP best practices)
LOG_FILE_PATH = os.path.join(PROJECT_ROOT, 'basecamp_fastmcp.log')
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE_PATH),
        logging.StreamHandler(sys.stderr)  # Critical: log to stderr, not stdout
    ]
)
logger = logging.getLogger('basecamp_fastmcp')

# Initialize FastMCP server
mcp = FastMCP(
    "basecamp",
    instructions="""This MCP server connects to Basecamp 3 and exposes 82 tools for managing projects, tasks, documents, and team communication.

## Authentication
OAuth tokens are managed automatically. If you receive an authentication error, the user must visit http://localhost:8000 to re-authenticate via OAuth.

## Key Tool Categories

### Projects
- get_projects — list all accessible projects (paginated)
- get_project — get a single project with dock links to all its tools

### Todos & Task Management
- get_todolists, get_todolist, create_todolist, update_todolist, trash_todolist
- get_todos, get_todo, create_todo, update_todo, delete_todo
- complete_todo, uncomplete_todo, archive_todo, reposition_todo
- get_todolist_groups, create_todolist_group, reposition_todolist_group

### Documents & File Organization (Docs & Files)
Basecamp organizes documents in vaults (folders). Navigate the hierarchy first:
1. Call get_project → find the "vault" dock entry to get the root vault_id
2. Call get_vaults to list subfolders, or get_vault for details
3. Call get_documents to list documents in a vault
- get_vaults — list subfolders inside a vault
- get_vault — get vault details (document/upload/subfolder counts)
- create_vault — create a new subfolder inside a vault
- update_vault — rename a vault
- get_documents, get_document, create_document, update_document, trash_document

### Uploads
- get_uploads, get_upload

### Card Tables (Kanban Boards)
- get_card_tables, get_card_table, get_columns, get_column, create_column, update_column
- get_cards, get_card, create_card, update_card, move_card, complete_card, uncomplete_card
- get_card_steps, create_card_step, get_card_step, update_card_step, complete_card_step, uncomplete_card_step, delete_card_step

### Messages & Communication
- get_message_board, get_messages, get_message, get_message_categories, create_message
- get_campfire_lines (Campfire chat)
- get_comments, create_comment

### Inbox (Email Forwards)
- get_inbox, get_forwards, get_forward, get_inbox_replies, get_inbox_reply, trash_forward

### People
- get_people, get_project_people, search_people

### Search
- search_basecamp — search within a project
- global_search — search across all projects

### Other
- get_daily_check_ins, get_question_answers, get_events, get_uploads, create_attachment
- get_webhooks, create_webhook, delete_webhook

## Attaching files & images (READ THIS BEFORE ATTACHING ANYTHING)
Basecamp uses a strict TWO-STEP flow. You CANNOT pass a file path, URL, or base64 blob
directly to create_todo / create_message / etc. — those tools only accept an
`attachable_sgid` (an opaque ID returned by an upload tool).

Step 1 — Upload the file to get an `attachable_sgid`:
- `create_attachment_from_url(url, name=None, content_type=None)` — when you have a public/HTTP URL.
- `create_attachment(file_content_b64, name, content_type)` — when you have raw bytes (base64-encoded).
Both return `result.attachment.attachable_sgid`.

Step 2 — Pass the sgid into the `attachable_sgids=[...]` parameter of one of these creation/update tools:
- `create_todo`, `update_todo`
- `create_message`
- `create_comment`
- `create_card`
- `create_document`, `update_document`

The sgid is rendered as a `<bc-attachment sgid="...">` tag inline in the recording's HTML body.
Skipping Step 2 leaves the file uploaded but invisible on any recording. Items in
`attachable_sgids` may be plain sgid strings or `{"sgid": "...", "caption": "..."}` dicts.

## Attaching files & images (READ THIS BEFORE ATTACHING ANYTHING)
Basecamp uses a strict TWO-STEP flow. You CANNOT pass a file path, URL, or base64 blob
directly to create_todo / create_message / etc. — those tools only accept an
`attachable_sgid` (an opaque ID returned by an upload tool).

Step 1 — Upload the file to get an `attachable_sgid`:
- `create_attachment_from_url(url, name=None, content_type=None)` — when you have a public/HTTP URL.
- `create_attachment(file_content_b64, name, content_type)` — when you have raw bytes (base64-encoded).
Both return `result.attachment.attachable_sgid`.

Step 2 — Pass the sgid into the `attachable_sgids=[...]` parameter of one of these creation/update tools:
- `create_todo`, `update_todo`
- `create_message`
- `create_comment`
- `create_card`
- `create_document`, `update_document`

The sgid renders as a `<bc-attachment sgid="...">` tag inline in the recording's HTML body.
Skipping Step 2 leaves the file uploaded but invisible on any recording. Items in
`attachable_sgids` may be plain sgid strings or `{"sgid": "...", "caption": "..."}` dicts.

## Important Behaviors
- **Safe deletions**: All delete/trash operations archive items instead of permanently deleting them. Archived items remain recoverable via the Basecamp web UI.
- **Pagination**: List endpoints return paginated results; the server fetches all pages automatically.
- **IDs**: All resource IDs are numeric strings. Get them from list/get calls — never guess or hardcode IDs.
- **HTML content**: Document and message content fields accept HTML markup.
- **Workflow tip**: For documents, always call get_project first to find the root vault_id, then navigate with get_vaults before creating or listing documents.
"""
)

# Auth: per-request access token forwarded by mcp-oauth-proxy.
# The proxy validates the user's session (against 37signals via OAuth 2.0)
# and forwards the upstream Basecamp access token in the
# `x-forwarded-access-token` header for each MCP tool call.
def _get_access_token() -> Optional[str]:
    """Return the per-request Basecamp access token from the proxy header."""
    try:
        headers = get_http_headers()
    except Exception:
        return None
    return headers.get("x-forwarded-access-token")


def _account_id_from_token(access_token: str) -> Optional[str]:
    """Resolve Basecamp account_id from /authorization.json.

    Falls back to BASECAMP_ACCOUNT_ID env var if the call fails or the
    user's identity has no Basecamp 3 accounts. Cached on the BasecampClient
    side; this helper is only called when constructing a fresh client.
    """
    env_account = os.getenv('BASECAMP_ACCOUNT_ID')
    try:
        resp = httpx.get(
            "https://launchpad.37signals.com/authorization.json",
            headers={
                "Authorization": f"Bearer {access_token}",
                "User-Agent": os.getenv("USER_AGENT", "Basecamp MCP Server (ops@satvasolutions.com)"),
            },
            timeout=10.0,
        )
        if resp.status_code == 200:
            data = resp.json()
            for acct in data.get("accounts", []):
                if acct.get("product") == "bc3":
                    return str(acct.get("id"))
    except Exception as e:
        logger.warning("Failed to resolve account_id from /authorization.json: %s", e)
    return env_account


def _get_basecamp_client() -> Optional[BasecampClient]:
    """Build a Basecamp client from the per-request forwarded access token."""
    access_token = _get_access_token()
    if not access_token:
        logger.error("No x-forwarded-access-token header on request")
        return None
    account_id = _account_id_from_token(access_token)
    if not account_id:
        logger.error("Could not resolve Basecamp account_id (env BASECAMP_ACCOUNT_ID unset and /authorization.json had no bc3 accounts)")
        return None
    user_agent = os.getenv('USER_AGENT') or "Basecamp MCP Server (ops@satvasolutions.com)"
    return BasecampClient(
        access_token=access_token,
        account_id=account_id,
        user_agent=user_agent,
        auth_mode='oauth',
    )


def _get_auth_error_response() -> Dict[str, Any]:
    """Return consistent auth error response."""
    if not _get_access_token():
        return {
            "error": "Authentication required",
            "message": "No Basecamp access token forwarded by the OAuth proxy. The user needs to complete the OAuth flow against this MCP server.",
        }
    return {
        "error": "Basecamp account resolution failed",
        "message": "An access token was forwarded but no Basecamp 3 account is associated with it. Verify BASECAMP_ACCOUNT_ID or that the user has access to the expected Basecamp account.",
    }

async def _run_sync(func, *args, **kwargs):
    """Wrapper to run synchronous functions in thread pool."""
    return await anyio.to_thread.run_sync(func, *args, **kwargs)


def _attachments_to_html(attachable_sgids: Optional[List[Any]]) -> str:
    """Convert a list of attachable_sgids into Basecamp <bc-attachment> tags.

    Each item may be either a plain string sgid, or a dict
    {"sgid": "...", "caption": "..."}. Returns the concatenated HTML
    fragment, or empty string if the list is empty/None.

    Basecamp 3 renders <bc-attachment sgid="..."> inline in HTML bodies for
    todos, messages, comments, documents, and cards. Without this tag the
    uploaded file exists in the account but is not visible on the recording.
    """
    if not attachable_sgids:
        return ""
    parts: List[str] = []
    for item in attachable_sgids:
        if isinstance(item, dict):
            sgid = item.get("sgid")
            caption = item.get("caption")
        else:
            sgid = item
            caption = None
        if not sgid:
            continue
        if caption:
            safe_caption = str(caption).replace('"', '&quot;')
            parts.append(f'<bc-attachment sgid="{sgid}" caption="{safe_caption}"></bc-attachment>')
        else:
            parts.append(f'<bc-attachment sgid="{sgid}"></bc-attachment>')
    return "".join(parts)


def _merge_description_with_attachments(description: Optional[str], attachable_sgids: Optional[List[Any]]) -> Optional[str]:
    """Append attachment HTML to a description, returning the merged HTML.

    Returns None only if BOTH description and attachable_sgids are empty.
    """
    attachments_html = _attachments_to_html(attachable_sgids)
    if not attachments_html:
        return description
    if description:
        return description + attachments_html
    return attachments_html


# Core MCP Tools - Starting with essential ones from original server

@mcp.tool()
async def get_projects() -> Dict[str, Any]:
    """Get all Basecamp projects."""
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()
    
    try:
        projects = await _run_sync(client.get_projects)
        return {
            "status": "success",
            "projects": projects,
            "count": len(projects)
        }
    except Exception as e:
        logger.error(f"Error getting projects: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }

@mcp.tool()
async def get_project(project_id: str) -> Dict[str, Any]:
    """Get details for a specific project.
    
    Args:
        project_id: The project ID
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()
    
    try:
        project = await _run_sync(client.get_project, project_id)
        return {
            "status": "success",
            "project": project
        }
    except Exception as e:
        logger.error(f"Error getting project {project_id}: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }

@mcp.tool()
async def search_basecamp(query: str, project_id: Optional[str] = None) -> Dict[str, Any]:
    """Search across Basecamp projects, todos, and messages.
    
    Args:
        query: Search query
        project_id: Optional project ID to limit search scope
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()
    
    try:
        search = BasecampSearch(client=client)
        results = {}

        if project_id:
            # Search within specific project
            results["todolists"] = await _run_sync(search.search_todolists, query, project_id)
            results["todos"] = await _run_sync(search.search_todos, query, project_id)
        else:
            # Search across all projects
            results["projects"] = await _run_sync(search.search_projects, query)
            results["todos"] = await _run_sync(search.search_todos, query)
            results["messages"] = await _run_sync(search.search_messages, query)

        return {
            "status": "success",
            "query": query,
            "results": results
        }
    except Exception as e:
        logger.error(f"Error searching Basecamp: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }

@mcp.tool()
async def get_todolists(project_id: str) -> Dict[str, Any]:
    """Get todo lists for a project.
    
    Args:
        project_id: The project ID
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()
    
    try:
        todolists = await _run_sync(client.get_todolists, project_id)
        return {
            "status": "success",
            "todolists": todolists,
            "count": len(todolists)
        }
    except Exception as e:
        logger.error(f"Error getting todolists: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }

@mcp.tool()
async def get_todos(project_id: str, todolist_id: str) -> Dict[str, Any]:
    """Get todos from a todo list.
    
    Args:
        project_id: Project ID
        todolist_id: The todo list ID
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()
    
    try:
        todos = await _run_sync(client.get_todos, project_id, todolist_id)
        return {
            "status": "success",
            "todos": todos,
            "count": len(todos)
        }
    except Exception as e:
        logger.error(f"Error getting todos: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }

@mcp.tool()
async def get_todo(project_id: str, todo_id: str) -> Dict[str, Any]:
    """Get a single todo item by its ID.

    Args:
        project_id: Project ID
        todo_id: The todo ID
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()

    try:
        todo = await _run_sync(client.get_todo, project_id, todo_id)
        return {
            "status": "success",
            "todo": todo
        }
    except Exception as e:
        logger.error(f"Error getting todo {todo_id}: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }

@mcp.tool()
async def create_todo(project_id: str, todolist_id: str, content: str,
                     description: Optional[str] = None,
                     assignee_ids: Optional[List[str]] = None,
                     completion_subscriber_ids: Optional[List[str]] = None,
                     notify: bool = False,
                     due_on: Optional[str] = None,
                     starts_on: Optional[str] = None,
                     attachable_sgids: Optional[List[Any]] = None) -> Dict[str, Any]:
    """Create a new to-do item in a to-do list.

    IMPORTANT: This creates a to-do (task) inside a to-do list, NOT a message.
    Use create_message() to post announcements/discussions on the Message Board.
    Use create_comment() to add a comment/reply on an existing to-do or message.

    To attach images/files to the to-do, follow this two-step flow:
      1. Call create_attachment(file_content_b64, name, content_type) for each
         file. Capture the returned attachable_sgid from the response.
      2. Pass those sgids here as attachable_sgids. They will be embedded into
         the description as <bc-attachment> tags so Basecamp renders them inline.

    Args:
        project_id: Project ID
        todolist_id: The to-do list ID (use get_todolists to find available lists)
        content: The to-do item's text/title (required)
        description: HTML description with additional details for the to-do
        assignee_ids: List of person IDs to assign (use get_people or search_people to find IDs)
        completion_subscriber_ids: List of person IDs to notify on completion
        notify: Whether to notify assignees
        due_on: Due date in YYYY-MM-DD format
        starts_on: Start date in YYYY-MM-DD format
        attachable_sgids: List of attachable_sgid strings from create_attachment calls,
            or list of {"sgid": "...", "caption": "..."} dicts. These are appended
            to the description as <bc-attachment> tags so the files render inline
            on the to-do.
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()

    description = _merge_description_with_attachments(description, attachable_sgids)

    try:
        # Use lambda to properly handle keyword arguments
        todo = await _run_sync(
            lambda: client.create_todo(
                project_id, todolist_id, content,
                description=description,
                assignee_ids=assignee_ids,
                completion_subscriber_ids=completion_subscriber_ids,
                notify=notify,
                due_on=due_on,
                starts_on=starts_on
            )
        )
        return {
            "status": "success",
            "todo": todo,
            "message": f"Todo '{content}' created successfully"
        }
    except Exception as e:
        logger.error(f"Error creating todo: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }

@mcp.tool()
async def update_todo(project_id: str, todo_id: str,
                     content: Optional[str] = None,
                     description: Optional[str] = None,
                     assignee_ids: Optional[List[str]] = None,
                     completion_subscriber_ids: Optional[List[str]] = None,
                     notify: Optional[bool] = None,
                     due_on: Optional[str] = None,
                     starts_on: Optional[str] = None,
                     attachable_sgids: Optional[List[Any]] = None) -> Dict[str, Any]:
    """Update an existing todo item.

    To add file/image attachments, follow the standard two-step flow:
      1. Call create_attachment_from_url(url) (for a URL) or
         create_attachment(file_content_b64, name, content_type) (for raw bytes)
         and capture the returned attachable_sgid.
      2. Pass those sgids here as attachable_sgids — they are appended to the
         description as <bc-attachment> tags so Basecamp renders them inline.

    NOTE: Basecamp's update endpoint REPLACES the description, so include the
    existing description text along with any new attachments to preserve content.

    Args:
        project_id: Project ID
        todo_id: The todo ID
        content: The todo item's text
        description: HTML description of the todo
        assignee_ids: List of person IDs to assign
        completion_subscriber_ids: List of person IDs to notify on completion
        due_on: Due date in YYYY-MM-DD format
        starts_on: Start date in YYYY-MM-DD format
        attachable_sgids: List of attachable_sgid strings from create_attachment /
            create_attachment_from_url, or {"sgid","caption"} dicts. Appended to
            description as <bc-attachment> tags.
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()
    
    try:
        # Guard against no-op updates
        if all(v is None for v in [content, description, assignee_ids,
                                   completion_subscriber_ids, notify,
                                   due_on, starts_on, attachable_sgids]):
            return {
                "error": "Invalid input",
                "message": "At least one field to update must be provided"
            }
        description = _merge_description_with_attachments(description, attachable_sgids)
        # Use lambda to properly handle keyword arguments
        todo = await _run_sync(
            lambda: client.update_todo(
                project_id, todo_id,
                content=content,
                description=description,
                assignee_ids=assignee_ids,
                completion_subscriber_ids=completion_subscriber_ids,
                notify=notify,
                due_on=due_on,
                starts_on=starts_on
            )
        )
        return {
            "status": "success",
            "todo": todo,
            "message": "Todo updated successfully"
        }
    except Exception as e:
        logger.error(f"Error updating todo: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }

@mcp.tool()
async def delete_todo(project_id: str, todo_id: str) -> Dict[str, Any]:
    """Archive a todo item (safe deletion — archives instead of permanently deleting).

    The todo will be hidden from the active list but remains accessible
    via the Basecamp web UI and can be unarchived at any time.

    Args:
        project_id: Project ID
        todo_id: The todo ID
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()

    try:
        await _run_sync(client.delete_todo, project_id, todo_id)
        return {
            "status": "success",
            "message": "Todo archived (safe deletion)"
        }
    except Exception as e:
        logger.error(f"Error archiving todo: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }

@mcp.tool()
async def complete_todo(project_id: str, todo_id: str) -> Dict[str, Any]:
    """Mark a todo item as complete.
    
    Args:
        project_id: Project ID
        todo_id: The todo ID
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()
    
    try:
        completion = await _run_sync(client.complete_todo, project_id, todo_id)
        return {
            "status": "success",
            "completion": completion,
            "message": "Todo marked as complete"
        }
    except Exception as e:
        logger.error(f"Error completing todo: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }

@mcp.tool()
async def uncomplete_todo(project_id: str, todo_id: str) -> Dict[str, Any]:
    """Mark a todo item as incomplete.
    
    Args:
        project_id: Project ID
        todo_id: The todo ID
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()
    
    try:
        await _run_sync(client.uncomplete_todo, project_id, todo_id)
        return {
            "status": "success",
            "message": "Todo marked as incomplete"
        }
    except Exception as e:
        logger.error(f"Error uncompleting todo: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }

@mcp.tool()
async def archive_todo(project_id: str, todo_id: str) -> Dict[str, Any]:
    """Archive a todo item (this is the safe way to remove a todo).

    Archived todos are hidden from the active list but remain accessible
    via the Basecamp web UI and can be unarchived at any time.

    Args:
        project_id: Project ID
        todo_id: The todo ID
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()

    try:
        await _run_sync(client.archive_todo, project_id, todo_id)
        return {"status": "success", "message": f"Todo {todo_id} archived"}
    except Exception as e:
        logger.error(f"Error archiving todo {todo_id}: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {"error": "OAuth token expired", "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."}
        return {"error": "Execution error", "message": str(e)}


@mcp.tool()
async def reposition_todo(
    project_id: str,
    todo_id: str,
    position: int,
    parent_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Reposition a todo within its list, or move it to another list or group.

    Args:
        project_id: The project ID
        todo_id: The todo ID
        position: New 1-based position within the target list
        parent_id: ID of the target todolist or group to move the todo into.
                   Omit to keep the todo in its current list and only change position.
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()

    if position < 1:
        return {"error": "Invalid input", "message": "position must be >= 1"}

    try:
        await _run_sync(
            lambda: client.reposition_todo(project_id, todo_id, position, parent_id)
        )
        return {"status": "success", "message": f"Todo {todo_id} moved to position {position}"}
    except Exception as e:
        logger.error(f"Error repositioning todo {todo_id}: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {"error": "OAuth token expired", "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."}
        return {"error": "Execution error", "message": str(e)}


@mcp.tool()
async def global_search(query: str) -> Dict[str, Any]:
    """Search projects, todos and campfire messages across all projects.
    
    Args:
        query: Search query
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()
    
    try:
        search = BasecampSearch(client=client)
        results = await _run_sync(search.global_search, query)
        return {
            "status": "success",
            "query": query,
            "results": results
        }
    except Exception as e:
        logger.error(f"Error in global search: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }

@mcp.tool()
async def get_comments(recording_id: str, project_id: str, page: int = 1) -> Dict[str, Any]:
    """Get comments/replies on a Basecamp to-do, message, document, or other recording.

    Args:
        recording_id: The ID of the to-do, message, document, or other item to get comments for
        project_id: The project ID (also called bucket ID)
        page: Page number for pagination (default: 1). Basecamp uses geared pagination:
              page 1 has 15 results, page 2 has 30, page 3 has 50, page 4+ has 100.
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()

    try:
        result = await _run_sync(client.get_comments, project_id, recording_id, page)
        return {
            "status": "success",
            "comments": result["comments"],
            "count": len(result["comments"]),
            "page": page,
            "total_count": result["total_count"],
            "next_page": result["next_page"]
        }
    except Exception as e:
        logger.error(f"Error getting comments: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }

@mcp.tool()
async def create_comment(recording_id: str, project_id: str, content: str,
                         attachable_sgids: Optional[List[Any]] = None) -> Dict[str, Any]:
    """Create a comment on any Basecamp recording — including to-dos, messages, documents, and other items.

    Use this tool to add a comment/reply to an existing to-do item, message thread, document, or any
    other Basecamp recording. In Basecamp, every commentable item has a unique recording ID.

    Common use cases:
    - Comment on a to-do: pass the to-do's ID as recording_id
    - Reply to a message thread: pass the message's ID as recording_id
    - Comment on a document: pass the document's ID as recording_id

    To include image/file attachments, upload each via create_attachment() first
    to get an attachable_sgid, then pass them via attachable_sgids. They will be
    embedded as <bc-attachment> tags appended to the comment content.

    Args:
        recording_id: The ID of the to-do, message, document, or other Basecamp item to comment on
        project_id: The project ID (also called bucket ID) that contains the item
        content: The comment content in HTML format (e.g. '<p>Looks good!</p>')
        attachable_sgids: Optional list of attachable_sgid strings (or {"sgid","caption"} dicts)
            from create_attachment. Each renders as a <bc-attachment> tag inline.
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()

    content = _merge_description_with_attachments(content, attachable_sgids) or content

    try:
        comment = await _run_sync(client.create_comment, recording_id, project_id, content)
        return {
            "status": "success",
            "comment": comment,
            "message": "Comment created successfully"
        }
    except Exception as e:
        logger.error(f"Error creating comment: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again.",
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }

@mcp.tool()
async def attach_url(project_id: str, recording_id: str, url: str, link_title: str, note: Optional[str] = None) -> Dict[str, Any]:
    """Attach a URL (e.g. a Google Docs link) to any Basecamp item as a clickable comment.

    Posts a comment on the target recording containing a formatted HTML hyperlink.
    Viewers who have access to the linked resource (Google Docs, Figma, etc.) can
    open it directly; those without access can request it through the source system.

    Use this instead of downloading and re-uploading external files — just share
    the URL and let permissions be managed at the source.

    Args:
        project_id: The project ID
        recording_id: ID of the document, todo, message, or other item to attach the link to
        url: The URL to link to (e.g. a Google Docs URL)
        link_title: Display text for the clickable link
        note: Optional extra context to include above the link (e.g. "Latest design spec")
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()

    try:
        parts = []
        if note:
            parts.append(f"<p>{note}</p>")
        parts.append(f'<p><a href="{url}">{link_title}</a></p>')
        html_content = "\n".join(parts)

        comment = await _run_sync(client.create_comment, recording_id, project_id, html_content)
        return {
            "status": "success",
            "comment": comment,
            "message": f"Link attached: {link_title}"
        }
    except Exception as e:
        logger.error(f"Error attaching URL: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }

@mcp.tool()
async def get_campfire_lines(project_id: str, campfire_id: str) -> Dict[str, Any]:
    """Get recent messages from a Basecamp campfire (chat room).
    
    Args:
        project_id: The project ID
        campfire_id: The campfire/chat room ID
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()
    
    try:
        lines = await _run_sync(client.get_campfire_lines, project_id, campfire_id)
        return {
            "status": "success",
            "campfire_lines": lines,
            "count": len(lines)
        }
    except Exception as e:
        logger.error(f"Error getting campfire lines: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }

@mcp.tool()
async def get_message_board(project_id: str) -> Dict[str, Any]:
    """Get the message board for a project.

    Args:
        project_id: The project ID
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()

    try:
        message_board = await _run_sync(client.get_message_board, project_id)
        return {
            "status": "success",
            "message_board": message_board
        }
    except Exception as e:
        logger.error(f"Error getting message board: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }

@mcp.tool()
async def get_messages(project_id: str, message_board_id: Optional[str] = None) -> Dict[str, Any]:
    """Get all messages from a project's message board.

    Args:
        project_id: The project ID
        message_board_id: Optional message board ID. If not provided, will be auto-discovered from the project.
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()

    try:
        messages = await _run_sync(client.get_messages, project_id, message_board_id)
        return {
            "status": "success",
            "messages": messages,
            "count": len(messages)
        }
    except Exception as e:
        logger.error(f"Error getting messages: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }

@mcp.tool()
async def get_message(project_id: str, message_id: str) -> Dict[str, Any]:
    """Get a specific message by ID.

    Args:
        project_id: The project ID
        message_id: The message ID
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()

    try:
        message = await _run_sync(client.get_message, project_id, message_id)
        return {
            "status": "success",
            "message": message
        }
    except Exception as e:
        logger.error(f"Error getting message: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }


@mcp.tool()
async def get_message_categories(project_id: str) -> Dict[str, Any]:
    """Get message categories (types) for a project.

    Args:
        project_id: The project ID
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()

    try:
        categories = await _run_sync(client.get_message_categories, project_id)
        return {
            "status": "success",
            "categories": categories,
            "count": len(categories)
        }
    except Exception as e:
        logger.error(f"Error getting message categories: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }


@mcp.tool()
async def create_message(project_id: str, subject: str, content: str,
                         message_board_id: Optional[str] = None,
                         category_id: Optional[str] = None,
                         attachable_sgids: Optional[List[Any]] = None) -> Dict[str, Any]:
    """Create a new message (announcement/discussion thread) on a project's Message Board in Basecamp.

    IMPORTANT: This creates a Message Board post (like an announcement or discussion thread), NOT a to-do item.
    Use create_todo() to create to-do items. Use create_comment() to reply to existing messages or to-dos.

    Messages in Basecamp are discussion threads posted to the Message Board. They have a subject line
    and body content, similar to an email or forum post. They appear under the "Message Board" section
    of a Basecamp project.

    To include image/file attachments, upload each via create_attachment() first to get an
    attachable_sgid, then pass them via attachable_sgids. They will be embedded as
    <bc-attachment> tags appended to the message content.

    Args:
        project_id: The project ID
        subject: Message title/subject line (required — this is the thread title)
        content: Message body in HTML format (e.g. '<p>Here is the update...</p>')
        message_board_id: Optional message board ID. If not provided, will be auto-discovered from the project.
        category_id: Optional message type/category ID (use get_message_categories to find available types)
        attachable_sgids: Optional list of attachable_sgid strings (or {"sgid","caption"} dicts)
            from create_attachment. Each renders as a <bc-attachment> tag inline.
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()

    content = _merge_description_with_attachments(content, attachable_sgids) or content

    try:
        message = await _run_sync(
            lambda: client.create_message(
                project_id, subject, content,
                message_board_id=message_board_id,
                category_id=category_id
            )
        )
        return {
            "status": "success",
            "message": message,
            "result": f"Message '{subject}' created successfully"
        }
    except Exception as e:
        logger.error(f"Error creating message: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }


# Inbox Tools (Email Forwards)
@mcp.tool()
async def get_inbox(project_id: str) -> Dict[str, Any]:
    """Get the inbox for a project (for email forwards).

    Args:
        project_id: The project ID
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()

    try:
        inbox = await _run_sync(client.get_inbox, project_id)
        return {
            "status": "success",
            "inbox": inbox
        }
    except Exception as e:
        logger.error(f"Error getting inbox: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }


@mcp.tool()
async def get_forwards(project_id: str, inbox_id: Optional[str] = None) -> Dict[str, Any]:
    """Get all forwarded emails from a project's inbox.

    Args:
        project_id: The project ID
        inbox_id: Optional inbox ID. If not provided, will be auto-discovered from the project.
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()

    try:
        forwards = await _run_sync(client.get_forwards, project_id, inbox_id)
        return {
            "status": "success",
            "forwards": forwards,
            "count": len(forwards)
        }
    except Exception as e:
        logger.error(f"Error getting forwards: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }


@mcp.tool()
async def get_forward(project_id: str, forward_id: str) -> Dict[str, Any]:
    """Get a specific forwarded email by ID.

    Args:
        project_id: The project ID
        forward_id: The forward ID
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()

    try:
        forward = await _run_sync(client.get_forward, project_id, forward_id)
        return {
            "status": "success",
            "forward": forward
        }
    except Exception as e:
        logger.error(f"Error getting forward: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }


@mcp.tool()
async def get_inbox_replies(project_id: str, forward_id: str) -> Dict[str, Any]:
    """Get all replies to a forwarded email.

    Args:
        project_id: The project ID
        forward_id: The forward ID
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()

    try:
        replies = await _run_sync(client.get_inbox_replies, project_id, forward_id)
        return {
            "status": "success",
            "replies": replies,
            "count": len(replies)
        }
    except Exception as e:
        logger.error(f"Error getting inbox replies: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }


@mcp.tool()
async def get_inbox_reply(project_id: str, forward_id: str, reply_id: str) -> Dict[str, Any]:
    """Get a specific reply to a forwarded email.

    Args:
        project_id: The project ID
        forward_id: The forward ID
        reply_id: The reply ID
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()

    try:
        reply = await _run_sync(client.get_inbox_reply, project_id, forward_id, reply_id)
        return {
            "status": "success",
            "reply": reply
        }
    except Exception as e:
        logger.error(f"Error getting inbox reply: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }


@mcp.tool()
async def trash_forward(project_id: str, forward_id: str) -> Dict[str, Any]:
    """Archive a forwarded email (safe deletion — archives instead of permanently deleting).

    The forward will be hidden but remains accessible via the Basecamp web UI.

    Args:
        project_id: The project ID
        forward_id: The forward ID
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()

    try:
        await _run_sync(client.trash_forward, project_id, forward_id)
        return {
            "status": "success",
            "message": "Forward archived (safe deletion)"
        }
    except Exception as e:
        logger.error(f"Error archiving forward: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }


@mcp.tool()
async def get_card_tables(project_id: str) -> Dict[str, Any]:
    """Get all card tables for a project.
    
    Args:
        project_id: The project ID
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()
    
    try:
        card_tables = await _run_sync(client.get_card_tables, project_id)
        return {
            "status": "success",
            "card_tables": card_tables,
            "count": len(card_tables)
        }
    except Exception as e:
        logger.error(f"Error getting card tables: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }

@mcp.tool()
async def get_card_table(project_id: str) -> Dict[str, Any]:
    """Get the card table details for a project.
    
    Args:
        project_id: The project ID
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()
    
    try:
        card_table = await _run_sync(client.get_card_table, project_id)
        card_table_details = await _run_sync(client.get_card_table_details, project_id, card_table['id'])
        return {
            "status": "success",
            "card_table": card_table_details
        }
    except Exception as e:
        logger.error(f"Error getting card table: {e}")
        error_msg = str(e)
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "status": "error",
            "message": f"Error getting card table: {error_msg}",
            "debug": error_msg
        }

@mcp.tool()
async def get_columns(project_id: str, card_table_id: str) -> Dict[str, Any]:
    """Get all columns in a card table.
    
    Args:
        project_id: The project ID
        card_table_id: The card table ID
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()
    
    try:
        columns = await _run_sync(client.get_columns, project_id, card_table_id)
        return {
            "status": "success",
            "columns": columns,
            "count": len(columns)
        }
    except Exception as e:
        logger.error(f"Error getting columns: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }

@mcp.tool()
async def get_cards(project_id: str, column_id: str) -> Dict[str, Any]:
    """Get all cards in a column.
    
    Args:
        project_id: The project ID
        column_id: The column ID
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()
    
    try:
        cards = await _run_sync(client.get_cards, project_id, column_id)
        return {
            "status": "success",
            "cards": cards,
            "count": len(cards)
        }
    except Exception as e:
        logger.error(f"Error getting cards: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }

@mcp.tool()
async def create_card(project_id: str, column_id: str, title: str, content: Optional[str] = None, due_on: Optional[str] = None, notify: bool = False,
                     attachable_sgids: Optional[List[Any]] = None) -> Dict[str, Any]:
    """Create a new card in a column.

    To include image/file attachments, follow the two-step flow:
      1. Upload via create_attachment_from_url(url) or create_attachment(file_content_b64, name, content_type)
         and capture result.attachment.attachable_sgid.
      2. Pass those sgids here as attachable_sgids. They are appended to the
         content as <bc-attachment> tags so Basecamp renders them inline on the card.

    Args:
        project_id: The project ID
        column_id: The column ID
        title: The card title
        content: Optional card content/description
        due_on: Optional due date (ISO 8601 format)
        notify: Whether to notify assignees (default: false)
        attachable_sgids: Optional list of attachable_sgid strings (or {"sgid","caption"} dicts)
            from create_attachment. Each renders as a <bc-attachment> tag inline.
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()

    content = _merge_description_with_attachments(content, attachable_sgids)

    try:
        card = await _run_sync(client.create_card, project_id, column_id, title, content, due_on, notify)
        return {
            "status": "success",
            "card": card,
            "message": f"Card '{title}' created successfully"
        }
    except Exception as e:
        logger.error(f"Error creating card: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }

@mcp.tool()
async def get_column(project_id: str, column_id: str) -> Dict[str, Any]:
    """Get details for a specific column.
    
    Args:
        project_id: The project ID
        column_id: The column ID
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()
    
    try:
        column = await _run_sync(client.get_column, project_id, column_id)
        return {
            "status": "success",
            "column": column
        }
    except Exception as e:
        logger.error(f"Error getting column: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }

@mcp.tool()
async def create_column(project_id: str, card_table_id: str, title: str) -> Dict[str, Any]:
    """Create a new column in a card table.
    
    Args:
        project_id: The project ID
        card_table_id: The card table ID
        title: The column title
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()
    
    try:
        column = await _run_sync(client.create_column, project_id, card_table_id, title)
        return {
            "status": "success",
            "column": column,
            "message": f"Column '{title}' created successfully"
        }
    except Exception as e:
        logger.error(f"Error creating column: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }

@mcp.tool()
async def move_card(project_id: str, card_id: str, column_id: str) -> Dict[str, Any]:
    """Move a card to a new column.
    
    Args:
        project_id: The project ID
        card_id: The card ID
        column_id: The destination column ID
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()
    
    try:
        await _run_sync(client.move_card, project_id, card_id, column_id)
        return {
            "status": "success",
            "message": f"Card moved to column {column_id}"
        }
    except Exception as e:
        logger.error(f"Error moving card: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }

@mcp.tool()
async def complete_card(project_id: str, card_id: str) -> Dict[str, Any]:
    """Mark a card as complete.
    
    Args:
        project_id: The project ID
        card_id: The card ID
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()
    
    try:
        await _run_sync(client.complete_card, project_id, card_id)
        return {
            "status": "success",
            "message": "Card marked as complete"
        }
    except Exception as e:
        logger.error(f"Error completing card: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }

@mcp.tool()
async def get_card(project_id: str, card_id: str) -> Dict[str, Any]:
    """Get details for a specific card.
    
    Args:
        project_id: The project ID
        card_id: The card ID
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()
    
    try:
        card = await _run_sync(client.get_card, project_id, card_id)
        return {
            "status": "success",
            "card": card
        }
    except Exception as e:
        logger.error(f"Error getting card: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired", 
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }

@mcp.tool()
async def update_card(project_id: str, card_id: str, title: Optional[str] = None, content: Optional[str] = None, due_on: Optional[str] = None, assignee_ids: Optional[List[str]] = None) -> Dict[str, Any]:
    """Update a card.
    
    Args:
        project_id: The project ID
        card_id: The card ID
        title: The new card title
        content: The new card content/description
        due_on: Due date (ISO 8601 format)
        assignee_ids: Array of person IDs to assign to the card
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()
    
    try:
        card = await _run_sync(client.update_card, project_id, card_id, title, content, due_on, assignee_ids)
        return {
            "status": "success",
            "card": card,
            "message": "Card updated successfully"
        }
    except Exception as e:
        logger.error(f"Error updating card: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }

@mcp.tool()
async def get_daily_check_ins(project_id: str, page: Optional[int] = None) -> Dict[str, Any]:
    """Get project's daily checking questionnaire.
    
    Args:
        project_id: The project ID
        page: Page number paginated response
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()
    
    try:
        if page is not None and not isinstance(page, int):
            page = 1
        answers = await _run_sync(client.get_daily_check_ins, project_id, page or 1)
        return {
            "status": "success",
            "campfire_lines": answers,
            "count": len(answers)
        }
    except Exception as e:
        logger.error(f"Error getting daily check ins: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }

@mcp.tool()
async def get_question_answers(project_id: str, question_id: str, page: Optional[int] = None) -> Dict[str, Any]:
    """Get answers on daily check-in question.
    
    Args:
        project_id: The project ID
        question_id: The question ID
        page: Page number paginated response
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()
    
    try:
        if page is not None and not isinstance(page, int):
            page = 1
        answers = await _run_sync(client.get_question_answers, project_id, question_id, page or 1)
        return {
            "status": "success",
            "campfire_lines": answers,
            "count": len(answers)
        }
    except Exception as e:
        logger.error(f"Error getting question answers: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }

# Column Management Tools
@mcp.tool()
async def update_column(project_id: str, column_id: str, title: str) -> Dict[str, Any]:
    """Update a column title.
    
    Args:
        project_id: The project ID
        column_id: The column ID
        title: The new column title
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()
    
    try:
        column = await _run_sync(client.update_column, project_id, column_id, title)
        return {
            "status": "success",
            "column": column,
            "message": "Column updated successfully"
        }
    except Exception as e:
        logger.error(f"Error updating column: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }

@mcp.tool()
async def move_column(project_id: str, card_table_id: str, column_id: str, position: int) -> Dict[str, Any]:
    """Move a column to a new position.
    
    Args:
        project_id: The project ID
        card_table_id: The card table ID
        column_id: The column ID
        position: The new 1-based position
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()
    
    try:
        await _run_sync(client.move_column, project_id, column_id, position, card_table_id)
        return {
            "status": "success",
            "message": f"Column moved to position {position}"
        }
    except Exception as e:
        logger.error(f"Error moving column: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }

@mcp.tool()
async def update_column_color(project_id: str, column_id: str, color: str) -> Dict[str, Any]:
    """Update a column color.
    
    Args:
        project_id: The project ID
        column_id: The column ID
        color: The hex color code (e.g., #FF0000)
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()
    
    try:
        column = await _run_sync(client.update_column_color, project_id, column_id, color)
        return {
            "status": "success",
            "column": column,
            "message": f"Column color updated to {color}"
        }
    except Exception as e:
        logger.error(f"Error updating column color: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }

@mcp.tool()
async def put_column_on_hold(project_id: str, column_id: str) -> Dict[str, Any]:
    """Put a column on hold (freeze work).
    
    Args:
        project_id: The project ID
        column_id: The column ID
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()
    
    try:
        await _run_sync(client.put_column_on_hold, project_id, column_id)
        return {
            "status": "success",
            "message": "Column put on hold"
        }
    except Exception as e:
        logger.error(f"Error putting column on hold: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }

@mcp.tool()
async def remove_column_hold(project_id: str, column_id: str) -> Dict[str, Any]:
    """Remove hold from a column (unfreeze work).
    
    Args:
        project_id: The project ID
        column_id: The column ID
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()
    
    try:
        await _run_sync(client.remove_column_hold, project_id, column_id)
        return {
            "status": "success",
            "message": "Column hold removed"
        }
    except Exception as e:
        logger.error(f"Error removing column hold: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }

@mcp.tool()
async def watch_column(project_id: str, column_id: str) -> Dict[str, Any]:
    """Subscribe to notifications for changes in a column.
    
    Args:
        project_id: The project ID
        column_id: The column ID
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()
    
    try:
        await _run_sync(client.watch_column, project_id, column_id)
        return {
            "status": "success",
            "message": "Column notifications enabled"
        }
    except Exception as e:
        logger.error(f"Error watching column: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }

@mcp.tool()
async def unwatch_column(project_id: str, column_id: str) -> Dict[str, Any]:
    """Unsubscribe from notifications for a column.
    
    Args:
        project_id: The project ID
        column_id: The column ID
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()
    
    try:
        await _run_sync(client.unwatch_column, project_id, column_id)
        return {
            "status": "success",
            "message": "Column notifications disabled"
        }
    except Exception as e:
        logger.error(f"Error unwatching column: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }

# More Card Management Tools  
@mcp.tool()
async def uncomplete_card(project_id: str, card_id: str) -> Dict[str, Any]:
    """Mark a card as incomplete.
    
    Args:
        project_id: The project ID
        card_id: The card ID
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()
    
    try:
        await _run_sync(client.uncomplete_card, project_id, card_id)
        return {
            "status": "success",
            "message": "Card marked as incomplete"
        }
    except Exception as e:
        logger.error(f"Error uncompleting card: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }

# Card Steps (Sub-tasks) Management
@mcp.tool()
async def get_card_steps(project_id: str, card_id: str) -> Dict[str, Any]:
    """Get all steps (sub-tasks) for a card.
    
    Args:
        project_id: The project ID
        card_id: The card ID
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()
    
    try:
        steps = await _run_sync(client.get_card_steps, project_id, card_id)
        return {
            "status": "success",
            "steps": steps,
            "count": len(steps)
        }
    except Exception as e:
        logger.error(f"Error getting card steps: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }

@mcp.tool()
async def create_card_step(project_id: str, card_id: str, title: str, due_on: Optional[str] = None, assignee_ids: Optional[List[str]] = None) -> Dict[str, Any]:
    """Create a new step (sub-task) for a card.
    
    Args:
        project_id: The project ID
        card_id: The card ID
        title: The step title
        due_on: Optional due date (ISO 8601 format)
        assignee_ids: Array of person IDs to assign to the step
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()
    
    try:
        step = await _run_sync(client.create_card_step, project_id, card_id, title, due_on, assignee_ids)
        return {
            "status": "success",
            "step": step,
            "message": f"Step '{title}' created successfully"
        }
    except Exception as e:
        logger.error(f"Error creating card step: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }

@mcp.tool()
async def get_card_step(project_id: str, step_id: str) -> Dict[str, Any]:
    """Get details for a specific card step.
    
    Args:
        project_id: The project ID
        step_id: The step ID
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()
    
    try:
        step = await _run_sync(client.get_card_step, project_id, step_id)
        return {
            "status": "success",
            "step": step
        }
    except Exception as e:
        logger.error(f"Error getting card step: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }

@mcp.tool()
async def update_card_step(project_id: str, step_id: str, title: Optional[str] = None, due_on: Optional[str] = None, assignee_ids: Optional[List[str]] = None) -> Dict[str, Any]:
    """Update a card step.
    
    Args:
        project_id: The project ID
        step_id: The step ID
        title: The step title
        due_on: Due date (ISO 8601 format)
        assignee_ids: Array of person IDs to assign to the step
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()
    
    try:
        step = await _run_sync(client.update_card_step, project_id, step_id, title, due_on, assignee_ids)
        return {
            "status": "success",
            "step": step,
            "message": f"Step updated successfully"
        }
    except Exception as e:
        logger.error(f"Error updating card step: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }

@mcp.tool()
async def delete_card_step(project_id: str, step_id: str) -> Dict[str, Any]:
    """Delete a card step.
    
    Args:
        project_id: The project ID
        step_id: The step ID
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()
    
    try:
        await _run_sync(client.delete_card_step, project_id, step_id)
        return {
            "status": "success",
            "message": "Step deleted successfully"
        }
    except Exception as e:
        logger.error(f"Error deleting card step: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }

@mcp.tool()
async def complete_card_step(project_id: str, step_id: str) -> Dict[str, Any]:
    """Mark a card step as complete.
    
    Args:
        project_id: The project ID
        step_id: The step ID
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()
    
    try:
        await _run_sync(client.complete_card_step, project_id, step_id)
        return {
            "status": "success",
            "message": "Step marked as complete"
        }
    except Exception as e:
        logger.error(f"Error completing card step: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }

@mcp.tool()
async def uncomplete_card_step(project_id: str, step_id: str) -> Dict[str, Any]:
    """Mark a card step as incomplete.
    
    Args:
        project_id: The project ID
        step_id: The step ID
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()
    
    try:
        await _run_sync(client.uncomplete_card_step, project_id, step_id)
        return {
            "status": "success",
            "message": "Step marked as incomplete"
        }
    except Exception as e:
        logger.error(f"Error uncompleting card step: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }

# Attachments, Events, and Webhooks
@mcp.tool()
async def create_attachment(file_content_b64: str, name: str, content_type: Optional[str] = None) -> Dict[str, Any]:
    """Upload a file to Basecamp's attachments endpoint and return an attachable_sgid.

    This is STEP 1 of attaching a file. The upload alone does not associate the
    file with any to-do, message, document, comment, or card — it only puts the
    bytes in Basecamp's attachment store. To make the file appear on a recording
    you must complete STEP 2:

      STEP 2: Pass the returned `attachable_sgid` (from response.attachment.attachable_sgid)
              to a creation tool's `attachable_sgids` parameter, e.g.:
                create_todo(..., attachable_sgids=["<sgid_from_step_1>"])
                create_message(..., attachable_sgids=[...])
                create_comment(..., attachable_sgids=[...])
                create_card(..., attachable_sgids=[...])
                create_document(..., attachable_sgids=[...])
                update_todo(..., attachable_sgids=[...])
                update_document(..., attachable_sgids=[...])

    Without STEP 2 the file will exist in the account but will not be visible
    on any to-do or message — this is the most common cause of "image
    didn't attach" issues.

    If you have a file URL instead of bytes, use create_attachment_from_url()
    which fetches the URL and performs the upload in one call.

    Args:
        file_content_b64: Base64-encoded file content (encode your file bytes as base64 before calling)
        name: Filename for Basecamp (e.g. "screenshot.png", "report.pdf")
        content_type: MIME type (e.g. "image/png", "image/jpeg", "application/pdf").
            Defaults to application/octet-stream — set this correctly for images
            so Basecamp renders them inline.
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()

    try:
        result = await _run_sync(client.create_attachment, file_content_b64, name, content_type or "application/octet-stream")
        return {
            "status": "success",
            "attachment": result,
            "next_step": "Pass result.attachment.attachable_sgid into attachable_sgids on create_todo / create_message / create_comment / create_card / create_document so the file actually appears on a recording.",
        }
    except Exception as e:
        logger.error(f"Error creating attachment: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }


@mcp.tool()
async def create_attachment_from_url(url: str, name: Optional[str] = None, content_type: Optional[str] = None) -> Dict[str, Any]:
    """Fetch a file from a URL and upload it to Basecamp's attachments endpoint in one call.

    Convenience wrapper around create_attachment for the common case where the
    AI has a URL (e.g. an image link) instead of raw file bytes. The HTTP
    response's Content-Type is used as the upload type when content_type is not
    provided. Returns the same attachable_sgid that create_attachment returns,
    which you must then pass to a creation tool's attachable_sgids parameter
    (see create_attachment for the full STEP 2 list).

    Args:
        url: HTTP(S) URL of the file to fetch and upload (must be reachable from this server).
        name: Filename to store as in Basecamp. If omitted, the URL's basename is used.
        content_type: MIME type override. If omitted, taken from the response's
            Content-Type header, falling back to application/octet-stream.
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()

    import base64
    from urllib.parse import urlparse

    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as http:
            resp = await http.get(url)
            if resp.status_code != 200:
                return {"error": "Fetch failed", "message": f"GET {url} returned HTTP {resp.status_code}"}
            data = resp.content
            detected_ct = resp.headers.get("Content-Type", "").split(";")[0].strip() or "application/octet-stream"

        effective_name = name or (urlparse(url).path.rsplit("/", 1)[-1] or "attachment")
        effective_ct = content_type or detected_ct
        b64 = base64.b64encode(data).decode("ascii")

        result = await _run_sync(client.create_attachment, b64, effective_name, effective_ct)
        return {
            "status": "success",
            "attachment": result,
            "fetched_bytes": len(data),
            "content_type_used": effective_ct,
            "next_step": "Pass result.attachment.attachable_sgid into attachable_sgids on create_todo / create_message / create_comment / create_card / create_document so the file actually appears on a recording.",
        }
    except Exception as e:
        logger.error(f"Error in create_attachment_from_url: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {"error": "Execution error", "message": str(e)}


@mcp.tool()
async def get_events(project_id: str, recording_id: str) -> Dict[str, Any]:
    """Get events for a recording.
    
    Args:
        project_id: Project ID
        recording_id: Recording ID
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()
    
    try:
        events = await _run_sync(client.get_events, project_id, recording_id)
        return {
            "status": "success",
            "events": events,
            "count": len(events)
        }
    except Exception as e:
        logger.error(f"Error getting events: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }

@mcp.tool()
async def get_webhooks(project_id: str) -> Dict[str, Any]:
    """List webhooks for a project.
    
    Args:
        project_id: Project ID
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()
    
    try:
        hooks = await _run_sync(client.get_webhooks, project_id)
        return {
            "status": "success",
            "webhooks": hooks,
            "count": len(hooks)
        }
    except Exception as e:
        logger.error(f"Error getting webhooks: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }

@mcp.tool()
async def create_webhook(project_id: str, payload_url: str, types: Optional[List[str]] = None) -> Dict[str, Any]:
    """Create a webhook.
    
    Args:
        project_id: Project ID
        payload_url: Payload URL
        types: Event types
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()
    
    try:
        hook = await _run_sync(client.create_webhook, project_id, payload_url, types)
        return {
            "status": "success",
            "webhook": hook
        }
    except Exception as e:
        logger.error(f"Error creating webhook: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }

@mcp.tool()
async def delete_webhook(project_id: str, webhook_id: str) -> Dict[str, Any]:
    """Delete a webhook.
    
    Args:
        project_id: Project ID
        webhook_id: Webhook ID
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()
    
    try:
        await _run_sync(client.delete_webhook, project_id, webhook_id)
        return {
            "status": "success",
            "message": "Webhook deleted"
        }
    except Exception as e:
        logger.error(f"Error deleting webhook: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }

# Document Management
@mcp.tool()
async def get_documents(project_id: str, vault_id: str) -> Dict[str, Any]:
    """List documents in a vault.
    
    Args:
        project_id: Project ID
        vault_id: Vault ID
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()
    
    try:
        docs = await _run_sync(client.get_documents, project_id, vault_id)
        return {
            "status": "success",
            "documents": docs,
            "count": len(docs)
        }
    except Exception as e:
        logger.error(f"Error getting documents: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }

@mcp.tool()
async def get_document(project_id: str, document_id: str) -> Dict[str, Any]:
    """Get a single document.
    
    Args:
        project_id: Project ID
        document_id: Document ID
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()
    
    try:
        doc = await _run_sync(client.get_document, project_id, document_id)
        return {
            "status": "success",
            "document": doc
        }
    except Exception as e:
        logger.error(f"Error getting document: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }

@mcp.tool()
async def create_document(project_id: str, vault_id: str, title: str, content: str,
                          attachable_sgids: Optional[List[Any]] = None) -> Dict[str, Any]:
    """Create a document in a vault.

    To embed image/file attachments inline, upload each via create_attachment()
    first to get an attachable_sgid, then pass them via attachable_sgids.
    They will be embedded as <bc-attachment> tags appended to the content.

    Args:
        project_id: Project ID
        vault_id: Vault ID
        title: Document title
        content: Document HTML content
        attachable_sgids: Optional list of attachable_sgid strings (or {"sgid","caption"} dicts)
            from create_attachment. Each renders as a <bc-attachment> tag inline.
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()

    content = _merge_description_with_attachments(content, attachable_sgids) or content

    try:
        doc = await _run_sync(client.create_document, project_id, vault_id, title, content)
        return {
            "status": "success",
            "document": doc
        }
    except Exception as e:
        logger.error(f"Error creating document: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }

@mcp.tool()
async def update_document(project_id: str, document_id: str, title: Optional[str] = None, content: Optional[str] = None,
                          attachable_sgids: Optional[List[Any]] = None) -> Dict[str, Any]:
    """Update a document.

    To add image/file attachments, follow the two-step flow:
      1. Upload via create_attachment_from_url(url) or create_attachment(file_content_b64, name, content_type)
         and capture result.attachment.attachable_sgid.
      2. Pass those sgids here as attachable_sgids — they are appended to the
         content as <bc-attachment> tags so Basecamp renders them inline.

    NOTE: Basecamp's update endpoint REPLACES the content, so include the
    existing content along with any new attachments to preserve the document.

    Args:
        project_id: Project ID
        document_id: Document ID
        title: New title
        content: New HTML content
        attachable_sgids: Optional list of attachable_sgid strings (or {"sgid","caption"} dicts)
            from create_attachment. Each renders as a <bc-attachment> tag inline.
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()

    content = _merge_description_with_attachments(content, attachable_sgids)

    try:
        doc = await _run_sync(client.update_document, project_id, document_id, title, content)
        return {
            "status": "success",
            "document": doc
        }
    except Exception as e:
        logger.error(f"Error updating document: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }

@mcp.tool()
async def trash_document(project_id: str, document_id: str) -> Dict[str, Any]:
    """Archive a document (safe deletion — archives instead of permanently deleting).

    The document will be hidden but remains accessible via the Basecamp web UI.

    Args:
        project_id: Project ID
        document_id: Document ID
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()

    try:
        await _run_sync(client.trash_document, project_id, document_id)
        return {
            "status": "success",
            "message": "Document archived (safe deletion)"
        }
    except Exception as e:
        logger.error(f"Error archiving document: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }

@mcp.tool()
async def move_document(project_id: str, document_id: str, target_vault_id: str) -> Dict[str, Any]:
    """Move a document to a different vault (folder) within the same project.

    Because Basecamp has no native move API, this tool:
    1. Fetches the source document's title and content
    2. Fetches all comments on the source document (across all pages)
    3. Creates a new document in the target vault with the same title and content
    4. Re-creates each comment on the new document, noting the original author
    5. Archives the source document (safe deletion — recoverable via Basecamp web UI)

    The archived original and all its new copies remain accessible during the
    30-day recovery window in Basecamp's trash.

    Args:
        project_id: The project ID
        document_id: ID of the document to move
        target_vault_id: ID of the destination vault (folder)
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()

    try:
        result = await _run_sync(client.move_document, project_id, document_id, target_vault_id)
        new_doc = result['new_document']
        return {
            "status": "success",
            "new_document_id": str(new_doc['id']),
            "new_document_title": new_doc.get('title'),
            "new_document_url": new_doc.get('app_url'),
            "comments_moved": result['comments_moved'],
            "archived_document_id": result['archived_document_id'],
            "message": (
                f"Document moved: '{new_doc.get('title')}' is now in the target vault. "
                f"{result['comments_moved']} comment(s) re-created. "
                f"Original (ID {result['archived_document_id']}) archived."
            )
        }
    except Exception as e:
        logger.error(f"Error moving document {document_id}: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }

# Vault Management (document folders)
@mcp.tool()
async def get_vaults(project_id: str, vault_id: str) -> Dict[str, Any]:
    """List child vaults (subfolders) inside a vault in a Basecamp project.

    Use this to navigate the document folder structure. To find the top-level
    vault for a project's Docs & Files section, call get_project first and
    look for the 'vault' dock entry which contains the root vault_id.

    Args:
        project_id: The project ID
        vault_id: The parent vault ID whose children to list
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()

    try:
        vaults = await _run_sync(client.get_vaults, project_id, vault_id)
        return {
            "status": "success",
            "vaults": vaults,
            "count": len(vaults)
        }
    except Exception as e:
        logger.error(f"Error getting vaults: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }

@mcp.tool()
async def get_vault(project_id: str, vault_id: str) -> Dict[str, Any]:
    """Get details of a specific vault (folder) in a Basecamp project.

    Returns vault metadata including title, counts of documents, uploads,
    and child vaults, plus URLs to list its contents.

    Args:
        project_id: The project ID
        vault_id: The vault ID
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()

    try:
        vault = await _run_sync(client.get_vault, project_id, vault_id)
        return {
            "status": "success",
            "vault": vault
        }
    except Exception as e:
        logger.error(f"Error getting vault: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }

@mcp.tool()
async def create_vault(project_id: str, vault_id: str, title: str) -> Dict[str, Any]:
    """Create a new subfolder (child vault) inside an existing vault in a Basecamp project.

    Args:
        project_id: The project ID
        vault_id: The parent vault ID to create the subfolder inside
        title: Name for the new subfolder
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()

    try:
        vault = await _run_sync(client.create_vault, project_id, vault_id, title)
        return {
            "status": "success",
            "vault": vault
        }
    except Exception as e:
        logger.error(f"Error creating vault: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }

@mcp.tool()
async def update_vault(project_id: str, vault_id: str, title: str) -> Dict[str, Any]:
    """Rename a vault (folder) in a Basecamp project.

    Args:
        project_id: The project ID
        vault_id: The vault ID to rename
        title: The new name for the vault
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()

    try:
        vault = await _run_sync(client.update_vault, project_id, vault_id, title)
        return {
            "status": "success",
            "vault": vault
        }
    except Exception as e:
        logger.error(f"Error updating vault: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }

# Upload Management
@mcp.tool()
async def get_uploads(project_id: str, vault_id: Optional[str] = None) -> Dict[str, Any]:
    """List uploads in a project or vault.
    
    Args:
        project_id: Project ID
        vault_id: Optional vault ID to limit to specific vault
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()
    
    try:
        uploads = await _run_sync(client.get_uploads, project_id, vault_id)
        return {
            "status": "success",
            "uploads": uploads,
            "count": len(uploads)
        }
    except Exception as e:
        logger.error(f"Error getting uploads: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }

@mcp.tool()
async def get_upload(project_id: str, upload_id: str) -> Dict[str, Any]:
    """Get details for a specific upload.
    
    Args:
        project_id: Project ID
        upload_id: Upload ID
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()
    
    try:
        upload = await _run_sync(client.get_upload, project_id, upload_id)
        return {
            "status": "success",
            "upload": upload
        }
    except Exception as e:
        logger.error(f"Error getting upload: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }

@mcp.tool()
async def get_todolist(project_id: str, todolist_id: str) -> Dict[str, Any]:
    """Get a specific todo list by ID.

    Args:
        project_id: The project ID
        todolist_id: The todo list ID
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()

    try:
        todolist = await _run_sync(client.get_todolist, project_id, todolist_id)
        return {"status": "success", "todolist": todolist}
    except Exception as e:
        logger.error(f"Error getting todolist {todolist_id}: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {"error": "OAuth token expired", "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."}
        return {"error": "Execution error", "message": str(e)}


@mcp.tool()
async def create_todolist(
    project_id: str,
    name: str,
    description: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a new todo list in a project.

    Args:
        project_id: The project ID
        name: Todo list name
        description: Optional HTML description
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()

    try:
        todolist = await _run_sync(
            lambda: client.create_todolist(project_id, name, description)
        )
        return {"status": "success", "todolist": todolist}
    except Exception as e:
        logger.error(f"Error creating todolist: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {"error": "OAuth token expired", "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."}
        return {"error": "Execution error", "message": str(e)}


@mcp.tool()
async def update_todolist(
    project_id: str,
    todolist_id: str,
    name: str,
    description: Optional[str] = None,
) -> Dict[str, Any]:
    """Update an existing todo list.

    The Basecamp API requires the name even when only updating the description.

    Args:
        project_id: The project ID
        todolist_id: The todo list ID
        name: Todo list name (required)
        description: Optional HTML description
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()

    try:
        todolist = await _run_sync(
            lambda: client.update_todolist(project_id, todolist_id, name, description)
        )
        return {"status": "success", "todolist": todolist}
    except Exception as e:
        logger.error(f"Error updating todolist {todolist_id}: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {"error": "OAuth token expired", "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."}
        return {"error": "Execution error", "message": str(e)}


@mcp.tool()
async def trash_todolist(project_id: str, todolist_id: str) -> Dict[str, Any]:
    """Archive a todo list (safe deletion — archives instead of permanently deleting).

    The todo list will be hidden from the active view but remains accessible
    via the Basecamp web UI and can be unarchived at any time.

    Args:
        project_id: The project ID
        todolist_id: The todo list ID
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()

    try:
        await _run_sync(client.trash_todolist, project_id, todolist_id)
        return {"status": "success", "message": f"Todolist {todolist_id} archived (safe deletion)"}
    except Exception as e:
        logger.error(f"Error archiving todolist {todolist_id}: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {"error": "OAuth token expired", "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."}
        return {"error": "Execution error", "message": str(e)}


@mcp.tool()
async def get_todolist_groups(project_id: str, todolist_id: str) -> Dict[str, Any]:
    """Get all groups in a todo list.

    Groups are named sections within a todo list (e.g. "Phase 1", "Backlog").

    Args:
        project_id: The project ID
        todolist_id: The todo list ID
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()

    try:
        groups = await _run_sync(client.get_todolist_groups, project_id, todolist_id)
        return {"status": "success", "groups": groups, "count": len(groups)}
    except Exception as e:
        logger.error(f"Error getting todolist groups: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {"error": "OAuth token expired", "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."}
        return {"error": "Execution error", "message": str(e)}


@mcp.tool()
async def create_todolist_group(
    project_id: str,
    todolist_id: str,
    name: str,
    color: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a new group inside a todo list.

    Groups act as named sections to organise todos within a list.

    Args:
        project_id: The project ID
        todolist_id: The todo list ID
        name: Group name
        color: Optional color – one of: white, red, orange, yellow, green,
               blue, aqua, purple, gray, pink, brown
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()

    try:
        group = await _run_sync(
            lambda: client.create_todolist_group(project_id, todolist_id, name, color)
        )
        return {"status": "success", "group": group}
    except Exception as e:
        logger.error(f"Error creating todolist group: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {"error": "OAuth token expired", "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."}
        return {"error": "Execution error", "message": str(e)}


@mcp.tool()
async def reposition_todolist_group(
    project_id: str, group_id: str, position: int
) -> Dict[str, Any]:
    """Reposition a todo list group to a new location within its list.

    Args:
        project_id: The project ID
        group_id: The group ID
        position: New 1-based position
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()

    if position < 1:
        return {"error": "Invalid input", "message": "position must be >= 1"}

    try:
        await _run_sync(
            lambda: client.reposition_todolist_group(project_id, group_id, position)
        )
        return {"status": "success", "message": f"Group {group_id} repositioned to position {position}"}
    except Exception as e:
        logger.error(f"Error repositioning todolist group {group_id}: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {"error": "OAuth token expired", "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."}
        return {"error": "Execution error", "message": str(e)}


# People Management

@mcp.tool()
async def get_people() -> Dict[str, Any]:
    """Get all people in the Basecamp account.

    Returns a list of all people with their IDs, names, and email addresses.
    Use this to look up person IDs needed for assigning todos, cards, etc.
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()

    try:
        people = await _run_sync(client.get_people)
        return {"status": "success", "count": len(people), "data": people}
    except Exception as e:
        logger.error(f"Error getting people: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {"error": "OAuth token expired", "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."}
        return {"error": "Execution error", "message": str(e)}


@mcp.tool()
async def get_project_people(project_id: str) -> Dict[str, Any]:
    """Get all people who have access to a specific project.

    Use this to find who is on a project before assigning todos or cards.
    Returns person IDs, names, and email addresses for everyone on the project.

    Args:
        project_id: The project ID to get people for
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()

    try:
        people = await _run_sync(client.get_project_people, project_id)
        return {"status": "success", "count": len(people), "data": people}
    except Exception as e:
        logger.error(f"Error getting project people: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {"error": "OAuth token expired", "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."}
        return {"error": "Execution error", "message": str(e)}


@mcp.tool()
async def search_people(name: str, project_id: Optional[str] = None) -> Dict[str, Any]:
    """Search for people by name (case-insensitive, partial match).

    Use this to find a person's ID when you only know their name (or part of it).
    For example, searching "zen" will match "Zenul Abidin".

    Args:
        name: Name (or partial name) to search for
        project_id: Optional project ID to limit search to people on that project
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()

    try:
        if project_id:
            people = await _run_sync(client.get_project_people, project_id)
        else:
            people = await _run_sync(client.get_people)

        search_lower = name.lower()
        matches = [
            p for p in people
            if search_lower in p.get("name", "").lower()
            or search_lower in p.get("email_address", "").lower()
        ]

        return {
            "status": "success",
            "search_term": name,
            "match_count": len(matches),
            "matches": matches
        }
    except Exception as e:
        logger.error(f"Error searching people: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {"error": "OAuth token expired", "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."}
        return {"error": "Execution error", "message": str(e)}


PORT = int(os.getenv("PORT", "9000"))
MCP_PATH = os.getenv("MCP_PATH", "/basecamp/")


def streamable_http_server():
    """Hosted entry point: serve Streamable HTTP behind mcp-oauth-proxy."""
    logger.info("Starting Basecamp FastMCP over Streamable HTTP on :%s%s", PORT, MCP_PATH)
    mcp.run(
        transport="streamable-http",
        host="0.0.0.0",
        port=PORT,
        path=MCP_PATH,
    )


def stdio_server():
    """Local CLI entry point (Cursor / Claude Desktop)."""
    logger.info("Starting Basecamp FastMCP over stdio")
    mcp.run()


if __name__ == "__main__":
    if os.getenv("MCP_TRANSPORT", "stdio").lower() in ("http", "streamable-http", "sse"):
        streamable_http_server()
    else:
        stdio_server() 