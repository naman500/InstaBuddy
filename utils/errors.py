"""Custom exceptions.

Every exception carries a ``user_message`` suitable for display in the UI. The
technical detail stays in the exception string and the logs; users never see a
raw traceback.
"""

from __future__ import annotations


class AppError(Exception):
    """Base class for all application errors."""

    user_message = "Something went wrong. Please check the logs for details."

    def __init__(self, message: str = "", *, user_message: str | None = None):
        super().__init__(message or self.user_message)
        if user_message:
            self.user_message = user_message


class ValidationError(AppError):
    user_message = "Please enter a valid Instagram post, reel or video URL."


class InstagramError(AppError):
    user_message = (
        "Instagram could not provide this content. The post may be unavailable, "
        "require authentication, or Instagram may have temporarily limited access."
    )


class InstagramDownloadError(InstagramError):
    user_message = "The media could not be downloaded. Please try again."


class InstagramAuthenticationError(InstagramError):
    user_message = (
        "Instagram authentication failed. Check the saved session, or try "
        "enabling an authenticated Instagram session."
    )


class InstagramContentUnavailableError(InstagramError):
    user_message = (
        "Instagram could not provide this content. The post may be unavailable, "
        "require authentication, or Instagram may have temporarily limited access. "
        "Try enabling an authenticated Instagram session."
    )


class InstagramRateLimitError(InstagramError):
    user_message = (
        "Instagram is temporarily limiting requests. Please wait a while before "
        "trying again."
    )


class GoogleDriveError(AppError):
    user_message = "Google Drive reported a problem."


class GoogleDriveAuthenticationError(GoogleDriveError):
    user_message = (
        "Could not connect to this Google Drive account. Please reconnect it "
        "from the Google Drive page."
    )


class GoogleDriveUploadError(GoogleDriveError):
    user_message = (
        "The upload to Google Drive did not complete. Your downloaded files have "
        "been kept locally and the upload can be retried from History."
    )


class ManifestError(AppError):
    user_message = "The local records database could not be updated."


class AppAuthenticationError(AppError):
    user_message = "Incorrect username or password."


class ConfigurationError(AppError):
    user_message = (
        "The application is not configured correctly. Please check your settings."
    )
