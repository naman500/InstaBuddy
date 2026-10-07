"""The ingestion orchestrator.

One entry point, :meth:`IngestionService.process`, runs the whole pipeline::

    validate URL -> resolve post -> validate selection -> create staging dir
      -> download media -> hash files -> write metadata.json -> write document.txt
      -> record in manifest -> upload to Drive -> record uploads
      -> optional cleanup -> return result

Two rules shape the error handling throughout:

* A job row is created before the first network call, so a crash always leaves a
  record rather than silence.
* A failed upload never costs the user their media. Download success followed by
  upload failure is ``PARTIAL``, the local files stay, and the upload can be
  retried from History without touching Instagram again.

Streamlit is not imported here. The UI calls this service; the service knows
nothing about the UI.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Protocol

from gdrive.client import DriveClient
from gdrive.folders import FolderResolver
from gdrive.uploader import DriveUploader, UploadProgress
from instagram.client import InstagramClient
from instagram.downloader import DownloadProgress, MediaDownloader
from instagram.metadata import write_metadata
from instagram.client import is_rate_limited, rate_limit_error
from instagram.resolver import (
    build_post_model,
    post_url_for_shortcode,
    resolve_post,
    resolve_profile_posts,
)
from models.instagram import InstagramPost
from models.job import Job, JobStatus
from models.media import DestinationMode, MediaSelection, MediaType
from models.upload import DownloadedFile, UploadResult, UploadStatus
from services.cleanup import cleanup_directory, remove_partial_files, should_cleanup
from services.hashing import calculate_sha256
from services.jobs import JobService
from services.manifest import Manifest
from utils.config import AppConfig, DriveAccountConfig, get_drive_account
from utils.errors import (
    AppError,
    GoogleDriveError,
    InstagramError,
    InstagramRateLimitError,
    ValidationError,
)
from utils.paths import ensure_directory, post_directory, post_relative_path
from utils.validation import extract_instagram_shortcode

logger = logging.getLogger(__name__)


class ProgressReporter(Protocol):
    """Receives human-readable progress updates."""

    def __call__(self, message: str, fraction: float | None = None) -> None: ...


def _noop_progress(message: str, fraction: float | None = None) -> None:
    """Default reporter used when the caller does not supply one."""


class ProfileMediaRule(str, Enum):
    """A single media choice applied uniformly across every post in a profile.

    A profile mixes images, reels and carousels, each of which offers different
    per-post :class:`MediaSelection` options. This global rule is resolved to
    the best available selection for each post individually, so one choice can
    be applied to a whole account.
    """

    EVERYTHING = "everything"
    IMAGES_ONLY = "images_only"
    VIDEOS_ONLY = "videos_only"

    @property
    def label(self) -> str:
        return {
            ProfileMediaRule.EVERYTHING: "Everything (images + videos)",
            ProfileMediaRule.IMAGES_ONLY: "Images only",
            ProfileMediaRule.VIDEOS_ONLY: "Videos only",
        }[self]

    def selection_for(self, post: InstagramPost) -> MediaSelection | None:
        """Pick the per-post selection that satisfies this rule, if any.

        Returns ``None`` when the post holds nothing the rule asks for, e.g.
        a photo post under :attr:`VIDEOS_ONLY`. Such posts are skipped rather
        than failed, which keeps a batch moving.
        """
        available = post.available_selections()
        if not available:
            return None

        if self is ProfileMediaRule.EVERYTHING:
            # Prefer the broadest option the content type offers.
            for candidate in (
                MediaSelection.IMAGES_AND_VIDEOS,
                MediaSelection.VIDEO_AND_COVER,
                MediaSelection.VIDEO,
                MediaSelection.IMAGE,
            ):
                if candidate in available:
                    return candidate
            return available[0]

        if self is ProfileMediaRule.IMAGES_ONLY:
            for candidate in (
                MediaSelection.IMAGES,
                MediaSelection.IMAGE,
                MediaSelection.COVER,
            ):
                if candidate in available:
                    return candidate
            return None

        # VIDEOS_ONLY
        for candidate in (MediaSelection.VIDEOS, MediaSelection.VIDEO):
            if candidate in available:
                return candidate
        return None


@dataclass
class IngestionOptions:
    """Everything the user chose before pressing Download."""

    selection: MediaSelection
    destination_mode: DestinationMode = DestinationMode.LOCAL_AND_DRIVE
    drive_account: str | None = None
    root_folder: str = "Instagram-Knowledge-Base"
    organize_by_username: bool = True
    organize_by_year: bool = True
    organize_by_month: bool = True
    organize_by_shortcode: bool = True
    organize_by_date_folder: bool = False
    cleanup_temp_files: bool = False
    force_redownload: bool = False

    @classmethod
    def from_config(
        cls,
        config: AppConfig,
        selection: MediaSelection,
        **overrides: Any,
    ) -> IngestionOptions:
        """Build options from saved settings, then apply per-run overrides."""
        try:
            mode = DestinationMode(config.default_destination)
        except ValueError:
            mode = DestinationMode.LOCAL_AND_DRIVE

        options = cls(
            selection=selection,
            destination_mode=mode,
            root_folder=config.drive_root_folder,
            organize_by_username=config.organize_by_username,
            organize_by_year=config.organize_by_year,
            organize_by_month=config.organize_by_month,
            organize_by_shortcode=config.organize_by_shortcode,
            organize_by_date_folder=config.organize_by_date_folder,
            cleanup_temp_files=config.cleanup_temp_files,
        )
        for key, value in overrides.items():
            if value is not None and hasattr(options, key):
                setattr(options, key, value)
        return options


@dataclass
class IngestionResult:
    """Outcome of one ingestion run."""

    job_id: str
    status: JobStatus
    post: InstagramPost | None = None
    directory: Path | None = None
    files: list[DownloadedFile] = field(default_factory=list)
    metadata_file: Path | None = None
    document_file: Path | None = None
    uploads: list[UploadResult] = field(default_factory=list)
    remote_path: str = ""
    drive_folder_id: str | None = None
    drive_account: str | None = None
    message: str = ""
    cleaned_up: bool = False
    #: True when the run failed because Instagram rate limited it.
    rate_limited: bool = False

    @property
    def succeeded(self) -> bool:
        return self.status is JobStatus.COMPLETED

    @property
    def all_filenames(self) -> list[str]:
        names = [f.filename for f in self.files]
        if self.metadata_file:
            names.append(self.metadata_file.name)
        if self.document_file:
            names.append(self.document_file.name)
        return names

    @property
    def uploaded_count(self) -> int:
        return len([u for u in self.uploads if u.succeeded])

    @property
    def failed_uploads(self) -> list[UploadResult]:
        return [u for u in self.uploads if not u.succeeded]


@dataclass
class ProfileAnalysis:
    """Result of the profile *analyze* phase.

    Lists the account's post shortcodes without downloading anything, so the UI
    can show how many posts will be processed before the user commits.
    """

    username: str
    url: str
    shortcodes: list[str] = field(default_factory=list)
    limit: int | None = None
    #: Instaloader ``Post`` objects from the listing, keyed by shortcode. Used
    #: to build models without a second lookup per post.
    posts: dict[str, Any] = field(default_factory=dict, repr=False)
    #: Original URLs keyed by shortcode, for URL-list batches. Keeps the
    #: ``/reel/`` hint and the exact link the user supplied in History.
    source_urls: dict[str, str] = field(default_factory=dict)

    @property
    def post_count(self) -> int:
        return len(self.shortcodes)

    @classmethod
    def from_url_list(cls, entries: list[Any], *, label: str = "URL list") -> "ProfileAnalysis":
        """Build a batch from parsed URL-list entries (``.shortcode``, ``.url``).

        No Instagram request is made. Each post is looked up individually,
        one at a time, when the batch runs.
        """
        return cls(
            username=label,
            url="",
            shortcodes=[entry.shortcode for entry in entries],
            source_urls={entry.shortcode: entry.url for entry in entries},
        )


@dataclass
class BatchItemResult:
    """One post's outcome within a profile batch."""

    shortcode: str
    status: JobStatus
    message: str = ""
    result: IngestionResult | None = None


