"""Metadata and document generation, plus hashing."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from instagram.metadata import (
    DOCUMENT_FILENAME,
    METADATA_FILENAME,
    build_document,
    build_metadata,
    read_metadata,
    write_metadata,
)
from instagram.resolver import build_post_model
from models.media import MediaType
from models.metadata import APPLICATION_VERSION, SCHEMA_VERSION
from models.upload import DownloadedFile
from services.hashing import (
    calculate_sha256,
    calculate_sha256_bytes,
    is_valid_sha256,
    verify_file_hash,
)
from tests.conftest import FakePost

REEL_URL = "https://www.instagram.com/reel/ABC123/"

# Known-answer vector: SHA-256 of the empty string.
EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
# SHA-256 of b"abc"
ABC_SHA256 = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


@pytest.fixture
def reel_files() -> list[DownloadedFile]:
    return [
        DownloadedFile(
            filename="video.mp4",
            path=Path("/tmp/video.mp4"),
            media_type=MediaType.VIDEO,
            file_size=18_800_000,
            sha256="a" * 64,
        ),
        DownloadedFile(
            filename="cover.jpg",
            path=Path("/tmp/cover.jpg"),
            media_type=MediaType.IMAGE,
            file_size=120_000,
            sha256="b" * 64,
        ),
    ]


@pytest.fixture
def reel_model(reel_post: FakePost):
    return build_post_model(reel_post, url=REEL_URL, url_hint="reel")


# ---------------------------------------------------------------------- hashing


def test_sha256_known_vectors(tmp_path: Path) -> None:
    empty = tmp_path / "empty.bin"
    empty.write_bytes(b"")
    assert calculate_sha256(empty) == EMPTY_SHA256

    abc = tmp_path / "abc.bin"
    abc.write_bytes(b"abc")
    assert calculate_sha256(abc) == ABC_SHA256


def test_sha256_returns_64_hex_chars(tmp_path: Path) -> None:
    target = tmp_path / "f.bin"
    target.write_bytes(b"some content")
    digest = calculate_sha256(target)
    assert len(digest) == 64
    assert digest == digest.lower()
    assert is_valid_sha256(digest)


def test_sha256_chunking_matches_single_read(tmp_path: Path) -> None:
    """A large file hashed in small chunks must match a one-shot hash."""
    target = tmp_path / "big.bin"
    payload = bytes(range(256)) * 5000  # ~1.28 MB
    target.write_bytes(payload)
    assert calculate_sha256(target, chunk_size=1024) == calculate_sha256_bytes(payload)


def test_sha256_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        calculate_sha256(tmp_path / "nope.bin")


def test_is_valid_sha256_rejects_junk() -> None:
    assert is_valid_sha256("") is False
    assert is_valid_sha256("abc") is False
    assert is_valid_sha256("z" * 64) is False
    assert is_valid_sha256("a" * 63) is False


def test_verify_file_hash(tmp_path: Path) -> None:
    target = tmp_path / "f.bin"
    target.write_bytes(b"abc")
    assert verify_file_hash(target, ABC_SHA256) is True
    assert verify_file_hash(target, ABC_SHA256.upper()) is True
    assert verify_file_hash(target, "0" * 64) is False
    assert verify_file_hash(tmp_path / "missing.bin", ABC_SHA256) is False


# --------------------------------------------------------------- metadata model


def test_metadata_captures_required_fields(reel_model, reel_files) -> None:
    metadata = build_metadata(reel_model, reel_files)

    assert metadata.schema_version == SCHEMA_VERSION
    assert metadata.source.platform == "instagram"
    assert metadata.source.url == REEL_URL
    assert metadata.source.shortcode == "ABC123"
    assert metadata.creator.username == "example_user"
    assert metadata.content.type == "reel"
    assert metadata.content.caption == "An example caption"
    assert metadata.content.created_at == datetime(
        2026, 9, 21, 10, 0, tzinfo=timezone.utc
    )
    assert metadata.ingestion.application_version == APPLICATION_VERSION


def test_metadata_media_entries_carry_hashes(reel_model, reel_files) -> None:
    metadata = build_metadata(reel_model, reel_files)
    assert [m.filename for m in metadata.media] == ["video.mp4", "cover.jpg"]
    assert [m.type for m in metadata.media] == ["video", "image"]
    assert metadata.media[0].sha256 == "a" * 64
    assert metadata.media[0].file_size == 18_800_000


def test_metadata_records_job_attribution(reel_model, reel_files) -> None:
    metadata = build_metadata(
        reel_model, reel_files, job_id="job-1", downloaded_by="naman"
    )
    assert metadata.ingestion.job_id == "job-1"
    assert metadata.ingestion.downloaded_by == "naman"


def test_processing_flags_start_false(reel_model, reel_files) -> None:
    """Future-pipeline placeholders exist but nothing sets them in V1."""
    processing = build_metadata(reel_model, reel_files).processing
    assert processing.ocr_completed is False
    assert processing.transcription_completed is False
    assert processing.frames_extracted is False
    assert processing.embeddings_generated is False


# ------------------------------------------------------------------- document.txt


def test_document_contains_all_sections(reel_model, reel_files) -> None:
    document = build_document(reel_model, reel_files)

    assert "SOURCE: Instagram" in document
    assert "CONTENT TYPE: Reel" in document
    assert "AUTHOR: example_user" in document
    assert "ORIGINAL URL:" in document
    assert REEL_URL in document
    assert "CREATED AT:" in document
    assert "CAPTION:" in document
    assert "An example caption" in document
    assert "MEDIA:" in document
    assert "video.mp4" in document
    assert "cover.jpg" in document


def test_document_section_order(reel_model, reel_files) -> None:
    document = build_document(reel_model, reel_files)
    for earlier, later in [
        ("SOURCE:", "CONTENT TYPE:"),
        ("CONTENT TYPE:", "AUTHOR:"),
        ("AUTHOR:", "ORIGINAL URL:"),
        ("ORIGINAL URL:", "CREATED AT:"),
        ("CREATED AT:", "CAPTION:"),
        ("CAPTION:", "MEDIA:"),
    ]:
        assert document.index(earlier) < document.index(later)


def test_document_handles_missing_caption(reel_post: FakePost, reel_files) -> None:
    reel_post.caption = None
    model = build_post_model(reel_post, url=REEL_URL, url_hint="reel")
    assert "(no caption)" in build_document(model, reel_files)


def test_document_handles_multiline_caption(reel_post: FakePost, reel_files) -> None:
    reel_post.caption = "Line one\nLine two\n\nLine four"
    model = build_post_model(reel_post, url=REEL_URL, url_hint="reel")
    document = build_document(model, reel_files)
    assert "Line one" in document
    assert "Line four" in document


def test_document_lists_carousel_files_in_order(mixed_carousel_post: FakePost) -> None:
    model = build_post_model(
        mixed_carousel_post, url="https://www.instagram.com/p/ABC123/", url_hint="p"
    )
    files = [
        DownloadedFile(
            filename=item.filename,
            path=Path("/tmp") / item.filename,
            media_type=item.media_type,
            file_size=10,
            sha256="c" * 64,
        )
        for item in model.media_items
    ]
    document = build_document(model, files)
    media_block = document.split("MEDIA:")[1]
    positions = [media_block.index(n) for n in ["001.jpg", "002.mp4", "003.jpg"]]
    assert positions == sorted(positions)


# ------------------------------------------------------------------- write / read


def test_write_metadata_creates_both_files(tmp_path, reel_model, reel_files) -> None:
    target = tmp_path / "creator" / "2026" / "09" / "ABC123"
    metadata_path, document_path = write_metadata(target, reel_model, reel_files)

    assert metadata_path == target / METADATA_FILENAME
    assert document_path == target / DOCUMENT_FILENAME
    assert metadata_path.is_file()
    assert document_path.is_file()


def test_written_metadata_is_valid_json(tmp_path, reel_model, reel_files) -> None:
    metadata_path, _ = write_metadata(tmp_path, reel_model, reel_files)
    payload = json.loads(metadata_path.read_text(encoding="utf-8"))

    assert payload["schema_version"] == "1.0"
    assert payload["source"]["shortcode"] == "ABC123"
    assert payload["creator"]["username"] == "example_user"
    assert payload["content"]["type"] == "reel"
    assert len(payload["media"]) == 2
    assert payload["media"][0]["sha256"] == "a" * 64
    assert "downloaded_at" in payload["ingestion"]
    assert payload["processing"]["ocr_completed"] is False


def test_write_metadata_creates_missing_directories(
    tmp_path, reel_model, reel_files
) -> None:
    deep = tmp_path / "a" / "b" / "c"
    assert not deep.exists()
    write_metadata(deep, reel_model, reel_files)
    assert deep.is_dir()


def test_read_metadata_round_trip(tmp_path, reel_model, reel_files) -> None:
    metadata_path, _ = write_metadata(tmp_path, reel_model, reel_files)
    loaded = read_metadata(metadata_path)
    assert loaded["source"]["shortcode"] == "ABC123"


def test_read_metadata_missing_returns_empty(tmp_path: Path) -> None:
    assert read_metadata(tmp_path / "nope.json") == {}


def test_read_metadata_corrupt_returns_empty(tmp_path: Path) -> None:
    bad = tmp_path / "metadata.json"
    bad.write_text("{not json", encoding="utf-8")
    assert read_metadata(bad) == {}


def test_unicode_caption_survives_round_trip(
    tmp_path, reel_post: FakePost, reel_files
) -> None:
    reel_post.caption = "Café ☕ 日本語 emoji 🎉"
    model = build_post_model(reel_post, url=REEL_URL, url_hint="reel")
    metadata_path, document_path = write_metadata(tmp_path, model, reel_files)

    payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert payload["content"]["caption"] == "Café ☕ 日本語 emoji 🎉"
    assert "Café ☕ 日本語 emoji 🎉" in document_path.read_text(encoding="utf-8")
