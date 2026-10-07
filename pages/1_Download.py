"""Download page: analyze, choose, then download.

The two-phase flow is deliberate. Analyze fetches only enough to describe the
post and writes nothing to disk. The user then decides what to keep, which is
what makes selective download meaningful and avoids wasted transfers.
"""

from __future__ import annotations

from pathlib import Path

import streamlit as st

from auth.instagram_auth import list_saved_sessions
from models.job import JobStatus
from models.media import DestinationMode, MediaSelection
from services.ingestion import IngestionOptions, ProfileAnalysis, ProfileMediaRule
from utils.config import PROJECT_ROOT, get_drive_accounts
from utils.validation import MAX_URL_LIST_ENTRIES, parse_url_list
from utils.errors import AppError
from utils.ui import (
    bootstrap,
    caption_preview,
    current_user,
    drive_link,
    get_ingestion_service,
    get_manifest,
    human_bytes,
    open_folder_hint,
    require_login,
    show_error,
)

STATE_POST = "download_post"
STATE_URL = "download_url"
STATE_RESULT = "download_result"
STATE_DUPLICATES = "download_duplicates"

STATE_PROFILE = "download_profile_analysis"
STATE_PROFILE_URL = "download_profile_url"
STATE_BATCH_RESULT = "download_batch_result"

STATE_LIST_TEXT = "download_list_text"
STATE_LIST_PARSED = "download_list_parsed"
STATE_LIST_RESULT = "download_list_result"

MODE_SINGLE = "Single post"
MODE_PROFILE = "Whole profile (batch)"
MODE_URL_LIST = "List of URLs (batch)"

config = bootstrap()
user = require_login() if current_user() is None else current_user()

st.title("Download Instagram content")


# --------------------------------------------------------------------- helpers


def reset_analysis() -> None:
    for key in (STATE_POST, STATE_DUPLICATES, STATE_RESULT):
        st.session_state.pop(key, None)


def reset_profile_analysis() -> None:
    for key in (STATE_PROFILE, STATE_BATCH_RESULT):
        st.session_state.pop(key, None)


#: Default URL list in the project folder, read by the URL-list mode.
DEFAULT_URL_FILE = PROJECT_ROOT / "urls.csv"


def read_default_url_file() -> str:
    """Contents of ``urls.csv``, or an empty string if it is missing."""
    try:
        return DEFAULT_URL_FILE.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def reset_url_list() -> None:
    for key in (STATE_LIST_PARSED, STATE_LIST_RESULT):
        st.session_state.pop(key, None)


def selected_session() -> str | None:
    """The Instagram session username, or None for public mode."""
    return st.session_state.get("instagram_session_username") or None


def render_instagram_auth() -> None:
    """Shared Instagram public/authenticated-session picker."""
    with st.expander("Instagram authentication", expanded=False):
        st.caption(
            "Public content needs no Instagram login. Only use an authenticated "
            "session for content you are authorised to access."
        )
        saved_sessions = list_saved_sessions(config.session_path)

        auth_mode = st.radio(
            "Access mode",
            options=[
                "Public content / no login",
                "Use authenticated Instagram session",
            ],
            index=0,
            key="instagram_auth_mode",
        )

        if auth_mode == "Use authenticated Instagram session":
            if saved_sessions:
                st.selectbox(
                    "Saved session",
                    options=saved_sessions,
                    key="instagram_session_username",
                )
            else:
                st.info(
                    "No saved Instagram sessions found. Add one on the "
                    "**Settings** page under *Instagram sessions*: log in once "
                    "and it is saved automatically for reuse here."
                )
                st.session_state["instagram_session_username"] = None
        else:
            st.session_state["instagram_session_username"] = None


# ------------------------------------------------------------------- mode select

download_mode = st.radio(
    "What do you want to download?",
    options=[MODE_SINGLE, MODE_PROFILE, MODE_URL_LIST],
    horizontal=True,
    key="download_mode",
)

