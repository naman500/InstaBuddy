"""Dashboard authentication: hashing, verification, lockout, roles.

A recurring assertion here is that no plaintext password ever appears in a
stored value or a log record.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from auth import app_auth
from auth.app_auth import (
    ALGORITHM,
    DEFAULT_ITERATIONS,
    LoginAttemptTracker,
    authenticate,
    has_configured_users,
    hash_password,
    is_valid_hash,
    load_users,
    suggest_password,
    verify_password,
)
from models.user import Role
from utils import config as config_module
from utils.errors import AppAuthenticationError

PASSWORD = "correct horse battery staple"

#: Production uses DEFAULT_ITERATIONS. Fixtures use a low count purely to keep
#: the suite fast; the real default is asserted separately in
#: test_hash_format_is_self_describing.
TEST_ITERATIONS = 1_000


def fast_hash(password: str) -> str:
    return hash_password(password, iterations=TEST_ITERATIONS)


@pytest.fixture
def secrets_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point config at a temporary secrets.toml and reset caches."""

    def write(content: str) -> Path:
        path = tmp_path / "secrets.toml"
        path.write_text(content, encoding="utf-8")
        monkeypatch.setattr(config_module, "SECRETS_PATH", path)
        config_module.load_secrets.cache_clear()
        config_module.get_config.cache_clear()
        app_auth.reset_tracker()
        return path

    yield write
    config_module.load_secrets.cache_clear()
    config_module.get_config.cache_clear()
    app_auth.reset_tracker()


def users_toml(password: str = PASSWORD, role: str = "admin") -> str:
    return f"""
[auth]
session_timeout_minutes = 60
max_failed_attempts = 3
lockout_minutes = 15

[auth.users.naman]
display_name = "Naman"
role = "{role}"
password_hash = "{fast_hash(password)}"

[auth.users.viewer]
display_name = "Read Only"
role = "viewer"
password_hash = "{fast_hash('viewer-password')}"
"""


# ------------------------------------------------------------------- hashing


def test_hash_format_is_self_describing() -> None:
    encoded = hash_password(PASSWORD)
    parts = encoded.split("$")
    assert len(parts) == 4
    assert parts[0] == ALGORITHM
    assert int(parts[1]) == DEFAULT_ITERATIONS


def test_hash_never_contains_the_plaintext() -> None:
    encoded = hash_password(PASSWORD)
    assert PASSWORD not in encoded
    for word in PASSWORD.split():
        assert word not in encoded


def test_same_password_hashes_differently_each_time() -> None:
    """A random per-user salt means identical passwords produce different hashes."""
    assert hash_password(PASSWORD) != hash_password(PASSWORD)


def test_verify_accepts_correct_password() -> None:
    assert verify_password(PASSWORD, hash_password(PASSWORD)) is True


@pytest.mark.parametrize(
    "wrong",
    [
        "wrong password",
        PASSWORD.upper(),
        PASSWORD + " ",
        " " + PASSWORD,
        PASSWORD[:-1],
        "",
    ],
)
def test_verify_rejects_wrong_password(wrong: str) -> None:
    assert verify_password(wrong, hash_password(PASSWORD)) is False


def test_verify_handles_malformed_hashes() -> None:
    for bad in ["", "garbage", "pbkdf2_sha256$only$three", "md5$1$a$b", "$$$"]:
        assert verify_password(PASSWORD, bad) is False


def test_empty_password_is_refused() -> None:
    with pytest.raises(AppAuthenticationError):
        hash_password("")


def test_low_iteration_counts_are_refused() -> None:
    with pytest.raises(ValueError):
        hash_password(PASSWORD, iterations=10)


def test_custom_iterations_round_trip() -> None:
    encoded = hash_password(PASSWORD, iterations=2000)
    assert encoded.split("$")[1] == "2000"
    assert verify_password(PASSWORD, encoded) is True


def test_unicode_password_round_trips() -> None:
    password = "пароль-日本語-🎉"
    assert verify_password(password, hash_password(password)) is True


