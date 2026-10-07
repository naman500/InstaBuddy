"""Google OAuth for Drive access.

Uses the installed-application loopback flow: a short-lived local HTTP server
receives the redirect from Google. The out-of-band paste-a-code flow is no
longer supported by Google for new clients, so loopback is the correct choice
for a desktop tool.

Scope is ``drive.file`` only. That grants access exclusively to files this
application creates, which is the minimum needed to upload and organise our own
content. It deliberately cannot read the rest of the user's Drive.

Tokens are cached as per-account JSON files with owner-only permissions, never
in SQLite and never in logs.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from google.auth.exceptions import GoogleAuthError, RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from utils.config import DriveAccountConfig
from utils.errors import GoogleDriveAuthenticationError

logger = logging.getLogger(__name__)

#: Minimum scope required to create and manage our own files.
SCOPES = ["https://www.googleapis.com/auth/drive.file"]

_AUTH_URI = "https://accounts.google.com/o/oauth2/auth"
_TOKEN_URI = "https://oauth2.googleapis.com/token"

#: How long to wait for the user to finish consenting in the browser.
DEFAULT_AUTH_TIMEOUT_SECONDS = 180


def token_path(token_dir: Path | str, account_key: str) -> Path:
    """Location of the cached token for one account."""
    safe = "".join(c for c in account_key if c.isalnum() or c in "._-") or "account"
    return Path(token_dir) / f"{safe}.json"


def build_client_config(account: DriveAccountConfig) -> dict[str, Any]:
    """Assemble the OAuth client config that google-auth expects."""
    if not account.is_usable:
        raise GoogleDriveAuthenticationError(
            f"Account {account.key!r} has no client_id configured",
            user_message=(
                f"'{account.display_name}' is not fully configured. Please add its "
                "Google OAuth client details."
            ),
        )
    return {
        "installed": {
            "client_id": account.client_id,
            "client_secret": account.client_secret,
            "auth_uri": _AUTH_URI,
            "token_uri": _TOKEN_URI,
            "redirect_uris": ["http://localhost"],
        }
    }


def is_connected(token_dir: Path | str, account_key: str) -> bool:
    """True if a cached token exists. Does not verify it still works."""
    return token_path(token_dir, account_key).is_file()


def save_credentials(
    token_dir: Path | str, account_key: str, credentials: Credentials
) -> Path:
    """Persist credentials with owner-only permissions."""
    path = token_path(token_dir, account_key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(credentials.to_json(), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:  # pragma: no cover - best effort on exotic filesystems
        logger.debug("Could not tighten permissions on token file")
    logger.info("Stored Google Drive credentials for account=%s", account_key)
    return path


def load_credentials(
    token_dir: Path | str,
    account_key: str,
    account: DriveAccountConfig | None = None,
    *,
    auto_refresh: bool = True,
) -> Credentials | None:
    """Load cached credentials, refreshing them when expired.

    Returns None when no token is cached. Raises only when a cached token exists
    but has become unusable, since that needs the user to reconnect.
    """
    path = token_path(token_dir, account_key)
    if not path.is_file():
        return None

    try:
        info = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GoogleDriveAuthenticationError(
            f"Corrupt token cache for {account_key}: {exc}",
            user_message=(
                "The saved Google Drive connection could not be read. Please "
                "reconnect the account."
            ),
        ) from exc

    # A stored token without a client_secret cannot be refreshed. Fill it back
    # in from config so long-lived sessions keep working.
    if account is not None:
        info.setdefault("client_id", account.client_id)
        info.setdefault("client_secret", account.client_secret)

    try:
        credentials = Credentials.from_authorized_user_info(info, SCOPES)
    except (ValueError, KeyError) as exc:
        raise GoogleDriveAuthenticationError(
            f"Unusable token cache for {account_key}: {exc}",
            user_message=(
                "The saved Google Drive connection is not valid. Please reconnect "
                "the account."
            ),
        ) from exc

    if not auto_refresh:
        return credentials

    if credentials.valid:
        return credentials

    if credentials.expired and credentials.refresh_token:
        try:
            credentials.refresh(Request())
        except RefreshError as exc:
            raise GoogleDriveAuthenticationError(
                f"Refresh failed for {account_key}: {exc}",
                user_message=(
                    "The Google Drive connection has expired and could not be "
                    "renewed. Please reconnect the account."
                ),
            ) from exc
        except GoogleAuthError as exc:
            raise GoogleDriveAuthenticationError(
                f"Auth error refreshing {account_key}: {exc}"
            ) from exc

        save_credentials(token_dir, account_key, credentials)
        logger.info("Refreshed Google Drive credentials for account=%s", account_key)
        return credentials

    raise GoogleDriveAuthenticationError(
        f"Credentials for {account_key} are invalid and cannot be refreshed",
        user_message=(
            "The Google Drive connection is no longer valid. Please reconnect the "
            "account."
        ),
    )


def authorize(
    token_dir: Path | str,
    account: DriveAccountConfig,
    *,
    port: int = 0,
    open_browser: bool = True,
    timeout_seconds: int = DEFAULT_AUTH_TIMEOUT_SECONDS,
) -> Credentials:
    """Run the consent flow and cache the resulting credentials.

    Blocks until the user finishes in their browser or the timeout elapses.
    Intended to be triggered by an explicit button press.
    """
    flow = InstalledAppFlow.from_client_config(build_client_config(account), SCOPES)

    logger.info("Starting Google consent flow for account=%s", account.key)
    try:
        credentials = flow.run_local_server(
            port=port,
            open_browser=open_browser,
            timeout_seconds=timeout_seconds,
            success_message=(
                "Connected. You can close this tab and return to the application."
            ),
        )
    except GoogleAuthError as exc:
        raise GoogleDriveAuthenticationError(
            f"Consent flow failed for {account.key}: {exc}",
            user_message="The Google sign-in did not complete.",
        ) from exc
    except Exception as exc:
        # Covers socket errors, user abandonment and the timeout path.
        raise GoogleDriveAuthenticationError(
            f"Consent flow error for {account.key}: {type(exc).__name__}: {exc}",
            user_message=(
                "The Google sign-in did not complete. Please try connecting again."
            ),
        ) from exc

    if credentials is None:
        raise GoogleDriveAuthenticationError(
            "Consent flow returned no credentials",
            user_message="The Google sign-in did not complete.",
        )

    save_credentials(token_dir, account.key, credentials)
    return credentials


def disconnect(token_dir: Path | str, account_key: str) -> bool:
    """Delete the cached token. Returns True if one was removed."""
    path = token_path(token_dir, account_key)
    if path.is_file():
        path.unlink()
        logger.info("Removed Google Drive credentials for account=%s", account_key)
        return True
    return False