# ------------------------------------------------------- 1. URL and Instagram auth

single_mode = download_mode == MODE_SINGLE
post = None

if single_mode:
    st.subheader("1. Instagram URL")

    url = st.text_input(
        "Instagram URL",
        value=st.session_state.get(STATE_URL, ""),
        placeholder="https://www.instagram.com/reel/XXXXXXXX/",
        help="Supports post, reel and video links.",
    )

    render_instagram_auth()

    analyze_clicked = st.button(
        "Analyze", type="primary", disabled=not url.strip()
    )

    if analyze_clicked:
        reset_analysis()
        st.session_state[STATE_URL] = url.strip()
        service = get_ingestion_service(config, selected_session())

        with st.spinner("Resolving Instagram post..."):
            try:
                post = service.analyze(url.strip())
                st.session_state[STATE_POST] = post
                st.session_state[STATE_DUPLICATES] = service.find_duplicates(
                    post.shortcode
                )
            except AppError as exc:
                show_error(exc)
            except Exception as exc:  # unexpected; keep the UI calm
                show_error(exc)

    post = st.session_state.get(STATE_POST)


# --------------------------------------------------------------- 2. Content preview

if post is not None:
    st.divider()
    st.subheader("2. Content")

    columns = st.columns(4)
    columns[0].metric("Content type", post.content_type.value.title())
    columns[1].metric("Author", post.username)
    columns[2].metric("Media items", post.media_count)
    columns[3].metric("Posted", post.created_at.strftime("%Y-%m-%d"))

    st.caption("Caption")
    st.write(caption_preview(post.caption, limit=600))

    breakdown = []
    if post.video_count:
        breakdown.append(f"{post.video_count} video")
    if post.image_count:
        breakdown.append(f"{post.image_count} image")
    if post.has_cover:
        breakdown.append("1 cover image")
    st.caption("Available media: " + (", ".join(breakdown) or "none"))

    duplicates = st.session_state.get(STATE_DUPLICATES) or []
    force_redownload = False

    if duplicates:
        st.warning("This Instagram post has already been downloaded.")
        previous = duplicates[0]
        st.caption(
            f"Last downloaded {previous.get('downloaded_at', 'previously')} to "
            f"{previous.get('local_directory', 'unknown location')}"
        )
        choice = st.radio(
            "How would you like to proceed?",
            options=["Skip", "Download again"],
            horizontal=True,
            key="duplicate_choice",
        )
        force_redownload = choice == "Download again"
        if not force_redownload:
            st.info("Nothing will be downloaded while Skip is selected.")

    # ------------------------------------------------------- 3. Media selection

    st.divider()
    st.subheader("3. What to download")

    available = post.available_selections()
    default_selection = post.default_selection()
    selection = st.radio(
        "Media",
        options=available,
        index=available.index(default_selection),
        format_func=lambda option: option.label,
        key="media_selection",
    )

    if post.has_cover:
        st.caption(
            "For reels and videos, the image option means the cover/thumbnail. "
            "Frame extraction is not part of this version."
        )

    # ---------------------------------------------------------- 4. Destination

    st.divider()
    st.subheader("4. Destination")

    accounts = get_drive_accounts()
    usable_accounts = [a for a in accounts if a.is_usable]

    try:
        default_mode = DestinationMode(config.default_destination)
    except ValueError:
        default_mode = DestinationMode.LOCAL_AND_DRIVE

    mode_options = list(DestinationMode)
    destination_mode = st.radio(
        "Where should the files go?",
        options=mode_options,
        index=mode_options.index(default_mode),
        format_func=lambda option: option.label,
        horizontal=True,
        key="destination_mode",
    )

    drive_account = None
    if destination_mode.uploads_to_drive:
        if not usable_accounts:
            st.error(
                "No Google Drive accounts are configured. Add one in "
                ".streamlit/secrets.toml, then connect it on the Google Drive page."
            )
        else:
            chosen = st.selectbox(
                "Google Drive account",
                options=usable_accounts,
                format_func=lambda account: account.display_name,
                key="drive_account_choice",
            )
            drive_account = chosen.key if chosen else None

    with st.expander("Folder organisation", expanded=False):
        root_folder = st.text_input(
            "Root folder", value=config.drive_root_folder, key="root_folder"
        )
        by_date_folder = st.checkbox(
            "Date folder (YYYY_MM_DD)",
            value=config.organize_by_date_folder,
            help="One folder per publish date, e.g. 2026_09_21, instead of "
            "separate year and month folders.",
        )
        toggle_columns = st.columns(4)
        by_username = toggle_columns[0].checkbox(
            "Username", value=config.organize_by_username
        )
        by_year = toggle_columns[1].checkbox(
            "Year", value=config.organize_by_year, disabled=by_date_folder
        )
        by_month = toggle_columns[2].checkbox(
            "Month", value=config.organize_by_month, disabled=by_date_folder
        )
        by_shortcode = toggle_columns[3].checkbox(
            "Shortcode", value=config.organize_by_shortcode
        )

    options = IngestionOptions(
        selection=selection,
        destination_mode=destination_mode,
        drive_account=drive_account,
        root_folder=root_folder or config.drive_root_folder,
        organize_by_username=by_username,
        organize_by_year=by_year,
        organize_by_month=by_month,
        organize_by_shortcode=by_shortcode,
        organize_by_date_folder=by_date_folder,
        cleanup_temp_files=config.cleanup_temp_files,
        force_redownload=force_redownload,
    )

    # ------------------------------------------------------------ 5. Preview

    st.divider()
    st.subheader("5. Destination preview")

    service = get_ingestion_service(config, selected_session())
    preview = service.preview_destination(post, options)

    preview_columns = st.columns(2)
    with preview_columns[0]:
        st.caption("Local")
        st.code(str(preview["local_directory"]), language=None)
    with preview_columns[1]:
        if options.destination_mode.uploads_to_drive:
            account_label = next(
                (a.display_name for a in usable_accounts if a.key == drive_account),
                "No account selected",
            )
            st.caption(f"Google Drive - {account_label}")
            st.code(preview["remote_path"], language=None)
        else:
            st.caption("Google Drive")
            st.write("Not uploading.")

    st.caption("Files")
    st.write(" · ".join(preview["filenames"]))

    # ------------------------------------------------------------ 6. Download

    st.divider()
    st.subheader("6. Run")

    blocked_reason = None
    if not user.can_download:
        blocked_reason = "Downloading requires an administrator account."
    elif duplicates and not force_redownload:
        blocked_reason = "Select 'Download again' above to re-download this post."
    elif options.destination_mode.uploads_to_drive and not drive_account:
        blocked_reason = "Choose a Google Drive account, or switch to Local only."

    if blocked_reason:
        st.info(blocked_reason)

    button_label = (
        "Download + Upload"
        if options.destination_mode.uploads_to_drive
        else "Download"
    )

    if st.button(button_label, type="primary", disabled=bool(blocked_reason)):
        progress_bar = st.progress(0.0)
        status_line = st.empty()

        def report(message: str, fraction: float | None = None) -> None:
            status_line.write(message)
            if fraction is not None:
                progress_bar.progress(min(max(fraction, 0.0), 1.0))

        try:
            result = service.process(
                st.session_state[STATE_URL],
                options,
                user=user.username,
                post=post,
                progress=report,
            )
            st.session_state[STATE_RESULT] = result
        except AppError as exc:
            show_error(exc)
        except Exception as exc:
            show_error(exc)