def test_is_valid_hash() -> None:
    assert is_valid_hash(hash_password(PASSWORD)) is True
    assert is_valid_hash("nope") is False
    assert is_valid_hash("") is False


def test_suggested_password_is_long_and_random() -> None:
    first, second = suggest_password(), suggest_password()
    assert first != second
    assert len(first) >= 20


# --------------------------------------------------------------------- lockout


def test_lockout_after_configured_failures() -> None:
    tracker = LoginAttemptTracker(max_attempts=3, lockout_minutes=15)

    for _ in range(2):
        tracker.record_failure("naman")
    assert tracker.is_locked("naman") is False

    tracker.record_failure("naman")
    assert tracker.is_locked("naman") is True
    assert tracker.seconds_remaining("naman") > 0


def test_success_clears_failure_count() -> None:
    tracker = LoginAttemptTracker(max_attempts=3)
    tracker.record_failure("naman")
    tracker.record_failure("naman")
    tracker.record_success("naman")

    assert tracker.failures("naman") == 0
    tracker.record_failure("naman")
    assert tracker.is_locked("naman") is False


def test_lockout_expires_after_cooling_off() -> None:
    tracker = LoginAttemptTracker(max_attempts=1, lockout_minutes=15)
    tracker.record_failure("naman")
    assert tracker.is_locked("naman") is True

    # Simulate the cooling-off period having elapsed.
    tracker._locked_until["naman"] = datetime.now(timezone.utc) - timedelta(seconds=1)
    assert tracker.is_locked("naman") is False
    assert tracker.failures("naman") == 0


def test_lockout_is_per_username() -> None:
    tracker = LoginAttemptTracker(max_attempts=1)
    tracker.record_failure("naman")
    assert tracker.is_locked("naman") is True
    assert tracker.is_locked("someone_else") is False


def test_seconds_remaining_is_zero_when_not_locked() -> None:
    assert LoginAttemptTracker().seconds_remaining("nobody") == 0


# ----------------------------------------------------------------- user loading


def test_load_users_from_secrets(secrets_file) -> None:
    secrets_file(users_toml())
    users = load_users()

    assert set(users) == {"naman", "viewer"}
    assert users["naman"].display_name == "Naman"
    assert users["naman"].role is Role.ADMIN
    assert users["viewer"].role is Role.VIEWER


def test_user_model_carries_no_credential(secrets_file) -> None:
    secrets_file(users_toml())
    user = load_users()["naman"]
    dumped = user.model_dump()

    assert "password" not in dumped
    assert "password_hash" not in dumped
    assert PASSWORD not in str(dumped)


def test_users_without_valid_hash_are_skipped(secrets_file) -> None:
    secrets_file(
        """
[auth.users.broken]
display_name = "Broken"
role = "admin"
password_hash = "PASTE_HASH_FROM_scripts/hash_password.py"

[auth.users.ok]
display_name = "Fine"
role = "admin"
password_hash = "%s"
"""
        % fast_hash(PASSWORD)
    )
    users = load_users()
    assert set(users) == {"ok"}


def test_unknown_role_falls_back_to_viewer(secrets_file) -> None:
    secrets_file(
        f"""
[auth.users.odd]
display_name = "Odd"
role = "superuser"
password_hash = "{fast_hash(PASSWORD)}"
"""
    )
    assert load_users()["odd"].role is Role.VIEWER


def test_no_users_configured(secrets_file) -> None:
    secrets_file("[auth]\n")
    assert load_users() == {}
    assert has_configured_users() is False


def test_has_configured_users(secrets_file) -> None:
    secrets_file(users_toml())
    assert has_configured_users() is True


# ---------------------------------------------------------------- authenticate


def test_authenticate_success(secrets_file) -> None:
    secrets_file(users_toml())
    user = authenticate("naman", PASSWORD)
    assert user.username == "naman"
    assert user.is_admin is True
    assert user.can_download is True


def test_authenticate_trims_username(secrets_file) -> None:
    secrets_file(users_toml())
    assert authenticate("  naman  ", PASSWORD).username == "naman"


