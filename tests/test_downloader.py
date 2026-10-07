"""Media downloader: streaming, atomic writes, retry and progress."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator

import pytest
from instaloader import exceptions as ig_errors

from instagram import downloader as downloader_module
from instagram.downloader import PART_SUFFIX, DownloadProgress, MediaDownloader
from instagram.resolver import build_post_model
from models.media import ContentType, MediaSelection
from utils.errors import (
    InstagramContentUnavailableError,
    InstagramDownloadError,
    InstagramRateLimitError,
)
from tests.conftest import FakePost

REEL_URL = "https://www.instagram.com/reel/ABC123/"
POST_URL = "https://www.instagram.com/p/ABC123/"


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep retry tests instant."""
    monkeypatch.setattr(downloader_module.time, "sleep", lambda _s: None)


class FakeResponse:
    """Mimics the streaming ``requests.Response`` that ``get_raw`` returns."""

    def __init__(self, payload: bytes = b"data", chunk_size: int = 4) -> None:
        self._payload = payload
        self._chunk_size = chunk_size
        self.headers = {"Content-Length": str(len(payload))}
        self.closed = False

    def iter_content(self, chunk_size: int = 1024) -> Iterator[bytes]:
        size = self._chunk_size or chunk_size
        for start in range(0, len(self._payload), size):
            yield self._payload[start : start + size]

    def close(self) -> None:
        self.closed = True


class FakeFetcher:
    """Scripted fetcher: raises queued errors, then serves payloads."""

    def __init__(
        self,
        payload: bytes = b"binary-content",
        errors: list[Exception] | None = None,
    ) -> None:
        self.payload = payload
        self.errors = list(errors or [])
        self.calls: list[str] = []
        self.responses: list[FakeResponse] = []

    def __call__(self, url: str) -> FakeResponse:
        self.calls.append(url)
        if self.errors:
            raise self.errors.pop(0)
        response = FakeResponse(self.payload)
        self.responses.append(response)
        return response


def make_downloader(fetch: Any, max_retries: int = 3) -> MediaDownloader:
    return MediaDownloader(object(), max_retries=max_retries, fetch=fetch)


@pytest.fixture
def reel_model(reel_post: FakePost):
    return build_post_model(reel_post, url=REEL_URL, url_hint="reel")


# ------------------------------------------------------------------ happy paths


def test_downloads_reel_video_and_cover(tmp_path: Path, reel_model) -> None:
    fetch = FakeFetcher(payload=b"movie-bytes")
    files = make_downloader(fetch).download_items(
        reel_model,
        reel_model.items_for_selection(MediaSelection.VIDEO_AND_COVER),
        tmp_path,
    )

    assert [f.filename for f in files] == ["video.mp4", "cover.jpg"]
    assert (tmp_path / "video.mp4").read_bytes() == b"movie-bytes"
    assert all(f.file_size == len(b"movie-bytes") for f in files)
    assert all(len(f.sha256) == 64 for f in files)


def test_carousel_ordering_is_preserved(tmp_path: Path, mixed_carousel_post) -> None:
    model = build_post_model(mixed_carousel_post, url=POST_URL, url_hint="p")
    files = make_downloader(FakeFetcher()).download_items(
        model, model.items_for_selection(MediaSelection.IMAGES_AND_VIDEOS), tmp_path
    )
    assert [f.filename for f in files] == ["001.jpg", "002.mp4", "003.jpg", "004.mp4"]


def test_creates_target_directory(tmp_path: Path, reel_model) -> None:
    target = tmp_path / "creator" / "2026" / "09" / "ABC123"
    make_downloader(FakeFetcher()).download_items(
        reel_model, reel_model.items_for_selection(MediaSelection.VIDEO), target
    )
    assert (target / "video.mp4").is_file()


def test_no_part_files_remain(tmp_path: Path, reel_model) -> None:
    make_downloader(FakeFetcher()).download_items(
        reel_model,
        reel_model.items_for_selection(MediaSelection.VIDEO_AND_COVER),
        tmp_path,
    )
    assert list(tmp_path.glob(f"*{PART_SUFFIX}")) == []


def test_response_is_closed(tmp_path: Path, reel_model) -> None:
    fetch = FakeFetcher()
    make_downloader(fetch).download_items(
        reel_model, reel_model.items_for_selection(MediaSelection.VIDEO), tmp_path
    )
    assert all(r.closed for r in fetch.responses)


def test_large_payload_streams_in_chunks(tmp_path: Path, reel_model) -> None:
    payload = bytes(range(256)) * 4096  # ~1 MB
    fetch = FakeFetcher(payload=payload)
    files = make_downloader(fetch).download_items(
        reel_model, reel_model.items_for_selection(MediaSelection.VIDEO), tmp_path
    )
    assert files[0].file_size == len(payload)
    assert (tmp_path / "video.mp4").read_bytes() == payload


# ---------------------------------------------------------------------- failures


def test_empty_selection_raises(tmp_path: Path, reel_model) -> None:
    with pytest.raises(InstagramDownloadError) as exc_info:
        make_downloader(FakeFetcher()).download_items(reel_model, [], tmp_path)
    assert "at least one" in exc_info.value.user_message


