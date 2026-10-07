"""The ingestion orchestrator, end to end with fakes.

Central assertions:

* a job record exists even when the run fails early
* download success plus upload failure yields PARTIAL, never FAILED
* local files always survive an upload failure
* retrying an upload never contacts Instagram
* duplicates are skipped unless the user forces a re-download
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from models.job import JobStatus
from models.media import DestinationMode, MediaSelection, MediaType
from models.upload import DownloadedFile, UploadResult, UploadStatus
from services.ingestion import IngestionOptions, IngestionService
from services.manifest import Manifest
from utils import config as config_module
from utils.config import AppConfig, DriveAccountConfig
from utils.errors import (
    GoogleDriveAuthenticationError,
    InstagramContentUnavailableError,
    InstagramDownloadError,
    ValidationError,
)
from tests.conftest import FakePost

REEL_URL = "https://www.instagram.com/reel/ABC123/"
POST_URL = "https://www.instagram.com/p/ABC123/"

ACCOUNT = DriveAccountConfig(
    key="personal",
    display_name="Personal Google Drive",
    client_id="cid.apps.googleusercontent.com",
    client_secret="secret",
)


# ------------------------------------------------------------------ test doubles


class FakeInstagramClient:
    def __init__(self, post: Any = None, error: Exception | None = None) -> None:
        self._post = post
        self._error = error
        self.fetch_calls: list[str] = []
        self.context = object()

    def fetch_post(self, shortcode: str) -> Any:
        self.fetch_calls.append(shortcode)
        if self._error is not None:
            raise self._error
        return self._post


class FakeDownloader:
    """Writes real bytes to disk so hashing and existence checks are genuine."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls = 0

    def download_items(self, post, items, directory, *, progress=None):
        self.calls += 1
        if self.error is not None:
            raise self.error

        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        results: list[DownloadedFile] = []

        for position, item in enumerate(items, start=1):
            path = directory / item.filename
            payload = f"bytes-for-{item.filename}".encode()
            path.write_bytes(payload)

            from services.hashing import calculate_sha256

            if progress is not None:
                from instagram.downloader import DownloadProgress

                progress(
                    DownloadProgress(position, len(items), item.filename, 1, 1)
                )

            results.append(
                DownloadedFile(
                    filename=item.filename,
                    path=path,
                    media_type=item.media_type,
                    file_size=len(payload),
                    sha256=calculate_sha256(path),
                )
            )
        return results


class FakeDriveClient:
    """Records folder creation and file uploads without any network."""

    def __init__(
        self,
        account: DriveAccountConfig = ACCOUNT,
        *,
        fail_files: set[str] | None = None,
        connect_error: Exception | None = None,
    ) -> None:
        self.account = account
        self.fail_files = fail_files or set()
        self.connect_error = connect_error
        self.folders: list[str] = []
        self.uploaded: list[str] = []

    # Used by FolderResolver
    def find_child(self, name, parent_id=None, *, folders_only=False):
        if self.connect_error is not None:
            raise self.connect_error
        return None

    def create_folder(self, name, parent_id=None):
        if self.connect_error is not None:
            raise self.connect_error
        self.folders.append(name)
        return {"id": f"folder-{len(self.folders)}", "name": name}


class FakeUploader:
    """Stands in for DriveUploader, honouring a configured failure set."""

    instances: list["FakeUploader"] = []

    def __init__(self, client, *, chunk_size=0, max_retries=3) -> None:
        self.client = client
        self.chunk_size = chunk_size
        self.max_retries = max_retries
        FakeUploader.instances.append(self)

    def upload_file(
        self,
        local_path,
        folder_id,
        *,
        remote_path="",
        progress=None,
        file_index=1,
        file_count=1,
    ) -> UploadResult:
        path = Path(local_path)
        result = UploadResult(
            account=self.client.account.key,
            filename=path.name,
            remote_path=remote_path or path.name,
            status=UploadStatus.PENDING,
        )

        if not path.is_file():
            result.status = UploadStatus.FAILED
            result.error_message = "The local file is missing."
            return result

        if path.name in self.client.fail_files:
            result.status = UploadStatus.FAILED
            result.error_message = "Upload rejected."
            return result

        if progress is not None:
            from gdrive.uploader import UploadProgress

            size = path.stat().st_size
            progress(UploadProgress(path.name, size, size, file_index, file_count))

        self.client.uploaded.append(path.name)
        result.status = UploadStatus.UPLOADED
        result.drive_file_id = f"drive-{len(self.client.uploaded)}"
        result.uploaded_at = datetime.now(timezone.utc)
        return result


