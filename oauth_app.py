"""
Flask application for handling the Basecamp 3 OAuth 2.0 authorization flow.

This application provides endpoints for:
1. Redirecting users to Basecamp for authorization
2. Handling the OAuth callback
3. Using the obtained token to access the Basecamp API
4. Providing a secure token endpoint for the MCP server
"""

import os
import sys
import json
import secrets
import time
import logging
from flask import Flask, request, redirect, url_for, session, render_template_string, jsonify
from dotenv import load_dotenv
from basecamp_oauth import BasecampOAuth
from basecamp_client import BasecampClient
from search_utils import BasecampSearch
import token_storage
import user_store

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("oauth_app.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()

# Check for required environment variables
required_vars = ['BASECAMP_CLIENT_ID', 'BASECAMP_CLIENT_SECRET', 'BASECAMP_REDIRECT_URI', 'USER_AGENT']
missing_vars = [var for var in required_vars if not os.getenv(var)]
if missing_vars:
    logger.error(f"Missing required environment variables: {', '.join(missing_vars)}")
    logger.error("Please set these variables in your .env file or environment")
    sys.exit(1)

# Create Flask app
app = Flask(__name__)
app.secret_key = os.getenv('FLASK_SECRET_KEY', secrets.token_hex(16))

# UI templates: base layout and pages (richer UI with copy-config, from V1)
BASE_LAYOUT = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{{ page_title | default('Basecamp MCP') }}</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=DM+Sans:ital,opsz,wght@0,9..40,400;0,9..40,500;0,9..40,600;0,9..40,700&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg: #0f1419;
            --surface: #1a2332;
            --surface-hover: #243044;
            --border: #2d3a4d;
            --text: #e6edf3;
            --text-muted: #8b9eb5;
            --accent: #3b82f6;
            --accent-hover: #2563eb;
            --success: #22c55e;
            --success-bg: rgba(34, 197, 94, 0.12);
            --warn: #eab308;
            --warn-bg: rgba(234, 179, 8, 0.12);
            --error: #ef4444;
            --error-bg: rgba(239, 68, 68, 0.12);
            --radius: 12px;
            --radius-sm: 8px;
            --shadow: 0 4px 24px rgba(0,0,0,0.25);
        }
        * { box-sizing: border-box; }
        body {
            font-family: 'DM Sans', -apple-system, sans-serif;
            margin: 0;
            min-height: 100vh;
            background: var(--bg);
            color: var(--text);
            line-height: 1.6;
            -webkit-font-smoothing: antialiased;
        }
        .page {
            max-width: 480px;
            margin: 0 auto;
            padding: 48px 24px;
        }
        .logo {
            font-size: 1.5rem;
            font-weight: 700;
            letter-spacing: -0.02em;
            margin-bottom: 8px;
            color: var(--text);
        }
        .logo span { color: var(--accent); }
        .subtitle { color: var(--text-muted); font-size: 0.95rem; margin-bottom: 32px; }
        .card {
            background: var(--surface);
            border: 1px solid var(--border);
            border-radius: var(--radius);
            padding: 28px;
            margin-bottom: 24px;
            box-shadow: var(--shadow);
        }
        .card h2 { font-size: 1.15rem; margin: 0 0 16px; font-weight: 600; }
        .card p { margin: 0 0 12px; color: var(--text-muted); font-size: 0.9rem; }
        .btn {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            gap: 8px;
            padding: 14px 24px;
            font-family: inherit;
            font-size: 0.95rem;
            font-weight: 600;
            border: none;
            border-radius: var(--radius-sm);
            cursor: pointer;
            text-decoration: none;
            transition: background 0.2s, transform 0.05s;
        }
        .btn:active { transform: scale(0.98); }
        .btn-primary {
            background: var(--accent);
            color: white;
            width: 100%;
        }
        .btn-primary:hover { background: var(--accent-hover); }
        .btn-secondary {
            background: var(--surface-hover);
            color: var(--text);
            border: 1px solid var(--border);
        }
        .btn-secondary:hover { background: var(--border); }
        .btn-sm {
            padding: 8px 14px;
            font-size: 0.85rem;
        }
        .alert {
            padding: 14px 18px;
            border-radius: var(--radius-sm);
            margin-bottom: 20px;
            font-size: 0.9rem;
        }
        .alert-warn { background: var(--warn-bg); color: var(--warn); border: 1px solid rgba(234,179,8,0.3); }
        .alert-error { background: var(--error-bg); color: var(--error); border: 1px solid rgba(239,68,68,0.3); }
        .alert-success { background: var(--success-bg); color: var(--success); border: 1px solid rgba(34,197,94,0.3); }
        .value-box {
            background: var(--bg);
            border: 1px solid var(--border);
            border-radius: var(--radius-sm);
            padding: 12px 14px;
            font-family: ui-monospace, monospace;
            font-size: 0.8rem;
            word-break: break-all;
            margin: 8px 0 12px;
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 12px;
        }
        .value-box code { flex: 1; min-width: 0; }
        .copy-btn {
            flex-shrink: 0;
            padding: 6px 12px;
            font-size: 0.75rem;
            background: var(--surface-hover);
            color: var(--text-muted);
            border: 1px solid var(--border);
            border-radius: 6px;
            cursor: pointer;
        }
        .copy-btn:hover { color: var(--text); background: var(--border); }
        .copy-btn.copied { background: var(--success-bg); color: var(--success); }
        .label { font-size: 0.8rem; font-weight: 600; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.04em; margin-bottom: 4px; }
        .actions { margin-top: 24px; display: flex; flex-wrap: wrap; gap: 12px; }
        pre { background: var(--bg); border: 1px solid var(--border); border-radius: var(--radius-sm); padding: 14px; overflow-x: auto; font-size: 0.85rem; margin: 8px 0; }
    </style>
