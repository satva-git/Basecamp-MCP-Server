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
from mcp.server.fastmcp import FastMCP

# Import existing business logic
from basecamp_client import BasecampClient
from search_utils import BasecampSearch
import token_storage
import auth_manager
from dotenv import load_dotenv

# Multi-user: when running over SSE, run_mcp_server_sse sets request-scoped user_id
try:
    import mcp_auth_context
except ImportError:
    mcp_auth_context = None

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
mcp = FastMCP("basecamp")

# Auth helper functions (multi-user aware when run over SSE)
def _get_request_user_id() -> Optional[str]:
    """Return request-scoped user_id when running over SSE; None for stdio or single-user."""
    if mcp_auth_context is not None:
        return mcp_auth_context.get_user_id_for_request()
    return None

def _get_basecamp_client() -> Optional[BasecampClient]:
    """Get authenticated Basecamp client. Uses per-user token when user_id is set (SSE)."""
    try:
        user_id = _get_request_user_id()
        token_data = token_storage.get_token(user_id=user_id)
        logger.debug("Token data retrieved for user_id=%s", user_id)

        if not token_data or not token_data.get('access_token'):
            logger.error("No OAuth token available")
            return None

        if not auth_manager.ensure_authenticated(user_id=user_id):
            logger.error("OAuth token has expired and automatic refresh failed")
            return None

        token_data = token_storage.get_token(user_id=user_id)
        account_id = token_data.get('account_id') or os.getenv('BASECAMP_ACCOUNT_ID')
        user_agent = os.getenv('USER_AGENT') or "Basecamp MCP Server (cursor@example.com)"

        if not account_id:
            logger.error("Missing account_id. Token data: %s, Env BASECAMP_ACCOUNT_ID: %s", token_data, os.getenv('BASECAMP_ACCOUNT_ID'))
            return None

        logger.debug("Creating Basecamp client with account_id: %s, user_agent: %s", account_id, user_agent)
        return BasecampClient(
            access_token=token_data['access_token'],
            account_id=account_id,
            user_agent=user_agent,
            auth_mode='oauth'
        )
    except Exception as e:
        logger.error("Error creating Basecamp client: %s", e)
        return None

def _get_auth_error_response() -> Dict[str, Any]:
    """Return consistent auth error response (uses request user_id when in SSE mode)."""
    user_id = _get_request_user_id()
    if token_storage.is_token_expired(user_id=user_id):
        return {
            "error": "OAuth token expired",
            "message": "Your Basecamp OAuth token has expired. Please re-authenticate by visiting the OAuth app and completing the flow again."
        }
    return {
        "error": "Authentication required",
        "message": "Please authenticate with Basecamp first (visit the OAuth app to link your account and get an API key)."
    }

async def _run_sync(func, *args, **kwargs):
    """Wrapper to run synchronous functions in thread pool."""
    return await anyio.to_thread.run_sync(func, *args, **kwargs)

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
                     starts_on: Optional[str] = None) -> Dict[str, Any]:
    """Create a new to-do item in a to-do list.

    IMPORTANT: This creates a to-do (task) inside a to-do list, NOT a message.
    Use create_message() to post announcements/discussions on the Message Board.
    Use create_comment() to add a comment/reply on an existing to-do or message.

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
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()
    
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
                     starts_on: Optional[str] = None) -> Dict[str, Any]:
    """Update an existing todo item.
    
    Args:
        project_id: Project ID
        todo_id: The todo ID
        content: The todo item's text
        description: HTML description of the todo
        assignee_ids: List of person IDs to assign
        completion_subscriber_ids: List of person IDs to notify on completion
        due_on: Due date in YYYY-MM-DD format
        starts_on: Start date in YYYY-MM-DD format
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()
    
    try:
        # Guard against no-op updates
        if all(v is None for v in [content, description, assignee_ids,
                                   completion_subscriber_ids, notify,
                                   due_on, starts_on]):
            return {
                "error": "Invalid input",
                "message": "At least one field to update must be provided"
            }
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
async def create_comment(recording_id: str, project_id: str, content: str) -> Dict[str, Any]:
    """Create a comment on any Basecamp recording — including to-dos, messages, documents, and other items.

    Use this tool to add a comment/reply to an existing to-do item, message thread, document, or any
    other Basecamp recording. In Basecamp, every commentable item has a unique recording ID.

    Common use cases:
    - Comment on a to-do: pass the to-do's ID as recording_id
    - Reply to a message thread: pass the message's ID as recording_id
    - Comment on a document: pass the document's ID as recording_id

    Args:
        recording_id: The ID of the to-do, message, document, or other Basecamp item to comment on
        project_id: The project ID (also called bucket ID) that contains the item
        content: The comment content in HTML format (e.g. '<p>Looks good!</p>')
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()

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
                         category_id: Optional[str] = None) -> Dict[str, Any]:
    """Create a new message (announcement/discussion thread) on a project's Message Board in Basecamp.

    IMPORTANT: This creates a Message Board post (like an announcement or discussion thread), NOT a to-do item.
    Use create_todo() to create to-do items. Use create_comment() to reply to existing messages or to-dos.

    Messages in Basecamp are discussion threads posted to the Message Board. They have a subject line
    and body content, similar to an email or forum post. They appear under the "Message Board" section
    of a Basecamp project.

    Args:
        project_id: The project ID
        subject: Message title/subject line (required — this is the thread title)
        content: Message body in HTML format (e.g. '<p>Here is the update...</p>')
        message_board_id: Optional message board ID. If not provided, will be auto-discovered from the project.
        category_id: Optional message type/category ID (use get_message_categories to find available types)
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()

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
async def create_card(project_id: str, column_id: str, title: str, content: Optional[str] = None, due_on: Optional[str] = None, notify: bool = False) -> Dict[str, Any]:
    """Create a new card in a column.
    
    Args:
        project_id: The project ID
        column_id: The column ID
        title: The card title
        content: Optional card content/description
        due_on: Optional due date (ISO 8601 format)
        notify: Whether to notify assignees (default: false)
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()
    
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
        answers = await _run_sync(client.get_daily_check_ins, project_id, page=page or 1)
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
        answers = await _run_sync(client.get_question_answers, project_id, question_id, page=page or 1)
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
async def create_attachment(file_path: str, name: str, content_type: Optional[str] = None) -> Dict[str, Any]:
    """Upload a file as an attachment.
    
    Args:
        file_path: Local path to file
        name: Filename for Basecamp
        content_type: MIME type
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()
    
    try:
        result = await _run_sync(client.create_attachment, file_path, name, content_type or "application/octet-stream")
        return {
            "status": "success",
            "attachment": result
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
async def create_document(project_id: str, vault_id: str, title: str, content: str) -> Dict[str, Any]:
    """Create a document in a vault.
    
    Args:
        project_id: Project ID
        vault_id: Vault ID
        title: Document title
        content: Document HTML content
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()
    
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
async def update_document(project_id: str, document_id: str, title: Optional[str] = None, content: Optional[str] = None) -> Dict[str, Any]:
    """Update a document.
    
    Args:
        project_id: Project ID
        document_id: Document ID
        title: New title
        content: New HTML content
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()
    
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


if __name__ == "__main__":
    logger.info("Starting Basecamp FastMCP server")
    # Run using official MCP stdio transport
    mcp.run(transport='stdio') 