# ---------------------------------------------------------------------- fixtures


@pytest.fixture(autouse=True)
def patch_drive(monkeypatch: pytest.MonkeyPatch):
    """Swap the real uploader and account lookup for fakes."""
    FakeUploader.instances.clear()
    import services.ingestion as ingestion_module

    monkeypatch.setattr(ingestion_module, "DriveUploader", FakeUploader)
    monkeypatch.setattr(ingestion_module, "get_drive_account", lambda key: ACCOUNT)
    yield


@pytest.fixture
def config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AppConfig:
    monkeypatch.setattr(
        config_module, "SETTINGS_OVERLAY_PATH", tmp_path / "settings.json"
    )
    monkeypatch.setattr(config_module, "SECRETS_PATH", tmp_path / "secrets.toml")
    config_module.get_config.cache_clear()
    config_module.load_secrets.cache_clear()

    cfg = AppConfig(
        download_dir=str(tmp_path / "downloads"),
        temp_dir=str(tmp_path / "temp"),
        data_dir=str(tmp_path / "data"),
        token_dir=str(tmp_path / "tokens"),
        session_dir=str(tmp_path / "sessions"),
        log_dir=str(tmp_path / "logs"),
    )
    cfg.ensure_directories()
    yield cfg
    config_module.get_config.cache_clear()
    config_module.load_secrets.cache_clear()


@pytest.fixture
def manifest(config: AppConfig) -> Manifest:
    return Manifest(config.manifest_db_path)


def build_service(
    config: AppConfig,
    manifest: Manifest,
    *,
    post: Any = None,
    fetch_error: Exception | None = None,
    download_error: Exception | None = None,
    drive_client: FakeDriveClient | None = None,
) -> IngestionService:
    client = FakeInstagramClient(post=post, error=fetch_error)
    downloader = FakeDownloader(error=download_error)
    drive = drive_client or FakeDriveClient()

    service = IngestionService(
        config,
        manifest,
        instagram_client=client,  # type: ignore[arg-type]
        downloader_factory=lambda _c: downloader,  # type: ignore[arg-type,return-value]
        drive_client_factory=lambda _a: drive,  # type: ignore[arg-type,return-value]
    )
    service._test_client = client  # type: ignore[attr-defined]
    service._test_downloader = downloader  # type: ignore[attr-defined]
    service._test_drive = drive  # type: ignore[attr-defined]
    return service


def reel() -> FakePost:
    return FakePost(
        typename="GraphVideo",
        is_video=True,
        video_url="https://cdn.example/v.mp4",
        product_type="clips",
        owner_username="creator1",
    )


def local_options(**overrides: Any) -> IngestionOptions:
    base = dict(
        selection=MediaSelection.VIDEO_AND_COVER,
        destination_mode=DestinationMode.LOCAL_ONLY,
    )
    base.update(overrides)
    return IngestionOptions(**base)


def drive_options(**overrides: Any) -> IngestionOptions:
    base = dict(
        selection=MediaSelection.VIDEO_AND_COVER,
        destination_mode=DestinationMode.LOCAL_AND_DRIVE,
        drive_account="personal",
        root_folder="Instagram-Knowledge-Base",
    )
    base.update(overrides)
    return IngestionOptions(**base)


# ------------------------------------------------------------------- analyze


def test_analyze_does_not_write_anything(config: AppConfig, manifest: Manifest) -> None:
    service = build_service(config, manifest, post=reel())
    post = service.analyze(REEL_URL)

    assert post.shortcode == "ABC123"
    assert list(config.download_path.iterdir()) == []
    assert manifest.list_jobs() == []


