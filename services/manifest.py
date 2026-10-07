"""SQLite manifest: the record of everything ingested.

Four tables::

    jobs 1 --- 0..1 posts 1 --- n media_files 1 --- n uploads

A job may exist without a post, because a job row is written before the first
network call and the run may fail while resolving. A post always belongs to a
job.

OAuth tokens, passwords and password hashes are never stored here.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from models.job import Job, JobStatus, JobSummary
from models.media import DestinationMode, MediaSelection
from models.metadata import APPLICATION_VERSION
from models.upload import UploadStatus
from utils.errors import ManifestError

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_info (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id                  TEXT PRIMARY KEY,
    created_by          TEXT NOT NULL,
    requested_url       TEXT NOT NULL,
    shortcode           TEXT,
    requested_media     TEXT,
    destination_mode    TEXT NOT NULL,
    drive_account       TEXT,
    status              TEXT NOT NULL,
    current_step        TEXT,
    attempt_count       INTEGER NOT NULL DEFAULT 1,
    started_at          TEXT NOT NULL,
    finished_at         TEXT,
    error_message       TEXT,
    error_detail        TEXT,
    application_version TEXT
);

CREATE TABLE IF NOT EXISTS posts (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id           TEXT,
    shortcode        TEXT NOT NULL,
    instagram_url    TEXT NOT NULL,
    username         TEXT NOT NULL,
    caption          TEXT,
    content_type     TEXT NOT NULL,
    created_at       TEXT,
    downloaded_at    TEXT NOT NULL,
    status           TEXT NOT NULL,
    local_directory  TEXT,
    drive_account    TEXT,
    FOREIGN KEY (job_id) REFERENCES jobs (id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS media_files (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id       INTEGER NOT NULL,
    filename      TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    media_type    TEXT NOT NULL,
    file_size     INTEGER NOT NULL DEFAULT 0,
    sha256        TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    FOREIGN KEY (post_id) REFERENCES posts (id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS uploads (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    media_file_id  INTEGER NOT NULL,
    drive_account  TEXT NOT NULL,
    remote_path    TEXT NOT NULL,
    status         TEXT NOT NULL,
    uploaded_at    TEXT,
    drive_file_id  TEXT,
    error_message  TEXT,
    FOREIGN KEY (media_file_id) REFERENCES media_files (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_posts_shortcode   ON posts (shortcode);
CREATE INDEX IF NOT EXISTS idx_posts_job         ON posts (job_id);
CREATE INDEX IF NOT EXISTS idx_jobs_started      ON jobs (started_at DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_status       ON jobs (status);
CREATE INDEX IF NOT EXISTS idx_media_post        ON media_files (post_id);
CREATE INDEX IF NOT EXISTS idx_uploads_media     ON uploads (media_file_id);
CREATE INDEX IF NOT EXISTS idx_uploads_status    ON uploads (status);
"""


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Manifest:
    """Data access for the ingestion manifest."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    # ------------------------------------------------------------ connection

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """Yield a connection with foreign keys on and rows as mappings."""
        connection = sqlite3.connect(self.db_path, timeout=15.0)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            yield connection
            connection.commit()
        except sqlite3.Error as exc:
            connection.rollback()
            raise ManifestError(f"Database error: {exc}") from exc
        finally:
            connection.close()

    def _initialize(self) -> None:
        try:
            with self.connect() as connection:
                connection.executescript(_SCHEMA)
                row = connection.execute("SELECT version FROM schema_info").fetchone()
                if row is None:
                    connection.execute(
                        "INSERT INTO schema_info (version) VALUES (?)",
                        (SCHEMA_VERSION,),
                    )
        except ManifestError:
            raise
        except sqlite3.Error as exc:  # pragma: no cover - defensive
            raise ManifestError(f"Could not initialise manifest: {exc}") from exc

    # ------------------------------------------------------------------- jobs

    def create_job(self, job: Job) -> Job:
        """Insert a job row. Called before any network activity."""
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO jobs (
                    id, created_by, requested_url, shortcode, requested_media,
                    destination_mode, drive_account, status, current_step,
                    attempt_count, started_at, finished_at, error_message,
                    error_detail, application_version
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    job.id,
                    job.created_by,
                    job.requested_url,
                    job.shortcode,
                    job.requested_media.value if job.requested_media else None,
                    job.destination_mode.value,
                    job.drive_account,
                    job.status.value,
                    job.current_step,
                    job.attempt_count,
                    _iso(job.started_at),
                    _iso(job.finished_at),
                    job.error_message,
                    job.error_detail,
                    job.application_version or APPLICATION_VERSION,
                ),
            )
        logger.info("Job created id=%s url=%s", job.id, job.requested_url)
        return job

    def update_job(self, job_id: str, **fields: Any) -> None:
        """Update selected job columns.

        Enum values are stored as their string value; datetimes as ISO strings.
        """
        allowed = {
            "shortcode",
            "requested_media",
            "destination_mode",
            "drive_account",
            "status",
            "current_step",
            "attempt_count",
            "finished_at",
            "error_message",
            "error_detail",
        }
        updates: dict[str, Any] = {}
        for key, value in fields.items():
            if key not in allowed:
                continue
            if hasattr(value, "value"):
                value = value.value
            elif isinstance(value, datetime):
                value = _iso(value)
            updates[key] = value

        if not updates:
            return

        assignments = ", ".join(f"{k} = ?" for k in updates)
        with self.connect() as connection:
            connection.execute(
                f"UPDATE jobs SET {assignments} WHERE id = ?",
                (*updates.values(), job_id),
            )

    def set_job_status(
        self,
        job_id: str,
        status: JobStatus,
        *,
        step: str | None = None,
        error_message: str | None = None,
        error_detail: str | None = None,
    ) -> None:
        """Move a job to ``status``, stamping ``finished_at`` on terminal states."""
        fields: dict[str, Any] = {"status": status}
        if step is not None:
            fields["current_step"] = step
        if error_message is not None:
            fields["error_message"] = error_message
        if error_detail is not None:
            fields["error_detail"] = error_detail
        if status.is_terminal:
            fields["finished_at"] = _now()
        self.update_job(job_id, **fields)
        logger.info("Job %s status=%s", job_id, status.value)

    def get_job(self, job_id: str) -> Job | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
        return self._row_to_job(row) if row else None

    def list_jobs(
        self,
        *,
        limit: int = 200,
        offset: int = 0,
        status: JobStatus | None = None,
        username: str | None = None,
        content_type: str | None = None,
        drive_account: str | None = None,
        created_by: str | None = None,
        search: str | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Job rows joined with post details, newest first.

        Returns plain dicts because the History table needs a flat, display
        oriented shape rather than nested models.
        """
        clauses: list[str] = []
        params: list[Any] = []

        if status is not None:
            clauses.append("j.status = ?")
            params.append(status.value)
        if username:
            clauses.append("p.username = ?")
            params.append(username)
        if content_type:
            clauses.append("p.content_type = ?")
            params.append(content_type)
        if drive_account:
            clauses.append("j.drive_account = ?")
            params.append(drive_account)
        if created_by:
            clauses.append("j.created_by = ?")
            params.append(created_by)
        if search:
            clauses.append("(j.shortcode LIKE ? OR p.caption LIKE ?)")
            params.extend([f"%{search}%", f"%{search}%"])
        if date_from is not None:
            clauses.append("j.started_at >= ?")
            params.append(_iso(date_from))
        if date_to is not None:
            clauses.append("j.started_at <= ?")
            params.append(_iso(date_to))

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

        query = f"""
            SELECT
                j.*,
                p.id            AS post_id,
                p.username      AS username,
                p.content_type  AS content_type,
                p.caption       AS caption,
                p.local_directory AS local_directory,
                (SELECT COUNT(*) FROM media_files m WHERE m.post_id = p.id)
                    AS file_count,
                (SELECT COALESCE(SUM(m.file_size), 0) FROM media_files m
                    WHERE m.post_id = p.id) AS total_bytes,
                (SELECT COUNT(*) FROM uploads u
                    JOIN media_files m2 ON m2.id = u.media_file_id
                    WHERE m2.post_id = p.id AND u.status = ?) AS uploaded_count,
                (SELECT COUNT(*) FROM uploads u
                    JOIN media_files m3 ON m3.id = u.media_file_id
                    WHERE m3.post_id = p.id AND u.status = ?) AS failed_upload_count
            FROM jobs j
            LEFT JOIN posts p ON p.job_id = j.id
            {where}
            ORDER BY j.started_at DESC
            LIMIT ? OFFSET ?
        """

        with self.connect() as connection:
            rows = connection.execute(
                query,
                (
                    UploadStatus.UPLOADED.value,
                    UploadStatus.FAILED.value,
                    *params,
                    limit,
                    offset,
                ),
            ).fetchall()

        return [dict(row) for row in rows]

    def job_summary(self) -> JobSummary:
        """Aggregate counts for the History activity strip."""
        with self.connect() as connection:
            counts = connection.execute(
                "SELECT status, COUNT(*) AS n FROM jobs GROUP BY status"
            ).fetchall()
            total_bytes = connection.execute(
                "SELECT COALESCE(SUM(file_size), 0) AS n FROM media_files"
            ).fetchone()

        by_status = {row["status"]: row["n"] for row in counts}
        return JobSummary(
            total=sum(by_status.values()),
            completed=by_status.get(JobStatus.COMPLETED.value, 0),
            partial=by_status.get(JobStatus.PARTIAL.value, 0),
            failed=by_status.get(JobStatus.FAILED.value, 0),
            skipped=by_status.get(JobStatus.SKIPPED.value, 0),
            local_bytes=total_bytes["n"] if total_bytes else 0,
        )

    def distinct_values(self, column: str) -> list[str]:
        """Distinct filter options for the History page."""
        mapping = {
            "username": "SELECT DISTINCT username FROM posts WHERE username != ''",
            "content_type": "SELECT DISTINCT content_type FROM posts",
            "drive_account": (
                "SELECT DISTINCT drive_account FROM jobs "
                "WHERE drive_account IS NOT NULL"
            ),
            "created_by": "SELECT DISTINCT created_by FROM jobs",
        }
        query = mapping.get(column)
        if not query:
            return []
        with self.connect() as connection:
            rows = connection.execute(f"{query} ORDER BY 1").fetchall()
        return [row[0] for row in rows if row[0]]

    # ------------------------------------------------------------------ posts

    def record_post(
        self,
        *,
        job_id: str | None,
        shortcode: str,
        instagram_url: str,
        username: str,
        caption: str | None,
        content_type: str,
        created_at: datetime | None,
        local_directory: str | None,
        drive_account: str | None = None,
        status: str = "downloaded",
        downloaded_at: datetime | None = None,
    ) -> int:
        """Insert a post row and return its primary key."""
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO posts (
                    job_id, shortcode, instagram_url, username, caption,
                    content_type, created_at, downloaded_at, status,
                    local_directory, drive_account
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    job_id,
                    shortcode,
                    instagram_url,
                    username,
                    caption,
                    content_type,
                    _iso(created_at),
                    _iso(downloaded_at or _now()),
                    status,
                    local_directory,
                    drive_account,
                ),
            )
            post_id = int(cursor.lastrowid or 0)
        logger.info("Manifest updated post_id=%s shortcode=%s", post_id, shortcode)
        return post_id

    def find_posts_by_shortcode(self, shortcode: str) -> list[dict[str, Any]]:
        """All recorded downloads of a shortcode, newest first."""
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM posts
                WHERE shortcode = ?
                ORDER BY downloaded_at DESC
                """,
                (shortcode,),
            ).fetchall()
        return [dict(row) for row in rows]

    def shortcode_exists(self, shortcode: str) -> bool:
        """Duplicate check used before downloading."""
        with self.connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM posts WHERE shortcode = ? LIMIT 1", (shortcode,)
            ).fetchone()
        return row is not None

    def get_post(self, post_id: int) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM posts WHERE id = ?", (post_id,)
            ).fetchone()
        return dict(row) if row else None

    def get_post_for_job(self, job_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM posts WHERE job_id = ? ORDER BY id DESC LIMIT 1",
                (job_id,),
            ).fetchone()
        return dict(row) if row else None

    def delete_post(self, post_id: int) -> None:
        """Delete a post row. Cascades to media_files and uploads.

        Local media files on disk are never touched by this method.
        """
        with self.connect() as connection:
            connection.execute("DELETE FROM posts WHERE id = ?", (post_id,))
        logger.info("Manifest post deleted post_id=%s", post_id)

    def delete_job(self, job_id: str) -> None:
        with self.connect() as connection:
            connection.execute("DELETE FROM posts WHERE job_id = ?", (job_id,))
            connection.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
        logger.info("Manifest job deleted job_id=%s", job_id)

    # ------------------------------------------------------------ media files

    def record_media_file(
        self,
        *,
        post_id: int,
        filename: str,
        relative_path: str,
        media_type: str,
        file_size: int,
        sha256: str,
    ) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO media_files (
                    post_id, filename, relative_path, media_type,
                    file_size, sha256, created_at
                ) VALUES (?,?,?,?,?,?,?)
                """,
                (
                    post_id,
                    filename,
                    relative_path,
                    media_type,
                    file_size,
                    sha256,
                    _iso(_now()),
                ),
            )
            return int(cursor.lastrowid or 0)

    def list_media_files(self, post_id: int) -> list[dict[str, Any]]:
        """Media files for a post, each with its latest upload outcome."""
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    m.*,
                    u.id            AS upload_id,
                    u.status        AS upload_status,
                    u.remote_path   AS remote_path,
                    u.drive_file_id AS drive_file_id,
                    u.drive_account AS upload_account,
                    u.uploaded_at   AS uploaded_at,
                    u.error_message AS upload_error
                FROM media_files m
                LEFT JOIN uploads u ON u.id = (
                    SELECT id FROM uploads
                    WHERE media_file_id = m.id
                    ORDER BY id DESC LIMIT 1
                )
                WHERE m.post_id = ?
                ORDER BY m.id
                """,
                (post_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def find_media_by_hash(self, sha256: str) -> list[dict[str, Any]]:
        """Locate files by content hash, for duplicate and integrity checks."""
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM media_files WHERE sha256 = ?", (sha256,)
            ).fetchall()
        return [dict(row) for row in rows]

    # ---------------------------------------------------------------- uploads

    def record_upload(
        self,
        *,
        media_file_id: int,
        drive_account: str,
        remote_path: str,
        status: UploadStatus,
        drive_file_id: str | None = None,
        error_message: str | None = None,
        uploaded_at: datetime | None = None,
    ) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO uploads (
                    media_file_id, drive_account, remote_path, status,
                    uploaded_at, drive_file_id, error_message
                ) VALUES (?,?,?,?,?,?,?)
                """,
                (
                    media_file_id,
                    drive_account,
                    remote_path,
                    status.value,
                    _iso(uploaded_at) if uploaded_at else None,
                    drive_file_id,
                    error_message,
                ),
            )
            return int(cursor.lastrowid or 0)

    def update_upload(
        self,
        upload_id: int,
        *,
        status: UploadStatus,
        drive_file_id: str | None = None,
        error_message: str | None = None,
        uploaded_at: datetime | None = None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE uploads
                SET status = ?, drive_file_id = ?, error_message = ?, uploaded_at = ?
                WHERE id = ?
                """,
                (
                    status.value,
                    drive_file_id,
                    error_message,
                    _iso(uploaded_at) if uploaded_at else None,
                    upload_id,
                ),
            )

    def list_failed_uploads(self) -> list[dict[str, Any]]:
        """Failed uploads joined to enough context to retry them."""
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    u.*, m.filename, m.relative_path, m.sha256, m.file_size,
                    p.shortcode, p.username, p.local_directory, p.id AS post_id
                FROM uploads u
                JOIN media_files m ON m.id = u.media_file_id
                JOIN posts p ON p.id = m.post_id
                WHERE u.status = ?
                ORDER BY u.id DESC
                """,
                (UploadStatus.FAILED.value,),
            ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------ misc

    def export_job(self, job_id: str) -> str:
        """Serialize a job and its content to JSON, for the detail view."""
        job = self.get_job(job_id)
        post = self.get_post_for_job(job_id)
        media = self.list_media_files(int(post["id"])) if post else []
        return json.dumps(
            {
                "job": job.model_dump(mode="json") if job else None,
                "post": post,
                "media_files": media,
            },
            indent=2,
            default=str,
        )

    @staticmethod
    def _row_to_job(row: sqlite3.Row) -> Job:
        return Job(
            id=row["id"],
            created_by=row["created_by"],
            requested_url=row["requested_url"],
            shortcode=row["shortcode"],
            requested_media=(
                MediaSelection(row["requested_media"])
                if row["requested_media"]
                else None
            ),
            destination_mode=DestinationMode(row["destination_mode"]),
            drive_account=row["drive_account"],
            status=JobStatus(row["status"]),
            current_step=row["current_step"],
            attempt_count=row["attempt_count"],
            started_at=_parse_dt(row["started_at"]) or _now(),
            finished_at=_parse_dt(row["finished_at"]),
            error_message=row["error_message"],
            error_detail=row["error_detail"],
            application_version=row["application_version"] or APPLICATION_VERSION,
        )
