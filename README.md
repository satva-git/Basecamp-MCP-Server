# Basecamp MCP Integration

This project provides a **FastMCP-powered** integration for Basecamp 3, allowing AI clients to interact with Basecamp directly through the MCP protocol.

✅ **Migration Complete:** Successfully migrated to official Anthropic FastMCP framework with **100% feature parity** (all 78 tools)
🚀 **Ready for Production:** Full protocol compliance with MCP 2025-06-18

## Quick Setup

This server works with **Cursor**, **Codex**, and **Claude Desktop**. Choose your preferred client:

### Prerequisites

- **Python 3.10+** (required for MCP SDK) — or use `uv` which auto-downloads the correct version
- A Basecamp 3 account
- A Basecamp OAuth application (create one at <https://launchpad.37signals.com/integrations>)

## For Cursor Users

### One-Command Setup

1. **Clone and set up with uv (recommended):**

   ```bash
   git clone <repository-url>
   cd Basecamp-MCP-Server

   # Using uv (recommended - auto-downloads Python 3.12)
   uv venv --python 3.12 venv
   source venv/bin/activate  # or venv\Scripts\activate on Windows
   uv pip install -r requirements.txt
   uv pip install mcp
   ```

   **Alternative: Using pip** (requires Python 3.10+ already installed):

   ```bash
   python setup.py
   ```

   The setup automatically:
   - ✅ Creates virtual environment
   - ✅ Installs all dependencies (FastMCP SDK, etc.)
   - ✅ Creates `.env` template file
   - ✅ Tests MCP server functionality

2. **Configure OAuth credentials:**
   Edit the generated `.env` file:

   ```bash
   BASECAMP_CLIENT_ID=your_client_id_here
   BASECAMP_CLIENT_SECRET=your_client_secret_here
   BASECAMP_ACCOUNT_ID=your_account_id_here
   USER_AGENT="Your App Name (your@email.com)"
   ```

3. **Authenticate with Basecamp:**

   ```bash
   python oauth_app.py
   ```

   Visit <http://localhost:8000> and complete the OAuth flow.

4. **Generate Cursor configuration:**

   ```bash
   python generate_cursor_config.py
   ```

5. **Restart Cursor completely** (quit and reopen, not just reload)

6. **Verify in Cursor:**
   - Go to Cursor Settings → MCP
   - You should see "basecamp" with a **green checkmark**
   - Available tools: **78 tools** for complete Basecamp control

### Test Your Setup

```bash
# Quick test the FastMCP server (works with both clients)
echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"test","version":"1.0"}}}
{"jsonrpc":"2.0","method":"notifications/initialized","params":{}}
{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' | python basecamp_fastmcp.py

# Run automated tests  
python -m pytest tests/ -v
```

## For Codex Users

Codex integration is fully automated with a local path-agnostic script.
The script computes all paths from this repository root, so it works no matter where the repo is installed.

### Setup Steps

1. **Complete the basic setup** (same as Cursor steps 1-3 above):

   ```bash
   git clone <repository-url>
   cd Basecamp-MCP-Server
   python setup.py
   # Configure .env file with OAuth credentials
   python oauth_app.py
   ```

2. **Generate Codex configuration automatically:**

   ```bash
   python generate_codex_config.py
   ```

   Optional flags:

   ```bash
   # Preview commands only (no changes):
   python generate_codex_config.py --dry-run

   # Use legacy server instead of FastMCP:
   python generate_codex_config.py --legacy
   ```

3. **Verify in Codex:**

   ```bash
   codex mcp get basecamp
   codex mcp list
   ```

### Codex Configuration

The script writes to Codex global config:

- `~/.codex/config.toml`

It creates this MCP server entry shape:

```toml
[mcp_servers.basecamp]
command = "/path/to/your/project/venv/bin/python"
args = ["/path/to/your/project/basecamp_fastmcp.py"]

[mcp_servers.basecamp.env]
PYTHONPATH = "/path/to/your/project"
VIRTUAL_ENV = "/path/to/your/project/venv"
BASECAMP_ACCOUNT_ID = "your_account_id"
```

## For Claude Desktop Users

Based on the [official MCP quickstart guide](https://modelcontextprotocol.io/quickstart/server), Claude Desktop integration follows these steps:

### Setup Steps

1. **Complete the basic setup** (steps 1-3 from Cursor setup above):

   ```bash
   git clone <repository-url>
   cd Basecamp-MCP-Server
   python setup.py
   # Configure .env file with OAuth credentials
   python oauth_app.py
   ```

2. **Generate Claude Desktop configuration:**

   ```bash
   python generate_claude_desktop_config.py
   ```

3. **Restart Claude Desktop completely** (quit and reopen the application)

4. **Verify in Claude Desktop:**
   - Look for the "Search and tools" icon (🔍) in the chat interface
   - You should see "basecamp" listed with all 78 tools available
   - Toggle the tools on to enable Basecamp integration

### Claude Desktop Configuration

The configuration is automatically created at:

- **macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows**: `~/AppData/Roaming/Claude/claude_desktop_config.json`  
- **Linux**: `~/.config/claude-desktop/claude_desktop_config.json`

Example configuration generated:

```json
{
  "mcpServers": {
    "basecamp": {
      "command": "/path/to/your/project/venv/bin/python",
      "args": ["/path/to/your/project/basecamp_fastmcp.py"],
      "env": {
        "PYTHONPATH": "/path/to/your/project",
        "VIRTUAL_ENV": "/path/to/your/project/venv",
        "BASECAMP_ACCOUNT_ID": "your_account_id"
      }
    }
  }
}
```

### Usage in Claude Desktop

Ask Claude things like:

- "What are my current Basecamp projects?"
- "Show me the latest campfire messages from the Technology project"
- "Create a new card in the Development column with title 'Fix login bug'"
- "Get all todo items from the Marketing project"
- "Search for messages containing 'deadline'"

### Troubleshooting Claude Desktop

**Check Claude Desktop logs** (following [official debugging guide](https://modelcontextprotocol.io/quickstart/server#troubleshooting)):

```bash
# macOS/Linux - Monitor logs in real-time
tail -n 20 -f ~/Library/Logs/Claude/mcp*.log

# Check for specific errors
ls ~/Library/Logs/Claude/mcp-server-basecamp.log
```

**Common issues:**

- **Tools not appearing**: Verify configuration file syntax and restart Claude Desktop
- **Connection failures**: Check that Python path and script path are absolute paths
- **Authentication errors**: Ensure OAuth flow completed successfully (`oauth_tokens.json` exists)

## Available MCP Tools

Once configured, you can use these tools in Cursor:

- `get_projects` - Get all Basecamp projects (returns all pages; handles Basecamp pagination transparently)
- `get_project` - Get details for a specific project
- `get_todolists` - Get todo lists for a project
- `get_todolist` - Get a specific todo list by ID
- `create_todolist` - Create a new todo list in a project
- `update_todolist` - Update an existing todo list (name and/or description)
- `trash_todolist` - Move a todo list to the trash (recoverable within 30 days)
- `get_todos` - Get todos from a todo list (returns all pages; handles Basecamp pagination transparently)
- `get_todo` - Get a single todo item by its ID
- `create_todo` - Create a new todo item in a todo list (with assignees, due dates, descriptions)
- `update_todo` - Update an existing todo item (content, description, assignees, due date, etc.)
- `delete_todo` - Move a todo item to the trash (recoverable within 30 days)
- `complete_todo` - Mark a todo item as complete
- `uncomplete_todo` - Mark a todo item as incomplete
- `reposition_todo` - Reposition a todo within its list, or move it to another list or group
- `archive_todo` - Archive a todo item (hidden from active list, accessible via web UI)
- `search_basecamp` - Search across projects, todos, and messages
- `global_search` - Search projects, todos, and campfire messages across all projects
- `get_comments` - Get comments for a Basecamp item
- `create_comment` - Create a comment on a Basecamp item
- `get_campfire_lines` - Get recent messages from a Basecamp campfire
- `get_message_board` - Get the message board for a project
- `get_messages` - Get all messages from a project's message board
- `get_message` - Get a specific message by ID
- `get_message_categories` - Get available message categories (types) for a project (e.g. Announcement, FYI, Heartbeat, Pitch, Question)
- `create_message` - Create a new message on a project's message board, with optional category
- `get_daily_check_ins` - Get project's daily check-in questions
- `get_question_answers` - Get answers to daily check-in questions
- `create_attachment` - Upload a file as an attachment
- `get_uploads` - List uploads in a project or vault
- `get_upload` - Get details for a specific upload
- `get_events` - Get events for a recording
- `get_webhooks` - List webhooks for a project
- `create_webhook` - Create a webhook
- `delete_webhook` - Delete a webhook
- `get_documents` - List documents in a vault
- `get_document` - Get a single document
- `create_document` - Create a document
- `update_document` - Update a document
- `trash_document` - Move a document to trash

### People Tools

- `get_people` - Get all people in the Basecamp account (handles pagination)
- `get_project_people` - Get all people with access to a specific project (handles pagination)
- `search_people` - Search for people by name or email (case-insensitive, partial match) — use this to find person IDs for assigning todos, cards, etc.

### Todo List Group Tools

- `get_todolist_groups` - Get all groups in a todo list (named sections like "Phase 1", "Backlog")
- `create_todolist_group` - Create a new group inside a todo list (supports colors: white, red, orange, yellow, green, blue, aqua, purple, gray, pink, brown)
- `reposition_todolist_group` - Reposition a todo list group to a new location within its list

### Inbox Tools (Email Forwards)

- `get_inbox` - Get the inbox for a project (email forwards container)
- `get_forwards` - Get all forwarded emails from a project's inbox
- `get_forward` - Get a specific forwarded email by ID
- `get_inbox_replies` - Get all replies to a forwarded email
- `get_inbox_reply` - Get a specific reply to a forwarded email
- `trash_forward` - Move a forwarded email to trash

### Card Table Tools

- `get_card_tables` - Get all card tables for a project
- `get_card_table` - Get the card table details for a project
- `get_columns` - Get all columns in a card table
- `get_column` - Get details for a specific column
- `create_column` - Create a new column in a card table
- `update_column` - Update a column title
- `move_column` - Move a column to a new position
- `update_column_color` - Update a column color
- `put_column_on_hold` - Put a column on hold (freeze work)
- `remove_column_hold` - Remove hold from a column (unfreeze work)
- `watch_column` - Subscribe to notifications for changes in a column
- `unwatch_column` - Unsubscribe from notifications for a column
- `get_cards` - Get all cards in a column
- `get_card` - Get details for a specific card
- `create_card` - Create a new card in a column
- `update_card` - Update a card
- `move_card` - Move a card to a new column
- `complete_card` - Mark a card as complete
- `uncomplete_card` - Mark a card as incomplete
- `get_card_steps` - Get all steps (sub-tasks) for a card
- `create_card_step` - Create a new step (sub-task) for a card
- `get_card_step` - Get details for a specific card step
- `update_card_step` - Update a card step
- `delete_card_step` - Delete a card step
- `complete_card_step` - Mark a card step as complete
- `uncomplete_card_step` - Mark a card step as incomplete

### Example Cursor Usage

Ask Cursor things like:

- "Show me all my Basecamp projects"
- "What todos are in project X?"
- "Create a new todo 'Review PR' in the Sprint Backlog list"
- "Mark the 'Deploy v2' todo as complete"
- "Show me the messages from the message board in project X"
- "What message categories are available in project X?"
- "Post a new Announcement to the message board in project X: 'We shipped v2.0!'"
- "Create a Heartbeat message in project X with a weekly progress update"
- "Search for messages containing 'deadline'"
- "Get details for the Technology project"
- "Show me the card table for project X"
- "Create a new card in the 'In Progress' column"
- "Move this card to the 'Done' column"
- "Update the color of the 'Urgent' column to red"
- "Mark card as complete"
- "Show me all steps for this card"
- "Create a sub-task for this card"
- "Mark this card step as complete"
- "Find the person named Zenul in the SatvaSolutions project"
- "Assign this todo to Zenul"

## Architecture

The project uses the **official Anthropic FastMCP framework** for maximum reliability and compatibility:

1. **FastMCP Server** (`basecamp_fastmcp.py`) - Official MCP SDK with 78 tools, compatible with Cursor, Codex, and Claude Desktop
2. **OAuth App** (`oauth_app.py`) - Handles OAuth 2.0 flow with Basecamp  
3. **Token Storage** (`token_storage.py`) - Securely stores OAuth tokens
4. **Basecamp Client** (`basecamp_client.py`) - Basecamp API client library
5. **Search Utilities** (`search_utils.py`) - Search across Basecamp resources
6. **Setup Automation** (`setup.py`) - One-command installation
7. **Configuration Generators**:
   - `generate_cursor_config.py` - For Cursor IDE integration
   - `generate_codex_config.py` - For Codex CLI integration
   - `generate_claude_desktop_config.py` - For Claude Desktop integration

## Hosting the MCP server (SSE) & Multi-user

You can run the MCP server over **SSE (Server-Sent Events)** so it is reachable via HTTP (e.g. on a remote host or in Docker). This mode supports **multiple users**, each with their own Basecamp link and API key.

### Environment variables

| Variable | Description | Default |
|----------|-------------|---------|
| `MCP_HOST` | Bind address for the SSE server | `0.0.0.0` |
| `MCP_PORT` | Port for the SSE server | `8010` |
| `MCP_REQUIRE_AUTH` | If `1`, `true`, or `yes`, require `Authorization: Bearer <api_key>` on every request | unset (single-user fallback allowed) |
| `MCP_SSE_URL` | Public URL of the SSE server (shown to users after linking Basecamp) | `http://localhost:8010` |
| `DATA_DIR` | Directory for SQLite DB (created under project root as `data/` if not set) | `./data` |

User and token data are stored in **`data/basecamp_mcp.db`** (SQLite). Create this directory or set `DATA_DIR` if you run from a different working directory.

### API key flow (multi-user)

1. **Run the OAuth app** (e.g. `python oauth_app.py`) and the SSE server (e.g. `python run_mcp_server_sse.py`).
2. **Visit the OAuth app** in a browser (e.g. `http://localhost:8000` or your deployed URL).
3. Click **“Sign up with Basecamp”** (or the link to connect Basecamp), complete the Basecamp OAuth flow.
4. On success you get a **personal API key** and an **MCP config** snippet. Copy the API key (it is not shown again) and use it as `Authorization: Bearer <api_key>` when connecting to the SSE server.
5. **Configure your MCP client** with the SSE URL and header:
   - **URL:** `http://<host>:<port>/` (e.g. `http://localhost:8010/` or `https://your-domain.com/`)
   - **Header:** `Authorization: Bearer <your_api_key>`

Example Cursor config for SSE (multi-user):

```json
{
  "mcpServers": {
    "basecamp": {
      "url": "http://localhost:8010/",
      "headers": { "Authorization": "Bearer YOUR_API_KEY" }
    }
  }
}
```

For **remote hosting** (e.g. Coolify, Docker, or a VPS), set `MCP_SSE_URL` to the public URL (e.g. `https://mcp.yourdomain.com`) so the success page shows the correct URL. On localhost, the default `http://localhost:8010` is fine.

### Auth behavior

- **Single user, no auth:** If only one user exists and `MCP_REQUIRE_AUTH` is not set, requests without an `Authorization` header are allowed (that user’s token is used).
- **Multiple users or `MCP_REQUIRE_AUTH=1`:** Every request must include `Authorization: Bearer <api_key>`. Invalid or missing API keys receive **401 Unauthorized**.

### Legacy migration

If **no users** exist but a legacy **`oauth_tokens.json`** file is present, the first run of `run_mcp_server_sse.py` or `oauth_app.py` migrates that token into the multi-user store and creates one user. The new API key is printed to stderr (SSE) or logged (OAuth app). After migration, use that API key for SSE connections.

## Troubleshooting

### Common Issues (Both Clients)

- 🔴 **Red/Yellow indicator:** Run `python setup.py` to create proper virtual environment
- 🔴 **"0 tools available":** Virtual environment missing MCP packages - run setup script
- 🔴 **"Tool not found" errors:** Restart your client (Cursor/Codex/Claude Desktop) completely
- ⚠️ **Missing BASECAMP_ACCOUNT_ID:** Add to `.env` file, then re-run the config generator

### Quick Fixes

**Problem: Server won't start**

```bash
# Test if FastMCP server works:
./venv/bin/python -c "import mcp; print('✅ MCP available')"
# If this fails, run: python setup.py
```

**Problem: Wrong Python version**

```bash
python --version  # Must be 3.10+
# If too old, use uv which auto-downloads the correct Python:
uv venv --python 3.12 venv && source venv/bin/activate && uv pip install -r requirements.txt && uv pip install mcp
```

**Problem: Authentication fails**

```bash  
# Check OAuth flow:
python oauth_app.py
# Visit http://localhost:8000 and complete login
```

### Manual Configuration (Last Resort)

**Cursor config location:** `~/.cursor/mcp.json` (macOS/Linux) or `%APPDATA%\Cursor\mcp.json` (Windows)  
**Codex config location:** `~/.codex/config.toml`  
**Claude Desktop config location:** `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS)

```json
{
    "mcpServers": {
        "basecamp": {
            "command": "/full/path/to/your/project/venv/bin/python",
            "args": ["/full/path/to/your/project/basecamp_fastmcp.py"],
            "cwd": "/full/path/to/your/project",
            "env": {
                "PYTHONPATH": "/full/path/to/your/project",
                "VIRTUAL_ENV": "/full/path/to/your/project/venv",
                "BASECAMP_ACCOUNT_ID": "your_account_id"
            }
        }
    }
}
```

Codex equivalent:

```toml
[mcp_servers.basecamp]
command = "/full/path/to/your/project/venv/bin/python"
args = ["/full/path/to/your/project/basecamp_fastmcp.py"]

[mcp_servers.basecamp.env]
PYTHONPATH = "/full/path/to/your/project"
VIRTUAL_ENV = "/full/path/to/your/project/venv"
BASECAMP_ACCOUNT_ID = "your_account_id"
```

## Finding Your Account ID

If you don't know your Basecamp account ID:

1. Log into Basecamp in your browser
2. Look at the URL - it will be like `https://3.basecamp.com/4389629/projects`
3. The number (4389629 in this example) is your account ID

## Security Notes

- Keep your `.env` file secure and never commit it to version control.
- **Stdio (local):** OAuth tokens are stored in `oauth_tokens.json` in the project directory.
- **SSE (multi-user):** Tokens are stored in `data/basecamp_mcp.db`. Each user has an API key; do not share API keys. Use `MCP_REQUIRE_AUTH=1` when hosting for multiple users or on a shared network.
- For production SSE hosting, use HTTPS and a secure secret for `FLASK_SECRET_KEY`.

## Version History

### v1.2.0 (2026-03-13)

- **New: People tools** — Added `get_people`, `get_project_people`, and `search_people` tools. AI assistants can now look up people by name (partial, case-insensitive) to find their IDs for assigning todos, cards, and other resources. Previously, assigning tasks required knowing numeric person IDs upfront.
- **Fix: `get_people` pagination** — The existing `get_people()` method only returned the first page of results. Now handles Basecamp pagination to return all people.

### v1.1.0 (2026-03-11)

- **Fix: `get_projects` now returns all projects** — Previously, only the first page (up to 15 projects) was returned because the Basecamp API paginates list endpoints. The `get_projects()` method in `basecamp_client.py` now iterates through all pages using the `Link` header, matching the existing pagination pattern used by `get_todos()` and other list endpoints.

### v1.0.0

- Initial release with 75 MCP tools for Basecamp 3 integration
- Support for Cursor, Codex, and Claude Desktop clients
- OAuth 2.0 and Basic Auth support
- SSE (Server-Sent Events) mode for multi-user hosting

## License

This project is licensed under the MIT License.