def test_preview_destination_needs_no_drive_connection(
    config: AppConfig, manifest: Manifest
) -> None:
    service = build_service(config, manifest, post=reel())
    post = service.analyze(REEL_URL)
    preview = service.preview_destination(post, drive_options())

    assert preview["remote_path"] == (
        "Instagram-Knowledge-Base/creator1/2026/09/ABC123"
    )
    assert preview["filenames"] == [
        "video.mp4",
        "cover.jpg",
        "metadata.json",
        "document.txt",
    ]
    assert preview["local_directory"].as_posix().endswith(
        "creator1/2026/09/ABC123"
    )


# -------------------------------------------------------------- local download


def test_local_only_run_produces_all_artifacts(
    config: AppConfig, manifest: Manifest
) -> None:
    service = build_service(config, manifest, post=reel())
    result = service.process(REEL_URL, local_options(), user="naman")

    assert result.status is JobStatus.COMPLETED
    assert result.succeeded is True

    directory = result.directory
    assert (directory / "video.mp4").is_file()
    assert (directory / "cover.jpg").is_file()
    assert (directory / "metadata.json").is_file()
    assert (directory / "document.txt").is_file()
    assert result.all_filenames == [
        "video.mp4",
        "cover.jpg",
        "metadata.json",
        "document.txt",
    ]


def test_local_only_run_uploads_nothing(
    config: AppConfig, manifest: Manifest
) -> None:
    service = build_service(config, manifest, post=reel())
    result = service.process(REEL_URL, local_options(), user="naman")

    assert result.uploads == []
    assert service._test_drive.uploaded == []


def test_folder_layout_uses_shortcode_as_leaf(
    config: AppConfig, manifest: Manifest
) -> None:
    service = build_service(config, manifest, post=reel())
    result = service.process(REEL_URL, local_options(), user="naman")

    relative = result.directory.relative_to(config.download_path)
    assert relative.as_posix() == "creator1/2026/09/ABC123"


def test_hashes_recorded_for_every_artifact(
    config: AppConfig, manifest: Manifest
) -> None:
    service = build_service(config, manifest, post=reel())
    result = service.process(REEL_URL, local_options(), user="naman")

    post_row = manifest.get_post_for_job(result.job_id)
    media = manifest.list_media_files(int(post_row["id"]))

    assert {m["filename"] for m in media} == {
        "video.mp4",
        "cover.jpg",
        "metadata.json",
        "document.txt",
    }
    assert all(len(m["sha256"]) == 64 for m in media)
    assert all(m["file_size"] > 0 for m in media)


def test_documents_recorded_with_document_type(
    config: AppConfig, manifest: Manifest
) -> None:
    service = build_service(config, manifest, post=reel())
    result = service.process(REEL_URL, local_options(), user="naman")

    post_row = manifest.get_post_for_job(result.job_id)
    media = {m["filename"]: m["media_type"] for m in manifest.list_media_files(int(post_row["id"]))}

    assert media["video.mp4"] == MediaType.VIDEO.value
    assert media["cover.jpg"] == MediaType.IMAGE.value
    assert media["metadata.json"] == MediaType.DOCUMENT.value
    assert media["document.txt"] == MediaType.DOCUMENT.value


def test_job_attributed_to_signed_in_user(
    config: AppConfig, manifest: Manifest
) -> None:
    service = build_service(config, manifest, post=reel())
    result = service.process(REEL_URL, local_options(), user="naman")

    assert manifest.get_job(result.job_id).created_by == "naman"


def test_metadata_records_job_and_user(
    config: AppConfig, manifest: Manifest
) -> None:
    import json

    service = build_service(config, manifest, post=reel())
    result = service.process(REEL_URL, local_options(), user="naman")

    payload = json.loads(result.metadata_file.read_text(encoding="utf-8"))
    assert payload["ingestion"]["job_id"] == result.job_id
    assert payload["ingestion"]["downloaded_by"] == "naman"