</head>
<body>
    <div class="page">
        {% block body %}{% endblock %}
    </div>
    {% block script %}{% endblock %}
</body>
</html>
"""

# Backwards-compatible template with copy-config UI
RESULTS_TEMPLATE = BASE_LAYOUT.replace("{% block body %}{% endblock %}", """
        <p class="logo">Basecamp <span>MCP</span></p>
        <p class="subtitle">{{ title }}</p>
        {% if message %}<p style="margin-bottom:20px;">{{ message }}</p>{% endif %}
        {% if warning %}<div class="alert alert-warn">{{ warning }}</div>{% endif %}
        {% if content %}<pre>{{ content }}</pre>{% endif %}
        {% if auth_url %}
            <div class="card">
                <a href="{{ auth_url }}" class="btn btn-primary">Connect with Basecamp</a>
            </div>
        {% endif %}
        {% if token_info %}
            <div class="card">
                <h2>OAuth Token</h2>
                <pre>{{ token_info | tojson(indent=2) }}</pre>
            </div>
        {% endif %}
        {% if api_key_success %}
            <div class="card" style="border-color: rgba(34,197,94,0.4);">
                <h2 style="color: var(--success);">You’re connected</h2>
                <p>Use this in Cursor: paste into your project’s <code>.cursor/mcp.json</code> (or merge into <code>mcpServers</code>). Save your API key; it won’t be shown again.</p>
                <div class="label">Copy this block into Cursor MCP config</div>
                <div style="background: var(--bg); border: 1px solid var(--border); border-radius: var(--radius-sm); overflow: hidden; margin: 8px 0 12px;">
                    <pre id="mcp-config" style="margin:0; padding:14px; font-size:0.8rem; white-space:pre; overflow:auto; max-height:200px;">{{ mcp_config_json | e }}</pre>
                    <button type="button" class="copy-btn" data-copy="mcp-config" style="margin: 12px 14px;">Copy config</button>
                </div>
                <p style="margin-top:12px; font-size:0.85rem;">Then reload Cursor (Ctrl+Shift+P → Developer: Reload Window).</p>
                <div class="actions"><a href="/help" class="btn btn-secondary">Instructions</a><a href="/" class="btn btn-secondary">Back to home</a></div>
            </div>
        {% endif %}
        {% if show_logout or show_home %}
            <div class="actions">
                {% if show_logout %}<a href="/logout" class="btn btn-secondary">Log out</a>{% endif %}
                {% if show_home %}<a href="/" class="btn btn-secondary">Home</a>{% endif %}
            </div>
        {% endif %}