# ------------------------------------------------------------------- 7. Result

result = st.session_state.get(STATE_RESULT) if single_mode else None

if result is not None:
    st.divider()
    st.subheader("Result")

    if result.status is JobStatus.COMPLETED:
        st.success(result.message or "Completed.")
    elif result.status is JobStatus.PARTIAL:
        st.warning(result.message or "Partially completed.")
        st.caption(
            "Your downloaded files have been kept locally. The upload can be "
            "retried from the History page."
        )
    elif result.status is JobStatus.SKIPPED:
        st.info(result.message or "Skipped.")
    else:
        st.error(result.message or "Failed.")

    if result.post is not None:
        detail_columns = st.columns(3)
        detail_columns[0].caption(f"Content: {result.post.content_type.value.title()}")
        detail_columns[1].caption(f"Author: {result.post.username}")
        detail_columns[2].caption(f"Shortcode: {result.post.shortcode}")

    if result.files:
        st.caption("Files")
        for downloaded in result.files:
            st.write(
                f"{downloaded.filename} - {human_bytes(downloaded.file_size)} - "
                f"SHA-256 {downloaded.sha256[:12]}..."
            )
        if result.metadata_file:
            st.write(result.metadata_file.name)
        if result.document_file:
            st.write(result.document_file.name)

    if result.uploads:
        st.caption("Google Drive")
        st.write(
            f"{result.uploaded_count} of {len(result.uploads)} files uploaded to "
            f"{result.remote_path}"
        )
        for failed in result.failed_uploads:
            st.write(f"Failed: {failed.filename} - {failed.error_message}")

        link = drive_link(result.drive_folder_id)
        if link:
            st.link_button("Open in Google Drive", link)

    if result.cleaned_up:
        st.caption("Local staging files were removed after the verified upload.")
    elif result.directory and Path(result.directory).exists():
        open_folder_hint(result.directory)

    if st.button("Start another download"):
        reset_analysis()
        st.session_state[STATE_URL] = ""
        st.rerun()