def test_progress_is_reported_monotonically(
    config: AppConfig, manifest: Manifest
) -> None:
    service = build_service(config, manifest, post=reel())
    updates: list[tuple[str, float | None]] = []

    service.process(
        REEL_URL,
        local_options(),
        user="naman",
        progress=lambda message, fraction=None: updates.append((message, fraction)),
    )

    fractions = [f for _, f in updates if f is not None]
    assert fractions == sorted(fractions)
    assert fractions[-1] == pytest.approx(1.0)
    messages = [m for m, _ in updates]
    assert any("Downloading" in m for m in messages)
    assert "Generating metadata..." in messages
    assert "Completed." in messages


def test_analyzed_post_is_reused_without_refetching(
    config: AppConfig, manifest: Manifest
) -> None:
    service = build_service(config, manifest, post=reel())
    post = service.analyze(REEL_URL)
    assert len(service._test_client.fetch_calls) == 1

    service.process(REEL_URL, local_options(), user="naman", post=post)
    assert len(service._test_client.fetch_calls) == 1, "no second Instagram call"


# ------------------------------------------------------------ selection checks


def test_carousel_images_only_downloads_images(
    config: AppConfig, manifest: Manifest, mixed_carousel_post: FakePost
) -> None:
    mixed_carousel_post.owner_username = "creator1"
    service = build_service(config, manifest, post=mixed_carousel_post)
    result = service.process(
        POST_URL, local_options(selection=MediaSelection.IMAGES), user="naman"
    )

    assert [f.filename for f in result.files] == ["001.jpg", "003.jpg"]


def test_carousel_mixed_preserves_order(
    config: AppConfig, manifest: Manifest, mixed_carousel_post: FakePost
) -> None:
    mixed_carousel_post.owner_username = "creator1"
    service = build_service(config, manifest, post=mixed_carousel_post)
    result = service.process(
        POST_URL,
        local_options(selection=MediaSelection.IMAGES_AND_VIDEOS),
        user="naman",
    )

    assert [f.filename for f in result.files] == [
        "001.jpg",
        "002.mp4",
        "003.jpg",
        "004.mp4",
    ]


def test_invalid_selection_for_content_type_fails_clearly(
    config: AppConfig, manifest: Manifest, image_post: FakePost
) -> None:
    image_post.owner_username = "creator1"
    service = build_service(config, manifest, post=image_post)
    result = service.process(
        POST_URL, local_options(selection=MediaSelection.VIDEO), user="naman"
    )

    assert result.status is JobStatus.FAILED
    assert "not available" in result.message
    assert manifest.get_job(result.job_id).status is JobStatus.FAILED


# --------------------------------------------------------- duplicate handling


def test_second_run_is_skipped(config: AppConfig, manifest: Manifest) -> None:
    service = build_service(config, manifest, post=reel())
    service.process(REEL_URL, local_options(), user="naman")

    second = service.process(REEL_URL, local_options(), user="naman")
    assert second.status is JobStatus.SKIPPED
    assert second.message == "This Instagram post has already been downloaded."
    assert service._test_downloader.calls == 1, "no second download"


def test_force_redownload_overrides_duplicate_guard(
    config: AppConfig, manifest: Manifest
) -> None:
    service = build_service(config, manifest, post=reel())
    service.process(REEL_URL, local_options(), user="naman")

    again = service.process(
        REEL_URL, local_options(force_redownload=True), user="naman"
    )
    assert again.status is JobStatus.COMPLETED
    assert service._test_downloader.calls == 2
    assert len(manifest.find_posts_by_shortcode("ABC123")) == 2


def test_duplicate_helpers(config: AppConfig, manifest: Manifest) -> None:
    service = build_service(config, manifest, post=reel())
    assert service.is_duplicate("ABC123") is False

    service.process(REEL_URL, local_options(), user="naman")
    assert service.is_duplicate("ABC123") is True
    assert len(service.find_duplicates("ABC123")) == 1


# -------------------------------------------------------------- drive uploads


