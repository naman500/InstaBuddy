"""Models for downloaded files and their upload outcomes."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, Field

from models.instagram import InstagramPost
from models.media import MediaType


class UploadStatus(str, Enum):
    PENDING = "PENDING"
    UPLOADING = "UPLOADING"
    UPLOADED = "UPLOADED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"

    @property
    def label(self) -> str:
        return {
            UploadStatus.PENDING: "Pending",
            UploadStatus.UPLOADING: "Uploading",
            UploadStatus.UPLOADED: "Uploaded",
            UploadStatus.FAILED: "Failed",
            UploadStatus.SKIPPED: "Not uploaded",
        }[self]


class DownloadedFile(BaseModel):
    """A file that exists on local disk, with its integrity hash."""

    filename: str
    path: Path
    media_type: MediaType
    file_size: int = 0
    sha256: str = ""

    @property
    def exists(self) -> bool:
        return self.path.exists()

    @property
    def size_human(self) -> str:
        return _human_bytes(self.file_size)


class DownloadResult(BaseModel):
    """Everything produced by the download phase of a job."""

    post: InstagramPost
    directory: Path
    files: list[DownloadedFile] = Field(default_factory=list)
    metadata_file: Path | None = None
    document_file: Path | None = None
    status: str = "success"

    @property
    def all_files(self) -> list[DownloadedFile]:
        return self.files

    @property
    def total_bytes(self) -> int:
        return sum(f.file_size for f in self.files)


class UploadResult(BaseModel):
    """Outcome of uploading one file to one Google Drive account."""

    account: str
    filename: str
    remote_path: str
    status: UploadStatus = UploadStatus.PENDING
    drive_file_id: str | None = None
    error_message: str | None = None
    uploaded_at: datetime | None = None

    @property
    def succeeded(self) -> bool:
        return self.status is UploadStatus.UPLOADED


def _human_bytes(size: int) -> str:
    """Format a byte count for display."""
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"
