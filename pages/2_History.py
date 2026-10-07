"""History page: activity summary, job list, and per-job metadata drill-down.

Every ingestion attempt appears here, including ones that failed before
resolving. The detail view exposes everything recorded in the manifest so the
user never has to open metadata.json in a text editor to see what was captured.
"""

from __future__ import annotations

from datetime import datetime, time, timezone

import streamlit as st

from models.job import JobStatus
from utils.config import get_drive_accounts
from utils.errors import AppError
from utils.ui import (
    bootstrap,
    current_user,
    drive_link,
    get_ingestion_service,
    get_job_service,
    render_summary_metrics,
    require_login,
    show_error,
)

STATE_SELECTED = "history_selected_job"

config = bootstrap()
user = require_login() if current_user() is None else current_user()
jobs = get_job_service(config)

st.title("History")

# ------------------------------------------------------------------- summary

try:
    summary = jobs.summary()
    render_summary_metrics(summary)
except AppError as exc:
    show_error(exc)
    st.stop()

if summary.total == 0:
    st.info("No jobs have run yet. Start on the Download page.")
    st.stop()

# ------------------------------------------------------------------- filters

options = jobs.filter_options()

with st.expander("Filters", expanded=False):
    row_one = st.columns(3)

    status_choice = row_one[0].selectbox(
        "Job status",
        options=["All", *[status.value for status in JobStatus]],
        format_func=lambda value: (
            "All" if value == "All" else JobStatus(value).label
        ),
    )
    username_choice = row_one[1].selectbox(
        "Creator", options=["All", *options["username"]]
    )
    type_choice = row_one[2].selectbox(
        "Content type", options=["All", *options["content_type"]]
    )

    row_two = st.columns(3)
    account_choice = row_two[0].selectbox(
        "Google Drive account", options=["All", *options["drive_account"]]
    )
    created_by_choice = row_two[1].selectbox(
        "Started by", options=["All", *options["created_by"]]
    )
    search = row_two[2].text_input("Search shortcode or caption", value="")

    date_range = st.date_input("Date range", value=(), help="Leave empty for all dates")

date_from = date_to = None
if isinstance(date_range, (tuple, list)) and len(date_range) == 2:
    date_from = datetime.combine(date_range[0], time.min, tzinfo=timezone.utc)
    date_to = datetime.combine(date_range[1], time.max, tzinfo=timezone.utc)

try:
    rows = jobs.history_rows(
        status=None if status_choice == "All" else JobStatus(status_choice),
        username=None if username_choice == "All" else username_choice,
        content_type=None if type_choice == "All" else type_choice,
        drive_account=None if account_choice == "All" else account_choice,
        created_by=None if created_by_choice == "All" else created_by_choice,
        search=search.strip() or None,
        date_from=date_from,
        date_to=date_to,
    )
except AppError as exc:
    show_error(exc)
    st.stop()

st.caption(f"{len(rows)} job(s)")

if not rows:
    st.info("No jobs match these filters.")
    st.stop()

# ------------------------------------------------------------------ job table

table_rows = [
    {
        "Date": row["date"],
        "Started by": row["account"],
        "Creator": row["username"],
        "Type": row["type"],
        "Shortcode": row["shortcode"],
        "Files": row["files"],
        "Size": row["size"],
        "Drive": row["drive"],
        "Upload": row["upload"],
        "Status": row["status"],
    }
    for row in rows
]

st.dataframe(table_rows, width="stretch", hide_index=True)

# --------------------------------------------------------------- job selection


def label_for(row: dict) -> str:
    return (
        f"{row['date']} - {row['username']} - {row['type']} - "
        f"{row['shortcode']} - {row['status']}"
    )


selected_label = st.selectbox(
    "Select a job to see its details",
    options=[label_for(row) for row in rows],
    key=STATE_SELECTED,
)
selected_row = next(
    (row for row in rows if label_for(row) == selected_label), rows[0]
)

try:
    detail = jobs.job_detail(selected_row["job_id"])
except AppError as exc:
    show_error(exc)
    st.stop()

job = detail["job"]
post = detail["post"]

st.divider()
st.subheader("Job detail")

# ------------------------------------------------------------------ run detail

run_columns = st.columns(4)
run_columns[0].caption(f"Status: {job.status.label}")
run_columns[1].caption(f"Started: {job.started_at.strftime('%Y-%m-%d %H:%M')}")
run_columns[2].caption(
    "Duration: "
    + (
        f"{job.duration_seconds:.0f}s"
        if job.duration_seconds is not None
        else "still running"
    )
)
run_columns[3].caption(f"Attempts: {job.attempt_count}")

meta_columns = st.columns(3)
meta_columns[0].caption(
    f"Requested media: {job.requested_media.label if job.requested_media else '-'}"
)
meta_columns[1].caption(f"Destination: {job.destination_mode.label}")
meta_columns[2].caption(f"Drive account: {job.drive_account or '-'}")

st.caption("Job ID")
st.code(job.id, language=None)

if job.error_message:
    st.warning(job.error_message)
    if job.error_detail:
        with st.expander("Technical detail"):
            st.code(job.error_detail, language=None)

# ---------------------------------------------------------------------- source