# =====================================================================
# Shared batch helpers (profile mode and URL-list mode)
# =====================================================================


def render_batch_options(prefix: str, post_count: int, source_label: str):
    """Media rule, destination, folders and run checks for a batch.

    Returns ``(media_rule, options, blocked_reason)``. Widget keys are
    namespaced by ``prefix`` so the two batch modes keep separate state.
    """
    # ------------------------------------------------- media rule
    st.divider()
    st.subheader("3. What to download from each post")

    rule_options = list(ProfileMediaRule)
    media_rule = st.radio(
        "Media rule",
        options=rule_options,
        index=rule_options.index(ProfileMediaRule.EVERYTHING),
        format_func=lambda rule: rule.label,
        key=f"{prefix}_media_rule",
    )
    st.caption(
        "Applied to every post. Posts with nothing matching the rule are "
        "skipped (for example, a photo post under 'Videos only')."
    )

    # ------------------------------------------------- destination
    st.divider()
    st.subheader("4. Destination")

    usable_accounts = [a for a in get_drive_accounts() if a.is_usable]

    try:
        default_mode = DestinationMode(config.default_destination)
    except ValueError:
        default_mode = DestinationMode.LOCAL_AND_DRIVE

    mode_options = list(DestinationMode)
    destination_mode = st.radio(
        "Where should the files go?",
        options=mode_options,
        index=mode_options.index(default_mode),
        format_func=lambda option: option.label,
        horizontal=True,
        key=f"{prefix}_destination_mode",
    )

    drive_account = None
    if destination_mode.uploads_to_drive:
        if not usable_accounts:
            st.error(
                "No Google Drive accounts are configured. Add one in "
                ".streamlit/secrets.toml, then connect it on the Google "
                "Drive page."
            )
        else:
            chosen = st.selectbox(
                "Google Drive account",
                options=usable_accounts,
                format_func=lambda account: account.display_name,
                key=f"{prefix}_drive_account_choice",
            )
            drive_account = chosen.key if chosen else None

    with st.expander("Folder organisation", expanded=False):
        root_folder = st.text_input(
            "Root folder",
            value=config.drive_root_folder,
            key=f"{prefix}_root_folder",
        )
        by_date_folder = st.checkbox(
            "Date folder (YYYY_MM_DD)",
            value=config.organize_by_date_folder,
            key=f"{prefix}_by_date_folder",
            help="One folder per publish date, e.g. 2026_09_21, instead of "
            "separate year and month folders.",
        )
        toggle_columns = st.columns(4)
        by_username = toggle_columns[0].checkbox(
            "Username", value=config.organize_by_username, key=f"{prefix}_by_username"
        )
        by_year = toggle_columns[1].checkbox(
            "Year",
            value=config.organize_by_year,
            key=f"{prefix}_by_year",
            disabled=by_date_folder,
        )
        by_month = toggle_columns[2].checkbox(
            "Month",
            value=config.organize_by_month,
            key=f"{prefix}_by_month",
            disabled=by_date_folder,
        )
        by_shortcode = toggle_columns[3].checkbox(
            "Shortcode",
            value=config.organize_by_shortcode,
            key=f"{prefix}_by_shortcode",
        )

    force_redownload = st.checkbox(
        "Re-download posts already in History",
        value=False,
        key=f"{prefix}_force_redownload",
        help="Off by default, so posts you already have are skipped without "
        "contacting Instagram.",
    )

    options = IngestionOptions(
        selection=MediaSelection.IMAGE,  # overridden per post by the rule
        destination_mode=destination_mode,
        drive_account=drive_account,
        root_folder=root_folder or config.drive_root_folder,
        organize_by_username=by_username,
        organize_by_year=by_year,
        organize_by_month=by_month,
        organize_by_shortcode=by_shortcode,
        organize_by_date_folder=by_date_folder,
        cleanup_temp_files=config.cleanup_temp_files,
        force_redownload=force_redownload,
    )

    # ------------------------------------------------- run checks
    st.divider()
    st.subheader("5. Run")

    blocked_reason = None
    if not user.can_download:
        blocked_reason = "Downloading requires an administrator account."
    elif destination_mode.uploads_to_drive and not drive_account:
        blocked_reason = "Choose a Google Drive account, or switch to Local only."

    if blocked_reason:
        st.info(blocked_reason)

    st.caption(
        f"About to process {post_count} post(s) from {source_label}, one at a "
        "time with a short pause between posts. Each becomes its own job in "
        "History. The batch stops on the first Instagram rate limit."
    )
    return media_rule, options, blocked_reason


