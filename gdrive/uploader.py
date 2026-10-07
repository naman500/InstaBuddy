"""Google Drive uploads.

Two paths:

* Small files go up in a single request.
* Large files use a resumable session, uploaded chunk by chunk.

Large files are streamed from disk by ``MediaFileUpload``, so a long video is
never read into memory in full. Transient failures are retried with exponential
backoff, and a resumable session is continued rather than restarted wherever
Google allows it.

Local files are never modified or deleted here. A failed upload leaves the
downloaded media untouched.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

from gdrive.client import DriveClient, guess_mime_type, is_retryable, wrap_http_error
from models.upload import UploadResult, UploadStatus
from utils.errors import GoogleDriveUploadError

logger = logging.getLogger(__name__)

#: Files at or below this size use a single request.
SIMPLE_UPLOAD_THRESHOLD = 5 * 1024 * 1024

#: Google requires resumable chunks to be a multiple of 256 KiB.
CHUNK_GRANULARITY = 256 * 1024

DEFAULT_CHUNK_SIZE = 5 * 1024 * 1024
_BACKOFF_BASE_SECONDS = 1.0


@dataclass
class UploadProgress:
    """Snapshot of upload progress, for the UI."""

    filename: str
    bytes_done: int
    bytes_total: int
    file_index: int = 1
    file_count: int = 1

    @property
    def fraction(self) -> float:
        if self.bytes_total <= 0:
            return 0.0
        return min(self.bytes_done / self.bytes_total, 1.0)

    @property
    def percent(self) -> int:
        return int(self.fraction * 100)

    @property
    def message(self) -> str:
        return f"Uploading {self.filename} ({self.percent}%)"


ProgressCallback = Callable[[UploadProgress], None]


def normalize_chunk_size(chunk_size: int) -> int:
    """Round a chunk size up to Google's required 256 KiB granularity."""
    if chunk_size < CHUNK_GRANULARITY:
        return CHUNK_GRANULARITY
    remainder = chunk_size % CHUNK_GRANULARITY
    if remainder == 0:
        return chunk_size
    return chunk_size + (CHUNK_GRANULARITY - remainder)


