"""Thin wrapper around Instaloader.

Responsibilities:

* build a conservatively configured :class:`instaloader.Instaloader`
* optionally load a saved session
* fetch a :class:`instaloader.Post` with bounded retries
* translate Instaloader exceptions into application errors

Deliberately absent: any attempt to work around rate limits, challenges,
CAPTCHAs or private-account restrictions. When Instagram says no, that is
reported to the user and the operation stops.
"""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import Any

import instaloader
from instaloader import exceptions as ig_errors

from auth.instagram_auth import load_session
from utils.errors import (
    InstagramAuthenticationError,
    InstagramContentUnavailableError,
    InstagramError,
    InstagramRateLimitError,
)

logger = logging.getLogger(__name__)

#: Base delay for exponential backoff: 1s, then 2s, then 4s.
_BACKOFF_BASE_SECONDS = 1.0

_HTTP_429_RE = re.compile(r"\b429\b")

RATE_LIMIT_MESSAGE = (
    "Instagram is temporarily limiting requests from this account or network. "
    "Wait at least 30-60 minutes before trying again, and avoid repeated "
    "attempts in the meantime, as each one extends the limit. Smaller batches "
    "help."
)


def is_rate_limited(exc: BaseException) -> bool:
    """True if ``exc`` is Instagram's HTTP 429 rate limit.

    With ``max_connection_attempts=1``, Instaloader re-raises a 429 as a plain
    ``ConnectionException`` whose text contains "429 Too Many Requests", so the
    exception type alone is not enough.
    """
    if isinstance(exc, ig_errors.TooManyRequestsException):
        return True
    text = str(exc)
    return "too many requests" in text.lower() or bool(_HTTP_429_RE.search(text))


def rate_limit_error(what: str, exc: BaseException) -> InstagramRateLimitError:
    """Build the error raised when Instagram rate limits a request.

    Rate limits last minutes to hours, so callers raise this immediately
    instead of retrying after a few seconds, which would only extend the limit.
    """
    logger.warning("Instagram rate limit (HTTP 429) while %s; stopping", what)
    return InstagramRateLimitError(
        f"Rate limited while {what}: {exc}", user_message=RATE_LIMIT_MESSAGE
    )