def run_batch(analysis, media_rule, options, result_key: str) -> None:
    """Run a batch with a live progress bar and store its result."""
    service = get_ingestion_service(config, selected_session())
    progress_bar = st.progress(0.0)
    status_line = st.empty()

    def batch_report(message: str, fraction: float | None = None) -> None:
        status_line.write(message)
        if fraction is not None:
            progress_bar.progress(min(max(fraction, 0.0), 1.0))

    try:
        st.session_state[result_key] = service.process_profile(
            analysis,
            media_rule,
            options,
            user=user.username,
            progress=batch_report,
        )
    except AppError as exc:
        show_error(exc)
    except Exception as exc:
        show_error(exc)


def render_batch_result(batch, *, resume_hint: str) -> None:
    """Summary, early-stop notice and per-post detail for a finished batch."""
    st.divider()
    st.subheader("Batch result")

    if batch.listing_message:
        st.info(batch.listing_message)

    if batch.stopped_reason:
        st.error(
            "The batch stopped early because Instagram is rate limiting. "
            f"{len(batch.not_attempted)} post(s) were not attempted."
        )
        st.caption(batch.stopped_reason)
        st.caption(resume_hint)

    summary = st.columns(5)
    summary[0].metric("Total", batch.total)
    summary[1].metric("Downloaded", batch.succeeded)
    summary[2].metric("Partial", batch.partial)
    summary[3].metric("Skipped", batch.skipped)
    summary[4].metric("Failed", batch.failed)

    if batch.failed:
        st.warning(
            f"{batch.failed} post(s) failed. They can be re-run individually "
            "from History."
        )
    if batch.partial:
        st.caption(
            "Partial posts downloaded locally but did not finish uploading. "
            "Retry their upload from the History page."
        )

    labels = {
        JobStatus.COMPLETED: "OK",
        JobStatus.PARTIAL: "PARTIAL",
        JobStatus.SKIPPED: "SKIPPED",
    }
    with st.expander("Per-post detail", expanded=False):
        for item in batch.items:
            icon = labels.get(item.status, "FAILED")
            st.write(f"[{icon}] {item.shortcode} - {item.message or ''}")
        for shortcode in batch.not_attempted:
            st.write(f"[NOT ATTEMPTED] {shortcode}")


