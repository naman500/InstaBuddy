"""SHA-256 hashing.

Hashes serve four purposes: duplicate detection, upload integrity verification,
avoiding repeat downloads, and detecting content changes.

Files are read in chunks so that a large video is never loaded into memory.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

#: Read size for streaming hash computation.
CHUNK_SIZE = 1024 * 1024

#: Length of a hex-encoded SHA-256 digest.
SHA256_HEX_LENGTH = 64


def calculate_sha256(file_path: Path | str, chunk_size: int = CHUNK_SIZE) -> str:
    """Return the SHA-256 of a file as 64 lowercase hex characters.

    Raises:
        FileNotFoundError: if the file does not exist.
        OSError: if the file cannot be read.
    """
    path = Path(file_path)
    if not path.is_file():
        raise FileNotFoundError(f"Cannot hash missing file: {path}")

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)

    result = digest.hexdigest()
    logger.info("SHA256 calculated file=%s hash=%s", path.name, result[:12] + "...")
    return result


def calculate_sha256_bytes(data: bytes) -> str:
    """Return the SHA-256 of an in-memory byte string."""
    return hashlib.sha256(data).hexdigest()


def is_valid_sha256(value: str) -> bool:
    """True if ``value`` looks like a hex-encoded SHA-256 digest."""
    if not value or len(value) != SHA256_HEX_LENGTH:
        return False
    return all(c in "0123456789abcdefABCDEF" for c in value)


def verify_file_hash(file_path: Path | str, expected_sha256: str) -> bool:
    """Check a file against an expected hash.

    Returns False rather than raising when the file is missing, because the
    common caller is an integrity check that should report, not explode.
    """
    path = Path(file_path)
    if not path.is_file():
        logger.warning("Cannot verify missing file: %s", path)
        return False

    actual = calculate_sha256(path)
    matches = actual.lower() == (expected_sha256 or "").lower()
    if not matches:
        logger.error(
            "Hash mismatch file=%s expected=%s actual=%s",
            path.name,
            expected_sha256[:12],
            actual[:12],
        )
    return matches