""").replace("{% block script %}{% endblock %}", """
<script>
(function () {
    function fallbackCopy(text, btn) {
        try {
            var textarea = document.createElement('textarea');
            textarea.value = text;
            textarea.setAttribute('readonly', '');
            textarea.style.position = 'fixed';
            textarea.style.opacity = '0';
            document.body.appendChild(textarea);
            textarea.select();
            document.execCommand('copy');
            document.body.removeChild(textarea);
            if (btn) {
                btn.textContent = 'Copied';
                btn.classList.add('copied');
                setTimeout(function () {
                    btn.textContent = 'Copy';
                    btn.classList.remove('copied');
                }, 2000);
            }
        } catch (e) {
            console.warn('Copy failed', e);
        }
    }

    function copyText(text, btn) {
        if (navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard.writeText(text).then(function () {
                if (btn) {
                    btn.textContent = 'Copied';
                    btn.classList.add('copied');
                    setTimeout(function () {
                        btn.textContent = 'Copy';
                        btn.classList.remove('copied');
                    }, 2000);
                }
            }).catch(function () {
                fallbackCopy(text, btn);
            });
        } else {
            fallbackCopy(text, btn);
        }
    }

    document.addEventListener('click', function (event) {
        var btn = event.target.closest('.copy-btn');
        if (!btn) return;
        var id = btn.getAttribute('data-copy');
        var el = document.getElementById(id);
        var text = el ? el.textContent : '';
        if (!text) return;
        copyText(text, btn);
    });
})();
</script>
""")

# Sign-up / landing page UI (multi-user)
SIGNUP_PAGE_TEMPLATE = BASE_LAYOUT.replace("{% block body %}{% endblock %}", """
        <p class="logo">Basecamp <span>MCP</span></p>
        <p class="subtitle">Connect Basecamp to Cursor and other MCP clients.</p>
        <div class="card">
            <h2>Sign up</h2>
            <p>Link your Basecamp account once and get a personal API key. Use it in Cursor (or any MCP client) to access your projects, todos, and more.</p>
            <a href="{{ url }}" class="btn btn-primary">Sign up with Basecamp</a>
        </div>
        <p style="color: var(--text-muted); font-size: 0.85rem;">You’ll be redirected to Basecamp to authorize, then return here to receive your API key.</p>
        <p style="margin-top: 24px;"><a href="/help" style="color: var(--accent); font-size: 0.9rem;">How to connect to MCP (instructions)</a></p>
""").replace("{% block script %}{% endblock %}", "")

# Help / instructions page (connecting to MCP from UI)
HELP_PAGE_TEMPLATE = BASE_LAYOUT.replace("{% block body %}{% endblock %}", """
        <p class="logo">Basecamp <span>MCP</span></p>
        <p class="subtitle">Instructions for connecting to MCP</p>
        <div class="card" style="max-width: 100%;">
            <h2>1. Open the signup page</h2>
            <p>Go to this app’s home page and click <strong>Sign up with Basecamp</strong>.</p>
        </div>
        <div class="card" style="max-width: 100%;">
            <h2>2. Authorize with Basecamp</h2>
            <p>You’ll be redirected to Basecamp to sign in and allow access. After you approve, you’ll return here.</p>
        </div>
        <div class="card" style="max-width: 100%;">
            <h2>3. Copy your config</h2>
            <p>On the success page, click <strong>Copy config</strong> to copy the JSON block. Save your API key if shown; it won’t be shown again.</p>
        </div>
        <div class="card" style="max-width: 100%;">
            <h2>4. Add the server in Cursor</h2>
            <p>Open your project’s <code>.cursor/mcp.json</code> (or Cursor Settings → MCP). Paste the copied config so the <code>basecamp</code> entry is inside <code>mcpServers</code>. If you already have other servers, merge the <code>basecamp</code> block in; don’t remove others.</p>
        </div>
        <div class="card" style="max-width: 100%;">
            <h2>5. Reload and verify</h2>
            <p>Reload Cursor (Ctrl+Shift+P → Developer: Reload Window). In Settings → Tools &amp; MCP you should see <strong>basecamp</strong> with all tools available. Try asking the chat to list your Basecamp projects.</p>
        </div>
        <div class="card" style="max-width: 100%;">
            <h2>Troubleshooting</h2>
            <p><strong>Copy button doesn’t work?</strong> Select the JSON in the box and copy manually (Ctrl+C).</p>
            <p><strong>401 Unauthorized?</strong> Check that the <code>Authorization: Bearer YOUR_API_KEY</code> header is set exactly, and the URL matches your SSE server.</p>
            <p><strong>Token expired?</strong> Visit this app again and complete the Basecamp signup flow to refresh.</p>
        </div>
        <div class="actions"><a href="/" class="btn btn-secondary">Back to home</a></div>
