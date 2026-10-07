"""Job service: lifecycle, history formatting and detail assembly."""

from __future__ import annotations

from pathlib import Path

import pytest

from instagram.metadata import write_metadata
from instagram.resolver import build_post_model
from models.job import JobStatus
from models.media import DestinationMode, MediaSelection, MediaType
from models.upload import DownloadedFile, UploadStatus
from services.jobs import JobService
from services.manifest import Manifest
from utils.errors import AppError, GoogleDriveUploadError
from tests.conftest import FakePost

REEL_URL = "https://www.instagram.com/reel/ABC123/"


@pytest.fixture
def service(tmp_path: Path) -> JobService:
    manifest = Manifest(tmp_path / "manifest.db")
    return JobService(manifest, tmp_path / "downloads")


def start(service: JobService, **overrides):
    defaults = dict(
        created_by="naman",
        requested_url=REEL_URL,
        shortcode="ABC123",
        requested_media=MediaSelection.VIDEO_AND_COVER,
        destination_mode=DestinationMode.LOCAL_AND_DRIVE,
        drive_account="personal",
    )
    defaults.update(overrides)
    return service.start_job(**defaults)


def seed_downloaded_post(
    service: JobService, job, *, with_files: bool = True, on_disk: bool = True
) -> tuple[int, Path]:
    """Create a post with real files on disk and matching manifest rows."""
    directory = service.download_root / "creator1" / "2026" / "09" / "ABC123"
    directory.mkdir(parents=True, exist_ok=True)

    post = build_post_model(
        FakePost(
            typename="GraphVideo",
            is_video=True,
            video_url="https://cdn.example/v.mp4",
            product_type="clips",
            owner_username="creator1",
        ),
        url=REEL_URL,
        url_hint="reel",
    )

    files: list[DownloadedFile] = []
    if with_files:
        for name, media_type, payload in (
            ("video.mp4", MediaType.VIDEO, b"video-bytes"),
            ("cover.jpg", MediaType.IMAGE, b"cover"),
        ):
            path = directory / name
            if on_disk:
                path.write_bytes(payload)
            files.append(
                DownloadedFile(
                    filename=name,
                    path=path,
                    media_type=media_type,
                    file_size=len(payload),
                    sha256=("a" if name.endswith("mp4") else "b") * 64,
                )
            )
        write_metadata(directory, post, files, job_id=job.id, downloaded_by="naman")

    post_id = service.manifest.record_post(
        job_id=job.id,
        shortcode="ABC123",
        instagram_url=REEL_URL,
        username="creator1",
        caption="An example caption",
        content_type="reel",
        created_at=post.created_at,
        local_directory=str(directory),
        drive_account="personal",
    )

    for downloaded in files:
        service.manifest.record_media_file(
            post_id=post_id,
            filename=downloaded.filename,
            relative_path=str(downloaded.path),
            media_type=downloaded.media_type.value,
            file_size=downloaded.file_size,
            sha256=downloaded.sha256,
        )

    return post_id, directory


# ------------------------------------------------------------------- lifecycle


def test_start_job_records_request(service: JobService) -> None:
    job = start(service)
    stored = service.manifest.get_job(job.id)

    assert stored.status is JobStatus.QUEUED
    assert stored.created_by == "naman"
    assert stored.requested_media is MediaSelection.VIDEO_AND_COVER
    assert stored.drive_account == "personal"


def test_advance_through_stages(service: JobService) -> None:
    job = start(service)
    service.advance(job.id, JobStatus.RESOLVING, step="resolving")
    assert service.manifest.get_job(job.id).current_step == "resolving"

    service.advance(job.id, JobStatus.DOWNLOADING)
    assert service.manifest.get_job(job.id).status is JobStatus.DOWNLOADING


def test_finish_with_exception_splits_friendly_and_technical(
    service: JobService,
) -> None:
    job = start(service)
    service.finish(job.id, JobStatus.PARTIAL, error=GoogleDriveUploadError("429 boom"))

    stored = service.manifest.get_job(job.id)
    assert stored.status is JobStatus.PARTIAL
    assert "kept locally" in stored.error_message
    assert "GoogleDriveUploadError" in stored.error_detail
    assert "429 boom" in stored.error_detail