def test_drive_run_uploads_everything(
    config: AppConfig, manifest: Manifest
) -> None:
    service = build_service(config, manifest, post=reel())
    result = service.process(REEL_URL, drive_options(), user="naman")

    assert result.status is JobStatus.COMPLETED
    assert result.uploaded_count == 4
    assert service._test_drive.uploaded == [
        "video.mp4",
        "cover.jpg",
        "metadata.json",
        "document.txt",
    ]
    assert result.remote_path == "Instagram-Knowledge-Base/creator1/2026/09/ABC123"


def test_drive_folder_chain_created_level_by_level(
    config: AppConfig, manifest: Manifest
) -> None:
    service = build_service(config, manifest, post=reel())
    service.process(REEL_URL, drive_options(), user="naman")

    assert service._test_drive.folders == [
        "Instagram-Knowledge-Base",
        "creator1",
        "2026",
        "09",
        "ABC123",
    ]


def test_organization_toggles_shape_remote_path(
    config: AppConfig, manifest: Manifest
) -> None:
    service = build_service(config, manifest, post=reel())
    result = service.process(
        REEL_URL,
        drive_options(organize_by_username=False, organize_by_month=False),
        user="naman",
    )
    assert result.remote_path == "Instagram-Knowledge-Base/2026/ABC123"


def test_upload_rows_recorded_in_manifest(
    config: AppConfig, manifest: Manifest
) -> None:
    service = build_service(config, manifest, post=reel())
    result = service.process(REEL_URL, drive_options(), user="naman")

    post_row = manifest.get_post_for_job(result.job_id)
    media = manifest.list_media_files(int(post_row["id"]))

    assert all(m["upload_status"] == UploadStatus.UPLOADED.value for m in media)
    assert all(m["drive_file_id"] for m in media)


def test_history_row_reflects_successful_upload(
    config: AppConfig, manifest: Manifest
) -> None:
    service = build_service(config, manifest, post=reel())
    service.process(REEL_URL, drive_options(), user="naman")

    row = service.jobs.history_rows()[0]
    assert row["status"] == "Success"
    assert row["upload"] == "Uploaded"
    assert row["files"] == 4


# ------------------------------------------------- upload failure is PARTIAL


def test_upload_failure_yields_partial_not_failed(
    config: AppConfig, manifest: Manifest
) -> None:
    drive = FakeDriveClient(fail_files={"video.mp4"})
    service = build_service(config, manifest, post=reel(), drive_client=drive)
    result = service.process(REEL_URL, drive_options(), user="naman")

    assert result.status is JobStatus.PARTIAL
    assert manifest.get_job(result.job_id).status is JobStatus.PARTIAL
    assert len(result.failed_uploads) == 1


def test_local_files_survive_upload_failure(
    config: AppConfig, manifest: Manifest
) -> None:
    """The headline reliability guarantee."""
    drive = FakeDriveClient(fail_files={"video.mp4", "cover.jpg"})
    service = build_service(config, manifest, post=reel(), drive_client=drive)
    result = service.process(REEL_URL, drive_options(), user="naman")

    assert result.status is JobStatus.PARTIAL
    assert (result.directory / "video.mp4").is_file()
    assert (result.directory / "cover.jpg").is_file()
    assert (result.directory / "metadata.json").is_file()


def test_partial_message_mentions_retry(
    config: AppConfig, manifest: Manifest
) -> None:
    drive = FakeDriveClient(fail_files={"video.mp4"})
    service = build_service(config, manifest, post=reel(), drive_client=drive)
    result = service.process(REEL_URL, drive_options(), user="naman")

    assert "retried from History" in result.message


def test_drive_connection_failure_is_partial_and_keeps_files(
    config: AppConfig, manifest: Manifest
) -> None:
    drive = FakeDriveClient(
        connect_error=GoogleDriveAuthenticationError("token expired")
    )
    service = build_service(config, manifest, post=reel(), drive_client=drive)
    result = service.process(REEL_URL, drive_options(), user="naman")

    assert result.status is JobStatus.PARTIAL
    assert (result.directory / "video.mp4").is_file()
    assert "reconnect" in result.message.lower()


def test_missing_drive_account_is_partial(
    config: AppConfig, manifest: Manifest
) -> None:
    service = build_service(config, manifest, post=reel())
    result = service.process(
        REEL_URL, drive_options(drive_account=None), user="naman"
    )

    assert result.status is JobStatus.PARTIAL
    assert "choose a Google Drive account" in result.message
    assert (result.directory / "video.mp4").is_file()


