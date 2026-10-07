"""Dashboard Instagram session creation, with Instaloader faked.

No network: a fake loader stands in for :class:`instaloader.Instaloader` so we
can drive plain login, two-factor login and failure paths deterministically.
"""

from __future__ import annotations

from pathlib import Path

import instaloader
import pytest

from auth import instagram_auth
from auth.instagram_auth import (
    PendingTwoFactor,
    begin_login,
    complete_two_factor,
    create_session,
    list_saved_sessions,
    normalize_username,
)
from utils.errors import InstagramAuthenticationError

ig_errors = instaloader.exceptions


class FakeLoader:
    """Mimics the parts of Instaloader the login helpers use."""

    def __init__(self, *, login_error=None, two_factor_error=None) -> None:
        self.login_error = login_error
        self.two_factor_error = two_factor_error
        self.logins: list[tuple[str, str]] = []
        self.codes: list[str] = []
        self.saved_to: str | None = None

    def login(self, username: str, password: str) -> None:
        self.logins.append((username, password))
        if self.login_error is not None:
            raise self.login_error

    def two_factor_login(self, code: str) -> None:
        self.codes.append(code)
        if self.two_factor_error is not None:
            raise self.two_factor_error

    def save_session_to_file(self, filename: str) -> None:
        self.saved_to = filename
        Path(filename).write_text("fake-session", encoding="utf-8")

    def close(self) -> None:
        pass


@pytest.fixture
def use_loader(monkeypatch: pytest.MonkeyPatch):
    def install(loader: FakeLoader) -> FakeLoader:
        monkeypatch.setattr(instagram_auth, "_new_login_loader", lambda: loader)
        return loader

    return install


def test_plain_login_saves_session(tmp_path: Path, use_loader) -> None:
    loader = use_loader(FakeLoader())

    path = begin_login("@someone ", "pw", tmp_path)

    assert isinstance(path, Path)
    assert path.name == "session-someone"
    assert path.read_text() == "fake-session"
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert loader.logins == [("someone", "pw")]
    assert list_saved_sessions(tmp_path) == ["someone"]


def test_password_never_written_to_disk(tmp_path: Path, use_loader) -> None:
    use_loader(FakeLoader())
    begin_login("someone", "super-secret-pw", tmp_path)

    for file in tmp_path.rglob("*"):
        if file.is_file():
            assert "super-secret-pw" not in file.read_text(errors="ignore")


def test_two_factor_flow(tmp_path: Path, use_loader) -> None:
    loader = use_loader(
        FakeLoader(login_error=ig_errors.TwoFactorAuthRequiredException("2fa"))
    )

    pending = begin_login("someone", "pw", tmp_path)
    assert isinstance(pending, PendingTwoFactor)
    assert list_saved_sessions(tmp_path) == []  # nothing saved yet

    path = complete_two_factor(pending, " 123 456 ", tmp_path)
    assert loader.codes == ["123456"]
    assert path.exists()
    assert list_saved_sessions(tmp_path) == ["someone"]


def test_wrong_two_factor_code_can_retry(tmp_path: Path, use_loader) -> None:
    loader = use_loader(
        FakeLoader(
            login_error=ig_errors.TwoFactorAuthRequiredException("2fa"),
            two_factor_error=ig_errors.BadCredentialsException("bad code"),
        )
    )
    pending = begin_login("someone", "pw", tmp_path)

    with pytest.raises(InstagramAuthenticationError) as info:
        complete_two_factor(pending, "000000", tmp_path)
    assert "rejected that code" in info.value.user_message

    loader.two_factor_error = None
    assert complete_two_factor(pending, "111111", tmp_path).exists()


def test_bad_credentials(tmp_path: Path, use_loader) -> None:
    use_loader(FakeLoader(login_error=ig_errors.BadCredentialsException("no")))
    with pytest.raises(InstagramAuthenticationError) as info:
        begin_login("someone", "wrong", tmp_path)
    assert "rejected those credentials" in info.value.user_message
    assert list_saved_sessions(tmp_path) == []