def test_finish_with_plain_message(service: JobService) -> None:
    job = start(service)
    service.finish(job.id, JobStatus.SKIPPED, message="Duplicate skipped")
    assert service.manifest.get_job(job.id).error_message == "Duplicate skipped"


def test_set_shortcode_and_selection(service: JobService) -> None:
    job = start(service, shortcode=None, requested_media=None)
    service.set_shortcode(job.id, "NEW456")
    service.set_selection(job.id, MediaSelection.VIDEO)

    stored = service.manifest.get_job(job.id)
    assert stored.shortcode == "NEW456"
    assert stored.requested_media is MediaSelection.VIDEO


# --------------------------------------------------------------------- history


def test_history_row_shape(service: JobService) -> None:
    job = start(service)
    post_id, _ = seed_downloaded_post(service, job)
    service.finish(job.id, JobStatus.COMPLETED)

    for media in service.manifest.list_media_files(post_id):
        service.manifest.record_upload(
            media_file_id=media["id"],
            drive_account="personal",
            remote_path=f"Instagram-Knowledge-Base/creator1/2026/09/ABC123/"
            f"{media['filename']}",
            status=UploadStatus.UPLOADED,
            drive_file_id="file-1",
        )

    row = service.history_rows()[0]
    assert row["username"] == "creator1"
    assert row["type"] == "Reel"
    assert row["shortcode"] == "ABC123"
    assert row["files"] == 2
    assert row["account"] == "naman"
    assert row["drive"] == "personal"
    assert row["upload"] == "Uploaded"
    assert row["status"] == "Success"
    assert row["date"].startswith("20")
    assert "B" in row["size"] or "KB" in row["size"]


def test_history_shows_partial_upload_state(service: JobService) -> None:
    job = start(service)
    post_id, _ = seed_downloaded_post(service, job)
    service.finish(job.id, JobStatus.PARTIAL)

    media = service.manifest.list_media_files(post_id)
    service.manifest.record_upload(
        media_file_id=media[0]["id"],
        drive_account="personal",
        remote_path="x/video.mp4",
        status=UploadStatus.UPLOADED,
    )
    service.manifest.record_upload(
        media_file_id=media[1]["id"],
        drive_account="personal",
        remote_path="x/cover.jpg",
        status=UploadStatus.FAILED,
        error_message="timeout",
    )

    row = service.history_rows()[0]
    assert row["upload"] == "Failed (1/2)"
    assert row["status"] == "Partial - upload failed"


def test_history_local_only_job_reports_local(service: JobService) -> None:
    job = start(service, destination_mode=DestinationMode.LOCAL_ONLY, drive_account=None)
    seed_downloaded_post(service, job)
    service.finish(job.id, JobStatus.COMPLETED)

    row = service.history_rows()[0]
    assert row["upload"] == "Local only"
    assert row["drive"] == "-"


def test_history_row_for_job_that_failed_before_resolving(
    service: JobService,
) -> None:
    job = start(service, shortcode=None, requested_media=None)
    service.finish(job.id, JobStatus.FAILED, message="Invalid URL")

    row = service.history_rows()[0]
    assert row["username"] == "-"
    assert row["type"] == "-"
    assert row["files"] == 0
    assert row["status"] == "Failed"
    assert row["error_message"] == "Invalid URL"


def test_history_filters_and_options(service: JobService) -> None:
    job = start(service)
    seed_downloaded_post(service, job)
    service.finish(job.id, JobStatus.COMPLETED)

    assert len(service.history_rows(status=JobStatus.COMPLETED)) == 1
    assert len(service.history_rows(status=JobStatus.FAILED)) == 0
    assert len(service.history_rows(username="creator1")) == 1
    assert len(service.history_rows(search="ABC")) == 1

    options = service.filter_options()
    assert options["username"] == ["creator1"]
    assert options["content_type"] == ["reel"]
    assert options["created_by"] == ["naman"]


def test_summary_counts(service: JobService) -> None:
    job = start(service)
    seed_downloaded_post(service, job)
    service.finish(job.id, JobStatus.COMPLETED)

    summary = service.summary()
    assert summary.total == 1
    assert summary.completed == 1
    assert summary.local_bytes > 0


# ---------------------------------------------------------------------- detail