# -------------------------------------------------------------------- cleanup


def test_drive_only_with_cleanup_removes_staging(
    config: AppConfig, manifest: Manifest
) -> None:
    service = build_service(config, manifest, post=reel())
    result = service.process(
        REEL_URL,
        drive_options(
            destination_mode=DestinationMode.DRIVE_ONLY, cleanup_temp_files=True
        ),
        user="naman",
    )

    assert result.status is JobStatus.COMPLETED
    assert result.cleaned_up is True
    assert not result.directory.exists()


def test_cleanup_skipped_when_upload_failed(
    config: AppConfig, manifest: Manifest
) -> None:
    """Cleanup must never run after a failure, even in DRIVE_ONLY mode."""
    drive = FakeDriveClient(fail_files={"video.mp4"})
    service = build_service(config, manifest, post=reel(), drive_client=drive)
    result = service.process(
        REEL_URL,
        drive_options(
            destination_mode=DestinationMode.DRIVE_ONLY, cleanup_temp_files=True
        ),
        user="naman",
    )

    assert result.cleaned_up is False
    assert (result.directory / "video.mp4").is_file()


def test_local_and_drive_never_cleans_up(
    config: AppConfig, manifest: Manifest
) -> None:
    service = build_service(config, manifest, post=reel())
    result = service.process(
        REEL_URL, drive_options(cleanup_temp_files=True), user="naman"
    )

    assert result.cleaned_up is False
    assert (result.directory / "video.mp4").is_file()


# ------------------------------------------------------------------- failures


def test_invalid_url_creates_no_job(config: AppConfig, manifest: Manifest) -> None:
    service = build_service(config, manifest, post=reel())
    with pytest.raises(ValidationError):
        service.process("https://example.com/nope", local_options(), user="naman")
    assert manifest.list_jobs() == []


def test_resolve_failure_still_records_a_job(
    config: AppConfig, manifest: Manifest
) -> None:
    """A failure before resolution must leave a visible record, not silence."""
    service = build_service(
        config, manifest, fetch_error=InstagramContentUnavailableError("gone")
    )
    result = service.process(REEL_URL, local_options(), user="naman")

    assert result.status is JobStatus.FAILED
    job = manifest.get_job(result.job_id)
    assert job is not None
    assert job.status is JobStatus.FAILED
    assert job.error_detail is not None


def test_download_failure_is_recorded(
    config: AppConfig, manifest: Manifest
) -> None:
    service = build_service(
        config, manifest, post=reel(), download_error=InstagramDownloadError("boom")
    )
    result = service.process(REEL_URL, local_options(), user="naman")

    assert result.status is JobStatus.FAILED
    assert manifest.get_post_for_job(result.job_id) is None, "no post row on failure"


def test_failed_job_keeps_friendly_and_technical_messages(
    config: AppConfig, manifest: Manifest
) -> None:
    service = build_service(
        config, manifest, fetch_error=InstagramContentUnavailableError("403 denied")
    )
    result = service.process(REEL_URL, local_options(), user="naman")

    job = manifest.get_job(result.job_id)
    assert "Instagram could not provide this content" in job.error_message
    assert "403 denied" in job.error_detail


# --------------------------------------------------------------- retry upload


def test_retry_uploads_without_touching_instagram(
    config: AppConfig, manifest: Manifest
) -> None:
    drive = FakeDriveClient(fail_files={"video.mp4"})
    service = build_service(config, manifest, post=reel(), drive_client=drive)
    first = service.process(REEL_URL, drive_options(), user="naman")
    assert first.status is JobStatus.PARTIAL

    fetches_before = len(service._test_client.fetch_calls)
    downloads_before = service._test_downloader.calls

    drive.fail_files.clear()
    retry = service.retry_upload(first.job_id, user="naman")

    assert retry.status is JobStatus.COMPLETED
    assert retry.message == "Upload completed."
    assert len(service._test_client.fetch_calls) == fetches_before
    assert service._test_downloader.calls == downloads_before


