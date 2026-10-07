"""Job lifecycle and History assembly.

Sits between the manifest and the UI. The Streamlit pages ask this service for
display-ready rows and detail bundles; they never build SQL or format status
strings themselves.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from instagram.metadata import DOCUMENT_FILENAME, METADATA_FILENAME, read_metadata
from models.job import Job, JobStatus, JobSummary
from models.media import DestinationMode, MediaSelection
from models.upload import UploadStatus, _human_bytes
from services.manifest import Manifest
from utils.errors import AppError

logger = logging.getLogger(__name__)


class JobService:
    """Creates, advances and reports on ingestion jobs."""

    def __init__(self, manifest: Manifest, download_root: Path | str) -> None:
        self.manifest = manifest
        self.download_root = Path(download_root)

    # ------------------------------------------------------------- lifecycle

    def start_job(
        self,
        *,
        created_by: str,
        requested_url: str,
        shortcode: str | None = None,
        requested_media: MediaSelection | None = None,
        destination_mode: DestinationMode = DestinationMode.LOCAL_AND_DRIVE,
        drive_account: str | None = None,
        attempt_count: int = 1,
    ) -> Job:
        """Record a new job before any network activity begins."""
        job = Job(
            created_by=created_by,
            requested_url=requested_url,
            shortcode=shortcode,
            requested_media=requested_media,
            destination_mode=destination_mode,
            drive_account=drive_account,
            attempt_count=attempt_count,
            status=JobStatus.QUEUED,
        )
        return self.manifest.create_job(job)

    def advance(self, job_id: str, status: JobStatus, step: str | None = None) -> None:
        """Move a job to a non-terminal stage."""
        self.manifest.set_job_status(job_id, status, step=step)

    def finish(
        self,
        job_id: str,
        status: JobStatus,
        *,
        error: Exception | None = None,
        message: str | None = None,
    ) -> None:
        """Close a job out.

        The friendly message goes to ``error_message`` and the technical detail
        to ``error_detail``, so the UI can show one and hide the other.
        """
        friendly = message
        detail = None
        if error is not None:
            friendly = friendly or getattr(error, "user_message", None) or str(error)
            detail = f"{type(error).__name__}: {error}"

        self.manifest.set_job_status(
            job_id,
            status,
            error_message=friendly,
            error_detail=detail,
        )

    def set_shortcode(self, job_id: str, shortcode: str) -> None:
        self.manifest.update_job(job_id, shortcode=shortcode)

    def set_selection(self, job_id: str, selection: MediaSelection) -> None:
        self.manifest.update_job(job_id, requested_media=selection)

    # --------------------------------------------------------------- reporting

    def summary(self) -> JobSummary:
        return self.manifest.job_summary()

    def filter_options(self) -> dict[str, list[str]]:
        """Values to populate the History filter controls."""
        return {
            "username": self.manifest.distinct_values("username"),
            "content_type": self.manifest.distinct_values("content_type"),
            "drive_account": self.manifest.distinct_values("drive_account"),
            "created_by": self.manifest.distinct_values("created_by"),
        }

    def history_rows(
        self,
        *,
        limit: int = 200,
        offset: int = 0,
        status: JobStatus | None = None,
        username: str | None = None,
        content_type: str | None = None,
        drive_account: str | None = None,
        created_by: str | None = None,
        search: str | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Display-ready history rows, newest first."""
        raw = self.manifest.list_jobs(
            limit=limit,
            offset=offset,
            status=status,
            username=username,
            content_type=content_type,
            drive_account=drive_account,
            created_by=created_by,
            search=search,
            date_from=date_from,
            date_to=date_to,
        )
        return [self._format_row(row) for row in raw]

    def _format_row(self, row: dict[str, Any]) -> dict[str, Any]:
        status = _safe_status(row.get("status"))
        file_count = int(row.get("file_count") or 0)
        uploaded = int(row.get("uploaded_count") or 0)
        failed = int(row.get("failed_upload_count") or 0)

        return {
            "job_id": row.get("id"),
            "date": _format_datetime(row.get("started_at")),
            "account": row.get("created_by") or "",
            "username": row.get("username") or "-",
            "type": _content_label(row.get("content_type")),
            "shortcode": row.get("shortcode") or "-",
            "files": file_count,
            "size": _human_bytes(int(row.get("total_bytes") or 0)),
            "drive": row.get("drive_account") or "-",
            "upload": _upload_label(file_count, uploaded, failed, row),
            "status": status.label if status else str(row.get("status") or ""),
            "status_value": status.value if status else "",
            "post_id": row.get("post_id"),
            "error_message": row.get("error_message"),
        }

    # ------------------------------------------------------------------ detail

    def job_detail(self, job_id: str) -> dict[str, Any]:
        """Everything the detail view needs for one job.

        Includes the stored metadata and document text, plus a flag for any
        file recorded in the manifest that is no longer present on disk.
        """
        job = self.manifest.get_job(job_id)
        if job is None:
            raise AppError(
                f"Unknown job {job_id}",
                user_message="That job could not be found.",
            )

        post = self.manifest.get_post_for_job(job_id)
        media: list[dict[str, Any]] = []
        metadata: dict[str, Any] = {}
        document_text = ""
        directory: Path | None = None

        if post:
            directory = self._resolve_directory(post.get("local_directory"))
            media = [
                self._format_media(entry, directory)
                for entry in self.manifest.list_media_files(int(post["id"]))
            ]
            if directory is not None:
                metadata = read_metadata(directory / METADATA_FILENAME)
                document_text = _read_text(directory / DOCUMENT_FILENAME)

        return {
            "job": job,
            "post": post,
            "media": media,
            "metadata": metadata,
            "document": document_text,
            "directory": directory,
            "directory_exists": bool(directory and directory.is_dir()),
            "missing_files": [m["filename"] for m in media if not m["exists"]],
            "can_retry_upload": self._can_retry_upload(job, media),
        }

    def _format_media(
        self, entry: dict[str, Any], directory: Path | None
    ) -> dict[str, Any]:
        local_path = self._resolve_media_path(entry.get("relative_path"), directory)
        upload_status = _safe_upload_status(entry.get("upload_status"))

        return {
            "media_file_id": entry.get("id"),
            "filename": entry.get("filename"),
            "media_type": entry.get("media_type"),
            "file_size": int(entry.get("file_size") or 0),
            "size_human": _human_bytes(int(entry.get("file_size") or 0)),
            "sha256": entry.get("sha256") or "",
            "sha256_short": (entry.get("sha256") or "")[:12],
            "local_path": str(local_path) if local_path else "",
            "exists": bool(local_path and local_path.is_file()),
            "remote_path": entry.get("remote_path") or "",
            "drive_file_id": entry.get("drive_file_id") or "",
            "upload_status": (
                upload_status.label if upload_status else UploadStatus.SKIPPED.label
            ),
            "upload_status_value": upload_status.value if upload_status else "",
            "upload_error": entry.get("upload_error") or "",
            "upload_id": entry.get("upload_id"),
        }

    @staticmethod
    def _can_retry_upload(job: Job, media: list[dict[str, Any]]) -> bool:
        """Retry is offered when an upload is outstanding and files still exist."""
        if not job.destination_mode.uploads_to_drive:
            return False
        if not media:
            return False
        outstanding = [
            m
            for m in media
            if m["upload_status_value"] != UploadStatus.UPLOADED.value
        ]
        return bool(outstanding) and any(m["exists"] for m in outstanding)

    def _resolve_directory(self, stored: str | None) -> Path | None:
        if not stored:
            return None
        path = Path(stored)
        if path.is_absolute():
            return path
        # Stored paths may or may not include the download root prefix.
        candidate = self.download_root / path
        if candidate.exists():
            return candidate
        try:
            trimmed = path.relative_to(self.download_root.name)
        except ValueError:
            return candidate
        return self.download_root / trimmed

    def _resolve_media_path(
        self, relative_path: str | None, directory: Path | None
    ) -> Path | None:
        if not relative_path:
            return None
        path = Path(relative_path)
        if path.is_absolute():
            return path
        if directory is not None:
            direct = directory / path.name
            if direct.exists():
                return direct
        resolved = self._resolve_directory(relative_path)
        return resolved


# ------------------------------------------------------------------- helpers


def _safe_status(value: Any) -> JobStatus | None:
    try:
        return JobStatus(value)
    except (ValueError, TypeError):
        return None


def _safe_upload_status(value: Any) -> UploadStatus | None:
    try:
        return UploadStatus(value)
    except (ValueError, TypeError):
        return None


def _content_label(value: Any) -> str:
    if not value:
        return "-"
    return {
        "reel": "Reel",
        "video": "Video",
        "image": "Image",
        "carousel": "Carousel",
    }.get(str(value), str(value).title())


def _upload_label(
    file_count: int, uploaded: int, failed: int, row: dict[str, Any]
) -> str:
    """Plain-text upload state. Never relies on colour to convey meaning."""
    mode = row.get("destination_mode")
    if mode == DestinationMode.LOCAL_ONLY.value:
        return "Local only"
    if file_count == 0:
        return "-"
    if uploaded >= file_count:
        return "Uploaded"
    if failed:
        return f"Failed ({uploaded}/{file_count})"
    if uploaded:
        return f"Partial ({uploaded}/{file_count})"
    return "Not uploaded"


def _format_datetime(value: Any) -> str:
    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return str(value)
    return parsed.strftime("%Y-%m-%d %H:%M")


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""
