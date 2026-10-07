"""Optional Instagram authentication via reusable Instaloader sessions.

The intended lifecycle is: authenticate once, save a session file, reuse it
indefinitely. Sessions can be created from the dashboard (Settings page) or
with the Instaloader command line tool. Passwords are used for the single login
call only: never stored, never written to disk by this module, and never logged.

Two-factor codes are entered by the account owner, which completes Instagram's
own verification rather than bypassing it. Nothing here attempts to work around
checkpoint challenges, CAPTCHAs or any other access control. If Instagram asks
for that kind of verification, it is reported and the operation stops.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import instaloader

from utils.errors import InstagramAuthenticationError

logger = logging.getLogger(__name__)

SESSION_PREFIX = "session-"


def session_file_path(session_dir: Path | str, username: str) -> Path:
    """Conventional session filename for a username."""
    safe = "".join(c for c in (username or "") if c.isalnum() or c in "._-")
    if not safe:
        raise InstagramAuthenticationError(
            "Empty Instagram username",
            user_message="Please provide the Instagram username for the session.",
        )
    return Path(session_dir) / f"{SESSION_PREFIX}{safe}"


def list_saved_sessions(session_dir: Path | str) -> list[str]:
    """Usernames for which a saved session file exists."""
    directory = Path(session_dir)
    if not directory.exists():
        return []
    return sorted(
        path.name[len(SESSION_PREFIX) :]
        for path in directory.iterdir()
        if path.is_file() and path.name.startswith(SESSION_PREFIX)
    )


def load_session(
    loader: instaloader.Instaloader,
    username: str,
    session_dir: Path | str,
) -> None:
    """Load a saved session into ``loader``.

    Raises:
        InstagramAuthenticationError: if the session file is missing or unusable.
    """
    path = session_file_path(session_dir, username)
    if not path.exists():
        raise InstagramAuthenticationError(
            f"No saved session for {username!r} at {path}",
            user_message=(
                f"No saved Instagram session found for '{username}'. "
                "Please create one first."
            ),
        )

    try:
        loader.load_session_from_file(username, str(path))
    except Exception as exc:
        # Includes corrupt files and sessions Instagram has since invalidated.
        raise InstagramAuthenticationError(
            f"Could not load session for {username!r}: {exc}",
            user_message=(
                f"The saved Instagram session for '{username}' could not be used. "
                "It may have expired; please create a new one."
            ),
        ) from exc

    logged_in = loader.test_login()
    if not logged_in:
        raise InstagramAuthenticationError(
            f"Session for {username!r} is no longer valid",
            user_message=(
                f"The saved Instagram session for '{username}' has expired. "
                "Please create a new one."
            ),
        )

    logger.info("Loaded Instagram session for user=%s", username)


def normalize_username(username: str) -> str:
    """Trim whitespace and a leading ``@`` from a typed username."""
    return (username or "").strip().lstrip("@").strip()


@dataclass
class PendingTwoFactor:
    """A login waiting for the user's own two-factor code.

    Holds the in-memory Instaloader instance that started the login. It carries
    Instagram's pending-2FA identifier, never the password, and is never
    written to disk.
    """

    username: str
    loader: instaloader.Instaloader


def _new_login_loader() -> instaloader.Instaloader:
    return instaloader.Instaloader(
        quiet=True,
        download_comments=False,
        save_metadata=False,
    )


def _save_loader_session(
    loader: instaloader.Instaloader, username: str, session_dir: Path | str
) -> Path:
    """Write the logged-in loader's session to disk with owner-only permissions."""
    path = session_file_path(session_dir, username)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        loader.save_session_to_file(str(path))
    except OSError as exc:
        raise InstagramAuthenticationError(
            f"Could not save session file: {exc}",
            user_message="The session could not be saved to disk.",
        ) from exc

    try:
        path.chmod(0o600)
    except OSError:  # pragma: no cover - best effort on exotic filesystems
        logger.debug("Could not tighten permissions on session file")

    logger.info("Saved Instagram session for user=%s", username)
    return path


def begin_login(
    username: str,
    password: str,
    session_dir: Path | str,
) -> Path | PendingTwoFactor:
    """Log in and save a session, or pause for a two-factor code.

    The password is used for this single call and is never stored or logged.

    Returns:
        The saved session path when login completes, or a
        :class:`PendingTwoFactor` when Instagram asks for the account owner's
        two-factor code. Pass that to :func:`complete_two_factor`.

    Raises:
        InstagramAuthenticationError: on bad credentials, or when Instagram
            requires a verification step other than two-factor (for example a
            checkpoint challenge). Those are reported, never worked around.
    """
    username = normalize_username(username)
    if not username or not password:
        raise InstagramAuthenticationError(
            "Missing Instagram credentials",
            user_message="Please provide both an Instagram username and password.",
        )

    loader = _new_login_loader()

    try:
        loader.login(username, password)
    except instaloader.exceptions.TwoFactorAuthRequiredException:
        # The account owner completes this with their own code. Instaloader
        # keeps the pending state on the loader; the password is not needed.
        logger.info("Instagram login awaiting two-factor code for user=%s", username)
        return PendingTwoFactor(username=username, loader=loader)
    except instaloader.exceptions.BadCredentialsException as exc:
        raise InstagramAuthenticationError(
            "Bad Instagram credentials",
            user_message="Instagram rejected those credentials.",
        ) from exc
    except instaloader.exceptions.LoginException as exc:
        # Generic refusal, most often Instagram wanting to verify the login.
        raise InstagramAuthenticationError(
            f"Instagram refused the login: {exc}",
            user_message=(
                "Instagram refused this login, usually because it wants to "
                "confirm it is you. Use 'Open browser and log in' instead, which "
                "lets you complete that check on Instagram's own page."
            ),
        ) from exc
    except instaloader.exceptions.ConnectionException as exc:
        raise InstagramAuthenticationError(
            f"Instagram connection error during login: {exc}",
            user_message=(
                "Instagram would not accept the login attempt right now. This can "
                "happen when it asks for additional verification (open the "
                "Instagram app and approve the login), or when it is limiting "
                "requests. Please try again later."
            ),
        ) from exc
    except Exception as exc:
        raise InstagramAuthenticationError(
            f"Unexpected Instagram login failure: {type(exc).__name__}",
            user_message="The Instagram login could not be completed.",
        ) from exc
    finally:
        # Drop the reference promptly; it is never persisted or logged.
        del password

    return _save_loader_session(loader, username, session_dir)


