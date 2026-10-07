"""Optional integration tests that contact live services.

These never run automatically. Enable them explicitly::

    RUN_INTEGRATION_TESTS=true .venv/bin/python -m pytest tests/integration -v

Requirements:

* Instagram tests need a public post URL you are authorised to download, set via
  ``INTEGRATION_INSTAGRAM_URL``.
* Google Drive tests need an account already connected from the Google Drive
  page, named via ``INTEGRATION_DRIVE_ACCOUNT``.

Anything not configured is skipped rather than failed, so a partial setup still
gives useful signal.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from gdrive.client import DriveClient
from gdrive.folders import FolderResolver
from gdrive.uploader import DriveUploader
from instagram.client import InstagramClient
from instagram.resolver import resolve_post
from models.upload import UploadStatus
from utils.config import get_config, get_drive_account, get_drive_accounts

pytestmark = pytest.mark.integration


INSTAGRAM_URL = os.environ.get("INTEGRATION_INSTAGRAM_URL", "")
DRIVE_ACCOUNT = os.environ.get("INTEGRATION_DRIVE_ACCOUNT", "")


# ------------------------------------------------------------------- Instagram


@pytest.mark.skipif(not INSTAGRAM_URL, reason="Set INTEGRATION_INSTAGRAM_URL")
def test_resolve_a_real_public_post() -> None:
    """Analyze a real post. Downloads nothing."""
    config = get_config()
    client = InstagramClient(max_retries=config.max_instagram_retries)
    try:
        post = resolve_post(INSTAGRAM_URL, client)
    finally:
        client.close()

    assert post.shortcode
    assert post.username
    assert post.media_items, "a real post should expose at least one media item"
    print(
        f"\nResolved {post.content_type.value} by {post.username} "
        f"with {post.media_count} media item(s)"
    )


# ---------------------------------------------------------------- Google Drive


@pytest.mark.skipif(not DRIVE_ACCOUNT, reason="Set INTEGRATION_DRIVE_ACCOUNT")
def test_drive_account_is_configured() -> None:
    keys = [account.key for account in get_drive_accounts()]
    assert DRIVE_ACCOUNT in keys, f"{DRIVE_ACCOUNT} not in {keys}"


@pytest.mark.skipif(not DRIVE_ACCOUNT, reason="Set INTEGRATION_DRIVE_ACCOUNT")
def test_drive_connection_works() -> None:
    config = get_config()
    client = DriveClient(get_drive_account(DRIVE_ACCOUNT), config.token_path)
    info = client.connection_info()

    assert info["account_email"], "connected account should report an email"
    print(f"\nConnected as {info['account_email']}")


@pytest.mark.skipif(not DRIVE_ACCOUNT, reason="Set INTEGRATION_DRIVE_ACCOUNT")
def test_round_trip_small_upload(tmp_path: Path) -> None:
    """Create a folder, upload a small file, verify it, then delete it."""
    config = get_config()
    client = DriveClient(get_drive_account(DRIVE_ACCOUNT), config.token_path)

    resolver = FolderResolver(client)
    folder_id = resolver.resolve_chain(
        [config.drive_root_folder, "_integration_test"]
    )
    assert folder_id

    payload = b"integration test payload"
    local = tmp_path / "integration_test.txt"
    local.write_bytes(payload)

    uploader = DriveUploader(client, chunk_size=config.upload_chunk_size)
    result = uploader.upload_file(local, folder_id, remote_path="test/integration.txt")

    assert result.status is UploadStatus.UPLOADED, result.error_message
    assert result.drive_file_id

    try:
        remote = client.get_file(result.drive_file_id)
        assert int(remote.get("size", 0)) == len(payload)
    finally:
        # Leave no litter behind in the user's Drive.
        client.delete_file(result.drive_file_id)