# =====================================================================
# Profile (batch) mode
# =====================================================================

if download_mode == MODE_PROFILE:
    st.subheader("1. Instagram profile URL")

    profile_url = st.text_input(
        "Profile URL",
        value=st.session_state.get(STATE_PROFILE_URL, ""),
        placeholder="https://www.instagram.com/username/",
        help="Paste a profile link to download its posts in bulk.",
    )

    st.caption(
        "Batch downloads can be slow and may hit Instagram rate limits on large "
        "accounts. The app honours Instagram's own rate limiting and only "
        "downloads content you are authorised to access. Private accounts "
        "generally require an authenticated session."
    )

    render_instagram_auth()

    # How many posts to list. "All" is offered but discouraged for big accounts.
    cap_columns = st.columns([1, 2])
    download_all = cap_columns[0].checkbox(
        "All posts", value=False, key="profile_all_posts"
    )
    post_cap = cap_columns[1].slider(
        "Maximum posts",
        min_value=1,
        max_value=200,
        value=30,
        step=1,
        disabled=download_all,
        key="profile_post_cap",
        help="Newest posts first. Ignored when 'All posts' is ticked.",
    )
    effective_limit = None if download_all else int(post_cap)

    if st.button("List posts", type="primary", disabled=not profile_url.strip()):
        reset_profile_analysis()
        st.session_state[STATE_PROFILE_URL] = profile_url.strip()
        service = get_ingestion_service(config, selected_session())

        with st.spinner("Listing the profile's posts..."):
            try:
                st.session_state[STATE_PROFILE] = service.analyze_profile(
                    profile_url.strip(), limit=effective_limit
                )
            except AppError as exc:
                show_error(exc)
            except Exception as exc:  # unexpected; keep the UI calm
                show_error(exc)

    analysis = st.session_state.get(STATE_PROFILE)

    if analysis is not None:
        st.divider()
        st.subheader("2. Profile")

        summary_columns = st.columns(2)
        summary_columns[0].metric("Account", f"@{analysis.username}")
        summary_columns[1].metric("Posts to process", analysis.post_count)

        if analysis.post_count == 0:
            st.warning(
                "No downloadable posts were found. The account may be empty, "
                "private, or require an authenticated session."
            )

    if analysis is not None and analysis.post_count > 0:
        media_rule, options, blocked_reason = render_batch_options(
            "profile", analysis.post_count, f"@{analysis.username}"
        )
        if st.button(
            "Download profile",
            type="primary",
            disabled=bool(blocked_reason),
            key="profile_run",
        ):
            run_batch(analysis, media_rule, options, STATE_BATCH_RESULT)

    batch = st.session_state.get(STATE_BATCH_RESULT)
    if batch is not None:
        render_batch_result(
            batch,
            resume_hint=(
                "Everything downloaded so far is kept. After waiting, list the "
                "profile again: posts already downloaded are skipped without "
                "contacting Instagram."
            ),
        )
        if st.button("Start another batch", key="profile_reset"):
            reset_profile_analysis()
            st.session_state[STATE_PROFILE_URL] = ""
            st.rerun()


# =====================================================================
# URL-list (batch) mode
# =====================================================================

