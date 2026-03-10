"""
Auth manager for Basecamp MCP server.
Handles token refresh logic automatically. Supports optional user_id for multi-user mode.
"""

import logging
import token_storage
from basecamp_oauth import BasecampOAuth
from datetime import datetime

logger = logging.getLogger(__name__)

def ensure_authenticated(user_id=None):
    """
    Checks if the current token is valid and refreshes it if necessary.
    When user_id is set, operates on that user's token (multi-user); otherwise legacy single-user.

    Returns:
        bool: True if authenticated (or successfully refreshed), False otherwise.
    """
    token_data = token_storage.get_token(user_id=user_id)
    
    if not token_data or not token_data.get('access_token'):
        logger.error("No token data found. Initial authentication required.")
        return False

    if not token_storage.is_token_expired(user_id=user_id):
        logger.debug("Token is still valid.")
        return True

    # Token is expired, try to refresh
    refresh_token = token_data.get('refresh_token')
    if not refresh_token:
        logger.error("Token expired and no refresh token available.")
        return False

    logger.info("Token expired. Attempting automatic refresh...")
    
    try:
        oauth = BasecampOAuth()
        new_token_data = oauth.refresh_token(refresh_token)
        
        new_access_token = new_token_data.get('access_token')
        new_refresh_token = new_token_data.get('refresh_token') or refresh_token
        expires_in = new_token_data.get('expires_in')
        account_id = token_data.get('account_id')

        token_storage.store_token(
            access_token=new_access_token,
            refresh_token=new_refresh_token,
            expires_in=expires_in,
            account_id=account_id,
            user_id=user_id,
        )
        
        logger.info("Successfully refreshed and stored new tokens.")
        return True
    except Exception as e:
        logger.error(f"Failed to refresh token: {e}")
        return False

if __name__ == "__main__":
    # Can be run as a standalone script to manually force a refresh check
    logging.basicConfig(level=logging.INFO)
    if ensure_authenticated():
        print("Authenticated!")
    else:
        print("Authentication failed.")
