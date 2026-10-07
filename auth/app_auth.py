"""Dashboard authentication.

Passwords are stored only as salted PBKDF2-HMAC-SHA256 hashes in
``.streamlit/secrets.toml``. Plaintext passwords are never written to config, to
the database, or to logs.

PBKDF2 is used rather than bcrypt or argon2 because it ships in the standard
library via :mod:`hashlib`, and the project rule is to avoid adding a dependency
until a feature genuinely requires it. With a per-user random salt and a high
iteration count it is appropriate for a local dashboard gate.

This module contains no Streamlit code. The UI gate lives in :mod:`utils.ui`.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from models.user import AppUser, Role
from utils.config import get_auth_settings, get_user_records
from utils.errors import AppAuthenticationError

logger = logging.getLogger(__name__)

ALGORITHM = "pbkdf2_sha256"
DEFAULT_ITERATIONS = 260_000
SALT_BYTES = 16
_MIN_ITERATIONS = 1_000

#: Used to burn equivalent CPU time when a username does not exist, so response
#: timing does not reveal whether an account is real.
_DUMMY_HASH = (
    "pbkdf2_sha256$260000$AAAAAAAAAAAAAAAAAAAAAA==$"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
)


# ------------------------------------------------------------------- hashing


def hash_password(
    password: str,
    *,
    iterations: int = DEFAULT_ITERATIONS,
    salt: bytes | None = None,
) -> str:
    """Hash a password into a self-describing string.

    Format: ``pbkdf2_sha256$<iterations>$<salt_b64>$<hash_b64>``. Embedding the
    algorithm and iteration count means stored hashes stay verifiable if the
    defaults change later.
    """
    if not password:
        raise AppAuthenticationError(
            "Empty password",
            user_message="Please choose a password.",
        )
    if iterations < _MIN_ITERATIONS:
        raise ValueError(f"iterations must be >= {_MIN_ITERATIONS}")

    salt_bytes = salt if salt is not None else os.urandom(SALT_BYTES)
    derived = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt_bytes, iterations
    )
    return (
        f"{ALGORITHM}${iterations}"
        f"${base64.b64encode(salt_bytes).decode('ascii')}"
        f"${base64.b64encode(derived).decode('ascii')}"
    )


def _parse_encoded(encoded: str) -> tuple[int, bytes, bytes]:
    parts = (encoded or "").split("$")
    if len(parts) != 4 or parts[0] != ALGORITHM:
        raise ValueError("Unrecognised password hash format")
    iterations = int(parts[1])
    return iterations, base64.b64decode(parts[2]), base64.b64decode(parts[3])


def verify_password(password: str, encoded: str) -> bool:
    """Check a password against a stored hash using a constant-time compare."""
    if not password or not encoded:
        return False
    try:
        iterations, salt, expected = _parse_encoded(encoded)
    except (ValueError, TypeError):
        logger.warning("Stored password hash is malformed")
        return False

    candidate = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, iterations
    )
    return hmac.compare_digest(candidate, expected)


def is_valid_hash(encoded: str) -> bool:
    """True if ``encoded`` is a parseable hash of the expected form."""
    try:
        _parse_encoded(encoded)
        return True
    except (ValueError, TypeError):
        return False


# ------------------------------------------------------------------- lockout


@dataclass
class LoginAttemptTracker:
    """Counts consecutive failures per username and enforces a cooling-off.

    Held in memory. A restart clears it, which is acceptable for a local tool
    and avoids persisting anything security-sensitive.
    """

    max_attempts: int = 5
    lockout_minutes: int = 15
    _failures: dict[str, int] = field(default_factory=dict)
    _locked_until: dict[str, datetime] = field(default_factory=dict)

    def is_locked(self, username: str) -> bool:
        until = self._locked_until.get(username)
        if until is None:
            return False
        if datetime.now(timezone.utc) >= until:
            # Cooling-off elapsed; clear it so the user may try again.
            self._locked_until.pop(username, None)
            self._failures.pop(username, None)
            return False
        return True

    def seconds_remaining(self, username: str) -> int:
        until = self._locked_until.get(username)
        if until is None:
            return 0
        remaining = (until - datetime.now(timezone.utc)).total_seconds()
        return max(0, int(remaining))

    def record_failure(self, username: str) -> None:
        count = self._failures.get(username, 0) + 1
        self._failures[username] = count
        if count >= self.max_attempts:
            self._locked_until[username] = datetime.now(timezone.utc) + timedelta(
                minutes=self.lockout_minutes
            )
            logger.warning(
                "Login locked out user=%s after %d failed attempts", username, count
            )

    def record_success(self, username: str) -> None:
        self._failures.pop(username, None)
        self._locked_until.pop(username, None)

    def failures(self, username: str) -> int:
        return self._failures.get(username, 0)

    def reset(self) -> None:
        self._failures.clear()
        self._locked_until.clear()


#: Process-wide tracker. Streamlit reruns the script constantly, so per-run
#: state would forget failures instantly.
_default_tracker: LoginAttemptTracker | None = None


def get_tracker() -> LoginAttemptTracker:
    """The shared attempt tracker, configured from settings on first use."""
    global _default_tracker
    if _default_tracker is None:
        settings = get_auth_settings()
        _default_tracker = LoginAttemptTracker(
            max_attempts=settings["max_failed_attempts"],
            lockout_minutes=settings["lockout_minutes"],
        )
    return _default_tracker


def reset_tracker() -> None:
    """Drop the shared tracker. Used by tests and after a settings change."""
    global _default_tracker
    _default_tracker = None


# --------------------------------------------------------------------- users


def load_users() -> dict[str, AppUser]:
    """Configured dashboard users, keyed by username.

    Entries without a usable password hash are skipped and logged, so a
    half-configured account cannot be signed into.
    """
    users: dict[str, AppUser] = {}
    for username, record in get_user_records().items():
        encoded = str(record.get("password_hash") or "")
        if not is_valid_hash(encoded):
            logger.warning(
                "Skipping user %r: password_hash is missing or malformed", username
            )
            continue
        try:
            role = Role(str(record.get("role", "viewer")).lower())
        except ValueError:
            logger.warning("Unknown role for user %r; defaulting to viewer", username)
            role = Role.VIEWER

        users[username] = AppUser(
            username=username,
            display_name=str(record.get("display_name") or username),
            role=role,
        )
    return users


def has_configured_users() -> bool:
    """True if at least one usable account exists."""
    return bool(load_users())


def authenticate(
    username: str,
    password: str,
    *,
    tracker: LoginAttemptTracker | None = None,
) -> AppUser:
    """Verify credentials and return the user.

    Raises:
        AppAuthenticationError: on lockout, unknown user or wrong password. The
            message is identical for unknown user and wrong password so the form
            cannot be used to enumerate accounts.
    """
    attempts = tracker if tracker is not None else get_tracker()
    name = (username or "").strip()

    if not name or not password:
        raise AppAuthenticationError(
            "Missing credentials",
            user_message="Please enter both a username and a password.",
        )

    if attempts.is_locked(name):
        # Round up: 899 seconds left on a 15 minute lockout should read as 15,
        # not 14.
        seconds = attempts.seconds_remaining(name)
        minutes = max(1, -(-seconds // 60))
        logger.warning("Login attempt while locked out user=%s", name)
        raise AppAuthenticationError(
            f"Locked out: {name}",
            user_message=(
                f"Too many failed attempts. Please try again in {minutes} "
                f"minute{'s' if minutes != 1 else ''}."
            ),
        )

    records = get_user_records()
    record = records.get(name)
    stored_hash = str((record or {}).get("password_hash") or "")

    # Always run a verification, even for an unknown user, so the response time
    # does not betray whether the account exists.
    matched = verify_password(password, stored_hash or _DUMMY_HASH)

    if not record or not stored_hash or not matched:
        attempts.record_failure(name)
        logger.warning("Login failed user=%s", name)
        raise AppAuthenticationError(
            f"Authentication failed for {name}",
            user_message="Incorrect username or password.",
        )

    user = load_users().get(name)
    if user is None:
        attempts.record_failure(name)
        logger.warning("Login failed user=%s (account not usable)", name)
        raise AppAuthenticationError(
            f"Account {name} is not usable",
            user_message="Incorrect username or password.",
        )

    attempts.record_success(name)
    logger.info("Login succeeded user=%s role=%s", name, user.role.value)
    return user


def generate_password_hash(password: str) -> str:
    """Convenience wrapper used by ``scripts/hash_password.py``."""
    return hash_password(password)


def suggest_password(length: int = 20) -> str:
    """A strong random password, offered by the hashing helper script."""
    return secrets.token_urlsafe(length)