if download_mode == MODE_URL_LIST:
    st.subheader("1. List of Instagram URLs")

    url_text = st.text_area(
        "Paste post / reel links",
        value=st.session_state.get(STATE_LIST_TEXT, ""),
        height=180,
        placeholder=(
            "https://www.instagram.com/reel/AAAAAAAAAAA/\n"
            "https://www.instagram.com/p/BBBBBBBBBBB/\n"
            "..."
        ),
        help="One link per line, or separated by commas or spaces.",
    )
    uploaded = st.file_uploader(
        "Or upload a .txt / .csv file of links",
        type=["txt", "csv"],
        key="url_list_file",
    )

    st.caption(
        "No profile lookup is made: each link is fetched on its own, one at a "
        "time with a pause between posts. Links already in History are skipped "
        "without contacting Instagram, so a stopped batch can simply be run "
        f"again. Up to {MAX_URL_LIST_ENTRIES} links per run."
    )

    # The project's own urls.csv, so a saved list needs no upload each time.
    default_list = parse_url_list(read_default_url_file())
    use_default_file = st.checkbox(
        f"Include links from urls.csv ({len(default_list.entries)} found)",
        value=bool(default_list.entries),
        key="url_list_use_default_file",
        help=f"Edit {DEFAULT_URL_FILE} in the project folder, one link per line.",
    )

    render_instagram_auth()

    combined_text = url_text
    if use_default_file:
        combined_text = combined_text + "\n" + read_default_url_file()
    if uploaded is not None:
        try:
            combined_text = (
                url_text + "\n" + uploaded.getvalue().decode("utf-8", "replace")
            )
        except Exception as exc:
            show_error(exc)

    if st.button("Check URLs", type="primary", disabled=not combined_text.strip()):
        reset_url_list()
        st.session_state[STATE_LIST_TEXT] = url_text
        st.session_state[STATE_LIST_PARSED] = parse_url_list(combined_text)

    parsed = st.session_state.get(STATE_LIST_PARSED)

    if parsed is not None:
        st.divider()
        st.subheader("2. Links")

        manifest = get_manifest(str(config.manifest_db_path))
        already = [s for s in parsed.shortcodes if manifest.shortcode_exists(s)]

        counts = st.columns(4)
        counts[0].metric("Valid links", len(parsed.entries))
        counts[1].metric("Already downloaded", len(already))
        counts[2].metric("Duplicates removed", parsed.duplicates)
        counts[3].metric(
            "Invalid", len(parsed.invalid) + len(parsed.profile_links)
        )

        if parsed.truncated:
            st.warning(
                f"Only the first {MAX_URL_LIST_ENTRIES} links are used; "
                f"{parsed.truncated} more were left out. Run the rest afterwards."
            )
        if parsed.profile_links:
            st.info(
                f"{len(parsed.profile_links)} profile link(s) were ignored. Use "
                "'Whole profile (batch)' for those."
            )
        if parsed.invalid:
            with st.expander(f"{len(parsed.invalid)} invalid link(s)"):
                for bad in parsed.invalid:
                    st.write(bad)
        if not parsed.entries:
            st.warning("No valid post, reel or video links were found.")

    if parsed is not None and parsed.entries:
        list_analysis = ProfileAnalysis.from_url_list(parsed.entries)
        media_rule, options, blocked_reason = render_batch_options(
            "urllist", list_analysis.post_count, "the URL list"
        )
        if st.button(
            "Download all",
            type="primary",
            disabled=bool(blocked_reason),
            key="url_list_run",
        ):
            run_batch(list_analysis, media_rule, options, STATE_LIST_RESULT)

    list_batch = st.session_state.get(STATE_LIST_RESULT)
    if list_batch is not None:
        render_batch_result(
            list_batch,
            resume_hint=(
                "Everything downloaded so far is kept. After waiting, press "
                "'Download all' again: links already downloaded are skipped "
                "without contacting Instagram."
            ),
        )
        if st.button("Start another list", key="url_list_reset"):
            reset_url_list()
            st.session_state[STATE_LIST_TEXT] = ""
            st.rerun()
