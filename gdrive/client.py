"""Google Drive API v3 client wrapper.

Thin layer over ``googleapiclient`` that keeps the rest of the application free
of Drive API detail, and translates ``HttpError`` into our own exceptions.

No browser automation anywhere: all communication goes through the official API.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from auth.google_auth import load_credentials
from utils.config import DriveAccountConfig
from utils.errors import GoogleDriveAuthenticationError, GoogleDriveError

logger = logging.getLogger(__name__)

FOLDER_MIME = "application/vnd.google-apps.folder"

#: Status codes worth retrying. 429 is rate limiting; 5xx are transient.
RETRYABLE_STATUS = {429, 500, 502, 503, 504}

_MIME_BY_SUFFIX = {
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".json": "application/json",
    ".txt": "text/plain",
}


def guess_mime_type(filename: str | Path) -> str:
    """Best-effort MIME type from the file extension."""
    return _MIME_BY_SUFFIX.get(Path(filename).suffix.lower(), "application/octet-stream")


def is_retryable(error: HttpError) -> bool:
    """True if an ``HttpError`` looks transient."""
    return _status_of(error) in RETRYABLE_STATUS


def _status_of(error: HttpError) -> int | None:
    status = getattr(error, "status_code", None)
    if status is not None:
        return int(status)
    response = getattr(error, "resp", None)
    code = getattr(response, "status", None)
    return int(code) if code is not None else None


def wrap_http_error(error: HttpError, context: str) -> GoogleDriveError:
    """Translate an ``HttpError`` into an application error.

    Authentication problems become a dedicated error type so the UI can point
    the user at the reconnect button rather than a generic failure.
    """
    status = _status_of(error)

    if status in (401, 403):
        # 403 covers both permission problems and quota exhaustion.
        reason = str(error).lower()
        if "quota" in reason or "rate" in reason or "limit" in reason:
            return GoogleDriveError(
                f"{context}: quota or rate limit ({status})",
                user_message=(
                    "Google Drive is rate limiting or has reached a quota. Please "
                    "try again later."
                ),
            )
        return GoogleDriveAuthenticationError(
            f"{context}: not authorised ({status})",
            user_message=(
                "Google Drive refused the request. Please reconnect the account "
                "from the Google Drive page."
            ),
        )

    if status == 404:
        return GoogleDriveError(
            f"{context}: not found",
            user_message="The Google Drive item could not be found.",
        )

    return GoogleDriveError(
        f"{context}: HTTP {status}: {error}",
        user_message="Google Drive reported an error. Please try again.",
    )


class DriveClient:
    """Authenticated Google Drive service for one configured account."""

    def __init__(
        self,
        account: DriveAccountConfig,
        token_dir: Path | str,
        *,
        service: Any = None,
    ) -> None:
        """
        Args:
            account: Configured account details.
            token_dir: Where cached tokens live.
            service: Pre-built service object. Tests inject a fake; production
                leaves this as None so credentials are loaded from cache.
        """
        self.account = account
        self.token_dir = Path(token_dir)

        if service is not None:
            self._service = service
            return

        credentials = load_credentials(token_dir, account.key, account)
        if credentials is None:
            raise GoogleDriveAuthenticationError(
                f"No cached credentials for {account.key}",
                user_message=(
                    f"'{account.display_name}' is not connected yet. Please connect "
                    "it from the Google Drive page."
                ),
            )
        try:
            self._service = build(
                "drive", "v3", credentials=credentials, cache_discovery=False
            )
        except Exception as exc:
            raise GoogleDriveError(
                f"Could not build Drive service: {exc}",
                user_message="Could not connect to Google Drive.",
            ) from exc

    # ------------------------------------------------------------------ props

    @property
    def service(self) -> Any:
        return self._service

    # ---------------------------------------------------------------- queries

    def about(self) -> dict[str, Any]:
        """Account and storage information, for the connection test."""
        try:
            return (
                self._service.about()
                .get(fields="user(displayName,emailAddress),storageQuota(limit,usage)")
                .execute()
            )
        except HttpError as exc:
            raise wrap_http_error(exc, "Reading Drive account info") from exc

    def connection_info(self) -> dict[str, Any]:
        """Display-safe summary of the connection.

        Deliberately returns no tokens, client IDs or tenant identifiers.
        """
        info = self.about()
        user = info.get("user") or {}
        quota = info.get("storageQuota") or {}
        return {
            "display_name": self.account.display_name,
            "account_name": user.get("displayName") or "",
            "account_email": user.get("emailAddress") or "",
            "storage_used": _safe_int(quota.get("usage")),
            "storage_limit": _safe_int(quota.get("limit")),
        }

    def find_child(
        self, name: str, parent_id: str | None = None, *, folders_only: bool = False
    ) -> dict[str, Any] | None:
        """Find a non-trashed child by exact name.

        With the ``drive.file`` scope this only ever sees items this application
        created, which is exactly what we want.
        """
        clauses = [f"name = '{_escape(name)}'", "trashed = false"]
        if folders_only:
            clauses.append(f"mimeType = '{FOLDER_MIME}'")
        clauses.append(f"'{_escape(parent_id or 'root')}' in parents")

        try:
            response = (
                self._service.files()
                .list(
                    q=" and ".join(clauses),
                    spaces="drive",
                    fields="files(id,name,mimeType,size)",
                    pageSize=10,
                )
                .execute()
            )
        except HttpError as exc:
            raise wrap_http_error(exc, f"Looking up {name!r}") from exc

        files = response.get("files") or []
        return files[0] if files else None

    def create_folder(self, name: str, parent_id: str | None = None) -> dict[str, Any]:
        """Create a folder and return its metadata."""
        metadata: dict[str, Any] = {"name": name, "mimeType": FOLDER_MIME}
        if parent_id:
            metadata["parents"] = [parent_id]

        try:
            folder = (
                self._service.files()
                .create(body=metadata, fields="id,name,mimeType")
                .execute()
            )
        except HttpError as exc:
            raise wrap_http_error(exc, f"Creating folder {name!r}") from exc

        logger.info("Created Drive folder name=%s id=%s", name, folder.get("id"))
        return folder

    def get_file(self, file_id: str) -> dict[str, Any]:
        try:
            return (
                self._service.files()
                .get(fileId=file_id, fields="id,name,size,md5Checksum,webViewLink")
                .execute()
            )
        except HttpError as exc:
            raise wrap_http_error(exc, f"Reading file {file_id}") from exc

    def delete_file(self, file_id: str) -> None:
        try:
            self._service.files().delete(fileId=file_id).execute()
        except HttpError as exc:
            raise wrap_http_error(exc, f"Deleting file {file_id}") from exc

    @staticmethod
    def folder_web_link(folder_id: str) -> str:
        """Browser URL for a Drive folder."""
        return f"https://drive.google.com/drive/folders/{folder_id}"


def _escape(value: str) -> str:
    """Escape a value for use inside a Drive query string literal."""
    return (value or "").replace("\\", "\\\\").replace("'", "\\'")


def _safe_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
