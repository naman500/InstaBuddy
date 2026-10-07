"""SQLite manifest: schema, job lifecycle, duplicate detection, uploads."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from models.job import Job, JobStatus
from models.media import DestinationMode, MediaSelection
from models.upload import UploadStatus
from services.manifest import Manifest

NOW = datetime(2026, 9, 21, 10, 0, tzinfo=timezone.utc)


@pytest.fixture
def manifest(tmp_path: Path) -> Manifest:
    return Manifest(tmp_path / "manifest.db")


def make_job(**overrides) -> Job:
    defaults = dict(
        created_by="naman",
        requested_url="https://www.instagram.com/reel/ABC123/",
        shortcode="ABC123",
        requested_media=MediaSelection.VIDEO_AND_COVER,
        destination_mode=DestinationMode.LOCAL_AND_DRIVE,
        drive_account="personal",
    )
    defaults.update(overrides)
    return Job(**defaults)


def seed_post(manifest: Manifest, job: Job, **overrides) -> int:
    defaults = dict(
        job_id=job.id,
        shortcode=job.shortcode or "ABC123",
        instagram_url=job.requested_url,
        username="creator1",
        caption="A caption",
        content_type="reel",
        created_at=NOW,
        local_directory="downloads/creator1/2026/09/ABC123",
        drive_account=job.drive_account,
    )
    defaults.update(overrides)
    return manifest.record_post(**defaults)


# ------------------------------------------------------------------- schema


def test_database_and_tables_created(tmp_path: Path) -> None:
    db_path = tmp_path / "nested" / "manifest.db"
    Manifest(db_path)
    assert db_path.is_file()

    with sqlite3.connect(db_path) as connection:
        names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert {"jobs", "posts", "media_files", "uploads", "schema_info"} <= names


def test_initialization_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "manifest.db"
    Manifest(path)
    Manifest(path)
    with sqlite3.connect(path) as connection:
        rows = connection.execute("SELECT COUNT(*) FROM schema_info").fetchone()
    assert rows[0] == 1


def test_indexes_exist(manifest: Manifest) -> None:
    with manifest.connect() as connection:
        indexes = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )
        }
    assert "idx_posts_shortcode" in indexes
    assert "idx_jobs_started" in indexes


# --------------------------------------------------------------------- jobs


def test_create_and_get_job(manifest: Manifest) -> None:
    job = manifest.create_job(make_job())
    loaded = manifest.get_job(job.id)

    assert loaded is not None
    assert loaded.id == job.id
    assert loaded.created_by == "naman"
    assert loaded.shortcode == "ABC123"
    assert loaded.requested_media is MediaSelection.VIDEO_AND_COVER
    assert loaded.destination_mode is DestinationMode.LOCAL_AND_DRIVE
    assert loaded.status is JobStatus.QUEUED
    assert loaded.attempt_count == 1


def test_get_missing_job_returns_none(manifest: Manifest) -> None:
    assert manifest.get_job("does-not-exist") is None


def test_job_exists_before_resolution(manifest: Manifest) -> None:
    """A job with no shortcode yet must still be recorded and visible."""
    job = manifest.create_job(make_job(shortcode=None, requested_media=None))
    loaded = manifest.get_job(job.id)
    assert loaded is not None
    assert loaded.shortcode is None
    assert manifest.list_jobs()[0]["id"] == job.id


def test_status_progression(manifest: Manifest) -> None:
    job = manifest.create_job(make_job())
    for status in (
        JobStatus.RESOLVING,
        JobStatus.DOWNLOADING,
        JobStatus.HASHING,
        JobStatus.GENERATING_METADATA,
        JobStatus.UPLOADING,
    ):
        manifest.set_job_status(job.id, status, step=status.value.lower())
        loaded = manifest.get_job(job.id)
        assert loaded.status is status
        assert loaded.finished_at is None, "non-terminal states must not finish"


def test_terminal_status_stamps_finished_at(manifest: Manifest) -> None:
    job = manifest.create_job(make_job())
    manifest.set_job_status(job.id, JobStatus.COMPLETED)
    loaded = manifest.get_job(job.id)
    assert loaded.status is JobStatus.COMPLETED
    assert loaded.finished_at is not None
    assert loaded.duration_seconds is not None


def test_partial_is_distinct_from_failed(manifest: Manifest) -> None:
    """PARTIAL means downloaded but not uploaded; recovery differs from FAILED."""
    partial = manifest.create_job(make_job())
    failed = manifest.create_job(make_job())

    manifest.set_job_status(
        partial.id, JobStatus.PARTIAL, error_message="Upload did not finish"
    )
    manifest.set_job_status(failed.id, JobStatus.FAILED, error_message="No media")

    assert manifest.get_job(partial.id).status is JobStatus.PARTIAL
    assert manifest.get_job(failed.id).status is JobStatus.FAILED

    summary = manifest.job_summary()
    assert summary.partial == 1
    assert summary.failed == 1


def test_error_fields_recorded(manifest: Manifest) -> None:
    job = manifest.create_job(make_job())
    manifest.set_job_status(
        job.id,
        JobStatus.FAILED,
        error_message="Friendly message",
        error_detail="Technical detail",
    )
    loaded = manifest.get_job(job.id)
    assert loaded.error_message == "Friendly message"
    assert loaded.error_detail == "Technical detail"


def test_update_job_ignores_unknown_columns(manifest: Manifest) -> None:
    job = manifest.create_job(make_job())
    manifest.update_job(job.id, shortcode="NEW123", bogus_column="x")
    assert manifest.get_job(job.id).shortcode == "NEW123"


def test_update_job_with_no_valid_fields_is_noop(manifest: Manifest) -> None:
    job = manifest.create_job(make_job())
    manifest.update_job(job.id, nothing_valid=1)
    assert manifest.get_job(job.id).shortcode == "ABC123"


def test_attempt_count_can_be_incremented(manifest: Manifest) -> None:
    job = manifest.create_job(make_job())
    manifest.update_job(job.id, attempt_count=2)
    assert manifest.get_job(job.id).attempt_count == 2


# ---------------------------------------------------------- duplicate detection


def test_shortcode_exists_is_false_before_download(manifest: Manifest) -> None:
    assert manifest.shortcode_exists("ABC123") is False


def test_shortcode_exists_after_recording(manifest: Manifest) -> None:
    job = manifest.create_job(make_job())
    seed_post(manifest, job)
    assert manifest.shortcode_exists("ABC123") is True
    assert manifest.shortcode_exists("OTHER1") is False


def test_find_posts_by_shortcode_returns_all_downloads(manifest: Manifest) -> None:
    """Re-running a URL creates another row; history is preserved."""
    first = manifest.create_job(make_job())
    seed_post(manifest, first, downloaded_at=NOW)
    second = manifest.create_job(make_job())
    seed_post(manifest, second, downloaded_at=NOW + timedelta(hours=1))

    rows = manifest.find_posts_by_shortcode("ABC123")
    assert len(rows) == 2
    assert rows[0]["downloaded_at"] > rows[1]["downloaded_at"], "newest first"


# -------------------------------------------------------------------- posts


def test_record_post_fields(manifest: Manifest) -> None:
    job = manifest.create_job(make_job())
    post_id = seed_post(manifest, job)
    post = manifest.get_post(post_id)

    assert post["shortcode"] == "ABC123"
    assert post["username"] == "creator1"
    assert post["content_type"] == "reel"
    assert post["caption"] == "A caption"
    assert post["job_id"] == job.id
    assert post["downloaded_at"] is not None


def test_get_post_for_job(manifest: Manifest) -> None:
    job = manifest.create_job(make_job())
    post_id = seed_post(manifest, job)
    assert manifest.get_post_for_job(job.id)["id"] == post_id
    assert manifest.get_post_for_job("missing") is None


# --------------------------------------------------------------- media files


def test_record_and_list_media_files(manifest: Manifest) -> None:
    job = manifest.create_job(make_job())
    post_id = seed_post(manifest, job)

    manifest.record_media_file(
        post_id=post_id,
        filename="video.mp4",
        relative_path="creator1/2026/09/ABC123/video.mp4",
        media_type="video",
        file_size=18_800_000,
        sha256="a" * 64,
    )
    manifest.record_media_file(
        post_id=post_id,
        filename="cover.jpg",
        relative_path="creator1/2026/09/ABC123/cover.jpg",
        media_type="image",
        file_size=120_000,
        sha256="b" * 64,
    )

    files = manifest.list_media_files(post_id)
    assert [f["filename"] for f in files] == ["video.mp4", "cover.jpg"]
    assert files[0]["sha256"] == "a" * 64
    assert files[0]["upload_status"] is None, "no upload recorded yet"


def test_find_media_by_hash(manifest: Manifest) -> None:
    job = manifest.create_job(make_job())
    post_id = seed_post(manifest, job)
    manifest.record_media_file(
        post_id=post_id,
        filename="video.mp4",
        relative_path="p/video.mp4",
        media_type="video",
        file_size=10,
        sha256="c" * 64,
    )
    assert len(manifest.find_media_by_hash("c" * 64)) == 1
    assert manifest.find_media_by_hash("d" * 64) == []


# ------------------------------------------------------------------- uploads


def test_record_and_update_upload(manifest: Manifest) -> None:
    job = manifest.create_job(make_job())
    post_id = seed_post(manifest, job)
    media_id = manifest.record_media_file(
        post_id=post_id,
        filename="video.mp4",
        relative_path="p/video.mp4",
        media_type="video",
        file_size=10,
        sha256="a" * 64,
    )

    upload_id = manifest.record_upload(
        media_file_id=media_id,
        drive_account="personal",
        remote_path="Instagram-Knowledge-Base/creator1/2026/09/ABC123/video.mp4",
        status=UploadStatus.PENDING,
    )
    manifest.update_upload(
        upload_id,
        status=UploadStatus.UPLOADED,
        drive_file_id="drive-file-1",
        uploaded_at=NOW,
    )

    files = manifest.list_media_files(post_id)
    assert files[0]["upload_status"] == UploadStatus.UPLOADED.value
    assert files[0]["drive_file_id"] == "drive-file-1"
    assert files[0]["remote_path"].endswith("video.mp4")


def test_latest_upload_wins_in_listing(manifest: Manifest) -> None:
    """A retry creates a new upload row; the listing shows the newest."""
    job = manifest.create_job(make_job())
    post_id = seed_post(manifest, job)
    media_id = manifest.record_media_file(
        post_id=post_id,
        filename="video.mp4",
        relative_path="p/video.mp4",
        media_type="video",
        file_size=10,
        sha256="a" * 64,
    )

    manifest.record_upload(
        media_file_id=media_id,
        drive_account="personal",
        remote_path="x/video.mp4",
        status=UploadStatus.FAILED,
        error_message="network",
    )
    manifest.record_upload(
        media_file_id=media_id,
        drive_account="personal",
        remote_path="x/video.mp4",
        status=UploadStatus.UPLOADED,
        drive_file_id="ok-1",
    )

    files = manifest.list_media_files(post_id)
    assert files[0]["upload_status"] == UploadStatus.UPLOADED.value
    assert files[0]["drive_file_id"] == "ok-1"


def test_list_failed_uploads_carries_retry_context(manifest: Manifest) -> None:
    job = manifest.create_job(make_job())
    post_id = seed_post(manifest, job)
    media_id = manifest.record_media_file(
        post_id=post_id,
        filename="video.mp4",
        relative_path="creator1/2026/09/ABC123/video.mp4",
        media_type="video",
        file_size=10,
        sha256="a" * 64,
    )
    manifest.record_upload(
        media_file_id=media_id,
        drive_account="personal",
        remote_path="x/video.mp4",
        status=UploadStatus.FAILED,
        error_message="timeout",
    )

    failed = manifest.list_failed_uploads()
    assert len(failed) == 1
    row = failed[0]
    assert row["filename"] == "video.mp4"
    assert row["shortcode"] == "ABC123"
    assert row["local_directory"] == "downloads/creator1/2026/09/ABC123"
    assert row["error_message"] == "timeout"


# ------------------------------------------------------------------ listing


def test_list_jobs_newest_first(manifest: Manifest) -> None:
    older = manifest.create_job(make_job(started_at=NOW - timedelta(days=1)))
    newer = manifest.create_job(make_job(started_at=NOW))
    ids = [row["id"] for row in manifest.list_jobs()]
    assert ids.index(newer.id) < ids.index(older.id)


def test_list_jobs_includes_post_aggregates(manifest: Manifest) -> None:
    job = manifest.create_job(make_job())
    post_id = seed_post(manifest, job)
    for name, size in (("video.mp4", 100), ("cover.jpg", 50)):
        media_id = manifest.record_media_file(
            post_id=post_id,
            filename=name,
            relative_path=f"p/{name}",
            media_type="video" if name.endswith("mp4") else "image",
            file_size=size,
            sha256="a" * 64,
        )
        manifest.record_upload(
            media_file_id=media_id,
            drive_account="personal",
            remote_path=f"x/{name}",
            status=UploadStatus.UPLOADED,
        )

    row = manifest.list_jobs()[0]
    assert row["username"] == "creator1"
    assert row["content_type"] == "reel"
    assert row["file_count"] == 2
    assert row["total_bytes"] == 150
    assert row["uploaded_count"] == 2
    assert row["failed_upload_count"] == 0


@pytest.mark.parametrize(
    "filters,expected",
    [
        ({"status": JobStatus.COMPLETED}, 1),
        ({"status": JobStatus.FAILED}, 0),
        ({"username": "creator1"}, 1),
        ({"username": "nobody"}, 0),
        ({"content_type": "reel"}, 1),
        ({"content_type": "image"}, 0),
        ({"drive_account": "personal"}, 1),
        ({"created_by": "naman"}, 1),
        ({"created_by": "someone_else"}, 0),
        ({"search": "ABC"}, 1),
        ({"search": "caption"}, 1),
        ({"search": "nomatch"}, 0),
    ],
)
def test_list_jobs_filters(manifest: Manifest, filters, expected: int) -> None:
    job = manifest.create_job(make_job())
    seed_post(manifest, job)
    manifest.set_job_status(job.id, JobStatus.COMPLETED)
    assert len(manifest.list_jobs(**filters)) == expected


def test_list_jobs_date_range(manifest: Manifest) -> None:
    manifest.create_job(make_job(started_at=NOW - timedelta(days=5)))
    manifest.create_job(make_job(started_at=NOW))

    assert len(manifest.list_jobs(date_from=NOW - timedelta(days=1))) == 1
    assert len(manifest.list_jobs(date_to=NOW - timedelta(days=1))) == 1
    assert len(manifest.list_jobs()) == 2


def test_list_jobs_pagination(manifest: Manifest) -> None:
    for index in range(5):
        manifest.create_job(make_job(started_at=NOW - timedelta(minutes=index)))
    assert len(manifest.list_jobs(limit=2)) == 2
    assert len(manifest.list_jobs(limit=2, offset=4)) == 1


def test_distinct_values(manifest: Manifest) -> None:
    job = manifest.create_job(make_job())
    seed_post(manifest, job, username="creator1")
    other = manifest.create_job(make_job())
    seed_post(manifest, other, username="creator2", content_type="carousel")

    assert manifest.distinct_values("username") == ["creator1", "creator2"]
    assert set(manifest.distinct_values("content_type")) == {"reel", "carousel"}
    assert manifest.distinct_values("created_by") == ["naman"]
    assert manifest.distinct_values("nonexistent") == []


def test_job_summary_counts(manifest: Manifest) -> None:
    assert manifest.job_summary().total == 0

    completed = manifest.create_job(make_job())
    post_id = seed_post(manifest, completed)
    manifest.record_media_file(
        post_id=post_id,
        filename="video.mp4",
        relative_path="p/video.mp4",
        media_type="video",
        file_size=500,
        sha256="a" * 64,
    )
    manifest.set_job_status(completed.id, JobStatus.COMPLETED)
    manifest.set_job_status(
        manifest.create_job(make_job()).id, JobStatus.SKIPPED
    )

    summary = manifest.job_summary()
    assert summary.total == 2
    assert summary.completed == 1
    assert summary.skipped == 1
    assert summary.local_bytes == 500


# ------------------------------------------------------------------ deletion


def test_delete_post_cascades_but_leaves_disk_alone(
    manifest: Manifest, tmp_path: Path
) -> None:
    media_on_disk = tmp_path / "video.mp4"
    media_on_disk.write_bytes(b"content")

    job = manifest.create_job(make_job())
    post_id = seed_post(manifest, job)
    media_id = manifest.record_media_file(
        post_id=post_id,
        filename="video.mp4",
        relative_path=str(media_on_disk),
        media_type="video",
        file_size=7,
        sha256="a" * 64,
    )
    manifest.record_upload(
        media_file_id=media_id,
        drive_account="personal",
        remote_path="x/video.mp4",
        status=UploadStatus.UPLOADED,
    )

    manifest.delete_post(post_id)

    assert manifest.get_post(post_id) is None
    assert manifest.list_media_files(post_id) == []
    with manifest.connect() as connection:
        remaining = connection.execute("SELECT COUNT(*) FROM uploads").fetchone()[0]
    assert remaining == 0
    assert media_on_disk.is_file(), "deleting a record must not delete media"


def test_delete_job_removes_its_post(manifest: Manifest) -> None:
    job = manifest.create_job(make_job())
    post_id = seed_post(manifest, job)
    manifest.delete_job(job.id)
    assert manifest.get_job(job.id) is None
    assert manifest.get_post(post_id) is None


# -------------------------------------------------------------------- export


def test_export_job_is_valid_json(manifest: Manifest) -> None:
    job = manifest.create_job(make_job())
    post_id = seed_post(manifest, job)
    manifest.record_media_file(
        post_id=post_id,
        filename="video.mp4",
        relative_path="p/video.mp4",
        media_type="video",
        file_size=10,
        sha256="a" * 64,
    )

    payload = json.loads(manifest.export_job(job.id))
    assert payload["job"]["id"] == job.id
    assert payload["post"]["shortcode"] == "ABC123"
    assert len(payload["media_files"]) == 1