class InstagramClient:
    """Fetches Instagram content, with or without an authenticated session."""

    def __init__(
        self,
        *,
        session_username: str | None = None,
        session_dir: Path | str | None = None,
        max_retries: int = 3,
        request_timeout: float = 30.0,
    ) -> None:
        self.session_username = session_username or None
        self.session_dir = session_dir
        self.max_retries = max(1, min(int(max_retries), 5))
        self._authenticated = False

        self._loader = instaloader.Instaloader(
            quiet=True,
            # We never let Instaloader write files; the downloader handles I/O.
            download_pictures=False,
            download_videos=False,
            download_video_thumbnails=False,
            download_geotags=False,
            download_comments=False,
            save_metadata=False,
            compress_json=False,
            # Instaloader retries internally too. Pinning it to a single attempt
            # keeps total request count equal to our own retry budget instead of
            # multiplying the two together.
            max_connection_attempts=1,
            request_timeout=request_timeout,
            # Leave Instaloader's own rate-limit sleeping enabled. This is the
            # opposite of evasion: it slows us down on purpose.
            sleep=True,
        )

        if self.session_username:
            if self.session_dir is None:
                raise InstagramAuthenticationError(
                    "Session directory not provided",
                    user_message="No Instagram session folder is configured.",
                )
            load_session(self._loader, self.session_username, self.session_dir)
            self._authenticated = True

    # ------------------------------------------------------------------ props

    @property
    def loader(self) -> instaloader.Instaloader:
        return self._loader

    @property
    def context(self) -> instaloader.instaloadercontext.InstaloaderContext:
        return self._loader.context

    @property
    def is_authenticated(self) -> bool:
        return self._authenticated

    # ----------------------------------------------------------------- public

    def fetch_post(self, shortcode: str) -> instaloader.Post:
        """Fetch a post by shortcode.

        Retries only transient failures, at most ``max_retries`` times, with
        exponential backoff. Permanent conditions such as "not found" or
        "login required" fail immediately rather than being hammered.
        """
        last_error: Exception | None = None

        for attempt in range(1, self.max_retries + 1):
            try:
                logger.info(
                    "Resolving Instagram shortcode=%s (attempt %d/%d)",
                    shortcode,
                    attempt,
                    self.max_retries,
                )
                post = instaloader.Post.from_shortcode(self.context, shortcode)
                # Touch a field so a lazily-raised fetch error surfaces here.
                _ = post.typename
                return post

            except ig_errors.QueryReturnedNotFoundException as exc:
                raise InstagramContentUnavailableError(
                    f"Post {shortcode} not found",
                    user_message=(
                        "Instagram could not provide this content. The post may "
                        "have been removed, or the link may be incorrect."
                    ),
                ) from exc

            except (
                ig_errors.LoginRequiredException,
                ig_errors.PrivateProfileNotFollowedException,
                ig_errors.QueryReturnedForbiddenException,
            ) as exc:
                raise InstagramContentUnavailableError(
                    f"Access denied for {shortcode}: {type(exc).__name__}",
                    user_message=(
                        "Instagram could not provide this content. The post may be "
                        "unavailable, require authentication, or Instagram may have "
                        "temporarily limited access. Try enabling an authenticated "
                        "Instagram session."
                    ),
                ) from exc

            except ig_errors.ConnectionException as exc:
                # Includes TooManyRequestsException and 429s re-raised as plain
                # ConnectionException. Rate limits are never retried.
                if is_rate_limited(exc):
                    raise rate_limit_error(f"fetching post {shortcode}", exc) from exc
                last_error = exc
                if attempt >= self.max_retries:
                    raise InstagramContentUnavailableError(
                        f"Connection failure fetching {shortcode}: {exc}"
                    ) from exc
                self._backoff(attempt, "connection error")

            except ig_errors.BadResponseException as exc:
                last_error = exc
                if attempt >= self.max_retries:
                    raise InstagramError(
                        f"Unexpected Instagram response for {shortcode}: {exc}"
                    ) from exc
                self._backoff(attempt, "bad response")

            except ig_errors.InstaloaderException as exc:
                # Anything else is treated as permanent; no point retrying.
                raise InstagramError(
                    f"Instagram error for {shortcode}: {type(exc).__name__}: {exc}"
                ) from exc

        raise InstagramError(
            f"Could not fetch {shortcode} after {self.max_retries} attempts: "
            f"{last_error}"
        )

    def fetch_profile_shortcodes(
        self, username: str, *, limit: int | None = None
    ) -> list[str]:
        """List a profile's post shortcodes, newest first."""
        return [
            post.shortcode
            for post in self.fetch_profile_posts(username, limit=limit)
        ]

    def fetch_profile_posts(
        self, username: str, *, limit: int | None = None
    ) -> list[Any]:
        """List a profile's posts, newest first, as Instaloader ``Post`` objects.

        The listing already carries each post's metadata and media URLs, so the
        ingestion service builds its models from these objects directly instead
        of looking every post up again. That roughly halves the Instagram
        requests a batch makes.

        Args:
            username: The account to list.
            limit: Maximum number of posts to return. ``None`` means all posts,
                which can be very large and slow on big accounts.

        Raises:
            InstagramContentUnavailableError: profile missing, private and not
                followed, or otherwise inaccessible.
            InstagramRateLimitError: Instagram rate limited the listing before
                any posts were returned.
            InstagramError: any other Instaloader failure.
        """
        profile = self._fetch_profile(username)

        posts: list[Any] = []
        try:
            for post in profile.get_posts():
                if getattr(post, "shortcode", None):
                    posts.append(post)
                if limit is not None and len(posts) >= limit:
                    break
        except (
            ig_errors.LoginRequiredException,
            ig_errors.PrivateProfileNotFollowedException,
            ig_errors.QueryReturnedForbiddenException,
        ) as exc:
            raise InstagramContentUnavailableError(
                f"Access denied listing posts for {username}: {type(exc).__name__}",
                user_message=(
                    "Instagram would not list this profile's posts. The account "
                    "may be private, or Instagram may require an authenticated "
                    "session. Try enabling an authenticated Instagram session."
                ),
            ) from exc
        except ig_errors.ConnectionException as exc:
            # Partial listings are still useful; return what we have rather than
            # losing the whole batch to a late failure.
            if posts:
                logger.warning(
                    "%s after listing %d posts for %s; returning partial",
                    "Rate limited" if is_rate_limited(exc) else "Connection error",
                    len(posts),
                    username,
                )
                return posts
            if is_rate_limited(exc):
                raise rate_limit_error(f"listing posts for {username}", exc) from exc
            raise InstagramContentUnavailableError(
                f"Connection failure listing posts for {username}: {exc}"
            ) from exc
        except ig_errors.InstaloaderException as exc:
            raise InstagramError(
                f"Instagram error listing posts for {username}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

        logger.info(
            "Listed %d post(s) for profile=%s (limit=%s)",
            len(posts),
            username,
            limit if limit is not None else "none",
        )
        return posts

    def _fetch_profile(self, username: str) -> instaloader.Profile:
        """Resolve a ``Profile`` by username, with bounded retries.

        Mirrors :meth:`fetch_post`: permanent conditions fail immediately,
        transient ones back off and retry up to ``max_retries`` times.
        """
        last_error: Exception | None = None

        for attempt in range(1, self.max_retries + 1):
            try:
                logger.info(
                    "Resolving Instagram profile=%s (attempt %d/%d)",
                    username,
                    attempt,
                    self.max_retries,
                )
                return instaloader.Profile.from_username(self.context, username)

            except ig_errors.ProfileNotExistsException as exc:
                raise InstagramContentUnavailableError(
                    f"Profile {username} does not exist",
                    user_message=(
                        "No Instagram account was found with that username. Please "
                        "check the profile link."
                    ),
                ) from exc

            except (
                ig_errors.LoginRequiredException,
                ig_errors.QueryReturnedForbiddenException,
            ) as exc:
                raise InstagramContentUnavailableError(
                    f"Access denied for profile {username}: {type(exc).__name__}",
                    user_message=(
                        "Instagram would not provide this profile. It may require "
                        "an authenticated session. Try enabling an authenticated "
                        "Instagram session."
                    ),
                ) from exc

            except ig_errors.ConnectionException as exc:
                if is_rate_limited(exc):
                    raise rate_limit_error(
                        f"looking up profile {username}", exc
                    ) from exc
                last_error = exc
                if attempt >= self.max_retries:
                    raise InstagramContentUnavailableError(
                        f"Connection failure resolving profile {username}: {exc}"
                    ) from exc
                self._backoff(attempt, "connection error")

            except ig_errors.InstaloaderException as exc:
                raise InstagramError(
                    f"Instagram error for profile {username}: "
                    f"{type(exc).__name__}: {exc}"
                ) from exc

        raise InstagramError(
            f"Could not resolve profile {username} after {self.max_retries} "
            f"attempts: {last_error}"
        )

    def close(self) -> None:
        """Release the underlying HTTP session."""
        try:
            self._loader.close()
        except Exception:  # pragma: no cover - best effort
            logger.debug("Instaloader close() raised; ignoring")

    # ---------------------------------------------------------------- private

    def _backoff(self, attempt: int, reason: str) -> None:
        delay = _BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
        logger.warning(
            "Instagram %s; waiting %.0fs before attempt %d", reason, delay, attempt + 1
        )
        time.sleep(delay)

    def __enter__(self) -> InstagramClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