def test_job_detail_exposes_full_metadata(service: JobService) -> None:
    job = start(service)
    seed_downloaded_post(service, job)
    service.finish(job.id, JobStatus.COMPLETED)

    detail = service.job_detail(job.id)

    assert detail["job"].id == job.id
    assert detail["post"]["shortcode"] == "ABC123"
    assert detail["post"]["caption"] == "An example caption"
    assert detail["directory_exists"] is True
    assert detail["missing_files"] == []

    assert detail["metadata"]["source"]["shortcode"] == "ABC123"
    assert detail["metadata"]["ingestion"]["job_id"] == job.id
    assert "SOURCE: Instagram" in detail["document"]

    video = detail["media"][0]
    assert video["filename"] == "video.mp4"
    assert video["media_type"] == "video"
    assert len(video["sha256"]) == 64, "full hash must be available to copy"
    assert video["sha256_short"] == "a" * 12
    assert video["exists"] is True
    assert video["local_path"].endswith("video.mp4")
    assert video["size_human"]


def test_job_detail_flags_missing_local_files(service: JobService) -> None:
    job = start(service)
    _, directory = seed_downloaded_post(service, job)
    service.finish(job.id, JobStatus.COMPLETED)

    (directory / "video.mp4").unlink()

    detail = service.job_detail(job.id)
    assert detail["missing_files"] == ["video.mp4"]
    assert detail["media"][0]["exists"] is False
    assert detail["media"][1]["exists"] is True


def test_job_detail_shows_upload_paths_and_status(service: JobService) -> None:
    job = start(service)
    post_id, _ = seed_downloaded_post(service, job)
    media = service.manifest.list_media_files(post_id)
    service.manifest.record_upload(
        media_file_id=media[0]["id"],
        drive_account="personal",
        remote_path="Instagram-Knowledge-Base/creator1/2026/09/ABC123/video.mp4",
        status=UploadStatus.UPLOADED,
        drive_file_id="drive-abc",
    )

    detail = service.job_detail(job.id)
    video = detail["media"][0]
    assert video["upload_status"] == "Uploaded"
    assert video["remote_path"].endswith("video.mp4")
    assert video["drive_file_id"] == "drive-abc"


def test_job_detail_for_unknown_job_raises(service: JobService) -> None:
    with pytest.raises(AppError) as exc_info:
        service.job_detail("missing")
    assert "could not be found" in exc_info.value.user_message


def test_job_detail_without_post_is_safe(service: JobService) -> None:
    job = start(service, shortcode=None)
    service.finish(job.id, JobStatus.FAILED, message="Nothing resolved")

    detail = service.job_detail(job.id)
    assert detail["post"] is None
    assert detail["media"] == []
    assert detail["metadata"] == {}
    assert detail["document"] == ""
    assert detail["can_retry_upload"] is False


# ----------------------------------------------------------------- retry gating


def test_retry_offered_when_upload_outstanding_and_file_present(
    service: JobService,
) -> None:
    job = start(service)
    seed_downloaded_post(service, job)
    service.finish(job.id, JobStatus.PARTIAL)
    assert service.job_detail(job.id)["can_retry_upload"] is True


def test_retry_not_offered_once_everything_uploaded(service: JobService) -> None:
    job = start(service)
    post_id, _ = seed_downloaded_post(service, job)
    for media in service.manifest.list_media_files(post_id):
        service.manifest.record_upload(
            media_file_id=media["id"],
            drive_account="personal",
            remote_path=f"x/{media['filename']}",
            status=UploadStatus.UPLOADED,
        )
    service.finish(job.id, JobStatus.COMPLETED)
    assert service.job_detail(job.id)["can_retry_upload"] is False


def test_retry_not_offered_for_local_only_job(service: JobService) -> None:
    job = start(service, destination_mode=DestinationMode.LOCAL_ONLY)
    seed_downloaded_post(service, job)
    service.finish(job.id, JobStatus.COMPLETED)
    assert service.job_detail(job.id)["can_retry_upload"] is False


def test_retry_not_offered_when_local_files_are_gone(service: JobService) -> None:
    """Without a local file there is nothing to re-upload."""
    job = start(service)
    _, directory = seed_downloaded_post(service, job)
    service.finish(job.id, JobStatus.PARTIAL)

    for name in ("video.mp4", "cover.jpg"):
        (directory / name).unlink()

    assert service.job_detail(job.id)["can_retry_upload"] is False
