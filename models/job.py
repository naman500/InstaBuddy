"""The job model: one run of the ingestion pipeline for one URL.

A job row is created *before* any network call so that a crash mid-run still
leaves a visible record rather than silence.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field

from models.media import DestinationMode, MediaSelection


class JobStatus(str, Enum):
    """Lifecycle of an ingestion run.

    ``PARTIAL`` is deliberately distinct from ``FAILED``: it means the media
    downloaded fine but the upload did not, so recovery is a retry of the
    upload alone rather than a fresh download.
    """

    QUEUED = "QUEUED"
    RESOLVING = "RESOLVING"
    DOWNLOADING = "DOWNLOADING"
    HASHING = "HASHING"
    GENERATING_METADATA = "GENERATING_METADATA"
    UPLOADING = "UPLOADING"
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    CANCELLED = "CANCELLED"

    @property
    def is_terminal(self) -> bool:
        return self in {
            JobStatus.COMPLETED,
            JobStatus.PARTIAL,
            JobStatus.FAILED,
            JobStatus.SKIPPED,
            JobStatus.CANCELLED,
        }

    @property
    def label(self) -> str:
        """Plain-text status for the UI.

        Status is never communicated by colour alone, so these strings matter.
        """
        return {
            JobStatus.QUEUED: "Queued",
            JobStatus.RESOLVING: "Resolving",
            JobStatus.DOWNLOADING: "Downloading",
            JobStatus.HASHING: "Hashing",
            JobStatus.GENERATING_METADATA: "Generating metadata",
            JobStatus.UPLOADING: "Uploading",
            JobStatus.COMPLETED: "Success",
            JobStatus.PARTIAL: "Partial - upload failed",
            JobStatus.FAILED: "Failed",
            JobStatus.SKIPPED: "Skipped - duplicate",
            JobStatus.CANCELLED: "Cancelled",
        }[self]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Job(BaseModel):
    """What was *requested*, kept separate from what was *produced*.

    That separation is what makes a job re-runnable and auditable.
    """

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    created_by: str
    requested_url: str
    shortcode: str | None = None
    requested_media: MediaSelection | None = None
    destination_mode: DestinationMode = DestinationMode.LOCAL_AND_DRIVE
    drive_account: str | None = None
    status: JobStatus = JobStatus.QUEUED
    current_step: str | None = None
    attempt_count: int = 1
    started_at: datetime = Field(default_factory=_utcnow)
    finished_at: datetime | None = None
    error_message: str | None = None
    error_detail: str | None = None
    application_version: str = "1.0.0"

    @property
    def duration_seconds(self) -> float | None:
        if self.finished_at is None:
            return None
        return (self.finished_at - self.started_at).total_seconds()


class JobSummary(BaseModel):
    """Aggregate counts for the History page activity strip."""

    total: int = 0
    completed: int = 0
    partial: int = 0
    failed: int = 0
    skipped: int = 0
    local_bytes: int = 0
