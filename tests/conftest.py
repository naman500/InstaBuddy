"""Shared test fixtures.

Instaloader and the Google Drive API are always faked. The unit suite makes no
network calls, so it is deterministic and runs without credentials.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Iterator

import pytest

CREATED_UTC = datetime(2026, 9, 21, 10, 0, 0, tzinfo=timezone.utc)


class FakeSidecarNode:
    """Stands in for :class:`instaloader.structures.PostSidecarNode`."""

    def __init__(
        self,
        *,
        is_video: bool = False,
        display_url: str | None = "https://cdn.example/img.jpg",
        video_url: str | None = None,
    ) -> None:
        self.is_video = is_video
        self.display_url = display_url
        self.video_url = video_url or (
            "https://cdn.example/vid.mp4" if is_video else None
        )


class FakePost:
    """Stands in for :class:`instaloader.Post`.

    Mirrors only the attributes the resolver actually reads, including the
    private ``_node`` dict that carries ``product_type``.
    """

    def __init__(
        self,
        *,
        typename: str = "GraphImage",
        shortcode: str = "ABC123",
        mediaid: int | None = 123456789,
        owner_username: str = "example_user",
        caption: str | None = "An example caption",
        date_utc: datetime | None = CREATED_UTC,
        is_video: bool = False,
        url: str | None = "https://cdn.example/display.jpg",
        video_url: str | None = None,
        product_type: str | None = None,
        sidecar_nodes: list[FakeSidecarNode] | None = None,
        sidecar_raises: Exception | None = None,
    ) -> None:
        self.typename = typename
        self.shortcode = shortcode
        self.mediaid = mediaid
        self.owner_username = owner_username
        self.caption = caption
        self.date_utc = date_utc
        self.is_video = is_video
        self.url = url
        self.video_url = video_url
        self._node: dict[str, Any] = {"shortcode": shortcode}
        if product_type:
            self._node["product_type"] = product_type
        self._sidecar_nodes = sidecar_nodes or []
        self._sidecar_raises = sidecar_raises

    def get_sidecar_nodes(self, start: int = 0, end: int = -1) -> Iterator[Any]:
        if self._sidecar_raises is not None:
            raise self._sidecar_raises
        return iter(self._sidecar_nodes)


class FakeClient:
    """Stands in for :class:`instagram.client.InstagramClient`."""

    def __init__(self, post: Any = None, error: Exception | None = None) -> None:
        self._post = post
        self._error = error
        self.requested: list[str] = []
        self.is_authenticated = False

    def fetch_post(self, shortcode: str) -> Any:
        self.requested.append(shortcode)
        if self._error is not None:
            raise self._error
        return self._post


# ------------------------------------------------------------------ factories


@pytest.fixture
def image_post() -> FakePost:
    return FakePost(typename="GraphImage", is_video=False)


@pytest.fixture
def video_post() -> FakePost:
    return FakePost(
        typename="GraphVideo",
        is_video=True,
        video_url="https://cdn.example/video.mp4",
    )


@pytest.fixture
def reel_post() -> FakePost:
    return FakePost(
        typename="GraphVideo",
        is_video=True,
        video_url="https://cdn.example/reel.mp4",
        product_type="clips",
    )


@pytest.fixture
def mixed_carousel_post() -> FakePost:
    return FakePost(
        typename="GraphSidecar",
        sidecar_nodes=[
            FakeSidecarNode(is_video=False, display_url="https://cdn.example/1.jpg"),
            FakeSidecarNode(is_video=True, video_url="https://cdn.example/2.mp4"),
            FakeSidecarNode(is_video=False, display_url="https://cdn.example/3.jpg"),
            FakeSidecarNode(is_video=True, video_url="https://cdn.example/4.mp4"),
        ],
    )


@pytest.fixture
def image_carousel_post() -> FakePost:
    return FakePost(
        typename="GraphSidecar",
        sidecar_nodes=[
            FakeSidecarNode(is_video=False, display_url="https://cdn.example/1.jpg"),
            FakeSidecarNode(is_video=False, display_url="https://cdn.example/2.jpg"),
        ],
    )


@pytest.fixture
def isolated_config(tmp_path, monkeypatch) -> Any:
    """An AppConfig rooted in a temp directory, with caches cleared."""
    from utils import config as config_module

    for key in (
        "DOWNLOAD_DIR",
        "TEMP_DIR",
        "SESSION_DIR",
        "TOKEN_DIR",
        "DATA_DIR",
        "LOG_LEVEL",
    ):
        monkeypatch.delenv(key, raising=False)

    monkeypatch.setenv("DOWNLOAD_DIR", str(tmp_path / "downloads"))
    monkeypatch.setenv("TEMP_DIR", str(tmp_path / "temp"))
    monkeypatch.setenv("SESSION_DIR", str(tmp_path / "sessions"))
    monkeypatch.setenv("TOKEN_DIR", str(tmp_path / "tokens"))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))

    monkeypatch.setattr(
        config_module, "SETTINGS_OVERLAY_PATH", tmp_path / "settings.json"
    )
    monkeypatch.setattr(config_module, "SECRETS_PATH", tmp_path / "secrets.toml")

    config_module.get_config.cache_clear()
    config_module.load_secrets.cache_clear()
    config = config_module.get_config()
    config.ensure_directories()
    yield config
    config_module.get_config.cache_clear()
    config_module.load_secrets.cache_clear()


def pytest_configure(config: Any) -> None:
    config.addinivalue_line(
        "markers", "integration: hits live services; opt-in via RUN_INTEGRATION_TESTS"
    )


def pytest_collection_modifyitems(config: Any, items: list[Any]) -> None:
    """Skip integration tests unless explicitly enabled."""
    if os.environ.get("RUN_INTEGRATION_TESTS", "").lower() == "true":
        return
    skip = pytest.mark.skip(reason="Set RUN_INTEGRATION_TESTS=true to run")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)
