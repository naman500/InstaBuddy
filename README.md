# Instagram Media Downloader & Google Drive LLM Knowledge Base

An Instagram content **ingestion system** for a future LLM/RAG knowledge base, with a
Streamlit dashboard. Paste a post or reel URL, choose exactly which media to keep,
and the application downloads it, generates structured metadata alongside it, files
it into a predictable folder structure, and uploads it to a chosen Google Drive
account.

The framing matters: this is not just a downloader. Every ingested item carries a
stable identifier, metadata, a SHA-256 hash, timestamps, storage location and upload
status, so OCR, transcription, frame extraction and embeddings can be added later
without rewriting anything.

> **Scope of use.** Only process content you are authorised to download and use. This
> application never stores Instagram or Google passwords.

---

## Table of contents

1. [What it does](#1-what-it-does)
2. [Architecture](#2-architecture)
3. [Requirements](#3-requirements)
4. [Installation](#4-installation)
5. [Create the first dashboard account](#5-create-the-first-dashboard-account)
6. [Google Cloud setup](#6-google-cloud-setup)
7. [Configure Google Drive accounts](#7-configure-google-drive-accounts)
8. [Optional Instagram authentication](#8-optional-instagram-authentication)
9. [Running the application](#9-running-the-application)
10. [Usage](#10-usage)
11. [Folder structure](#11-folder-structure)
12. [Output artifacts](#12-output-artifacts)
13. [Configuration reference](#13-configuration-reference)
14. [Testing](#14-testing)
15. [Security](#15-security)
16. [Troubleshooting](#16-troubleshooting)
17. [Known limitations](#17-known-limitations)
18. [Future LLM pipeline](#18-future-llm-pipeline)

---

## 1. What it does

- Accepts `/p/`, `/reel/`, `/reels/` and `/tv/` Instagram URLs, validated before any
  network request.
- **Two-phase flow.** *Analyze* fetches only enough to describe the post and writes
  nothing to disk. You then choose what to download.
- Detects content type from the Instaloader `Post` object, not the URL. A `/reel/`
  link pointing at a photo correctly resolves to an image.
- Selective download per content type:
  - Reel / video: video, cover image, or both
  - Image post: the image
  - Carousel: images only, videos only, or both, with original ordering preserved
    (`001.jpg`, `002.mp4`, ...)
- Generates `metadata.json` (schema-versioned) and `document.txt` (a flat text
  surface for future RAG ingestion) for every post.
- SHA-256 hashes every file, for deduplication and upload integrity.
- Records everything in a local SQLite manifest with a full job history.
- Uploads to multiple configurable Google Drive accounts via the Drive API, using
  resumable chunked sessions for large files, with retry and exponential backoff.
- Never deletes local media before an upload is verified. Failed uploads are
  retryable from History without re-contacting Instagram.
- Requires a dashboard sign-in, with admin and viewer roles.

---

## 2. Architecture

```
Instagram
    |
    v
Instaloader
    |
    v
Content Resolver  ........ determines type, media items (analyze phase)
    |
    v
Media Downloader  ........ streams to .part, renames on completion
    |
    +---------+---------+
    v                   v
 Images              Videos
    +---------+---------+
              v
     Metadata Generator  ... metadata.json + document.txt
              |
              v
       Local Staging  ...... downloads/user/YYYY/MM/SHORTCODE/
              |
              v
      Google Drive Manager  find-or-create folders, chunked upload
              |
              v
        Google Drive
              |
              v
     ( Future LLM pipeline )
```

Three rules hold the design together:

1. `app.py` does initialisation, the login gate and navigation only.
2. Business logic never imports Streamlit. `services/` is callable from a script or a
   test.
3. The Instagram and Google Drive layers never import each other; they meet only in
   the ingestion service.

### Module map

| Path | Responsibility |
|------|----------------|
| `app.py` | Entry point: bootstrap, login gate, navigation |
| `pages/` | Streamlit UI only |
| `auth/app_auth.py` | Dashboard login, PBKDF2 hashing, roles, lockout |
| `auth/google_auth.py` | Google OAuth loopback flow, token cache |
| `auth/instagram_auth.py` | Optional Instaloader session load/save |
| `instagram/client.py` | Instaloader wrapper, bounded retries |
| `instagram/resolver.py` | URL to `InstagramPost` |
| `instagram/downloader.py` | Streaming media download, hashing |
| `instagram/metadata.py` | `metadata.json` + `document.txt` |
| `gdrive/client.py` | Drive API v3 wrapper, error mapping |
| `gdrive/folders.py` | Folder chain resolution by ID |
| `gdrive/uploader.py` | Simple + resumable chunked uploads |
| `models/` | Pydantic models |
| `services/ingestion.py` | The orchestrator |
| `services/manifest.py` | SQLite data access |
| `services/jobs.py` | Job lifecycle, History assembly |
| `services/hashing.py` | SHA-256 |
| `services/cleanup.py` | Post-upload cleanup policy |
| `utils/` | Config, paths, validation, logging, errors, UI helpers |

---

## 3. Requirements

- **Python 3.11 or newer** (developed and tested on 3.14)
- A Google account and a Google Cloud project, for Drive uploads
- No Instagram account required for public content

Dependencies, from `requirements.txt`:

```
instaloader          Instagram access
streamlit            dashboard
requests             HTTP
pydantic             data models and validation
python-dotenv        .env loading
google-api-python-client, google-auth, google-auth-oauthlib
pytest               tests
```

No ffmpeg, OCR, Whisper or vector-database dependency is included. Those arrive only
when the feature that needs them is built.

---

## 4. Installation

```bash
git clone <your-repo-url> instagram-llm-kb
cd instagram-llm-kb

python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

pip install --upgrade pip
pip install -r requirements.txt
```

Create the configuration files:

```bash
cp .env.example .env
cp .streamlit/secrets.toml.example .streamlit/secrets.toml
```

Neither `.env` nor `.streamlit/secrets.toml` is committed; both are in `.gitignore`.

---

## 5. Create the first dashboard account

The dashboard requires a sign-in. Passwords are stored only as salted
PBKDF2-HMAC-SHA256 hashes.

```bash
python scripts/hash_password.py
```

The script reads the password without echoing it, never writes it to disk and never
puts it in your shell history. It prints a block to paste into
`.streamlit/secrets.toml`:

```toml
[auth.users.naman]
display_name = "Naman"
role = "admin"
password_hash = "pbkdf2_sha256$260000$...$..."
```

### Roles

| Role | Permissions |
|------|-------------|
| `admin` | Everything: downloads, uploads, connecting accounts, settings |
| `viewer` | Browse History and job detail only |

Until at least one account exists, the application starts and shows setup guidance
instead of a login form.

---

## 6. Google Cloud setup

1. Go to the [Google Cloud Console](https://console.cloud.google.com/) and create a
   project (or select an existing one).
2. Enable the **Google Drive API**: *APIs & Services > Library > Google Drive API >
   Enable*.
3. Configure the **OAuth consent screen**: *APIs & Services > OAuth consent screen*.
   - User type **External** is fine for personal use.
   - Add your own Google account under **Test users**.
   - Add the scope `https://www.googleapis.com/auth/drive.file`.
4. Create credentials: *APIs & Services > Credentials > Create credentials > OAuth
   client ID*.
   - Application type: **Desktop app**
   - Copy the **client ID** and **client secret**.

### Required scope

| Scope | Why |
|-------|-----|
| `https://www.googleapis.com/auth/drive.file` | Create and manage **only the files this application creates**. It cannot read the rest of your Drive. |

This is the minimum scope for the job. A consequence worth knowing: because the app
can only see its own files, it will not find or reuse folders you created by hand in
the Drive web UI. It creates and tracks its own folder tree.

The broader `drive` scope would allow access to your entire Drive. It is not
requested, and is not needed.

### Why a Desktop app client

The application uses the installed-application **loopback** flow: a temporary local
HTTP server receives the redirect from Google. The old out-of-band
(paste-the-code) flow was discontinued by Google, so loopback is the correct choice
for a locally run tool.

---

## 7. Configure Google Drive accounts

Add one section per account to `.streamlit/secrets.toml`:

```toml
[gdrive.personal]
display_name = "Personal Google Drive"
client_id = "YOUR_CLIENT_ID.apps.googleusercontent.com"
client_secret = "YOUR_CLIENT_SECRET"

[gdrive.work]
display_name = "Work Google Drive"
client_id = "YOUR_OTHER_CLIENT_ID.apps.googleusercontent.com"
client_secret = "YOUR_OTHER_CLIENT_SECRET"
```

Then in the app:

1. Open the **Google Drive** page.
2. Press **Connect** next to an account.
3. A browser window opens for Google sign-in. Grant access and return.
4. Press **Test connection** to confirm.

Tokens are cached as per-account JSON files in `tokens/`, with owner-only
permissions. They are never stored in the database and never shown in the UI.

---

## 8. Optional Instagram authentication

The default mode is **public content, no login**. In that mode the application never
asks for an Instagram username or password.

For content that requires a signed-in session, add one from the dashboard
(admin only), under **Settings → Instagram sessions**.

**Browser login (recommended).** Press **Open browser and log in**. A Chrome window
opens on Instagram's own login page. Sign in there, completing any two-factor or
"was this you?" checks with Instagram directly. When your feed loads, the window
closes and the session is saved. Your password goes only to Instagram; the app only
reads the resulting instagram.com cookies. The browser uses a throwaway profile, and
the window opens on the machine running the app (5-minute limit). It uses an
installed Google Chrome/Chromium; if neither exists, run `playwright install
chromium`.

**Username and password (fallback).**

1. Expand **Or log in with username and password**.
2. Enter the username and password of an account you own and press **Log in and
   save session**.
3. If the account uses two-factor authentication, enter the code from your
   authenticator app or SMS when prompted.

The session is saved to `sessions/session-<username>` with owner-only permissions.
Saved sessions are listed on the same page with **Test** and **Delete** buttons.
Then on the Download page open **Instagram authentication** and select **Use
authenticated Instagram session**.

You can still create a session with the CLI instead (`instaloader --login
YOUR_USERNAME`) and move the file into `sessions/`.

The password is used for the single login call only. It is never stored, written to
disk or logged, and the form clears it on submit. You enter your own two-factor
code; the app does not work around checkpoint challenges or CAPTCHAs. If Instagram
asks for that kind of verification, that is reported and the operation stops.

---

## 9. Running the application

```bash
source .venv/bin/activate
streamlit run app.py
```

Then open http://localhost:8501.

### Network exposure

**The dashboard login is a convenience gate, not a hardened security boundary.** It
stops casual access on a shared machine. It does not make the application safe to
expose to a network.

Streamlit binds to localhost by default. Keep it that way unless you put it behind
HTTPS and a reverse proxy that performs its own authentication.

---

## 10. Usage

1. **Paste a URL** on the Download page.
2. Press **Analyze**. The app shows content type, author, caption, media count and
   post date. Nothing is downloaded yet.
3. **Choose what to download.** Options depend on the detected content type.
4. **Choose a destination:** Local only, Google Drive, or Local + Google Drive.
5. **Pick a Google Drive account** if uploading.
6. **Review the destination preview**, which shows the exact local and remote paths
   plus the file list.
7. Press **Download** or **Download + Upload** and watch progress.
8. The **Result** section lists every file with its size and hash, the upload
   outcome, and the destination path.

### Duplicates

Before downloading, the shortcode is checked against the manifest. If the post has
been downloaded before you are told so and offered **Skip** or **Download again**.
Skip does nothing; Download again creates a new job and preserves the old record.

### History and retrying

The **History** page shows an activity summary and every job, including ones that
failed before resolving. Filter by creator, date, content type, upload status, job
status, Drive account or who started it, and search shortcodes and captions.

Selecting a job opens a detail view with the source, creator, full caption, a
per-file table (size, full copyable SHA-256, local path, remote path, upload status),
the rendered `document.txt`, the pretty-printed `metadata.json`, and any error.

Job statuses:

| Status | Meaning | Recovery |
|--------|---------|----------|
| `Success` | Everything requested succeeded | none needed |
| `Partial - upload failed` | Media downloaded and kept; upload did not finish | **Retry Google Drive upload** |
| `Failed` | No usable output | re-run the job |
| `Skipped - duplicate` | You chose Skip | none |

`Partial` is deliberately distinct from `Failed`, because the fix differs: a partial
job needs a retried upload, not another download. **Retry Google Drive upload** uses
the existing local files and never contacts Instagram.

---

## 11. Folder structure

### Local staging

```
downloads/
    creator_username/
        2026/
            09/
                ABC123XYZ/
                    video.mp4
                    cover.jpg
                    metadata.json
                    document.txt
```

Carousel posts preserve ordering:

```
                ABC123XYZ/
                    001.jpg
                    002.mp4
                    003.jpg
                    metadata.json
                    document.txt
```

The Instagram shortcode is the primary identifier. Timestamps only group content into
year and month folders; they are never the sole identifier.

### Google Drive

```
Instagram-Knowledge-Base/
    creator_username/
        2026/
            09/
                ABC123XYZ/
                    video.mp4
                    cover.jpg
                    metadata.json
                    document.txt
```

Each level (username, year, month, shortcode) can be toggled independently on the
Download page and in Settings.

Because Google Drive addresses folders by ID rather than path, the uploader resolves
or creates each level in turn and nests by parent ID. Resolved IDs are cached, so a
batch of uploads into one folder costs a single lookup chain.

---

## 12. Output artifacts

### `metadata.json`

```json
{
  "schema_version": "1.0",
  "source": {
    "platform": "instagram",
    "url": "https://www.instagram.com/reel/ABC123XYZ/",
    "shortcode": "ABC123XYZ"
  },
  "creator": { "username": "creator_username" },
  "content": {
    "type": "reel",
    "caption": "The original caption...",
    "created_at": "2026-09-21T10:00:00+00:00"
  },
  "media": [
    { "filename": "video.mp4", "type": "video", "sha256": "...", "file_size": 18800000 },
    { "filename": "cover.jpg", "type": "image", "sha256": "...", "file_size": 120000 }
  ],
  "ingestion": {
    "downloaded_at": "2026-09-21T10:05:00+00:00",
    "application_version": "1.0.0",
    "job_id": "…",
    "downloaded_by": "naman"
  },
  "processing": {
    "ocr_completed": false,
    "transcription_completed": false,
    "frames_extracted": false,
    "embeddings_generated": false
  }
}
```

The `processing` block is a set of placeholders for the future pipeline. Nothing sets
them to `true` in V1; they exist so later stages can be recorded without a schema
migration.

### `document.txt`

A flat, predictable text rendering with sections in a fixed order, intended as the
text surface a future chunker will read:

```
SOURCE: Instagram

CONTENT TYPE: Reel

AUTHOR: creator_username

ORIGINAL URL:
https://www.instagram.com/reel/ABC123XYZ/

CREATED AT:
2026-09-21T10:00:00+00:00

CAPTION:

The original caption...

MEDIA:

video.mp4
cover.jpg
```

### Database

SQLite at `data/manifest.db`, four tables:

```
jobs 1 --- 0..1 posts 1 --- n media_files 1 --- n uploads
```

A job may exist without a post, because the job row is written before the first
network call. No tokens, passwords or password hashes are stored here.

---

## 13. Configuration reference

### `.env`

| Variable | Default | Purpose |
|----------|---------|---------|
| `APP_ENV` | `development` | Environment label |
| `DOWNLOAD_DIR` | `downloads` | Local staging root |
| `TEMP_DIR` | `temp` | Temporary files |
| `SESSION_DIR` | `sessions` | Instaloader session files |
| `TOKEN_DIR` | `tokens` | Google OAuth token cache |
| `DATA_DIR` | `data` | Database location |
| `LOG_LEVEL` | `INFO` | Logging level |
| `DEFAULT_DRIVE_FOLDER` | `Instagram-Knowledge-Base` | Drive root folder |
| `UPLOAD_CHUNK_SIZE` | `5242880` | Resumable chunk size in bytes |
| `MAX_UPLOAD_RETRIES` | `3` | Upload attempts per file |
| `MAX_INSTAGRAM_RETRIES` | `3` | Instagram attempts per request |
| `SESSION_TIMEOUT_MINUTES` | `60` | Dashboard idle timeout |
| `MAX_FAILED_LOGIN_ATTEMPTS` | `5` | Failures before lockout |
| `LOCKOUT_MINUTES` | `15` | Lockout duration |
| `CLEANUP_TEMP_FILES` | `false` | Remove staging after verified upload |

### Settings page

The Settings page writes to `data/settings.json`, which overlays `.env`. That keeps UI
changes out of source control and lets them survive a restart without editing
environment files. Credentials are never editable from the UI.

Chunk sizes are rounded up to the nearest 256 KB, which Google Drive requires for
resumable uploads.

---

## 14. Testing

```bash
source .venv/bin/activate
python -m pytest                 # full unit suite
python -m pytest -v              # verbose
python -m pytest tests/test_ingestion.py
```

The unit suite is fully deterministic and makes **no network calls**. Instaloader is
replaced with fake `Post` objects and the Drive API with a fake service, so no
Instagram or Google credentials are needed.

Coverage includes URL validation, shortcode extraction, content-type detection,
carousel detection and ordering, metadata and document generation, hashing against
known vectors, local folder generation, path-traversal defence, duplicate detection,
Drive folder resolution, chunk arithmetic, simple and resumable uploads, retry and
backoff behaviour, manifest reads and writes, job lifecycle transitions, history
filtering, password hashing and verification, lockout, session expiry, role
enforcement, cleanup policy, and a Streamlit smoke test that renders every page.

### Integration tests

Optional and opt-in only:

```bash
RUN_INTEGRATION_TESTS=true \
INTEGRATION_INSTAGRAM_URL="https://www.instagram.com/p/SOMETHING/" \
INTEGRATION_DRIVE_ACCOUNT=personal \
python -m pytest tests/integration -v
```

Without `RUN_INTEGRATION_TESTS=true` they are skipped. Anything not configured is
skipped rather than failed. The Drive round-trip test deletes the file it uploads.

---

## 15. Security

### Never committed

`.gitignore` covers `.env`, `.streamlit/secrets.toml`, `sessions/*`, `tokens/*`,
`downloads/*`, `data/manifest.db`, `__pycache__/` and `client_secret*.json`.

`secrets.toml` holds both OAuth client secrets and dashboard password hashes, so
keeping it out of source control matters twice over.

### Credentials

- Instagram passwords are never stored. Access uses reusable session files.
- Google passwords are never seen; OAuth tokens are cached in `tokens/` with
  `0600` permissions, never in SQLite.
- Dashboard passwords exist only as PBKDF2-HMAC-SHA256 hashes with per-user random
  salts, compared in constant time.
- Unknown usernames still run a dummy hash verification, so response timing does not
  reveal whether an account exists. Wrong password and unknown user return the same
  message.

### Logging

Logs record events, not secrets. A redaction filter additionally scrubs
credential-shaped values (passwords, tokens, client secrets, `Bearer` headers,
password hashes, Google refresh tokens) as a safety net. Login attempts log the
username and outcome only.

---

## 16. Troubleshooting

**"No dashboard accounts are configured yet."**
Run `python scripts/hash_password.py`, paste the output into
`.streamlit/secrets.toml` under `[auth.users.<name>]`, and restart.

**"Incorrect username or password."**
The same message covers an unknown user and a wrong password, deliberately. Check the
username, and confirm the `password_hash` line was pasted whole.

**"Too many failed attempts."**
Lockout is in memory only. Wait it out, or restart the application.

**"Instagram could not provide this content."**
The post may be removed, private, or Instagram may be limiting access. Try an
authenticated session if you are authorised to view the content. The application will
not attempt to work around the restriction.

**"Instagram is temporarily limiting requests."**
Wait before retrying. Retries are capped at three attempts with exponential backoff.

**"Instagram refused to serve this media. The link may have expired."**
Signed CDN URLs expire. Press **Analyze** again to refresh them.

**"is not connected yet" / "Please reconnect the account."**
Open the Google Drive page and press **Connect** or **Re-authenticate**.

**Google sign-in window never completes.**
The consent flow times out after three minutes. Confirm your account is listed under
**Test users** on the OAuth consent screen, then try again.

**The app cannot see a folder I created in Drive myself.**
Expected. The `drive.file` scope limits access to files this application created. Let
the app create its own folder tree.

**Upload failed but I still have my files.**
That is the intended behaviour. The job is marked `Partial`, local files are kept, and
**Retry Google Drive upload** on the History page will re-send them without touching
Instagram.

**Settings changes do not appear to apply.**
Settings are written to `data/settings.json`. Saving clears the cache and reruns; if
something looks stale, restart the app.

---

## 17. Known limitations

- **Reel "image" means the cover/thumbnail.** Frame extraction is not implemented.
- **No batch or profile ingestion.** One URL per job.
- **`drive.file` scope cannot see pre-existing Drive folders**, as described above.
- **Lockout state is in memory**, so restarting clears it.
- **Single-process assumption.** The manifest is SQLite and the app is intended to run
  locally for one operator at a time.
- **Google's consent flow needs a browser on the same machine**, because it uses a
  loopback redirect.
- **No automatic "open local folder" button.** A server-side process cannot reliably
  open your file manager, so the path is shown for copying instead.
- **Uploads are sequential**, which is simpler and gentler on rate limits, but slower
  than parallel transfers for large carousels.
- **Resumable sessions are not persisted across application restarts.** An interrupted
  upload is retried within the run; after a restart a new session is created.

---

## 18. Future LLM pipeline

V1 is the ingestion layer only. The metadata format is designed to support what
follows, but none of it is built yet:

```
Google Drive / local archive
        |
        v
  Content Scanner
        |
   +----+----+
   v         v
Images    Videos
   |         |
   v         v
  OCR   Audio extraction
   |         |
   |         v
   |   Transcription
   +----+----+
        v
Text + frames + metadata + caption
        |
        v
    Documents
        |
        v
    Chunking
        |
        v
   Embeddings
        |
        v
  Vector database
        |
        v
       RAG
        |
        v
       LLM
```

Deliberately deferred: frame extraction, OCR, transcription, embeddings, vector
database, RAG chat, batch and CSV import, profile ingestion, scheduling, AI
summaries, hashtag and entity extraction, semantic deduplication, and cloud
deployment.

The extension point is `models/metadata.py`: add fields, bump `schema_version`, and
record progress in the existing `processing` block.

---

## Project layout

```
.
├── app.py                     entry point: bootstrap, login gate, navigation
├── requirements.txt
├── pytest.ini
├── prd.md                     product requirements
├── README.md
├── .env.example
├── .streamlit/
│   └── secrets.toml.example
├── auth/          app_auth, google_auth, instagram_auth
├── instagram/     client, resolver, downloader, metadata
├── gdrive/        client, folders, uploader
├── models/        media, instagram, job, user, upload, metadata
├── services/      ingestion, jobs, manifest, hashing, cleanup
├── utils/         config, paths, validation, logging_config, errors, ui
├── pages/         1_Download, 2_History, 3_GoogleDrive, 4_Settings
├── scripts/       hash_password.py
├── tests/         unit suite + tests/integration (opt-in)
├── downloads/     local staging (gitignored)
├── sessions/      Instaloader sessions (gitignored)
├── tokens/        OAuth token cache (gitignored)
└── data/          manifest.db, settings.json (gitignored)
```