def test_checkpoint_is_reported_not_bypassed(tmp_path: Path, use_loader) -> None:
    use_loader(
        FakeLoader(login_error=ig_errors.ConnectionException("checkpoint_required"))
    )
    with pytest.raises(InstagramAuthenticationError):
        begin_login("someone", "pw", tmp_path)
    assert list_saved_sessions(tmp_path) == []


def test_empty_inputs_rejected(tmp_path: Path, use_loader) -> None:
    use_loader(FakeLoader())
    with pytest.raises(InstagramAuthenticationError):
        begin_login("", "pw", tmp_path)
    with pytest.raises(InstagramAuthenticationError):
        begin_login("someone", "", tmp_path)


def test_create_session_refuses_two_factor(tmp_path: Path, use_loader) -> None:
    use_loader(
        FakeLoader(login_error=ig_errors.TwoFactorAuthRequiredException("2fa"))
    )
    with pytest.raises(InstagramAuthenticationError):
        create_session("someone", "pw", tmp_path)


def test_normalize_username() -> None:
    assert normalize_username("  @Some.One_ ") == "Some.One_"


# ------------------------------------------------------------- browser login

from types import SimpleNamespace  # noqa: E402

from auth import browser_login  # noqa: E402
from auth.instagram_auth import session_from_cookies  # noqa: E402

GOOD_COOKIES = {"sessionid": "s", "csrftoken": "c", "ds_user_id": "1", "mid": "m"}


class FakeCookieLoader:
    def __init__(self, username: str | None = "someone") -> None:
        self._username = username
        self.loaded: dict | None = None
        self.context = SimpleNamespace(username=None)

    def load_session(self, username: str, data: dict) -> None:
        self.loaded = data

    def test_login(self) -> str | None:
        return self._username

    def save_session_to_file(self, filename: str) -> None:
        Path(filename).write_text("fake-session", encoding="utf-8")


def test_session_from_cookies_saves(tmp_path: Path, monkeypatch) -> None:
    loader = FakeCookieLoader()
    monkeypatch.setattr(instagram_auth, "_new_login_loader", lambda: loader)

    username, path = session_from_cookies(GOOD_COOKIES, tmp_path)

    assert username == "someone"
    assert loader.loaded == GOOD_COOKIES
    assert loader.context.username == "someone"
    assert path.name == "session-someone"
    assert oct(path.stat().st_mode & 0o777) == "0o600"


def test_session_from_cookies_requires_login_cookies(tmp_path: Path) -> None:
    with pytest.raises(InstagramAuthenticationError):
        session_from_cookies({"csrftoken": "c"}, tmp_path)
    assert list_saved_sessions(tmp_path) == []


def test_session_from_cookies_rejected_by_instagram(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        instagram_auth, "_new_login_loader", lambda: FakeCookieLoader(username=None)
    )
    with pytest.raises(InstagramAuthenticationError):
        session_from_cookies(GOOD_COOKIES, tmp_path)
    assert list_saved_sessions(tmp_path) == []


def test_only_instagram_cookies_kept() -> None:
    raw = [
        {"name": "sessionid", "value": "s", "domain": ".instagram.com"},
        {"name": "csrftoken", "value": "c", "domain": "www.instagram.com"},
        {"name": "tracker", "value": "x", "domain": ".facebook.com"},
        {"name": "evil", "value": "y", "domain": "notinstagram.com"},
    ]
    assert browser_login._instagram_cookies(raw) == {"sessionid": "s", "csrftoken": "c"}


def test_login_with_browser_saves_session(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(browser_login, "_capture_cookies", lambda _t: GOOD_COOKIES)
    monkeypatch.setattr(instagram_auth, "_new_login_loader", lambda: FakeCookieLoader())

    username, path = browser_login.login_with_browser(tmp_path)

    assert username == "someone"
    assert path.exists()
    assert list_saved_sessions(tmp_path) == ["someone"]


def test_login_with_browser_cancel(tmp_path: Path, monkeypatch) -> None:
    def cancelled(_timeout):
        raise browser_login.BrowserLoginCancelled("closed")

    monkeypatch.setattr(browser_login, "_capture_cookies", cancelled)
    with pytest.raises(browser_login.BrowserLoginCancelled) as info:
        browser_login.login_with_browser(tmp_path)
    assert "closed before" in info.value.user_message
    assert list_saved_sessions(tmp_path) == []
