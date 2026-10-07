"""End-to-end tests for profile (batch) ingestion.

Reuses the single-post test doubles. A fake client that can both list a
profile's shortcodes and resolve each one lets us drive ``process_profile``
through the real per-post pipeline with no network.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from models.job import JobStatus
from models.media import DestinationMode, MediaSelection
from services.ingestion import (
    IngestionOptions,
    IngestionService,
    ProfileAnalysis,
    ProfileMediaRule,
)
from services.manifest import Manifest
from utils import config as config_module
from utils.config import AppConfig
from utils.errors import InstagramContentUnavailableError, ValidationError
from utils.validation import (
    canonical_profile_url,
    extract_username,
    is_profile_url,
    is_valid_instagram_url,
)

from tests.conftest import FakePost
from tests.test_ingestion import (
    ACCOUNT,
    FakeDownloader,
    FakeDriveClient,
    FakeUploader,  # noqa: F401 - imported so autouse patching target exists
)


# ------------------------------------------------------------------ test doubles


class FakeProfileClient:
    """Lists shortcodes and resolves each to a (possibly distinct) FakePost."""

    def __init__(self, posts_by_shortcode: dict[str, Any]) -> None:
        self._posts = posts_by_shortcode
        self.context = object()
        self.listed: list[str] = []
        self.fetched: list[str] = []

    def fetch_profile_posts(
        self, username: str, *, limit: int | None = None
    ) -> list[Any]:
        self.listed.append(username)
        posts = list(self._posts.values())
        return posts if limit is None else posts[:limit]

    def fetch_post(self, shortcode: str) -> Any:
        self.fetched.append(shortcode)
        post = self._posts.get(shortcode)
        if post is None:
            raise InstagramContentUnavailableError(f"missing {shortcode}")
        return post


# ---------------------------------------------------------------------- fixtures


@pytest.fixture(autouse=True)
def patch_drive(monkeypatch: pytest.MonkeyPatch):
    import services.ingestion as ingestion_module
    from tests.test_ingestion import FakeUploader as _Uploader

    _Uploader.instances.clear()
    monkeypatch.setattr(ingestion_module, "DriveUploader", _Uploader)
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


def build_profile_service(
    config: AppConfig,
    manifest: Manifest,
    posts_by_shortcode: dict[str, Any],
) -> tuple[IngestionService, FakeProfileClient]:
    client = FakeProfileClient(posts_by_shortcode)
    downloader = FakeDownloader()
    drive = FakeDriveClient()
    service = IngestionService(
        config,
        manifest,
        instagram_client=client,  # type: ignore[arg-type]
        downloader_factory=lambda _c: downloader,  # type: ignore[arg-type,return-value]
        drive_client_factory=lambda _a: drive,  # type: ignore[arg-type,return-value]
    )
    service.batch_delay_seconds = 0
    service._test_downloader = downloader  # type: ignore[attr-defined]
    return service, client


def img(shortcode: str) -> FakePost:
    return FakePost(
        typename="GraphImage",
        is_video=False,
        shortcode=shortcode,
        owner_username="creator1",
        url=f"https://cdn.example/{shortcode}.jpg",
    )


def vid(shortcode: str) -> FakePost:
    return FakePost(
        typename="GraphVideo",
        is_video=True,
        shortcode=shortcode,
        owner_username="creator1",
        video_url=f"https://cdn.example/{shortcode}.mp4",
        product_type="clips",
    )


def local_opts() -> IngestionOptions:
    return IngestionOptions(
        selection=MediaSelection.IMAGE,
        destination_mode=DestinationMode.LOCAL_ONLY,
    )


# ------------------------------------------------------------------------- URL


def test_profile_url_detection():
    assert is_profile_url("https://www.instagram.com/nasa/") is True
    assert extract_username("https://instagram.com/nasa/") == "nasa"
    # Single-post URLs must not be treated as profiles.
    assert is_profile_url("https://www.instagram.com/p/ABC123/") is False
    assert is_profile_url("https://www.instagram.com/reel/ABC123/") is False
    # Reserved feature paths are not profiles.
    assert is_profile_url("https://www.instagram.com/explore/") is False
    # And a real post URL still validates as a post.
    assert is_valid_instagram_url("https://www.instagram.com/p/ABC123/") is True


def test_canonical_profile_url():
    assert canonical_profile_url("nasa") == "https://www.instagram.com/nasa/"
    with pytest.raises(ValidationError):
        canonical_profile_url("bad name!")


# --------------------------------------------------------------------- analyze


def test_analyze_profile_lists_and_caps(config, manifest):
    posts = {"AA111": img("AA111"), "BB222": vid("BB222"), "CC333": img("CC333")}
    service, client = build_profile_service(config, manifest, posts)

    analysis = service.analyze_profile(
        "https://www.instagram.com/creator1/", limit=2
    )
    assert isinstance(analysis, ProfileAnalysis)
    assert analysis.username == "creator1"
    assert analysis.post_count == 2
    assert client.listed == ["creator1"]


# --------------------------------------------------------------------- batch run


def test_process_profile_downloads_all(config, manifest):
    posts = {"AA111": img("AA111"), "BB222": vid("BB222")}
    service, _ = build_profile_service(config, manifest, posts)
    analysis = ProfileAnalysis(
        username="creator1",
        url="https://www.instagram.com/creator1/",
        shortcodes=["AA111", "BB222"],
    )

    batch = service.process_profile(
        analysis, ProfileMediaRule.EVERYTHING, local_opts(), user="admin"
    )

    assert batch.total == 2
    assert batch.succeeded == 2
    assert batch.failed == 0
    # Each post became its own completed job with real files on disk.
    for item in batch.items:
        assert item.status is JobStatus.COMPLETED
        assert item.result is not None
        assert item.result.directory is not None
        assert Path(item.result.directory).exists()


def test_videos_only_skips_image_posts(config, manifest):
    posts = {"AA111": img("AA111"), "BB222": vid("BB222")}
    service, _ = build_profile_service(config, manifest, posts)
    analysis = ProfileAnalysis(
        username="creator1",
        url="https://www.instagram.com/creator1/",
        shortcodes=["AA111", "BB222"],
    )

    batch = service.process_profile(
        analysis, ProfileMediaRule.VIDEOS_ONLY, local_opts(), user="admin"
    )

    assert batch.total == 2
    assert batch.succeeded == 1  # the video
    assert batch.skipped == 1  # the image post, nothing matches the rule


def test_duplicates_skipped_on_second_run(config, manifest):
    posts = {"AA111": img("AA111")}
    service, _ = build_profile_service(config, manifest, posts)
    analysis = ProfileAnalysis(
        username="creator1",
        url="https://www.instagram.com/creator1/",
        shortcodes=["AA111"],
    )

    first = service.process_profile(
        analysis, ProfileMediaRule.EVERYTHING, local_opts(), user="admin"
    )
    assert first.succeeded == 1

    second = service.process_profile(
        analysis, ProfileMediaRule.EVERYTHING, local_opts(), user="admin"
    )
    assert second.skipped == 1
    assert second.succeeded == 0


def test_one_failure_does_not_stop_batch(config, manifest):
    # BB222 is listed but not resolvable, so it fails; the others still run.
    posts = {"AA111": img("AA111"), "CC333": img("CC333")}
    service, _ = build_profile_service(config, manifest, posts)
    analysis = ProfileAnalysis(
        username="creator1",
        url="https://www.instagram.com/creator1/",
        shortcodes=["AA111", "BB222", "CC333"],
    )

    batch = service.process_profile(
        analysis, ProfileMediaRule.EVERYTHING, local_opts(), user="admin"
    )

    assert batch.total == 3
    assert batch.succeeded == 2
    assert batch.failed == 1


# ------------------------------------------------------- request reduction


def _analysis_from(service: IngestionService, limit: int | None = None):
    return service.analyze_profile("https://www.instagram.com/creator1/", limit=limit)


def test_listing_data_reused_without_second_lookup(config, manifest):
    posts = {"AA111": img("AA111"), "BB222": vid("BB222")}
    service, client = build_profile_service(config, manifest, posts)

    batch = service.process_profile(
        _analysis_from(service), ProfileMediaRule.EVERYTHING, local_opts(), user="a"
    )

    assert batch.succeeded == 2
    assert client.fetched == [], "posts must not be looked up a second time"


def test_known_duplicates_skip_without_any_request(config, manifest):
    posts = {"AA111": img("AA111")}
    service, client = build_profile_service(config, manifest, posts)
    analysis = ProfileAnalysis(
        username="creator1",
        url="https://www.instagram.com/creator1/",
        shortcodes=["AA111"],
    )
    service.process_profile(analysis, ProfileMediaRule.EVERYTHING, local_opts())
    client.fetched.clear()
    calls_before = service._test_downloader.calls

    again = service.process_profile(analysis, ProfileMediaRule.EVERYTHING, local_opts())

    assert again.skipped == 1
    assert client.fetched == []
    assert service._test_downloader.calls == calls_before


def test_pause_between_posts(config, manifest, monkeypatch):
    posts = {"AA111": img("AA111"), "BB222": img("BB222"), "CC333": img("CC333")}
    service, _ = build_profile_service(config, manifest, posts)
    service.batch_delay_seconds = 2.5

    import services.ingestion as ingestion_module

    sleeps: list[float] = []
    monkeypatch.setattr(ingestion_module.time, "sleep", sleeps.append)

    service.process_profile(
        _analysis_from(service), ProfileMediaRule.EVERYTHING, local_opts()
    )
    assert sleeps == [2.5, 2.5], "pause between posts, not before the first"


def test_rate_limit_stops_the_batch(config, manifest):
    from utils.errors import InstagramRateLimitError

    posts = {"AA111": img("AA111"), "BB222": img("BB222"), "CC333": img("CC333")}
    service, _ = build_profile_service(config, manifest, posts)
    downloader = service._test_downloader
    original = downloader.download_items

    def limited(post, items, directory, *, progress=None):
        if post.shortcode == "BB222":
            raise InstagramRateLimitError("429")
        return original(post, items, directory, progress=progress)

    downloader.download_items = limited

    batch = service.process_profile(
        _analysis_from(service), ProfileMediaRule.EVERYTHING, local_opts()
    )

    assert batch.succeeded == 1
    assert batch.failed == 1
    assert batch.stopped_reason
    assert batch.not_attempted == ["CC333"]
    assert [item.shortcode for item in batch.items] == ["AA111", "BB222"]


# ---------------------------------------------------- 429 classification


def test_is_rate_limited_detects_disguised_429():
    from instaloader import exceptions as ig_errors

    from instagram.client import is_rate_limited

    assert is_rate_limited(ig_errors.TooManyRequestsException("x"))
    assert is_rate_limited(
        ig_errors.ConnectionException(
            "JSON Query to api/v1/users/web_profile_info/: 429 Too Many Requests "
            "when accessing https://www.instagram.com/api/v1/users/..."
        )
    )
    assert not is_rate_limited(ig_errors.ConnectionException("timed out"))
    assert not is_rate_limited(ig_errors.ConnectionException("shortcode AB429x"))


def test_profile_lookup_429_is_not_retried(monkeypatch):
    import instaloader
    from instaloader import exceptions as ig_errors

    from instagram.client import InstagramClient
    from utils.errors import InstagramRateLimitError

    calls: list[str] = []

    def from_username(_context, username):
        calls.append(username)
        raise ig_errors.ConnectionException(
            "JSON Query to api/v1/users/web_profile_info/: 429 Too Many Requests"
        )

    monkeypatch.setattr(instaloader.Profile, "from_username", from_username)
    client = InstagramClient(max_retries=3)

    with pytest.raises(InstagramRateLimitError) as info:
        client.fetch_profile_posts("someone")
    assert calls == ["someone"], "a 429 must not be retried"
    assert "30-60 minutes" in info.value.user_message


# ------------------------------------------------------------- URL list


def test_parse_url_list_dedupes_and_classifies():
    from utils.validation import parse_url_list

    text = """
    # my saved reels
    https://www.instagram.com/reel/AAAAA11111/
    https://www.instagram.com/p/AAAAA11111/?igsh=x   , instagram.com/p/BBBBB22222/
    https://www.instagram.com/nasa/
    https://www.instagram.com/p/bad!/
    url
    https://example.com/p/CCCCC33333/
    """
    parsed = parse_url_list(text)

    assert parsed.shortcodes == ["AAAAA11111", "BBBBB22222"]
    assert parsed.entries[0].url == "https://www.instagram.com/reel/AAAAA11111/"
    assert parsed.duplicates == 1
    assert parsed.profile_links == ["https://www.instagram.com/nasa/"]
    assert parsed.invalid == ["https://www.instagram.com/p/bad!/"]


def test_parse_url_list_caps_entries():
    from utils.validation import parse_url_list

    text = "\n".join(f"https://www.instagram.com/p/CODE{i:06d}/" for i in range(5))
    parsed = parse_url_list(text, max_entries=3)
    assert len(parsed.entries) == 3
    assert parsed.truncated == 2


def test_url_list_batch_downloads_each_link(config, manifest):
    from utils.validation import parse_url_list

    posts = {"AA111AA": img("AA111AA"), "BB222BB": vid("BB222BB")}
    service, client = build_profile_service(config, manifest, posts)
    parsed = parse_url_list(
        "https://www.instagram.com/p/AA111AA/\n"
        "https://www.instagram.com/reel/BB222BB/"
    )
    analysis = ProfileAnalysis.from_url_list(parsed.entries)

    batch = service.process_profile(
        analysis, ProfileMediaRule.EVERYTHING, local_opts(), user="admin"
    )

    assert batch.succeeded == 2
    assert client.listed == [], "no profile lookup for a URL list"
    assert client.fetched == ["AA111AA", "BB222BB"], "one lookup per link"
    # The user's own link is what gets recorded.
    urls = [item.result.post.url for item in batch.items]
    assert urls[1] == "https://www.instagram.com/reel/BB222BB/"