@dataclass
class BatchResult:
    """Aggregate outcome of a whole profile download."""

    username: str
    media_rule: "ProfileMediaRule"
    items: list[BatchItemResult] = field(default_factory=list)
    listing_message: str = ""
    #: Set when the batch stopped early, e.g. on an Instagram rate limit.
    stopped_reason: str = ""
    #: Shortcodes never attempted because the batch stopped early.
    not_attempted: list[str] = field(default_factory=list)

    def _count(self, status: JobStatus) -> int:
        return len([i for i in self.items if i.status is status])

    @property
    def total(self) -> int:
        return len(self.items)

    @property
    def succeeded(self) -> int:
        return self._count(JobStatus.COMPLETED)

    @property
    def partial(self) -> int:
        return self._count(JobStatus.PARTIAL)

    @property
    def skipped(self) -> int:
        return self._count(JobStatus.SKIPPED)

    @property
    def failed(self) -> int:
        return self._count(JobStatus.FAILED)


class IngestionService:
    """Coordinates Instagram download, metadata, manifest and Drive upload."""

    #: Pause between posts in a profile batch, so requests are spread out
    #: rather than fired back to back. Tests set this to 0.
    batch_delay_seconds: float = 3.0

    def __init__(
        self,
        config: AppConfig,
        manifest: Manifest,
        *,
        job_service: JobService | None = None,
        instagram_client: InstagramClient | None = None,
        client_factory: Callable[[], InstagramClient] | None = None,
        drive_client_factory: Callable[[DriveAccountConfig], DriveClient] | None = None,
        downloader_factory: Callable[[InstagramClient], MediaDownloader] | None = None,
    ) -> None:
        """
        Args:
            config: Effective application settings.
            manifest: Manifest data access.
            job_service: Job lifecycle helper; built from ``manifest`` if omitted.
            instagram_client: A ready client, reused across calls.
            client_factory: Builds an Instagram client on demand.
            drive_client_factory: Builds a Drive client for an account. Tests
                inject a fake here.
            downloader_factory: Builds a media downloader for a client.
        """
        self.config = config
        self.manifest = manifest
        self.jobs = job_service or JobService(manifest, config.download_path)
        self._instagram_client = instagram_client
        self._client_factory = client_factory
        self._drive_client_factory = drive_client_factory
        self._downloader_factory = downloader_factory

    # -------------------------------------------------------------- analyze

    def instagram_client(self) -> InstagramClient:
        """The Instagram client, created lazily and reused."""
        if self._instagram_client is None:
            if self._client_factory is not None:
                self._instagram_client = self._client_factory()
            else:
                self._instagram_client = InstagramClient(
                    max_retries=self.config.max_instagram_retries
                )
        return self._instagram_client

    def analyze(self, url: str) -> InstagramPost:
        """Resolve a URL without downloading anything.

        This is the *analyze* phase: enough information to show the user what
        the post contains so they can choose what to keep.
        """
        return resolve_post(url, self.instagram_client())

    def find_duplicates(self, shortcode: str) -> list[dict[str, Any]]:
        """Previous downloads of the same shortcode, newest first."""
        return self.manifest.find_posts_by_shortcode(shortcode)

    def is_duplicate(self, shortcode: str) -> bool:
        return self.manifest.shortcode_exists(shortcode)

    # --------------------------------------------------------- profile batch

    def analyze_profile(
        self, url: str, *, limit: int | None = None
    ) -> ProfileAnalysis:
        """List a profile's post shortcodes without downloading anything.

        The profile equivalent of :meth:`analyze`. One network listing call,
        no media transfer, so the UI can show the post count and let the user
        confirm before a potentially long batch begins.
        """
        username, posts = resolve_profile_posts(
            url, self.instagram_client(), limit=limit
        )
        by_shortcode = {post.shortcode: post for post in posts}
        return ProfileAnalysis(
            username=username,
            url=url,
            shortcodes=list(by_shortcode),
            limit=limit,
            posts=by_shortcode,
        )

    def _batch_post_model(
        self, analysis: ProfileAnalysis, shortcode: str, post_url: str
    ) -> InstagramPost:
        """Build a post model, reusing the listing's data when available.

        Falls back to a fresh lookup only if the listed post cannot be turned
        into a model. Rate limits are raised, never retried.
        """
        listed = analysis.posts.get(shortcode)
        if listed is not None:
            try:
                return build_post_model(listed, url=post_url)
            except Exception as exc:
                # Reading some fields can trigger a lazy Instagram request.
                if is_rate_limited(exc):
                    raise rate_limit_error(f"reading post {shortcode}", exc) from exc
                cause = exc.__cause__
                if cause is not None and is_rate_limited(cause):
                    raise rate_limit_error(f"reading post {shortcode}", cause) from exc
                logger.info(
                    "Listed data unusable for shortcode=%s (%s); looking it up",
                    shortcode,
                    type(exc).__name__,
                )
        return self.analyze(post_url)

    def process_profile(
        self,
        analysis: ProfileAnalysis,
        media_rule: ProfileMediaRule,
        options: IngestionOptions,
        *,
        user: str = "unknown",
        progress: ProgressReporter | None = None,
    ) -> BatchResult:
        """Download every listed post, applying one media rule to all of them.

        Each post runs through the same per-post pipeline as a single URL, so it
        gets its own job, its own History row, duplicate protection and upload
        retry. An ordinary failure on one post is recorded and the next post
        proceeds. An Instagram rate limit stops the whole batch, since every
        further request would be rejected and would extend the limit; posts
        not yet attempted are listed in ``not_attempted``.

        To keep request volume down, posts already in History are skipped with
        no Instagram request, listing data is reused instead of looking each
        post up again, and there is a pause between posts.

        The ``selection`` on ``options`` is ignored here; the per-post selection
        is derived from ``media_rule`` for each post's content type.
        """
        report = progress or _noop_progress
        batch = BatchResult(username=analysis.username, media_rule=media_rule)

        total = len(analysis.shortcodes)
        if total == 0:
            report("No posts found for this profile.", 1.0)
            batch.listing_message = "This profile has no downloadable posts."
            return batch

        made_request = False

        for index, shortcode in enumerate(analysis.shortcodes):
            # Already downloaded: skip before touching Instagram at all.
            if not options.force_redownload and self.manifest.shortcode_exists(
                shortcode
            ):
                batch.items.append(
                    BatchItemResult(
                        shortcode=shortcode,
                        status=JobStatus.SKIPPED,
                        message="Already downloaded.",
                    )
                )
                continue

            # Spread requests out instead of firing them back to back.
            if made_request and self.batch_delay_seconds > 0:
                report(
                    f"[{index + 1}/{total}] Pausing between posts...",
                    index / total,
                )
                time.sleep(self.batch_delay_seconds)
            made_request = True
            base = index / total
            span = 1.0 / total

            def post_report(
                message: str,
                fraction: float | None = None,
                *,
                _base: float = base,
                _span: float = span,
                _index: int = index,
            ) -> None:
                overall = _base + (fraction or 0.0) * _span
                report(
                    f"[{_index + 1}/{total}] {message}",
                    min(max(overall, 0.0), 1.0),
                )

            post_url = analysis.source_urls.get(shortcode) or post_url_for_shortcode(
                shortcode
            )

            # Build the post model first so the media rule can pick a selection
            # that actually exists for this content type.
            try:
                post = self._batch_post_model(analysis, shortcode, post_url)
            except (InstagramError, ValidationError, AppError) as exc:
                message = getattr(exc, "user_message", str(exc))
                logger.warning("Batch resolve failed shortcode=%s: %s", shortcode, exc)
                batch.items.append(
                    BatchItemResult(
                        shortcode=shortcode,
                        status=JobStatus.FAILED,
                        message=message,
                    )
                )
                if isinstance(exc, InstagramRateLimitError):
                    self._stop_batch(batch, analysis, index, message)
                    break
                continue

            selection = media_rule.selection_for(post)
            if selection is None:
                logger.info(
                    "Batch skip shortcode=%s: nothing matches rule=%s",
                    shortcode,
                    media_rule.value,
                )
                batch.items.append(
                    BatchItemResult(
                        shortcode=shortcode,
                        status=JobStatus.SKIPPED,
                        message=(
                            f"No {media_rule.label.lower()} content in this post."
                        ),
                    )
                )
                continue

            post_options = replace(options, selection=selection)
            result = self.process(
                post_url,
                post_options,
                user=user,
                post=post,
                progress=post_report,
            )
            batch.items.append(
                BatchItemResult(
                    shortcode=shortcode,
                    status=result.status,
                    message=result.message,
                    result=result,
                )
            )
            if result.rate_limited:
                self._stop_batch(batch, analysis, index, result.message)
                break

        if batch.stopped_reason:
            report(
                f"Stopped early: Instagram is rate limiting. {batch.succeeded} "
                f"downloaded, {len(batch.not_attempted)} not attempted.",
                1.0,
            )
        else:
            report(
                f"Finished: {batch.succeeded} downloaded, {batch.partial} partial, "
                f"{batch.skipped} skipped, {batch.failed} failed.",
                1.0,
            )
        return batch

    @staticmethod
    def _stop_batch(
        batch: BatchResult, analysis: ProfileAnalysis, index: int, message: str
    ) -> None:
        """Record an early stop after the post at ``index``."""
        batch.stopped_reason = message
        batch.not_attempted = list(analysis.shortcodes[index + 1 :])
        logger.warning(
            "Batch for %s stopped on rate limit; %d post(s) not attempted",
            analysis.username,
            len(batch.not_attempted),
        )

    def preview_destination(
        self, post: InstagramPost, options: IngestionOptions
    ) -> dict[str, Any]:
        """Destination preview shown before the user commits.

        Does not create any Drive folders, and works even when the account is
        not connected yet.
        """
        from utils.paths import remote_path_display, remote_path_segments

        local_directory = post_directory(
            self.config.download_path,
            post.username,
            post.created_at,
            post.shortcode,
        )
        segments = remote_path_segments(
            options.root_folder,
            post.username,
            post.created_at,
            post.shortcode,
            use_username=options.organize_by_username,
            use_year=options.organize_by_year,
            use_month=options.organize_by_month,
            use_shortcode=options.organize_by_shortcode,
            use_date_folder=options.organize_by_date_folder,
        )
        items = post.items_for_selection(options.selection)
        filenames = [item.filename for item in items] + [
            "metadata.json",
            "document.txt",
        ]
        return {
            "local_directory": local_directory,
            "remote_path": remote_path_display(segments),
            "filenames": filenames,
            "drive_account": options.drive_account,
            "uploads": options.destination_mode.uploads_to_drive,
        }

    # -------------------------------------------------------------- process

    def process(
        self,
        url: str,
        options: IngestionOptions,
        *,
        user: str = "unknown",
        post: InstagramPost | None = None,
        progress: ProgressReporter | None = None,
    ) -> IngestionResult:
        """Run the full pipeline for one URL.

        Args:
            url: The Instagram URL the user pasted.
            options: The user's selections.
            user: Signed-in dashboard username, recorded on the job.
            post: An already-analyzed post, to avoid a second Instagram call.
            progress: Optional progress reporter.

        Returns an :class:`IngestionResult` in every case. Only programming
        errors propagate; expected failures are reported through the result and
        the job record.
        """
        report = progress or _noop_progress

        # Validate before creating a job so a typo does not litter history.
        shortcode = extract_instagram_shortcode(url)

        job = self.jobs.start_job(
            created_by=user,
            requested_url=url,
            shortcode=shortcode,
            requested_media=options.selection,
            destination_mode=options.destination_mode,
            drive_account=(
                options.drive_account
                if options.destination_mode.uploads_to_drive
                else None
            ),
        )
        result = IngestionResult(
            job_id=job.id,
            status=JobStatus.QUEUED,
            drive_account=job.drive_account,
        )

        try:
            return self._run(job, url, options, post, report, result)
        except (InstagramError, ValidationError, GoogleDriveError, AppError) as exc:
            # Expected, already-classified failures.
            self.jobs.finish(job.id, JobStatus.FAILED, error=exc)
            result.status = JobStatus.FAILED
            result.message = getattr(exc, "user_message", str(exc))
            result.rate_limited = isinstance(exc, InstagramRateLimitError)
            logger.error("Download failed job=%s: %s", job.id, exc)
            return result
        except Exception as exc:
            self.jobs.finish(job.id, JobStatus.FAILED, error=exc)
            result.status = JobStatus.FAILED
            result.message = "Something went wrong during ingestion."
            logger.exception("Unexpected ingestion failure job=%s", job.id)
            return result

    def _run(
        self,
        job: Job,
        url: str,
        options: IngestionOptions,
        post: InstagramPost | None,
        report: ProgressReporter,
        result: IngestionResult,
    ) -> IngestionResult:
        # ---------------------------------------------------------- resolve
        if post is None:
            self.jobs.advance(job.id, JobStatus.RESOLVING, step="resolve")
            report("Resolving Instagram post...", 0.02)
            post = self.analyze(url)

        result.post = post
        self.jobs.set_shortcode(job.id, post.shortcode)

        # ------------------------------------------------- validate selection
        available = post.available_selections()
        if options.selection not in available:
            raise ValidationError(
                f"Selection {options.selection.value} invalid for "
                f"{post.content_type.value}",
                user_message=(
                    f"'{options.selection.label}' is not available for this "
                    f"{post.content_type.value}. Please choose a different option."
                ),
            )

        items = post.items_for_selection(options.selection)
        if not items:
            raise ValidationError(
                "Selection matched no media",
                user_message="No media matched that choice. Please pick another option.",
            )

        # --------------------------------------------------- duplicate guard
        if not options.force_redownload and self.manifest.shortcode_exists(
            post.shortcode
        ):
            self.jobs.finish(
                job.id,
                JobStatus.SKIPPED,
                message="This Instagram post has already been downloaded.",
            )
            result.status = JobStatus.SKIPPED
            result.message = "This Instagram post has already been downloaded."
            logger.info("Skipping duplicate shortcode=%s", post.shortcode)
            return result

        # ------------------------------------------------------ staging dir
        directory = ensure_directory(
            post_directory(
                self.config.download_path,
                post.username,
                post.created_at,
                post.shortcode,
            )
        )
        result.directory = directory
        remove_partial_files(directory)

        # ---------------------------------------------------------- download
        self.jobs.advance(job.id, JobStatus.DOWNLOADING, step="download")
        downloader = self._build_downloader()
        upload_span = 0.35 if options.destination_mode.uploads_to_drive else 0.0
        download_ceiling = 0.85 - upload_span

        def on_download(snapshot: DownloadProgress) -> None:
            report(
                snapshot.message,
                0.05 + snapshot.fraction * (download_ceiling - 0.05),
            )

        files = downloader.download_items(
            post, items, directory, progress=on_download
        )
        result.files = files

        # Hashes are computed by the downloader as each file lands.
        self.jobs.advance(job.id, JobStatus.HASHING, step="hash")

        # ---------------------------------------------------------- metadata
        self.jobs.advance(job.id, JobStatus.GENERATING_METADATA, step="metadata")
        report("Generating metadata...", download_ceiling)

        metadata_path, document_path = write_metadata(
            directory,
            post,
            files,
            job_id=job.id,
            downloaded_by=job.created_by,
        )
        result.metadata_file = metadata_path
        result.document_file = document_path

        # ---------------------------------------------------------- manifest
        relative = post_relative_path(post.username, post.created_at, post.shortcode)
        post_id = self.manifest.record_post(
            job_id=job.id,
            shortcode=post.shortcode,
            instagram_url=post.url,
            username=post.username,
            caption=post.caption,
            content_type=post.content_type.value,
            created_at=post.created_at,
            local_directory=str(directory),
            drive_account=job.drive_account,
            status="downloaded",
        )

        media_ids: dict[str, int] = {}
        for downloaded in self._all_artifacts(files, metadata_path, document_path):
            media_ids[downloaded.filename] = self.manifest.record_media_file(
                post_id=post_id,
                filename=downloaded.filename,
                relative_path=str(relative / downloaded.filename),
                media_type=downloaded.media_type.value,
                file_size=downloaded.file_size,
                sha256=downloaded.sha256,
            )

        # ------------------------------------------------------------ upload
        if not options.destination_mode.uploads_to_drive:
            report("Completed.", 1.0)
            self.jobs.finish(job.id, JobStatus.COMPLETED)
            result.status = JobStatus.COMPLETED
            result.message = "Download completed."
            return result

        upload_outcome = self._upload(
            job=job,
            post=post,
            options=options,
            directory=directory,
            files=files,
            metadata_path=metadata_path,
            document_path=document_path,
            media_ids=media_ids,
            report=report,
            start_fraction=download_ceiling,
        )

        result.uploads = upload_outcome["results"]
        result.remote_path = upload_outcome["remote_path"]
        result.drive_folder_id = upload_outcome["folder_id"]

        if upload_outcome["error"] is not None:
            # Drive failed entirely. Local files are deliberately untouched.
            error = upload_outcome["error"]
            self.jobs.finish(job.id, JobStatus.PARTIAL, error=error)
            result.status = JobStatus.PARTIAL
            result.message = getattr(error, "user_message", str(error))
            return result

        if result.failed_uploads:
            self.jobs.finish(
                job.id,
                JobStatus.PARTIAL,
                message=(
                    f"{len(result.failed_uploads)} of {len(result.uploads)} files "
                    "did not upload. Your local copies have been kept and the "
                    "upload can be retried from History."
                ),
            )
            result.status = JobStatus.PARTIAL
            result.message = (
                "Some files did not upload. Local copies were kept and can be "
                "retried from History."
            )
            return result

        # ----------------------------------------------------------- cleanup
        if should_cleanup(
            options.destination_mode,
            result.uploads,
            cleanup_enabled=options.cleanup_temp_files,
        ):
            result.cleaned_up = cleanup_directory(directory)

        report("Completed.", 1.0)
        self.jobs.finish(job.id, JobStatus.COMPLETED)
        result.status = JobStatus.COMPLETED
        result.message = "Download and upload completed."
        return result

    # --------------------------------------------------------------- upload

    def _upload(
        self,
        *,
        job: Job,
        post: InstagramPost,
        options: IngestionOptions,
        directory: Path,
        files: list[DownloadedFile],
        metadata_path: Path,
        document_path: Path,
        media_ids: dict[str, int],
        report: ProgressReporter,
        start_fraction: float,
    ) -> dict[str, Any]:
        """Upload all artifacts, recording each outcome in the manifest."""
        outcome: dict[str, Any] = {
            "results": [],
            "remote_path": "",
            "folder_id": None,
            "error": None,
        }

        account_key = options.drive_account
        if not account_key:
            outcome["error"] = GoogleDriveError(
                "No Google Drive account selected",
                user_message="Please choose a Google Drive account to upload to.",
            )
            return outcome

        self.jobs.advance(job.id, JobStatus.UPLOADING, step="upload")
        report("Connecting to Google Drive...", start_fraction)

        try:
            account = get_drive_account(account_key)
            client = self._build_drive_client(account)
            resolver = FolderResolver(client)
            folder_id, remote_path = resolver.resolve_for_post(
                root_folder=options.root_folder,
                username=post.username,
                created_at=post.created_at,
                shortcode=post.shortcode,
                use_username=options.organize_by_username,
                use_year=options.organize_by_year,
                use_month=options.organize_by_month,
                use_shortcode=options.organize_by_shortcode,
                use_date_folder=options.organize_by_date_folder,
            )
        except (GoogleDriveError, AppError) as exc:
            outcome["error"] = exc
            logger.error("Upload failed before transfer job=%s: %s", job.id, exc)
            return outcome

        outcome["remote_path"] = remote_path
        outcome["folder_id"] = folder_id

        uploader = DriveUploader(
            client,
            chunk_size=self.config.upload_chunk_size,
            max_retries=self.config.max_upload_retries,
        )

        paths = [f.path for f in files] + [metadata_path, document_path]
        total = len(paths)

        def on_upload(snapshot: UploadProgress) -> None:
            completed = snapshot.file_index - 1 + snapshot.fraction
            overall = start_fraction + (completed / max(total, 1)) * (
                0.99 - start_fraction
            )
            report(snapshot.message, min(overall, 0.99))

        results: list[UploadResult] = []
        for index, path in enumerate(paths, start=1):
            upload_result = uploader.upload_file(
                path,
                folder_id or "",
                remote_path=f"{remote_path}/{path.name}",
                progress=on_upload,
                file_index=index,
                file_count=total,
            )
            results.append(upload_result)

            media_file_id = media_ids.get(path.name)
            if media_file_id is not None:
                self.manifest.record_upload(
                    media_file_id=media_file_id,
                    drive_account=account_key,
                    remote_path=upload_result.remote_path,
                    status=upload_result.status,
                    drive_file_id=upload_result.drive_file_id,
                    error_message=upload_result.error_message,
                    uploaded_at=upload_result.uploaded_at,
                )

        outcome["results"] = results
        return outcome

    # ---------------------------------------------------------- retry upload

    def retry_upload(
        self,
        job_id: str,
        *,
        user: str = "unknown",
        drive_account: str | None = None,
        progress: ProgressReporter | None = None,
    ) -> IngestionResult:
        """Re-upload a job's existing local files.

        Instagram is never contacted. If the local files are gone there is
        nothing to upload, and the user is told to re-run the job instead.
        """
        report = progress or _noop_progress

        job = self.manifest.get_job(job_id)
        if job is None:
            raise AppError(
                f"Unknown job {job_id}", user_message="That job could not be found."
            )

        detail = self.jobs.job_detail(job_id)
        post_row = detail["post"]
        result = IngestionResult(
            job_id=job_id,
            status=job.status,
            drive_account=drive_account or job.drive_account,
        )

        if not post_row:
            result.status = JobStatus.FAILED
            result.message = "There is nothing recorded to upload for this job."
            return result

        account_key = drive_account or job.drive_account
        if not account_key:
            result.message = "Please choose a Google Drive account to upload to."
            result.status = JobStatus.PARTIAL
            return result

        pending = [
            entry
            for entry in detail["media"]
            if entry["upload_status_value"] != UploadStatus.UPLOADED.value
        ]
        existing = [entry for entry in pending if entry["exists"]]

        if not pending:
            result.status = JobStatus.COMPLETED
            result.message = "Everything for this job has already been uploaded."
            return result

        if not existing:
            result.status = JobStatus.FAILED
            result.message = (
                "The local files for this job are missing, so the upload cannot "
                "be retried. Re-run the job to download them again."
            )
            return result

        self.manifest.update_job(
            job_id,
            status=JobStatus.UPLOADING,
            current_step="retry_upload",
            attempt_count=job.attempt_count + 1,
            drive_account=account_key,
        )
        report("Connecting to Google Drive...", 0.05)

        try:
            account = get_drive_account(account_key)
            client = self._build_drive_client(account)
            resolver = FolderResolver(client)
            created_at = _parse_iso(post_row.get("created_at"))
            folder_id, remote_path = resolver.resolve_for_post(
                root_folder=self.config.drive_root_folder,
                username=post_row.get("username") or "unknown",
                created_at=created_at,
                shortcode=post_row.get("shortcode") or "unknown",
                use_username=self.config.organize_by_username,
                use_year=self.config.organize_by_year,
                use_month=self.config.organize_by_month,
                use_shortcode=self.config.organize_by_shortcode,
                use_date_folder=self.config.organize_by_date_folder,
            )
        except (GoogleDriveError, AppError) as exc:
            self.jobs.finish(job_id, JobStatus.PARTIAL, error=exc)
            result.status = JobStatus.PARTIAL
            result.message = getattr(exc, "user_message", str(exc))
            return result

        uploader = DriveUploader(
            client,
            chunk_size=self.config.upload_chunk_size,
            max_retries=self.config.max_upload_retries,
        )

        total = len(existing)
        results: list[UploadResult] = []

        for index, entry in enumerate(existing, start=1):
            def on_upload(snapshot: UploadProgress, i: int = index) -> None:
                overall = ((i - 1) + snapshot.fraction) / max(total, 1)
                report(snapshot.message, min(0.05 + overall * 0.94, 0.99))

            upload_result = uploader.upload_file(
                Path(entry["local_path"]),
                folder_id or "",
                remote_path=f"{remote_path}/{entry['filename']}",
                progress=on_upload,
                file_index=index,
                file_count=total,
            )
            results.append(upload_result)

            self.manifest.record_upload(
                media_file_id=int(entry["media_file_id"]),
                drive_account=account_key,
                remote_path=upload_result.remote_path,
                status=upload_result.status,
                drive_file_id=upload_result.drive_file_id,
                error_message=upload_result.error_message,
                uploaded_at=upload_result.uploaded_at,
            )

        result.uploads = results
        result.remote_path = remote_path
        result.drive_folder_id = folder_id

        still_failing = [r for r in results if not r.succeeded]
        missing_count = len(pending) - len(existing)

        if still_failing or missing_count:
            self.jobs.finish(
                job_id,
                JobStatus.PARTIAL,
                message="Some files still did not upload.",
            )
            result.status = JobStatus.PARTIAL
            result.message = "Some files still did not upload."
        else:
            self.jobs.finish(job_id, JobStatus.COMPLETED)
            result.status = JobStatus.COMPLETED
            result.message = "Upload completed."
            report("Completed.", 1.0)

        return result

    # -------------------------------------------------------------- helpers

    def _build_downloader(self) -> MediaDownloader:
        client = self.instagram_client()
        if self._downloader_factory is not None:
            return self._downloader_factory(client)
        return MediaDownloader(
            client, max_retries=self.config.max_instagram_retries
        )

    def _build_drive_client(self, account: DriveAccountConfig) -> DriveClient:
        if self._drive_client_factory is not None:
            return self._drive_client_factory(account)
        return DriveClient(account, self.config.token_path)

    @staticmethod
    def _all_artifacts(
        files: list[DownloadedFile],
        metadata_path: Path,
        document_path: Path,
    ) -> list[DownloadedFile]:
        """Media files plus the two generated documents, all hashed.

        The metadata and document files are recorded in the manifest too, so the
        History view can show their hashes and upload status alongside media.
        """
        artifacts = list(files)
        for path in (metadata_path, document_path):
            if not path.is_file():
                continue
            artifacts.append(
                DownloadedFile(
                    filename=path.name,
                    path=path,
                    media_type=MediaType.DOCUMENT,
                    file_size=path.stat().st_size,
                    sha256=calculate_sha256(path),
                )
            )
        return artifacts


def _parse_iso(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return datetime.now(timezone.utc)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