def test_retry_only_sends_outstanding_files(
    config: AppConfig, manifest: Manifest
) -> None:
    drive = FakeDriveClient(fail_files={"video.mp4"})
    service = build_service(config, manifest, post=reel(), drive_client=drive)
    first = service.process(REEL_URL, drive_options(), user="naman")

    drive.fail_files.clear()
    drive.uploaded.clear()
    service.retry_upload(first.job_id, user="naman")

    assert drive.uploaded == ["video.mp4"], "already-uploaded files are not resent"


def test_retry_marks_job_completed_in_manifest(
    config: AppConfig, manifest: Manifest
) -> None:
    drive = FakeDriveClient(fail_files={"cover.jpg"})
    service = build_service(config, manifest, post=reel(), drive_client=drive)
    first = service.process(REEL_URL, drive_options(), user="naman")

    drive.fail_files.clear()
    service.retry_upload(first.job_id, user="naman")

    job = manifest.get_job(first.job_id)
    assert job.status is JobStatus.COMPLETED
    assert job.attempt_count == 2


def test_retry_that_fails_again_stays_partial(
    config: AppConfig, manifest: Manifest
) -> None:
    drive = FakeDriveClient(fail_files={"video.mp4"})
    service = build_service(config, manifest, post=reel(), drive_client=drive)
    first = service.process(REEL_URL, drive_options(), user="naman")

    retry = service.retry_upload(first.job_id, user="naman")
    assert retry.status is JobStatus.PARTIAL
    assert (first.directory / "video.mp4").is_file()


def test_retry_when_local_files_are_missing(
    config: AppConfig, manifest: Manifest
) -> None:
    drive = FakeDriveClient(fail_files={"video.mp4", "cover.jpg"})
    service = build_service(config, manifest, post=reel(), drive_client=drive)
    first = service.process(REEL_URL, drive_options(), user="naman")

    for name in ("video.mp4", "cover.jpg", "metadata.json", "document.txt"):
        path = first.directory / name
        if path.exists():
            path.unlink()

    retry = service.retry_upload(first.job_id, user="naman")
    assert retry.status is JobStatus.FAILED
    assert "Re-run the job" in retry.message


def test_retry_when_nothing_outstanding(
    config: AppConfig, manifest: Manifest
) -> None:
    service = build_service(config, manifest, post=reel())
    first = service.process(REEL_URL, drive_options(), user="naman")

    retry = service.retry_upload(first.job_id, user="naman")
    assert retry.status is JobStatus.COMPLETED
    assert "already been uploaded" in retry.message


def test_retry_unknown_job_raises(config: AppConfig, manifest: Manifest) -> None:
    service = build_service(config, manifest, post=reel())
    from utils.errors import AppError

    with pytest.raises(AppError):
        service.retry_upload("no-such-job")


# -------------------------------------------------------------------- options


def test_options_from_config_applies_saved_settings() -> None:
    config = AppConfig(
        default_destination="LOCAL_ONLY",
        drive_root_folder="My-Folder",
        organize_by_month=False,
        cleanup_temp_files=True,
    )
    options = IngestionOptions.from_config(config, MediaSelection.VIDEO)

    assert options.destination_mode is DestinationMode.LOCAL_ONLY
    assert options.root_folder == "My-Folder"
    assert options.organize_by_month is False
    assert options.cleanup_temp_files is True


def test_options_overrides_win() -> None:
    config = AppConfig(default_destination="LOCAL_ONLY")
    options = IngestionOptions.from_config(
        config,
        MediaSelection.VIDEO,
        destination_mode=DestinationMode.DRIVE_ONLY,
        drive_account="work",
    )
    assert options.destination_mode is DestinationMode.DRIVE_ONLY
    assert options.drive_account == "work"


def test_options_ignores_invalid_saved_destination() -> None:
    config = AppConfig(default_destination="NONSENSE")
    options = IngestionOptions.from_config(config, MediaSelection.VIDEO)
    assert options.destination_mode is DestinationMode.LOCAL_AND_DRIVE