def test_wrong_password_rejected(secrets_file) -> None:
    secrets_file(users_toml())
    with pytest.raises(AppAuthenticationError) as exc_info:
        authenticate("naman", "wrong")
    assert exc_info.value.user_message == "Incorrect username or password."


def test_unknown_user_gives_identical_message(secrets_file) -> None:
    """The form must not reveal whether an account exists."""
    secrets_file(users_toml())

    with pytest.raises(AppAuthenticationError) as unknown:
        authenticate("ghost", PASSWORD)
    with pytest.raises(AppAuthenticationError) as wrong:
        authenticate("naman", "wrong")

    assert unknown.value.user_message == wrong.value.user_message


def test_missing_credentials_message(secrets_file) -> None:
    secrets_file(users_toml())
    with pytest.raises(AppAuthenticationError) as exc_info:
        authenticate("", "")
    assert "both a username and a password" in exc_info.value.user_message


def test_authenticate_locks_out_after_repeated_failures(secrets_file) -> None:
    secrets_file(users_toml())
    tracker = LoginAttemptTracker(max_attempts=3, lockout_minutes=15)

    for _ in range(3):
        with pytest.raises(AppAuthenticationError):
            authenticate("naman", "wrong", tracker=tracker)

    with pytest.raises(AppAuthenticationError) as exc_info:
        authenticate("naman", PASSWORD, tracker=tracker)
    assert "Too many failed attempts" in exc_info.value.user_message
    assert "15 minutes" in exc_info.value.user_message


def test_successful_login_resets_lockout_counter(secrets_file) -> None:
    secrets_file(users_toml())
    tracker = LoginAttemptTracker(max_attempts=3)

    for _ in range(2):
        with pytest.raises(AppAuthenticationError):
            authenticate("naman", "wrong", tracker=tracker)

    authenticate("naman", PASSWORD, tracker=tracker)
    assert tracker.failures("naman") == 0


def test_viewer_cannot_download(secrets_file) -> None:
    secrets_file(users_toml())
    viewer = authenticate("viewer", "viewer-password")
    assert viewer.role is Role.VIEWER
    assert viewer.is_admin is False
    assert viewer.can_download is False


def test_broken_account_cannot_sign_in(secrets_file) -> None:
    """A malformed hash must not become an authentication bypass."""
    secrets_file(
        """
[auth.users.broken]
display_name = "Broken"
password_hash = "not-a-real-hash"
"""
    )
    with pytest.raises(AppAuthenticationError):
        authenticate("broken", "anything")
    with pytest.raises(AppAuthenticationError):
        authenticate("broken", "")


# --------------------------------------------------------------------- logging


def test_password_never_reaches_the_logs(
    secrets_file, caplog: pytest.LogCaptureFixture
) -> None:
    secrets_file(users_toml())

    with caplog.at_level(logging.DEBUG):
        authenticate("naman", PASSWORD)
        with pytest.raises(AppAuthenticationError):
            authenticate("naman", "some-wrong-password")

    log_text = "\n".join(record.getMessage() for record in caplog.records)
    assert PASSWORD not in log_text
    assert "some-wrong-password" not in log_text
    assert "Login succeeded user=naman" in log_text
    assert "Login failed user=naman" in log_text


def test_failure_log_records_username_only(
    secrets_file, caplog: pytest.LogCaptureFixture
) -> None:
    secrets_file(users_toml())
    with caplog.at_level(logging.WARNING):
        with pytest.raises(AppAuthenticationError):
            authenticate("ghost", "secret-value-here")

    log_text = "\n".join(record.getMessage() for record in caplog.records)
    assert "secret-value-here" not in log_text
    assert "user=ghost" in log_text


def test_redaction_filter_scrubs_credentials() -> None:
    from utils.logging_config import RedactSecretsFilter

    redact = RedactSecretsFilter.redact
    assert "hunter2" not in redact("password=hunter2")
    assert "abc123" not in redact("access_token: abc123")
    assert "xyz" not in redact("Bearer xyz")
    assert "shh" not in redact('client_secret="shh"')
    assert "REDACTED" in redact(f"password_hash={hash_password(PASSWORD)}")
    assert redact("nothing sensitive here") == "nothing sensitive here"