""").replace("{% block script %}{% endblock %}", "")

# Legacy login card (single-user, no users yet)
LOGIN_PAGE_TEMPLATE = BASE_LAYOUT.replace("{% block body %}{% endblock %}", """
        <p class="logo">Basecamp <span>MCP</span></p>
        <p class="subtitle">Log in with your Basecamp account.</p>
        <div class="card">
            <h2>Log in</h2>
            <p>Authorize this app to use your Basecamp account with the MCP server.</p>
            <a href="{{ url }}" class="btn btn-primary">Log in with Basecamp</a>
        </div>
""").replace("{% block script %}{% endblock %}", "")

# Pending OAuth state for multi-user flow (state -> create user on callback). Expire after 600s.
PENDING_OAUTH = {}
PENDING_OAUTH_TTL = 600

def _pending_state_valid(state):
    if not state or state not in PENDING_OAUTH:
        return False
    created = PENDING_OAUTH[state].get("created_at", 0)
    if time.time() - created > PENDING_OAUTH_TTL:
        del PENDING_OAUTH[state]
        return False
    return True

@app.template_filter('tojson')
def to_json(value, indent=None):
    return json.dumps(value, indent=indent)

def get_oauth_client(redirect_uri=None):
    """Get a configured OAuth client. Uses request host for redirect_uri when in request context."""
    try:
        client_id = os.getenv('BASECAMP_CLIENT_ID')
        client_secret = os.getenv('BASECAMP_CLIENT_SECRET')
        user_agent = os.getenv('USER_AGENT')
        if redirect_uri is None and request:
            redirect_uri = url_for("auth_callback", _external=True)
        if redirect_uri is None:
            redirect_uri = os.getenv('BASECAMP_REDIRECT_URI')
        logger.info("Creating OAuth client with redirect_uri: %s", redirect_uri)
        return BasecampOAuth(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri,
            user_agent=user_agent
        )
    except Exception as e:
        logger.error("Error creating OAuth client: %s", str(e))
        raise

def ensure_valid_token():
    """
    Ensure we have a valid, non-expired token. 
    Attempts to refresh if expired.
    
    Returns:
        dict: Valid token data or None if authentication is needed
    """
    token_data = token_storage.get_token()
    
    if not token_data or not token_data.get('access_token'):
        logger.info("No token found")
        return None
    
    # Check if token is expired
    if token_storage.is_token_expired():
        logger.info("Token is expired, attempting to refresh")
        
        refresh_token = token_data.get('refresh_token')
        if not refresh_token:
            logger.warning("No refresh token available, user needs to re-authenticate")
            return None
        
        try:
            oauth_client = get_oauth_client()
            new_token_data = oauth_client.refresh_token(refresh_token)
            
            # Store the new token
            access_token = new_token_data.get('access_token')
            new_refresh_token = new_token_data.get('refresh_token', refresh_token)  # Use old refresh token if new one not provided
            expires_in = new_token_data.get('expires_in')
            account_id = token_data.get('account_id')  # Keep the existing account_id
            
            if access_token:
                token_storage.store_token(
                    access_token=access_token,
                    refresh_token=new_refresh_token,
                    expires_in=expires_in,
                    account_id=account_id
                )
                logger.info("Token refreshed successfully")
                return token_storage.get_token()
            else:
                logger.error("No access token in refresh response")
                return None
                
        except Exception as e:
            logger.error("Failed to refresh token: %s", str(e))
            return None
    
    logger.info("Token is valid")
    return token_data

@app.route('/signup')
def signup():
    """Sign-up page (multi-user): link Basecamp and get an API key."""
    return render_template_string(
        SIGNUP_PAGE_TEMPLATE,
        page_title="Sign up – Basecamp MCP",
        url=url_for("link_basecamp"),
    )

@app.route('/link-basecamp')
def link_basecamp():
    """Start multi-user OAuth flow: redirect to Basecamp with state; callback creates user and shows API key."""
    try:
        state = secrets.token_urlsafe(32)
        PENDING_OAUTH[state] = {"created_at": time.time()}
        oauth_client = get_oauth_client()
        auth_url = oauth_client.get_authorization_url(state=state)
        return redirect(auth_url)
    except Exception as e:
        logger.error("Error starting link-basecamp: %s", str(e))
        return render_template_string(
            RESULTS_TEMPLATE,
            title="Error",
            message=f"Error setting up OAuth: {str(e)}",
            show_home=True,
        )

@app.route('/')
def home():
    """Home page: show multi-user sign-up UI."""
    return render_template_string(
        SIGNUP_PAGE_TEMPLATE,
        page_title="Sign up – Basecamp MCP",
        url=url_for("link_basecamp"),
    )

@app.route('/help')
def help_page():
    """Instructions for connecting to MCP (from the UI point of view)."""
    return render_template_string(
        HELP_PAGE_TEMPLATE,
        page_title="How to connect – Basecamp MCP",
    )

@app.route('/legacy')
def legacy_home():
    """Legacy home: show token status or login (single-user)."""
    token_data = ensure_valid_token()

    if token_data and token_data.get('access_token'):
        # We have a valid token, show token information
        access_token = token_data['access_token']
        # Mask the token for security
        masked_token = f"{access_token[:10]}...{access_token[-10:]}" if len(access_token) > 20 else "***"

        token_info = {
            "access_token": masked_token,
            "account_id": token_data.get('account_id'),
            "has_refresh_token": bool(token_data.get('refresh_token')),
            "expires_at": token_data.get('expires_at'),
            "updated_at": token_data.get('updated_at')
        }

        logger.info("Home page: User is authenticated")

        return render_template_string(
            RESULTS_TEMPLATE,
            title="Basecamp OAuth Status",
            message="You are authenticated with Basecamp!",
            token_info=token_info,
            show_logout=True
        )
    else:
        # No valid token, show login button
        try:
            oauth_client = get_oauth_client()
            auth_url = oauth_client.get_authorization_url()

            logger.info("Home page: User not authenticated, showing login button")

            return render_template_string(
                RESULTS_TEMPLATE,
                title="Basecamp OAuth Demo",
                message="Welcome! Please log in with your Basecamp account to continue.",
                auth_url=auth_url
            )
        except Exception as e:
            logger.error("Error getting authorization URL: %s", str(e))
            return render_template_string(
                RESULTS_TEMPLATE,
                title="Error",
                message=f"Error setting up OAuth: {str(e)}",
            )

@app.route('/auth/callback')
def auth_callback():
    """Handle the OAuth callback from Basecamp. Multi-user: state present -> create user, show API key. Legacy: no state -> store global token."""
    logger.info("OAuth callback called with args: %s", request.args)

    code = request.args.get('code')
    error = request.args.get('error')
    state = request.args.get('state')

    if error:
        logger.error("OAuth callback error: %s", error)
        return render_template_string(
            RESULTS_TEMPLATE,
            title="Authentication Error",
            message=f"Basecamp returned an error: {error}",
            show_home=True
        )

    if not code:
        logger.error("OAuth callback: No code provided")
        return render_template_string(
            RESULTS_TEMPLATE,
            title="Error",
            message="No authorization code received.",
            show_home=True
        )

    try:
        oauth_client = get_oauth_client()
        logger.info("Exchanging code for token")
        token_data = oauth_client.exchange_code_for_token(code)
        logger.info("Raw token data from Basecamp exchange: %s", token_data)

        access_token = token_data.get('access_token')
        refresh_token = token_data.get('refresh_token')
        expires_in = token_data.get('expires_in')
        account_id = token_data.get('account_id') or os.getenv('BASECAMP_ACCOUNT_ID')

        if not access_token:
            logger.error("OAuth exchange: No access token received")
            return render_template_string(
                RESULTS_TEMPLATE,
                title="Authentication Error",
                message="No access token received from Basecamp.",
                show_home=True
            )

        # Get identity for account_id and email
        email = None
        try:
            logger.info("Getting user identity for account_id and email")
            identity = oauth_client.get_identity(access_token)
            if not account_id and identity.get('accounts'):
                for account in identity['accounts']:
                    if account.get('product') == 'bc3':
                        account_id = str(account['id'])
                        break
            if not account_id:
                account_id = os.getenv('BASECAMP_ACCOUNT_ID')
            ident = identity.get("identity") or {}
            email = ident.get("email_address") or ident.get("email") or identity.get("email")
        except Exception as identity_error:
            logger.error("Error getting identity: %s", identity_error)
            if not account_id:
                account_id = os.getenv('BASECAMP_ACCOUNT_ID')

        # Multi-user flow: state was set by /link-basecamp
        if state and _pending_state_valid(state):
            del PENDING_OAUTH[state]
            user_id, api_key = user_store.create_user(email=email)
            stored = token_storage.store_token(
                access_token=access_token,
                refresh_token=refresh_token,
                expires_in=expires_in,
                account_id=account_id,
                user_id=user_id,
            )
            if not stored:
                logger.error("Failed to store token for new user")
                return render_template_string(
                    RESULTS_TEMPLATE,
                    title="Error",
                    message="Failed to store token. Please try again.",
                    show_home=True,
                )
            sse_url = os.getenv("MCP_SSE_URL", "http://localhost:8010").rstrip("/")
            mcp_config = {
                "mcpServers": {
                    "basecamp": {
                        "url": sse_url + "/",
                        "headers": {"Authorization": f"Bearer {api_key}"},
                    }
                }
            }
            mcp_config_json = json.dumps(mcp_config, indent=2)
            return render_template_string(
                RESULTS_TEMPLATE,
                title="Connected",
                message="You can now use the MCP server with the details below. Save your API key; it won't be shown again.",
                api_key_success=True,
                api_key=api_key,
                sse_url=sse_url,
                mcp_config_json=mcp_config_json,
                show_home=True,
            )

        if state and not _pending_state_valid(state):
            return render_template_string(
                RESULTS_TEMPLATE,
                title="Link expired",
                message="This link has expired. Please start again from the home page.",
                show_home=True,
            )

        # Legacy: store in file for single-user stdio
        logger.info("Storing token (legacy) with account_id: %s", account_id)
        stored = token_storage.store_token(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_in=expires_in,
            account_id=account_id
        )
        if not stored:
            logger.error("Failed to store token")
            return render_template_string(
                RESULTS_TEMPLATE,
                title="Error",
                message="Failed to store token. Please try again.",
                show_home=True
            )
        session['access_token'] = access_token
        if refresh_token:
            session['refresh_token'] = refresh_token
        if account_id:
            session['account_id'] = account_id
        logger.info("OAuth flow completed successfully (legacy)")
        return redirect(url_for('legacy_home'))
    except Exception as e:
        logger.error("Error in OAuth callback: %s", str(e), exc_info=True)
        return render_template_string(
            RESULTS_TEMPLATE,
            title="Error",
            message=f"Failed to exchange code for token: {str(e)}",
            show_home=True
        )

@app.route('/api/token', methods=['GET'])
def get_token_api():
    """
    Secure API endpoint for the MCP server to get the token.
    This should only be accessible by the MCP server.
    """
    logger.info("Token API called with headers: %s", request.headers)

    # In production, implement proper authentication for this endpoint
    # For now, we'll use a simple API key check
    api_key = request.headers.get('X-API-Key')
    if not api_key or api_key != os.getenv('MCP_API_KEY', 'mcp_secret_key'):
        logger.error("Token API: Invalid API key")
        return jsonify({
            "error": "Unauthorized",
            "message": "Invalid or missing API key"
        }), 401

    # Use the ensure_valid_token function to get a fresh token
    token_data = ensure_valid_token()
    if not token_data or not token_data.get('access_token'):
        logger.error("Token API: No valid token available")
        return jsonify({
            "error": "Not authenticated",
            "message": "No valid token available"
        }), 404

    logger.info("Token API: Successfully returned token")
    return jsonify({
        "access_token": token_data['access_token'],
        "account_id": token_data.get('account_id')
    })

@app.route('/logout')
def logout():
    """Clear the session and token storage."""
    logger.info("Logout called")
    session.clear()
    token_storage.clear_tokens()
    return redirect(url_for('home'))

@app.route('/token/info')
def token_info():
    """Display information about the stored token."""
    logger.info("Token info called")
    token_data = token_storage.get_token()

    if not token_data:
        logger.info("Token info: No token stored")
        return render_template_string(
            RESULTS_TEMPLATE,
            title="Token Information",
            message="No token stored.",
            show_home=True
        )

    # Check if token is expired
    is_expired = token_storage.is_token_expired()
    
    # Mask the tokens for security
    access_token = token_data.get('access_token', '')
    refresh_token = token_data.get('refresh_token', '')

    masked_access = f"{access_token[:10]}...{access_token[-10:]}" if len(access_token) > 20 else "***"
    masked_refresh = f"{refresh_token[:10]}...{refresh_token[-10:]}" if refresh_token and len(refresh_token) > 20 else "***" if refresh_token else None

    display_info = {
        "access_token": masked_access,
        "has_refresh_token": bool(refresh_token),
        "account_id": token_data.get('account_id'),
        "expires_at": token_data.get('expires_at'),
        "updated_at": token_data.get('updated_at'),
        "is_expired": is_expired
    }

    warning_message = None
    if is_expired:
        warning_message = "Warning: Your token is expired! Visit the home page to automatically refresh it, or logout and log back in."

    logger.info("Token info: Returned token info")
    return render_template_string(
        RESULTS_TEMPLATE,
        title="Token Information",
        content=json.dumps(display_info, indent=2),
        warning=warning_message,
        show_home=True
    )

@app.route('/health')
def health_check():
    """Health check endpoint."""
    logger.info("Health check called")
    return jsonify({
        "status": "ok",
        "service": "basecamp-oauth-app"
    })

# One-time migration: legacy oauth_tokens.json -> one user in SQLite (when OAuth app loads)
try:
    api_key = user_store.migrate_legacy_tokens_if_needed()
    if api_key:
        logger.info("Migrated to multi-user. New API key created for legacy token.")
except Exception as e:
    logger.warning("Legacy migration check failed: %s", e)

if __name__ == '__main__':
    try:
        logger.info("Starting OAuth app on port %s", os.environ.get('PORT', 8000))
        # Run the Flask app
        port = int(os.environ.get('PORT', 8000))

        # Disable debug and auto-reloader when running in production or background
        is_debug = os.environ.get('FLASK_DEBUG', 'False').lower() == 'true'

        logger.info("Running in %s mode", "debug" if is_debug else "production")
        app.run(host='0.0.0.0', port=port, debug=is_debug, use_reloader=is_debug)
    except Exception as e:
        logger.error("Fatal error: %s", str(e), exc_info=True)
        sys.exit(1)