if post:
    st.markdown("**Source**")
    source_columns = st.columns(3)
    source_columns[0].caption(f"Platform: Instagram")
    source_columns[1].caption(f"Content type: {selected_row['type']}")
    source_columns[2].caption(f"Shortcode: {post.get('shortcode', '-')}")

    instagram_url = post.get("instagram_url")
    if instagram_url:
        st.link_button("Open original post", instagram_url)

    st.markdown("**Creator**")
    st.caption(f"Username: {post.get('username', '-')}")
    created_at = post.get("created_at")
    if created_at:
        st.caption(f"Posted at: {created_at}")

    caption_text = post.get("caption")
    with st.expander("Full caption", expanded=False):
        st.write(caption_text or "(no caption)")

# ----------------------------------------------------------------------- files

if detail["missing_files"]:
    st.warning(
        "These files are recorded but no longer on disk: "
        + ", ".join(detail["missing_files"])
    )

if detail["media"]:
    st.markdown("**Files**")
    st.dataframe(
        [
            {
                "Filename": entry["filename"],
                "Type": entry["media_type"],
                "Size": entry["size_human"],
                "SHA-256": entry["sha256_short"] + "...",
                "On disk": "Yes" if entry["exists"] else "Missing",
                "Upload": entry["upload_status"],
            }
            for entry in detail["media"]
        ],
        width="stretch",
        hide_index=True,
    )

    with st.expander("Full hashes and paths", expanded=False):
        for entry in detail["media"]:
            st.caption(entry["filename"])
            st.code(
                f"SHA-256:     {entry['sha256']}\n"
                f"Local path:  {entry['local_path']}\n"
                f"Remote path: {entry['remote_path'] or '-'}\n"
                f"Drive ID:    {entry['drive_file_id'] or '-'}",
                language=None,
            )
            if entry["upload_error"]:
                st.caption(f"Upload error: {entry['upload_error']}")

# ------------------------------------------------------------------- artifacts

artifact_columns = st.columns(2)

with artifact_columns[0]:
    if detail["document"]:
        with st.expander("document.txt", expanded=False):
            st.text(detail["document"])

with artifact_columns[1]:
    if detail["metadata"]:
        with st.expander("metadata.json", expanded=False):
            st.json(detail["metadata"])

if detail["directory"]:
    st.caption("Local folder")
    st.code(str(detail["directory"]), language=None)

# -------------------------------------------------------------------- actions

st.divider()
st.markdown("**Actions**")

action_columns = st.columns(4)

# Retry upload
with action_columns[0]:
    retry_disabled = not detail["can_retry_upload"] or not user.can_download
    if st.button(
        "Retry Google Drive upload",
        disabled=retry_disabled,
        help=(
            "Uses the existing local files. Instagram is not contacted."
            if detail["can_retry_upload"]
            else "Nothing outstanding to upload, or the local files are missing."
        ),
    ):
        accounts = [a for a in get_drive_accounts() if a.is_usable]
        target = job.drive_account or (accounts[0].key if accounts else None)
        service = get_ingestion_service(config)

        progress_bar = st.progress(0.0)
        status_line = st.empty()

        def report(message: str, fraction: float | None = None) -> None:
            status_line.write(message)
            if fraction is not None:
                progress_bar.progress(min(max(fraction, 0.0), 1.0))

        try:
            outcome = service.retry_upload(
                job.id, user=user.username, drive_account=target, progress=report
            )
            if outcome.status is JobStatus.COMPLETED:
                st.success(outcome.message)
            else:
                st.warning(outcome.message)
            st.rerun()
        except AppError as exc:
            show_error(exc)

# Re-run
with action_columns[1]:
    if st.button(
        "Re-run this job",
        disabled=not user.can_download,
        help="Runs the same URL again as a new job. This record is preserved.",
    ):
        st.session_state["download_url"] = job.requested_url
        st.session_state.pop("download_post", None)
        st.info(
            "The URL has been copied to the Download page. Open it and press "
            "Analyze."
        )

# Open in Drive
with action_columns[2]:
    uploaded_entry = next(
        (e for e in detail["media"] if e["drive_file_id"]), None
    )
    if uploaded_entry:
        st.link_button(
            "Open in Google Drive",
            f"https://drive.google.com/file/d/{uploaded_entry['drive_file_id']}/view",
        )

# Delete record
with action_columns[3]:
    with st.popover("Delete record", disabled=not user.is_admin):
        st.caption(
            "Removes this job from history. Local media is kept unless you tick "
            "the box below."
        )
        also_delete_files = st.checkbox("Also delete local files", value=False)
        if st.button("Confirm delete", type="primary"):
            try:
                if also_delete_files and detail["directory"]:
                    from services.cleanup import cleanup_directory

                    cleanup_directory(detail["directory"])
                jobs.manifest.delete_job(job.id)
                st.success("Record deleted.")
                st.session_state.pop(STATE_SELECTED, None)
                st.rerun()
            except AppError as exc:
                show_error(exc)

# ---------------------------------------------------------------------- export

with st.expander("Export job record as JSON", expanded=False):
    try:
        st.code(jobs.manifest.export_job(job.id), language="json")
    except AppError as exc:
        show_error(exc)