def chunk_count(file_size: int, chunk_size: int) -> int:
    """Number of chunks a file will be split into."""
    if file_size <= 0 or chunk_size <= 0:
        return 0
    return -(-file_size // chunk_size)


class DriveUploader:
    """Uploads local files into a resolved Drive folder."""

    def __init__(
        self,
        client: DriveClient,
        *,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        max_retries: int = 3,
        simple_threshold: int = SIMPLE_UPLOAD_THRESHOLD,
    ) -> None:
        self.client = client
        self.chunk_size = normalize_chunk_size(int(chunk_size))
        self.max_retries = max(1, min(int(max_retries), 10))
        self.simple_threshold = int(simple_threshold)

    # ----------------------------------------------------------------- public

    def upload_file(
        self,
        local_path: Path | str,
        folder_id: str,
        *,
        remote_path: str = "",
        progress: ProgressCallback | None = None,
        file_index: int = 1,
        file_count: int = 1,
    ) -> UploadResult:
        """Upload one file, returning its outcome.

        Never raises for an upload failure: the failure is captured in the
        returned :class:`UploadResult` so the caller can record it, keep the
        local file and offer a retry.
        """
        path = Path(local_path)
        display_remote = remote_path or f"{folder_id}/{path.name}"

        result = UploadResult(
            account=self.client.account.key,
            filename=path.name,
            remote_path=display_remote,
            status=UploadStatus.PENDING,
        )

        if not path.is_file():
            result.status = UploadStatus.FAILED
            result.error_message = "The local file is missing."
            logger.error("Upload failed: missing local file %s", path)
            return result

        size = path.stat().st_size
        use_resumable = size > self.simple_threshold

        logger.info(
            "Upload started file=%s bytes=%d mode=%s chunks=%d",
            path.name,
            size,
            "resumable" if use_resumable else "simple",
            chunk_count(size, self.chunk_size) if use_resumable else 1,
        )

        try:
            if use_resumable:
                file_id = self._upload_resumable(
                    path,
                    folder_id,
                    size,
                    progress=progress,
                    file_index=file_index,
                    file_count=file_count,
                )
            else:
                file_id = self._upload_simple(path, folder_id)
                if progress is not None:
                    progress(
                        UploadProgress(path.name, size, size, file_index, file_count)
                    )

            result.status = UploadStatus.UPLOADED
            result.drive_file_id = file_id
            result.uploaded_at = datetime.now(timezone.utc)
            logger.info("Upload completed file=%s id=%s", path.name, file_id)

        except GoogleDriveUploadError as exc:
            result.status = UploadStatus.FAILED
            result.error_message = exc.user_message
            logger.error("Upload failed file=%s: %s", path.name, exc)
        except Exception as exc:
            result.status = UploadStatus.FAILED
            result.error_message = getattr(
                exc, "user_message", "The upload did not complete."
            )
            logger.error(
                "Upload failed file=%s: %s: %s", path.name, type(exc).__name__, exc
            )

        return result

    def upload_files(
        self,
        paths: list[Path],
        folder_id: str,
        *,
        remote_prefix: str = "",
        progress: ProgressCallback | None = None,
    ) -> list[UploadResult]:
        """Upload several files, continuing past individual failures.

        Continuing matters: one failed cover image should not prevent the video
        and metadata from reaching Drive.
        """
        results: list[UploadResult] = []
        total = len(paths)
        for index, path in enumerate(paths, start=1):
            remote = f"{remote_prefix}/{Path(path).name}" if remote_prefix else ""
            results.append(
                self.upload_file(
                    path,
                    folder_id,
                    remote_path=remote,
                    progress=progress,
                    file_index=index,
                    file_count=total,
                )
            )
        return results

    # ---------------------------------------------------------------- private

    def _upload_simple(self, path: Path, folder_id: str) -> str:
        """Single-request upload for a small file."""
        media = MediaFileUpload(
            str(path), mimetype=guess_mime_type(path), resumable=False
        )
        metadata = {"name": path.name, "parents": [folder_id]}

        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                created = (
                    self.client.service.files()
                    .create(body=metadata, media_body=media, fields="id")
                    .execute()
                )
                file_id = created.get("id")
                if not file_id:
                    raise GoogleDriveUploadError("Drive returned no file id")
                return file_id

            except HttpError as exc:
                last_error = exc
                if not is_retryable(exc) or attempt >= self.max_retries:
                    raise wrap_http_error(exc, f"Uploading {path.name}") from exc
                self._backoff(attempt, f"HTTP error uploading {path.name}")

            except OSError as exc:
                raise GoogleDriveUploadError(
                    f"Could not read {path.name}: {exc}",
                    user_message=f"'{path.name}' could not be read from disk.",
                ) from exc

        raise GoogleDriveUploadError(f"Upload of {path.name} failed: {last_error}")

    def _upload_resumable(
        self,
        path: Path,
        folder_id: str,
        size: int,
        *,
        progress: ProgressCallback | None = None,
        file_index: int = 1,
        file_count: int = 1,
    ) -> str:
        """Chunked resumable upload for a large file.

        The session is created once. Individual chunk failures are retried
        against the *same* session, so a network blip does not restart the whole
        transfer.
        """
        media = MediaFileUpload(
            str(path),
            mimetype=guess_mime_type(path),
            chunksize=self.chunk_size,
            resumable=True,
        )
        metadata = {"name": path.name, "parents": [folder_id]}

        try:
            request = self.client.service.files().create(
                body=metadata, media_body=media, fields="id"
            )
        except HttpError as exc:
            raise wrap_http_error(exc, f"Starting upload of {path.name}") from exc

        response: dict[str, Any] | None = None
        consecutive_failures = 0

        while response is None:
            try:
                status, response = request.next_chunk()
                consecutive_failures = 0

                if status is not None and progress is not None:
                    progress(
                        UploadProgress(
                            path.name,
                            int(getattr(status, "resumable_progress", 0) or 0),
                            size,
                            file_index,
                            file_count,
                        )
                    )

            except HttpError as exc:
                consecutive_failures += 1
                if not is_retryable(exc) or consecutive_failures >= self.max_retries:
                    raise wrap_http_error(exc, f"Uploading {path.name}") from exc
                self._backoff(
                    consecutive_failures, f"chunk error uploading {path.name}"
                )

            except OSError as exc:
                raise GoogleDriveUploadError(
                    f"Could not read {path.name}: {exc}",
                    user_message=f"'{path.name}' could not be read from disk.",
                ) from exc

        if progress is not None:
            progress(UploadProgress(path.name, size, size, file_index, file_count))

        file_id = (response or {}).get("id")
        if not file_id:
            raise GoogleDriveUploadError(
                f"Drive returned no file id for {path.name}",
                user_message=f"Google Drive did not confirm the upload of '{path.name}'.",
            )
        return file_id

    @staticmethod
    def _backoff(attempt: int, reason: str) -> None:
        delay = _BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
        logger.warning("Drive %s; retrying in %.0fs", reason, delay)
        time.sleep(delay)