def complete_two_factor(
    pending: PendingTwoFactor,
    code: str,
    session_dir: Path | str,
) -> Path:
    """Finish a two-factor login with the owner's code and save the session.

    Raises:
        InstagramAuthenticationError: if the code is rejected or the pending
            login has expired. A rejected code can be retried with the same
            ``pending`` object.
    """
    code = "".join((code or "").split())
    if not code:
        raise InstagramAuthenticationError(
            "Empty two-factor code",
            user_message="Please enter the code from your authenticator app or SMS.",
        )

    try:
        pending.loader.two_factor_login(code)
    except instaloader.exceptions.BadCredentialsException as exc:
        raise InstagramAuthenticationError(
            "Two-factor code rejected",
            user_message="Instagram rejected that code. Please check it and try again.",
        ) from exc
    except instaloader.exceptions.InvalidArgumentException as exc:
        raise InstagramAuthenticationError(
            "No two-factor login pending",
            user_message="This login has expired. Please start again.",
        ) from exc
    except instaloader.exceptions.ConnectionException as exc:
        raise InstagramAuthenticationError(
            f"Instagram connection error during two-factor login: {exc}",
            user_message=(
                "Instagram would not accept the code right now. Please try again "
                "later."
            ),
        ) from exc
    except Exception as exc:
        raise InstagramAuthenticationError(
            f"Unexpected two-factor failure: {type(exc).__name__}",
            user_message="The two-factor login could not be completed.",
        ) from exc

    return _save_loader_session(pending.loader, pending.username, session_dir)


#: Cookies that must be present for a usable logged-in Instagram web session.
REQUIRED_SESSION_COOKIES = ("sessionid", "csrftoken", "ds_user_id")


def session_from_cookies(
    cookies: dict[str, str],
    session_dir: Path | str,
) -> tuple[str, Path]:
    """Turn logged-in Instagram browser cookies into a saved Instaloader session.

    Used by the browser login flow: the user signs in on instagram.com in a real
    browser, and only the resulting cookies reach this function. No password
    ever passes through the application.

    Returns:
        ``(username, session_path)``.

    Raises:
        InstagramAuthenticationError: if required cookies are missing or
            Instagram does not accept them as a logged-in session.
    """
    missing = [name for name in REQUIRED_SESSION_COOKIES if not cookies.get(name)]
    if missing:
        raise InstagramAuthenticationError(
            f"Browser session missing cookies: {', '.join(missing)}",
            user_message=(
                "The browser did not finish logging in to Instagram. Please try "
                "again and wait until your feed has loaded."
            ),
        )

    loader = _new_login_loader()
    try:
        loader.load_session("", dict(cookies))
        username = loader.test_login()
    except Exception as exc:
        raise InstagramAuthenticationError(
            f"Could not verify browser session: {type(exc).__name__}",
            user_message=(
                "Instagram did not accept the browser session. Please try again."
            ),
        ) from exc

    if not username:
        raise InstagramAuthenticationError(
            "Browser session not recognised as logged in",
            user_message=(
                "Instagram did not recognise the browser session as logged in. "
                "Please try again."
            ),
        )

    loader.context.username = username
    path = _save_loader_session(loader, username, session_dir)
    return username, path


def check_session(session_dir: Path | str, username: str) -> None:
    """Confirm a saved session still works. Raises if it does not."""
    loader = _new_login_loader()
    try:
        load_session(loader, username, session_dir)
    finally:
        try:
            loader.close()
        except Exception:  # pragma: no cover - best effort
            pass


def create_session(
    username: str,
    password: str,
    session_dir: Path | str,
) -> Path:
    """Log in once and save a reusable session file.

    Single-step variant of :func:`begin_login` for callers that cannot prompt
    for a two-factor code.

    Raises:
        InstagramAuthenticationError: on bad credentials, or when Instagram
            requires two-factor or another verification step.
    """
    outcome = begin_login(username, password, session_dir)
    del password
    if isinstance(outcome, PendingTwoFactor):
        raise InstagramAuthenticationError(
            "Two-factor authentication required",
            user_message=(
                "This account uses two-factor authentication. Add the session "
                "from the Settings page, which will ask for your code."
            ),
        )
    return outcome


def delete_session(session_dir: Path | str, username: str) -> bool:
    """Remove a saved session file. Returns True if something was deleted."""
    path = session_file_path(session_dir, username)
    if path.exists():
        path.unlink()
        logger.info("Deleted Instagram session for user=%s", username)
        return True
    return False
