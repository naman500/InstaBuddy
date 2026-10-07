"""Google Drive layer: folder resolution, uploads, retry and error mapping.

The Drive API is entirely faked. No credentials or network access required.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from googleapiclient.errors import HttpError

from gdrive import uploader as uploader_module
from gdrive.client import DriveClient, guess_mime_type, is_retryable, wrap_http_error
from gdrive.folders import FolderResolver
from gdrive.uploader import (
    CHUNK_GRANULARITY,
    DriveUploader,
    UploadProgress,
    chunk_count,
    normalize_chunk_size,
)
from models.upload import UploadStatus
from utils.config import DriveAccountConfig
from utils.errors import GoogleDriveAuthenticationError, GoogleDriveError

CREATED = datetime(2026, 9, 21, 10, 0, tzinfo=timezone.utc)

ACCOUNT = DriveAccountConfig(
    key="personal",
    display_name="Personal Google Drive",
    client_id="cid.apps.googleusercontent.com",
    client_secret="secret",
)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(uploader_module.time, "sleep", lambda _s: None)


# --------------------------------------------------------------- fake Drive API


class FakeResponse:
    def __init__(self, status: int) -> None:
        self.status = status
        self.reason = "error"


def http_error(status: int) -> HttpError:
    return HttpError(FakeResponse(status), b'{"error": {"message": "boom"}}')


class FakeExecutable:
    """Mimics a googleapiclient request with an ``execute()`` method."""

    def __init__(self, result: Any = None, error: Exception | None = None) -> None:
        self._result = result
        self._error = error
        self.executed = 0

    def execute(self) -> Any:
        self.executed += 1
        if self._error is not None:
            raise self._error
        return self._result


class FakeChunkStatus:
    def __init__(self, progress_bytes: int) -> None:
        self.resumable_progress = progress_bytes


class FakeResumableRequest:
    """Mimics a resumable upload request driven by ``next_chunk()``."""

    def __init__(
        self,
        *,
        size: int,
        chunk_size: int,
        file_id: str = "drive-file-1",
        errors: list[Exception] | None = None,
    ) -> None:
        self.size = size
        self.chunk_size = chunk_size
        self.file_id = file_id
        self.errors = list(errors or [])
        self.uploaded = 0
        self.calls = 0

    def next_chunk(self) -> tuple[Any, dict[str, Any] | None]:
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)

        self.uploaded = min(self.uploaded + self.chunk_size, self.size)
        if self.uploaded >= self.size:
            return None, {"id": self.file_id}
        return FakeChunkStatus(self.uploaded), None


def _is_resumable(media_body: Any) -> bool:
    """``MediaUpload.resumable`` is a method, not an attribute.

    Reading it with getattr alone always yields a truthy bound method, so it has
    to be called.
    """
    attribute = getattr(media_body, "resumable", False)
    return bool(attribute()) if callable(attribute) else bool(attribute)


class FakeFiles:
    """Records calls and serves scripted responses for ``files()``."""

    def __init__(self) -> None:
        self.folders: dict[str, dict[str, Any]] = {}
        self.created_files: list[dict[str, Any]] = []
        self.list_queries: list[str] = []
        self._next_id = 1
        self.resumable_factory: Any = None
        self.create_errors: list[Exception] = []
        self.simple_create_calls = 0

    def _new_id(self, prefix: str) -> str:
        value = f"{prefix}-{self._next_id}"
        self._next_id += 1
        return value

    def list(self, q: str = "", **_kwargs: Any) -> FakeExecutable:
        self.list_queries.append(q)
        for folder_id, folder in self.folders.items():
            name_clause = f"name = '{folder['name']}'"
            parent_clause = f"'{folder['parent'] or 'root'}' in parents"
            if name_clause in q and parent_clause in q:
                return FakeExecutable({"files": [{**folder, "id": folder_id}]})
        return FakeExecutable({"files": []})

    def create(
        self, body: dict[str, Any] | None = None, media_body: Any = None, **_kwargs: Any
    ) -> Any:
        body = body or {}

        if body.get("mimeType") == "application/vnd.google-apps.folder":
            folder_id = self._new_id("folder")
            parents = body.get("parents") or []
            self.folders[folder_id] = {
                "name": body["name"],
                "parent": parents[0] if parents else None,
                "mimeType": body["mimeType"],
            }
            return FakeExecutable({"id": folder_id, "name": body["name"]})

        if media_body is not None and _is_resumable(media_body):
            request = self.resumable_factory(body, media_body)
            self.created_files.append(body)
            return request

        self.simple_create_calls += 1
        if self.create_errors:
            return FakeExecutable(error=self.create_errors.pop(0))
        self.created_files.append(body)
        return FakeExecutable({"id": self._new_id("file")})

    def get(self, **_kwargs: Any) -> FakeExecutable:
        return FakeExecutable({"id": "file-1", "name": "video.mp4", "size": "10"})

    def delete(self, **_kwargs: Any) -> FakeExecutable:
        return FakeExecutable({})


class FakeAbout:
    def get(self, **_kwargs: Any) -> FakeExecutable:
        return FakeExecutable(
            {
                "user": {"displayName": "Test User", "emailAddress": "t@example.com"},
                "storageQuota": {"limit": "1000", "usage": "250"},
            }
        )


class FakeService:
    def __init__(self) -> None:
        self._files = FakeFiles()
        self._about = FakeAbout()

    def files(self) -> FakeFiles:
        return self._files

    def about(self) -> FakeAbout:
        return self._about


@pytest.fixture
def service() -> FakeService:
    return FakeService()


@pytest.fixture
def client(service: FakeService, tmp_path: Path) -> DriveClient:
    return DriveClient(ACCOUNT, tmp_path / "tokens", service=service)


# ----------------------------------------------------------------- mime / errors


@pytest.mark.parametrize(
    "filename,expected",
    [
        ("video.mp4", "video/mp4"),
        ("cover.jpg", "image/jpeg"),
        ("image.JPEG", "image/jpeg"),
        ("shot.png", "image/png"),
        ("metadata.json", "application/json"),
        ("document.txt", "text/plain"),
        ("mystery.xyz", "application/octet-stream"),
    ],
)
def test_guess_mime_type(filename: str, expected: str) -> None:
    assert guess_mime_type(filename) == expected


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_retryable_statuses(status: int) -> None:
    assert is_retryable(http_error(status)) is True


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409])
def test_non_retryable_statuses(status: int) -> None:
    assert is_retryable(http_error(status)) is False


def test_401_maps_to_auth_error() -> None:
    assert isinstance(
        wrap_http_error(http_error(401), "ctx"), GoogleDriveAuthenticationError
    )


def test_404_maps_to_generic_error() -> None:
    error = wrap_http_error(http_error(404), "ctx")
    assert isinstance(error, GoogleDriveError)
    assert "could not be found" in error.user_message


def test_500_message_is_user_friendly() -> None:
    assert "try again" in wrap_http_error(http_error(500), "ctx").user_message.lower()


# ------------------------------------------------------------------- client


def test_connection_info_hides_credentials(client: DriveClient) -> None:
    info = client.connection_info()
    assert info["display_name"] == "Personal Google Drive"
    assert info["account_name"] == "Test User"
    assert info["storage_used"] == 250

    flattened = str(info)
    assert "secret" not in flattened
    assert "cid.apps.googleusercontent.com" not in flattened


def test_missing_credentials_raises_auth_error(tmp_path: Path) -> None:
    with pytest.raises(GoogleDriveAuthenticationError) as exc_info:
        DriveClient(ACCOUNT, tmp_path / "tokens")
    assert "not connected" in exc_info.value.user_message


def test_query_escaping_prevents_breakage(client: DriveClient) -> None:
    client.find_child("folder's name", None, folders_only=True)
    query = client.service.files().list_queries[-1]
    assert "\\'" in query


def test_folder_web_link() -> None:
    assert DriveClient.folder_web_link("abc") == (
        "https://drive.google.com/drive/folders/abc"
    )


# ------------------------------------------------------------ folder resolution


def test_resolve_chain_creates_each_level(client: DriveClient) -> None:
    resolver = FolderResolver(client)
    folder_id = resolver.resolve_chain(
        ["Instagram-Knowledge-Base", "creator1", "2026", "09", "ABC123"]
    )

    assert folder_id is not None
    folders = client.service.files().folders
    assert len(folders) == 5
    names = [f["name"] for f in folders.values()]
    assert names == ["Instagram-Knowledge-Base", "creator1", "2026", "09", "ABC123"]


def test_resolve_chain_nests_by_parent_id(client: DriveClient) -> None:
    """Each level must be parented to the one above, not to the root."""
    resolver = FolderResolver(client)
    resolver.resolve_chain(["Root", "child", "grandchild"])

    folders = client.service.files().folders
    by_name = {f["name"]: (fid, f["parent"]) for fid, f in folders.items()}

    root_id, root_parent = by_name["Root"]
    child_id, child_parent = by_name["child"]
    _, grandchild_parent = by_name["grandchild"]

    assert root_parent is None
    assert child_parent == root_id
    assert grandchild_parent == child_id


def test_resolve_chain_reuses_existing_folders(client: DriveClient) -> None:
    resolver = FolderResolver(client)
    first = resolver.resolve_chain(["Root", "creator1"])
    created_after_first = len(client.service.files().folders)

    resolver.clear_cache()
    second = resolver.resolve_chain(["Root", "creator1"])

    assert first == second
    assert len(client.service.files().folders) == created_after_first


def test_resolver_cache_avoids_repeat_lookups(client: DriveClient) -> None:
    resolver = FolderResolver(client)
    resolver.resolve_chain(["Root", "creator1"])
    queries_after_first = len(client.service.files().list_queries)

    resolver.resolve_chain(["Root", "creator1"])
    assert len(client.service.files().list_queries) == queries_after_first


def test_resolve_without_create_returns_none(client: DriveClient) -> None:
    resolver = FolderResolver(client)
    assert resolver.resolve_chain(["Missing", "Deep"], create=False) is None
    assert client.service.files().folders == {}


def test_resolve_for_post_full_tree(client: DriveClient) -> None:
    resolver = FolderResolver(client)
    folder_id, display = resolver.resolve_for_post(
        root_folder="Instagram-Knowledge-Base",
        username="creator1",
        created_at=CREATED,
        shortcode="ABC123",
    )
    assert folder_id is not None
    assert display == "Instagram-Knowledge-Base/creator1/2026/09/ABC123"


def test_resolve_for_post_respects_toggles(client: DriveClient) -> None:
    resolver = FolderResolver(client)
    _, display = resolver.resolve_for_post(
        root_folder="Root",
        username="creator1",
        created_at=CREATED,
        shortcode="ABC123",
        use_username=False,
        use_month=False,
    )
    assert display == "Root/2026/ABC123"


def test_preview_path_available_before_folders_exist(client: DriveClient) -> None:
    """The preview must show a destination even when nothing exists yet."""
    resolver = FolderResolver(client)
    folder_id, display = resolver.resolve_for_post(
        root_folder="Root",
        username="creator1",
        created_at=CREATED,
        shortcode="ABC123",
        create=False,
    )
    assert folder_id is None
    assert display == "Root/creator1/2026/09/ABC123"


# ---------------------------------------------------------------- chunk maths


@pytest.mark.parametrize(
    "requested,expected",
    [
        (0, CHUNK_GRANULARITY),
        (1, CHUNK_GRANULARITY),
        (CHUNK_GRANULARITY, CHUNK_GRANULARITY),
        (CHUNK_GRANULARITY + 1, CHUNK_GRANULARITY * 2),
        (5 * 1024 * 1024, 5 * 1024 * 1024),
        (5 * 1024 * 1024 + 100, 5 * 1024 * 1024 + CHUNK_GRANULARITY),
    ],
)
def test_chunk_size_normalised_to_granularity(requested: int, expected: int) -> None:
    normalized = normalize_chunk_size(requested)
    assert normalized == expected
    assert normalized % CHUNK_GRANULARITY == 0


@pytest.mark.parametrize(
    "size,chunk,expected",
    [(0, 100, 0), (100, 100, 1), (101, 100, 2), (250, 100, 3), (1000, 0, 0)],
)
def test_chunk_count(size: int, chunk: int, expected: int) -> None:
    assert chunk_count(size, chunk) == expected


# -------------------------------------------------------------- simple uploads


def test_small_file_uses_single_request(
    client: DriveClient, tmp_path: Path
) -> None:
    target = tmp_path / "cover.jpg"
    target.write_bytes(b"small")

    uploader = DriveUploader(client, chunk_size=CHUNK_GRANULARITY)
    result = uploader.upload_file(target, "folder-1", remote_path="Root/cover.jpg")

    assert result.status is UploadStatus.UPLOADED
    assert result.succeeded is True
    assert result.drive_file_id
    assert result.uploaded_at is not None
    assert client.service.files().simple_create_calls == 1


def test_missing_local_file_fails_without_raising(
    client: DriveClient, tmp_path: Path
) -> None:
    uploader = DriveUploader(client)
    result = uploader.upload_file(tmp_path / "nope.mp4", "folder-1")

    assert result.status is UploadStatus.FAILED
    assert "missing" in result.error_message.lower()


def test_simple_upload_retries_then_succeeds(
    client: DriveClient, tmp_path: Path
) -> None:
    target = tmp_path / "cover.jpg"
    target.write_bytes(b"small")

    files = client.service.files()
    files.create_errors = [http_error(503)]

    uploader = DriveUploader(client, max_retries=3)
    result = uploader.upload_file(target, "folder-1")

    assert result.status is UploadStatus.UPLOADED
    assert files.simple_create_calls == 2


def test_simple_upload_gives_up_after_retry_budget(
    client: DriveClient, tmp_path: Path
) -> None:
    target = tmp_path / "cover.jpg"
    target.write_bytes(b"small")

    files = client.service.files()
    files.create_errors = [http_error(503) for _ in range(5)]

    uploader = DriveUploader(client, max_retries=3)
    result = uploader.upload_file(target, "folder-1")

    assert result.status is UploadStatus.FAILED
    assert files.simple_create_calls == 3


def test_non_retryable_error_fails_immediately(
    client: DriveClient, tmp_path: Path
) -> None:
    target = tmp_path / "cover.jpg"
    target.write_bytes(b"small")

    files = client.service.files()
    files.create_errors = [http_error(403) for _ in range(3)]

    uploader = DriveUploader(client, max_retries=3)
    result = uploader.upload_file(target, "folder-1")

    assert result.status is UploadStatus.FAILED
    assert files.simple_create_calls == 1, "no retries for a permanent rejection"


def test_failed_upload_leaves_local_file_intact(
    client: DriveClient, tmp_path: Path
) -> None:
    target = tmp_path / "cover.jpg"
    target.write_bytes(b"precious")

    files = client.service.files()
    files.create_errors = [http_error(403)]

    DriveUploader(client, max_retries=1).upload_file(target, "folder-1")

    assert target.read_bytes() == b"precious"


# ------------------------------------------------------------ resumable uploads


def _install_resumable(
    client: DriveClient, *, size: int, chunk_size: int, errors=None
) -> FakeResumableRequest:
    request = FakeResumableRequest(size=size, chunk_size=chunk_size, errors=errors)
    client.service.files().resumable_factory = lambda body, media: request
    return request


def test_large_file_uses_resumable_session(
    client: DriveClient, tmp_path: Path
) -> None:
    chunk = CHUNK_GRANULARITY
    size = chunk * 4
    target = tmp_path / "video.mp4"
    target.write_bytes(b"x" * size)

    request = _install_resumable(client, size=size, chunk_size=chunk)
    uploader = DriveUploader(client, chunk_size=chunk, simple_threshold=chunk)
    result = uploader.upload_file(target, "folder-1")

    assert result.status is UploadStatus.UPLOADED
    assert result.drive_file_id == "drive-file-1"
    assert request.calls == 4, "one call per chunk"
    assert client.service.files().simple_create_calls == 0


def test_resumable_progress_reaches_100_percent(
    client: DriveClient, tmp_path: Path
) -> None:
    chunk = CHUNK_GRANULARITY
    size = chunk * 3
    target = tmp_path / "video.mp4"
    target.write_bytes(b"x" * size)

    _install_resumable(client, size=size, chunk_size=chunk)
    uploader = DriveUploader(client, chunk_size=chunk, simple_threshold=chunk)

    seen: list[UploadProgress] = []
    uploader.upload_file(target, "folder-1", progress=seen.append)

    assert seen, "progress must be reported"
    assert seen[-1].percent == 100
    assert seen[-1].fraction == pytest.approx(1.0)
    assert [p.bytes_done for p in seen] == sorted(p.bytes_done for p in seen)
    assert seen[0].message.startswith("Uploading video.mp4")


def test_chunk_failure_retries_same_session(
    client: DriveClient, tmp_path: Path
) -> None:
    """A blip mid-transfer must not restart the whole upload."""
    chunk = CHUNK_GRANULARITY
    size = chunk * 3
    target = tmp_path / "video.mp4"
    target.write_bytes(b"x" * size)

    request = _install_resumable(
        client, size=size, chunk_size=chunk, errors=[http_error(503)]
    )
    uploader = DriveUploader(
        client, chunk_size=chunk, simple_threshold=chunk, max_retries=3
    )
    result = uploader.upload_file(target, "folder-1")

    assert result.status is UploadStatus.UPLOADED
    assert request.calls == 4, "3 chunks plus the one retried call"


def test_resumable_gives_up_after_consecutive_failures(
    client: DriveClient, tmp_path: Path
) -> None:
    chunk = CHUNK_GRANULARITY
    size = chunk * 3
    target = tmp_path / "video.mp4"
    target.write_bytes(b"x" * size)

    _install_resumable(
        client, size=size, chunk_size=chunk, errors=[http_error(503) for _ in range(6)]
    )
    uploader = DriveUploader(
        client, chunk_size=chunk, simple_threshold=chunk, max_retries=3
    )
    result = uploader.upload_file(target, "folder-1")

    assert result.status is UploadStatus.FAILED
    assert target.is_file(), "local file survives a failed upload"


def test_progress_snapshot_edge_cases() -> None:
    assert UploadProgress("f", 0, 0).fraction == 0.0
    assert UploadProgress("f", 50, 100).percent == 50
    assert UploadProgress("f", 500, 100).fraction == 1.0


# ---------------------------------------------------------------- batch uploads


def test_upload_files_continues_past_a_failure(
    client: DriveClient, tmp_path: Path
) -> None:
    """One bad file must not block the rest of the post."""
    good = tmp_path / "cover.jpg"
    good.write_bytes(b"ok")
    missing = tmp_path / "gone.mp4"
    another = tmp_path / "metadata.json"
    another.write_bytes(b"{}")

    uploader = DriveUploader(client)
    results = uploader.upload_files(
        [good, missing, another], "folder-1", remote_prefix="Root/ABC123"
    )

    assert [r.status for r in results] == [
        UploadStatus.UPLOADED,
        UploadStatus.FAILED,
        UploadStatus.UPLOADED,
    ]
    assert results[0].remote_path == "Root/ABC123/cover.jpg"


def test_upload_files_reports_indices(client: DriveClient, tmp_path: Path) -> None:
    files = []
    for name in ("a.jpg", "b.jpg"):
        path = tmp_path / name
        path.write_bytes(b"x")
        files.append(path)

    seen: list[UploadProgress] = []
    DriveUploader(client).upload_files(files, "folder-1", progress=seen.append)

    assert [(p.file_index, p.file_count) for p in seen] == [(1, 2), (2, 2)]