def test_zero_byte_download_is_rejected(tmp_path: Path, reel_model) -> None:
    fetch = FakeFetcher(payload=b"")
    with pytest.raises(InstagramDownloadError) as exc_info:
        make_downloader(fetch).download_items(
            reel_model, reel_model.items_for_selection(MediaSelection.VIDEO), tmp_path
        )
    assert "empty" in exc_info.value.user_message
    assert not (tmp_path / "video.mp4").exists()
    assert list(tmp_path.glob(f"*{PART_SUFFIX}")) == []


def test_retries_transient_error_then_succeeds(tmp_path: Path, reel_model) -> None:
    fetch = FakeFetcher(errors=[ig_errors.ConnectionException("blip")])
    files = make_downloader(fetch, max_retries=3).download_items(
        reel_model, reel_model.items_for_selection(MediaSelection.VIDEO), tmp_path
    )
    assert len(fetch.calls) == 2, "one failure, one success"
    assert files[0].filename == "video.mp4"


def test_retry_budget_is_respected(tmp_path: Path, reel_model) -> None:
    fetch = FakeFetcher(errors=[ig_errors.ConnectionException("x") for _ in range(5)])
    with pytest.raises(InstagramDownloadError):
        make_downloader(fetch, max_retries=3).download_items(
            reel_model, reel_model.items_for_selection(MediaSelection.VIDEO), tmp_path
        )
    assert len(fetch.calls) == 3, "must not exceed the retry budget"


@pytest.mark.parametrize(
    "error",
    [
        ig_errors.TooManyRequestsException("slow down"),
        # How Instaloader reports a 429 when its own retries are disabled.
        ig_errors.ConnectionException(
            "JSON Query to api/v1/x: 429 Too Many Requests when accessing ..."
        ),
    ],
)
def test_rate_limit_stops_without_retry(tmp_path: Path, reel_model, error) -> None:
    """Rate limits last minutes to hours; quick retries only extend them."""
    fetch = FakeFetcher(errors=[error for _ in range(3)])
    with pytest.raises(InstagramRateLimitError):
        make_downloader(fetch, max_retries=3).download_items(
            reel_model, reel_model.items_for_selection(MediaSelection.VIDEO), tmp_path
        )
    assert len(fetch.calls) == 1


def test_forbidden_fails_fast_without_retry(tmp_path: Path, reel_model) -> None:
    """An expired signed CDN URL is permanent; retrying it is pointless."""
    fetch = FakeFetcher(
        errors=[ig_errors.QueryReturnedForbiddenException("403") for _ in range(3)]
    )
    with pytest.raises(InstagramContentUnavailableError) as exc_info:
        make_downloader(fetch, max_retries=3).download_items(
            reel_model, reel_model.items_for_selection(MediaSelection.VIDEO), tmp_path
        )
    assert len(fetch.calls) == 1, "no retries for a permanent rejection"
    assert "expired" in exc_info.value.user_message


def test_partial_file_cleaned_up_on_failure(tmp_path: Path, reel_model) -> None:
    fetch = FakeFetcher(errors=[ig_errors.ConnectionException("x") for _ in range(3)])
    with pytest.raises(InstagramDownloadError):
        make_downloader(fetch, max_retries=3).download_items(
            reel_model, reel_model.items_for_selection(MediaSelection.VIDEO), tmp_path
        )
    assert list(tmp_path.glob("*")) == []


def test_earlier_files_survive_a_later_failure(tmp_path: Path, reel_model) -> None:
    """A cover failure must not remove an already-downloaded video."""

    class PartialFetcher:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self, url: str) -> FakeResponse:
            self.calls += 1
            if self.calls == 1:
                return FakeResponse(b"video-ok")
            raise ig_errors.ConnectionException("cover failed")

    with pytest.raises(InstagramDownloadError):
        make_downloader(PartialFetcher(), max_retries=1).download_items(
            reel_model,
            reel_model.items_for_selection(MediaSelection.VIDEO_AND_COVER),
            tmp_path,
        )

    assert (tmp_path / "video.mp4").read_bytes() == b"video-ok"


def test_downloader_requires_a_usable_client() -> None:
    with pytest.raises(InstagramDownloadError):
        MediaDownloader(object())


# ---------------------------------------------------------------------- progress


def test_progress_reports_each_item(tmp_path: Path, reel_model) -> None:
    seen: list[DownloadProgress] = []
    make_downloader(FakeFetcher()).download_items(
        reel_model,
        reel_model.items_for_selection(MediaSelection.VIDEO_AND_COVER),
        tmp_path,
        progress=seen.append,
    )

    assert {p.filename for p in seen} == {"video.mp4", "cover.jpg"}
    assert seen[0].total_items == 2
    assert seen[0].message == "Downloading media 1/2..."
    assert seen[-1].fraction == pytest.approx(1.0)


def test_progress_fraction_is_monotonic(tmp_path: Path, reel_model) -> None:
    fractions: list[float] = []
    make_downloader(FakeFetcher(payload=b"x" * 64)).download_items(
        reel_model,
        reel_model.items_for_selection(MediaSelection.VIDEO_AND_COVER),
        tmp_path,
        progress=lambda p: fractions.append(p.fraction),
    )
    assert fractions == sorted(fractions)
    assert all(0.0 <= f <= 1.0 for f in fractions)


def test_progress_fraction_handles_unknown_total() -> None:
    progress = DownloadProgress(1, 2, "video.mp4", bytes_done=100, bytes_total=None)
    assert progress.fraction == 0.0
    assert DownloadProgress(1, 0, "x").fraction == 0.0
