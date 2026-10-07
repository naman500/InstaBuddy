# Product Requirements Document

**Product:** Instagram Media Downloader & Google Drive LLM Knowledge Base
**Codename / repo folder:** `instagram-llm-kb`
**Document version:** 1.2
**Status:** Approved for V1 implementation
**Last updated:** 2026-09-21

> **Changelog — v1.2:** Storage target switched from OneDrive/Microsoft Graph to Google Drive API v3 with the minimal `drive.file` scope. Accounts are configured with `client_id` + `client_secret`; the `gdrive/` module replaces `onedrive/`. Note that Drive addresses folders by ID, not path, so each level is resolved or created and nested by parent ID.
>
> **Changelog — v1.1:** Added application login gate (§16). Expanded History into a full Jobs & History model with a `jobs` table and drill-down metadata view (§9.13, §13). Build plan grew from 20 to 21 phases.

---

## Table of Contents

1. [The One-Paragraph Summary](#1-the-one-paragraph-summary)
2. [Why This Exists](#2-why-this-exists)
3. [Goals and Non-Goals](#3-goals-and-non-goals)
4. [Who Uses This](#4-who-uses-this)
5. [Scope: V1 vs Later](#5-scope-v1-vs-later)
6. [The User Journey](#6-the-user-journey)
7. [System Architecture](#7-system-architecture)
8. [Project Structure](#8-project-structure)
9. [Functional Requirements](#9-functional-requirements)
10. [Data Model](#10-data-model)
11. [Output Artifacts](#11-output-artifacts)
12. [Storage Layout](#12-storage-layout)
13. [Database Schema](#13-database-schema)
14. [Non-Functional Requirements](#14-non-functional-requirements)
15. [Security and Compliance Rules](#15-security-and-compliance-rules)
16. [Application Access Control](#16-application-access-control)
17. [Configuration](#17-configuration)
18. [Error Handling Catalogue](#18-error-handling-catalogue)
19. [Testing Strategy](#19-testing-strategy)
20. [Build Plan: 21 Phases](#20-build-plan-21-phases)
21. [Definition of Done](#21-definition-of-done)
22. [Future Roadmap](#22-future-roadmap)
23. [Open Questions](#23-open-questions)
24. [Glossary](#24-glossary)

---

## 1. The One-Paragraph Summary

A local Python application with a Streamlit dashboard that takes an Instagram post or Reel URL, works out what kind of content it is, lets the user pick exactly which media to keep, downloads it through Instaloader, generates rich metadata alongside it, files it into a predictable folder structure, and uploads it to a chosen Google Drive account via Google Drive API. Every item is recorded in a local SQLite manifest so nothing is lost, duplicated, or unrecoverable.

The important framing: **this is an ingestion layer for a future AI knowledge base, not a media downloader.** The downloader is simply the first stage.

---

## 2. Why This Exists

### The problem

Valuable knowledge lives inside Instagram content — tutorials in Reels, explainers in carousels, insights in captions. That content is:

- Trapped in a feed with no search beyond hashtags
- Impossible to query semantically
- Not archived anywhere the user controls
- Detached from its own context (author, date, caption) once saved manually

### The insight

If you capture the media **plus** a structured, machine-readable description of it at download time, you can later run OCR, transcription, and embedding over the archive and turn it into a searchable, queryable knowledge base — without ever re-downloading anything.

### The design consequence

Metadata is not a nice-to-have. It is the product. The media files are almost a side effect.

That is why every downloaded object must carry all nine of these:

| # | Attribute | Why it matters |
|---|-----------|----------------|
| 1 | Original source | Provenance and attribution |
| 2 | Stable identifier | Idempotency, deduplication |
| 3 | Metadata | Context for retrieval |
| 4 | Media file | The raw payload |
| 5 | SHA-256 hash | Integrity, dedup, change detection |
| 6 | Download timestamp | Freshness, audit trail |
| 7 | Storage location | Retrievability |
| 8 | Upload status | Reliability, retry |
| 9 | Future processing status | Pipeline extensibility |

---

## 3. Goals and Non-Goals

### Goals

| ID | Goal |
|----|------|
| G1 | A non-developer can archive an Instagram post in under 30 seconds |
| G2 | Nothing is ever downloaded twice by accident |
| G3 | Nothing downloaded is ever lost, even when uploads fail |
| G4 | Large videos upload reliably over unstable connections |
| G5 | Multiple Google Drive accounts are switchable from the UI |
| G6 | OCR, transcription, and embeddings can be added later with zero changes to the downloader |
| G7 | No credentials, tokens, or secrets ever reach source control or logs |

### Non-Goals

| ID | Non-Goal | Reason |
|----|----------|--------|
| N1 | Scraping content the user is not authorized to access | Out of bounds, full stop |
| N2 | Bypassing authentication, CAPTCHAs, rate limits, or anti-bot systems | Explicitly prohibited |
| N3 | Bulk profile or hashtag harvesting | Not V1; also an abuse vector |
| N4 | Implementing the OCR / transcription / vector DB pipeline | V2+, design for it only |
| N5 | Cloud hosting or multi-user deployment | Local single-user tool for V1 |
| N6 | A visually elaborate UI | Function over polish |

---

## 4. Who Uses This

### Primary persona — "The Curator"

Builds a personal or team knowledge archive. Comfortable pasting a URL and clicking buttons. **Not** comfortable with terminals, OAuth jargon, API endpoints, or stack traces.

**What this means for the UI:** no Python terminology, no visible client IDs, no raw exceptions, no file-system paths presented as configuration puzzles. Errors are written in plain sentences.

### Secondary persona — "The Builder" (you)

Will extend this into the RAG layer later. Needs clean module boundaries, type hints, docstrings, structured logs, and a metadata schema that will not need to be rewritten.

**What this means for the code:** strict separation of concerns, Pydantic models everywhere, `schema_version` from day one.

---

## 5. Scope: V1 vs Later

### In scope for V1

- Single-URL ingestion for `/p/`, `/reel/`, `/reels/`, `/tv/`
- Content type detection: IMAGE, VIDEO, CAROUSEL, REEL
- Selective media download per content type
- Optional, session-file-based Instagram authentication
- `metadata.json` + `document.txt` generation
- SHA-256 hashing of every file
- SQLite manifest with duplicate detection
- Multi-account Google Drive upload via Google Drive API
- Resumable chunked uploads with retry
- Four Streamlit pages: Download, History, Google Drive, Settings
- Unit test suite with all external services mocked

### Explicitly deferred

Frame extraction · OCR · transcription · embeddings · vector DB · RAG chat · batch/CSV import · profile ingestion · scheduling · AI summaries · hashtag & entity extraction · semantic dedup · cloud deployment

**Rule:** do not add a dependency until the feature that needs it is being built. No ffmpeg, Whisper, or ChromaDB in V1.

---

## 6. The User Journey

### The happy path, end to end

```
Open app
   ↓
Sign in to the dashboard              ← app login gate, see §16
   ↓
Paste  https://www.instagram.com/reel/XXXXXXXX/
   ↓
Click [ Analyze ]                    ← no download happens yet
   ↓
App shows:   REEL
             Author:  example_user
             Caption: "Three ways to..."
             Media:   1 video, 1 cover image
   ↓
Select media:        ( ) Video   ( ) Cover   (•) Video + Cover
   ↓
Select destination:  ( ) Local   ( ) Google Drive   (•) Local + Google Drive
   ↓
Select account:      [ Personal Google Drive ▼ ]
   ↓
Preview destination: Instagram-Knowledge-Base/example_user/2026/09/XXXXXXXX
   ↓
Click [ Download & Upload ]
   ↓
Downloading...  ████████████████ 100%
Generating metadata...
Uploading...    ████████████████ 100%
   ↓
SUCCESS
Files: video.mp4, cover.jpg, metadata.json, document.txt
[ Open Local Folder ]   [ Open in Google Drive ]
```

### The critical design decision: Analyze before Download

**Analyze** and **Download** are two separate phases. Analyze retrieves only what is needed to describe the post: content type, username, caption, media count, available media types, timestamp, shortcode. Nothing is written to disk.

The user then decides what to download. This prevents wasted bandwidth, prevents unwanted files, and makes the selective-download feature meaningful.

### Page map

| Page | Purpose |
|------|---------|
| **Login gate** | Shown before any page when the user is not signed in |
| **Download** (primary) | URL → analyze → select → destination → download/upload |
| **History** | Activity summary, job list, per-job metadata drill-down, retry failed uploads |
| **Google Drive** | Connect, re-authenticate, and test configured accounts |
| **Settings** | Directories, defaults, chunk size, retries, cleanup, log level |

The login gate is not a page in the sidebar. It renders in place of whatever page was requested until the session is authenticated.

---

## 7. System Architecture

### Ingestion flow (V1)

```
                    Instagram
                        │
                        ▼
                   Instaloader
                        │
                        ▼
          Instagram Content Resolver          ← determines type, media items
                        │
                        ▼
              Media Downloader
                        │
              ┌─────────┴─────────┐
              ▼                   ▼
           Images              Videos
              └─────────┬─────────┘
                        ▼
              Metadata Generator               ← metadata.json + document.txt
                        │
                        ▼
                Local Staging                  ← downloads/user/YYYY/MM/SHORTCODE/
                        │
                        ▼
              Google Drive Manager                 ← folders, chunked resumable upload
                        │
                        ▼
                    Google Drive
                        │
                        ▼
          ( Future LLM Pipeline )               ← not built in V1
```

### Layered view

```
            ┌─────────────────────┐
            │  Streamlit Dashboard │        ← UI only, zero business logic
            └──────────┬──────────┘
                       │
        ┌──────────────┴──────────────┐
        ▼                             ▼
  Instagram Service            Google Drive Service      ← never import each other
        │                             │
        ▼                             ▼
   Instaloader                 Google Drive API
        └──────────────┬──────────────┘
                       ▼
               Ingestion Service               ← the orchestrator
                       │
            ┌──────────┴──────────┐
            ▼                     ▼
         SQLite               Local Files
            └──────────┬──────────┘
                       ▼
                   Google Drive
```

### The three architectural laws

1. **`app.py` does initialization and navigation only.** No Instagram logic, no Google Drive logic, no database logic, no metadata logic, no auth logic.
2. **Business logic never imports Streamlit.** The ingestion service must be callable from a script, a test, or a future CLI.
3. **Instagram and Google Drive layers are mutually ignorant.** They meet only inside the ingestion service.

---

## 8. Project Structure

```
instagram-llm-kb/
├── app.py                        # init + navigation ONLY
├── requirements.txt
├── README.md
├── .gitignore
├── .env.example
│
├── .streamlit/
│   └── secrets.toml.example      # Google Drive account config template
│
├── auth/
│   ├── __init__.py
│   ├── app_auth.py               # dashboard login, hashing, roles, session
│   ├── instagram_auth.py         # optional session load/save
│   └── google_auth.py            # Google OAuth loopback flow + token cache
│
├── instagram/
│   ├── __init__.py
│   ├── client.py                 # Instaloader wrapper
│   ├── resolver.py               # URL → InstagramPost
│   ├── downloader.py             # fetch selected media
│   └── metadata.py               # metadata.json + document.txt
│
├── gdrive/
│   ├── __init__.py
│   ├── client.py                 # Graph API wrapper
│   ├── uploader.py               # small + resumable chunked upload
│   └── folders.py                # remote path building & creation
│
├── models/
│   ├── __init__.py
│   ├── instagram.py              # InstagramPost, InstagramMediaItem
│   ├── media.py                  # MediaType, selection options
│   ├── metadata.py               # metadata schema models
│   ├── job.py                    # Job, JobStatus, JobSummary
│   ├── user.py                   # AppUser, Role
│   └── upload.py                 # UploadResult, destination modes
│
├── services/
│   ├── __init__.py
│   ├── ingestion.py              # process_instagram_url() orchestrator
│   ├── jobs.py                   # job lifecycle + history queries
│   ├── manifest.py               # SQLite read/write
│   ├── hashing.py                # calculate_sha256()
│   └── cleanup.py                # post-upload temp removal
│
├── utils/
│   ├── __init__.py
│   ├── logging_config.py
│   ├── paths.py                  # local path building
│   └── validation.py             # URL validation, shortcode extraction
│
├── pages/
│   ├── 1_Download.py
│   ├── 2_History.py
│   ├── 3_GoogleDrive.py
│   └── 4_Settings.py
│
├── downloads/       └── .gitkeep  # staged media (gitignored)
├── sessions/        └── .gitkeep  # Instaloader sessions (gitignored)
│
├── scripts/
│   └── hash_password.py          # generates a password hash for secrets.toml
│
├── tests/
│   ├── __init__.py
│   ├── test_instagram_resolver.py
│   ├── test_metadata.py
│   ├── test_paths.py
│   ├── test_manifest.py
│   ├── test_jobs.py
│   └── test_app_auth.py
│
└── data/
    └── manifests/   └── .gitkeep
```

---

## 9. Functional Requirements

Dashboard sign-in requirements live separately in [§16 Application Access Control](#16-application-access-control), since they gate all of the below.

### 9.1 URL Handling

| ID | Requirement |
|----|-------------|
| FR-1.1 | Accept `/p/`, `/reel/`, `/reels/`, and `/tv/` Instagram URLs |
| FR-1.2 | Validate the URL before any network request |
| FR-1.3 | Extract the shortcode from a valid URL |
| FR-1.4 | Reject non-Instagram and malformed URLs with: *"Please enter a valid Instagram post, reel or video URL."* |

Implemented in `utils/validation.py` as `is_valid_instagram_url(url)` and `extract_instagram_shortcode(url)`.

### 9.2 Instagram Authentication (Optional)

| ID | Requirement |
|----|-------------|
| FR-2.1 | Default mode is **"Public content — no Instagram login"** |
| FR-2.2 | In public mode, never ask for a username or password, and never present a login flow |
| FR-2.3 | Authenticated mode accepts a username and a session file selection |
| FR-2.4 | Prefer `load_session_from_file()`; never require a password per download |
| FR-2.5 | Never store an Instagram password in secrets, config, or the database |
| FR-2.6 | Never write passwords to logs |
| FR-2.7 | Never auto-handle or bypass challenges or CAPTCHAs |

**Intended lifecycle:** authenticate once → save session → reuse indefinitely.

### 9.3 Content Detection

| ID | Requirement |
|----|-------------|
| FR-3.1 | Support IMAGE, VIDEO, CAROUSEL, REEL |
| FR-3.2 | Determine type by inspecting the Instaloader `Post` object, **not** the URL string |
| FR-3.3 | A URL containing "reel" does not by itself make the content a Reel |
| FR-3.4 | Resolve: URL type, shortcode, media type, is_video, is_carousel, media count, media URLs, caption, author, timestamp |
| FR-3.5 | Return results as Pydantic models |

### 9.4 Media Selection

| Content type | Options offered | Default |
|--------------|-----------------|---------|
| **Reel** | Video · Cover image · Video + cover | Video |
| **Image post** | Image + metadata (no video option shown) | Image |
| **Video post** | Video · Cover image · Both | Video |
| **Carousel** | Images only · Videos only · Images + videos | Images + videos |

| ID | Requirement |
|----|-------------|
| FR-4.1 | The user must be able to change the selection before downloading |
| FR-4.2 | For Reels, "image" means the **cover/thumbnail only** — no frame extraction in V1 |
| FR-4.3 | Carousels must preserve original ordering: `001.jpg`, `002.mp4`, `003.jpg`, ... |
| FR-4.4 | Never rename carousel items arbitrarily |

### 9.5 Metadata Generation

| ID | Requirement |
|----|-------------|
| FR-5.1 | Every ingested post produces a `metadata.json` |
| FR-5.2 | Every ingested post produces a normalized `document.txt` for future RAG ingestion |
| FR-5.3 | Metadata must include: source, source_type, original_url, shortcode, author_username, caption, created_at, media_type, downloaded_at, files, file_hashes |
| FR-5.4 | Metadata carries `schema_version` (initially `"1.0"`) and `application_version` |

### 9.6 Hashing and Deduplication

| ID | Requirement |
|----|-------------|
| FR-6.1 | Compute SHA-256 for every downloaded file via `calculate_sha256(file_path)`, returning 64 hex chars |
| FR-6.2 | Before downloading, check the manifest for the shortcode |
| FR-6.3 | If already present, show *"This Instagram post has already been downloaded."* with **[Skip]** and **[Download again]** |
| FR-6.4 | On Skip, perform no download |
| FR-6.5 | Content identity = Instagram shortcode + SHA-256 |

Hashes serve four purposes: duplicate detection, upload integrity verification, avoiding repeat downloads, and detecting content changes.

### 9.7 Google Drive Integration

| ID | Requirement |
|----|-------------|
| FR-7.1 | Communicate exclusively through Google Drive API — never automate the Google Drive website |
| FR-7.2 | Support multiple configured accounts, selectable from the UI by friendly display name |
| FR-7.3 | Never display client IDs, client secrets, or tokens in the UI |
| FR-7.4 | Authenticate with Google OAuth via google-auth-oauthlib; never request or store Google passwords |
| FR-7.5 | Request the minimum Graph permissions needed to create and upload files; document them in the README |
| FR-7.6 | Cache tokens in a secure token cache — **not** in SQLite |

**Auth flow:** select account → app starts the Google OAuth loopback flow → Google sign-in page → user consents → a temporary local server receives the redirect → token cached securely → app calls the Google Drive API.

**Scope:** `https://www.googleapis.com/auth/drive.file` only. It grants access exclusively to files this application creates, which is the minimum needed to upload and organise our own content. A documented consequence: the app cannot see folders the user created by hand, so it maintains its own folder tree.

### 9.8 Upload Behaviour

| ID | Requirement |
|----|-------------|
| FR-8.1 | Small files may use a simple upload request |
| FR-8.2 | Large files, especially video, must use Graph upload sessions |
| FR-8.3 | Upload byte ranges, tracking the next expected range |
| FR-8.4 | Never load an entire large file into memory — stream chunked reads |
| FR-8.5 | Chunk size is configurable; default **5 MB (5242880 bytes)** |
| FR-8.6 | Track file size, chunk size, chunk count, and progress per upload |
| FR-8.7 | Display a progress bar in Streamlit |
| FR-8.8 | Retry transient failures up to 3 times with exponential backoff (1s, 2s, 4s) |
| FR-8.9 | Do not restart a whole upload if the session can be continued |
| FR-8.10 | Verify the final response before marking success |
| FR-8.11 | On interruption, preserve session info and resume where practical; otherwise open a new session. Never corrupt the local file |

### 9.9 Destination Modes

| Mode | Behaviour |
|------|-----------|
| `LOCAL_ONLY` | Download and keep locally |
| `DRIVE_ONLY` | Stage locally, upload, then optionally clean up |
| `LOCAL_AND_DRIVE` | **Default.** Keep locally and upload |

`DRIVE_ONLY` still stages locally because media must be retrieved before it can be uploaded.

### 9.10 Remote Folder Structure

Default root: `Instagram-Knowledge-Base`

```
Instagram-Knowledge-Base/
    techcreator/
        2026/
            09/
                Cx123ABC/
                    video.mp4
                    cover.jpg
                    metadata.json
                    document.txt
```

Each organization level is individually toggleable in the UI — Username, Year, Month, Shortcode — all enabled by default.

### 9.11 Destination Preview

Before uploading, show the account display name, the full folder path, and the list of files to be uploaded. Then offer **[Download Only]** and **[Download + Upload]**.

### 9.12 Cleanup and Reliability

```
Download → Validate file → Calculate hash → Upload
    → Verify upload → Update manifest → Optional cleanup
```

| ID | Requirement |
|----|-------------|
| FR-12.1 | Never delete a local file before its upload is verified successful |
| FR-12.2 | If upload fails, keep the local file and record the failure |
| FR-12.3 | The user must never lose downloaded media because of a Google Drive error |
| FR-12.4 | Failed uploads are retryable from History using the existing local file |
| FR-12.5 | Do not call Instagram again on retry unless the local file is missing |

### 9.13 Jobs and History

Every ingestion attempt is recorded as a **job**, whether it succeeds, fails, or is skipped as a duplicate. The History page is the window onto those jobs.

#### 9.13.1 What a job is

A job is one run of `process_instagram_url()`. It is created the moment the user clicks Download, before any network call, so a crash mid-run still leaves a visible record rather than silence.

A job stores what was *requested* (URL, selected media, destination mode, target account) separately from what was *produced* (files, hashes, remote paths). That separation is what makes a job re-runnable and auditable.

#### 9.13.2 Job status lifecycle

```
QUEUED
  → RESOLVING          fetching post details from Instagram
  → DOWNLOADING        pulling selected media
  → HASHING            computing SHA-256 per file
  → GENERATING_METADATA  writing metadata.json + document.txt
  → UPLOADING          transferring to Google Drive
  → COMPLETED
```

Terminal states:

| Status | Meaning |
|--------|---------|
| `COMPLETED` | Everything requested succeeded |
| `PARTIAL` | Media downloaded and kept locally, but upload failed or was incomplete |
| `FAILED` | Could not produce usable output |
| `SKIPPED` | Duplicate detected and the user chose Skip |
| `CANCELLED` | User abandoned the run |

`PARTIAL` exists because it is the common real-world outcome: the download worked, Google Drive did not. It must be visually and textually distinct from `FAILED`, because the recovery action is different — a `PARTIAL` job needs a retry upload, not a re-download.

#### 9.13.3 Activity summary

The top of the History page shows at-a-glance counts. No charts required.

```
Total jobs   Completed   Partial   Failed   Local storage used
    142         128         9         5          3.4 GB
```

#### 9.13.4 Job list

| Column | Example |
|--------|---------|
| Date | `2026-09-21 10:00` |
| Account (app user) | `naman` |
| Username (creator) | `creator1` |
| Type | `Reel` |
| Shortcode | `ABC123` |
| Files | `2` |
| Size | `18.4 MB` |
| Google Drive | `Personal Google Drive` |
| Upload | `Uploaded` |
| Status | `Success` |

Example row: `2026-09-21 | naman | creator1 | Reel | ABC123 | 2 | 18.4 MB | Personal | Uploaded | Success`

| ID | Requirement |
|----|-------------|
| FR-13.1 | Default sort is newest first |
| FR-13.2 | Filterable by creator username, date range, content type, upload status, job status, and Google Drive account |
| FR-13.3 | Free-text search across shortcode and caption |
| FR-13.4 | Status shown as text, never colour alone |

#### 9.13.5 Job detail view

Selecting a row expands or opens a detail view. This is where the full metadata becomes visible, so the user does not have to open `metadata.json` in a text editor to understand what was captured.

**Source**
Original URL (clickable) · shortcode · platform · detected content type

**Creator**
Username · caption in full (expandable when long) · original post timestamp

**Files** — one row per file:

| Filename | Type | Size | SHA-256 | Local path | Remote path | Upload status |
|----------|------|------|---------|-----------|-------------|---------------|
| `video.mp4` | video | 17.9 MB | `a3f1…9c2e` (truncated, copyable in full) | `downloads/creator1/2026/09/ABC123/video.mp4` | `Instagram-Knowledge-Base/creator1/2026/09/ABC123/video.mp4` | Uploaded |

**Run detail**
Job ID · requested media selection · destination mode · target Google Drive account · started at · finished at · duration · attempt count · current status

**Artifacts**
Rendered preview of `document.txt` and a collapsible, pretty-printed view of `metadata.json`.

**Errors**
When present, the friendly message plus the recorded technical detail in a collapsed block. Never a raw traceback in the main flow.

| ID | Requirement |
|----|-------------|
| FR-13.5 | Detail view shows every field stored in the manifest for that job |
| FR-13.6 | Full SHA-256 values must be viewable and copyable |
| FR-13.7 | Local and remote paths both shown per file |
| FR-13.8 | Flag files recorded in the manifest that are no longer present on disk |
| FR-13.9 | Never display tokens, client IDs, or client secrets |

#### 9.13.6 Actions available per job

| Action | Availability | Behaviour |
|--------|-------------|-----------|
| **Retry Google Drive Upload** | `PARTIAL`, or any failed upload | Re-uploads from the existing local file. Does not touch Instagram unless the local file is missing |
| **Re-run job** | Any terminal state | Runs the same URL with the same selections as a new job. The original record is preserved |
| **Open local folder** | Files present on disk | Opens the staging directory |
| **Open in Google Drive** | Upload succeeded | Opens the remote folder where practical |
| **Copy metadata** | Always | Copies `metadata.json` content |
| **Delete record** | Always | Removes the manifest entry. Must ask whether local files should also be deleted, and must default to keeping them |

| ID | Requirement |
|----|-------------|
| FR-13.10 | Retrying an upload never re-downloads content that already exists locally |
| FR-13.11 | Re-running creates a new job rather than overwriting history |
| FR-13.12 | Deleting a record never deletes local media silently |

### 9.14 Google Drive Page

Lists configured accounts with connection status (e.g. *Personal Google Drive — Connected*, *Work Google Drive — Not connected*). Buttons: **[Connect]**, **[Re-authenticate]**, **[Test Connection]**. For connected accounts, shows account name, drive name, and status. Never shows tokens.

### 9.15 Settings Page

Download directory · Temporary directory · Default media mode · Default destination · Google Drive root folder · Upload chunk size · Maximum retries · Cleanup temporary files · Logging level

### 9.16 Progress Feedback

Use `st.progress()` plus status text through the pipeline:

```
Resolving Instagram post...
Downloading media 1/3...
Downloading media 2/3...
Generating metadata...
Uploading to Google Drive...
Completed.
```

### 9.17 The Orchestrator

`services/ingestion.py` exposes `process_instagram_url()`:

```
validate URL → resolve post → detect content type
  → validate selected options → create staging directory
  → download media → calculate hashes
  → generate metadata.json → generate document.txt
  → save manifest → upload to Google Drive if requested
  → update upload records → cleanup if requested → return result
```

Streamlit pages call this service. They do not reimplement the workflow.

---

## 10. Data Model

All models use Pydantic.

### InstagramMediaItem

| Field | Type | Notes |
|-------|------|-------|
| `index` | int | Preserves carousel ordering |
| `media_type` | enum | image / video |
| `source_url` | str | Remote media URL |
| `filename` | str | Target local filename |
| `is_video` | bool | |

### InstagramPost

| Field | Type | Notes |
|-------|------|-------|
| `id` | str | |
| `shortcode` | str | **Primary content identifier** |
| `url` | str | Original URL |
| `username` | str | Author |
| `caption` | str \| None | |
| `created_at` | datetime | |
| `content_type` / `media_type` | enum | IMAGE / VIDEO / CAROUSEL / REEL |
| `is_video` | bool | |
| `is_carousel` | bool | |
| `media_items` | list[InstagramMediaItem] | Ordered |

### DownloadResult

`post` · `files` · `metadata_file` · `document_file` · `status`

### UploadResult

`account` · `remote_path` · `file` · `status` · `drive_file_id`

### Job

| Field | Type | Notes |
|-------|------|-------|
| `id` | str | UUID |
| `created_by` | str | App username |
| `requested_url` | str | As pasted |
| `shortcode` | str \| None | Null until resolved |
| `requested_media` | selection | What the user asked for |
| `destination_mode` | enum | Local / Google Drive / both |
| `drive_account` | str \| None | |
| `status` | JobStatus | See §9.13.2 |
| `current_step` | str \| None | For crash diagnosis |
| `attempt_count` | int | |
| `started_at` | datetime | |
| `finished_at` | datetime \| None | |
| `error_message` | str \| None | Friendly |
| `error_detail` | str \| None | Technical, never secret |

### AppUser

`username` · `display_name` · `role` — deliberately **no** password or hash field on the in-memory model, so a user object can never accidentally serialize a credential.

---

## 11. Output Artifacts

Every ingested post produces two descriptive files next to its media.

### `metadata.json`

```json
{
  "schema_version": "1.0",
  "source": {
    "platform": "instagram",
    "url": "...",
    "shortcode": "..."
  },
  "creator": {
    "username": "..."
  },
  "content": {
    "type": "reel",
    "caption": "...",
    "created_at": "..."
  },
  "media": [
    { "filename": "video.mp4", "type": "video", "sha256": "..." },
    { "filename": "cover.jpg", "type": "image", "sha256": "..." }
  ],
  "ingestion": {
    "downloaded_at": "...",
    "application_version": "1.0.0"
  }
}
```

### `document.txt`

A flat, human-readable, embedding-friendly rendering:

```
SOURCE: Instagram
CONTENT TYPE: Reel
AUTHOR: example_user

ORIGINAL URL:
https://www.instagram.com/reel/XXXX/

CREATED AT:
2026-09-21T10:00:00

CAPTION:
Original Instagram caption...

MEDIA:
video.mp4
cover.jpg
```

This file exists purely so the future RAG pipeline has a ready text surface per post.

---

## 12. Storage Layout

### Local staging

```
downloads/
    instagram_username/
        2026/
            09/
                SHORTCODE/
                    video.mp4
                    cover.jpg
                    metadata.json
                    document.txt
```

### Carousel example

```
downloads/
    username/
        2026/
            09/
                SHORTCODE/
                    001.jpg
                    002.mp4
                    003.jpg
                    metadata.json
                    document.txt
```

**Rule:** the shortcode is the primary identifier. Timestamps must never be the only folder identifier.

---

## 13. Database Schema

SQLite at `data/manifest.db`. Four tables.

```
jobs  1 ──── 0..1  posts  1 ──── n  media_files  1 ──── n  uploads
```

A job may exist without a post (it failed before resolving). A post always belongs to a job.

### `jobs`

| Field | Notes |
|-------|-------|
| `id` | Job identifier (UUID) |
| `created_by` | App username that started the run |
| `requested_url` | Exactly what the user pasted |
| `shortcode` | Populated once resolved; null on early failure |
| `requested_media` | Serialized selection, e.g. `video+cover` |
| `destination_mode` | `LOCAL_ONLY` / `DRIVE_ONLY` / `LOCAL_AND_DRIVE` |
| `drive_account` | Target account key, null for local-only |
| `status` | See §9.13.2 |
| `current_step` | Last known pipeline step, for crash diagnosis |
| `attempt_count` | Incremented on retry |
| `started_at` | |
| `finished_at` | Null while running |
| `error_message` | Friendly message |
| `error_detail` | Technical detail, never a secret |
| `application_version` | For future schema migration |

### `posts`

`id` · `job_id` · `shortcode` · `instagram_url` · `username` · `caption` · `content_type` · `created_at` · `downloaded_at` · `status` · `created_hash` · `created_at_local` · `drive_account`

### `media_files`

`id` · `post_id` · `filename` · `relative_path` · `media_type` · `file_size` · `sha256` · `created_at`

### `uploads`

`id` · `media_file_id` · `drive_account` · `remote_path` · `status` · `uploaded_at` · `drive_file_id` · `error_message`

### Indexes

On `posts.shortcode` for duplicate detection, and on `jobs.started_at` plus `jobs.status` for History filtering.

**Note:** OAuth tokens, passwords, and password hashes are never stored in this database.

---

## 14. Non-Functional Requirements

### Technology stack

**V1:** Python 3.11+ · Streamlit · Instaloader · Requests · google-auth-oauthlib · Google Drive API · Pydantic · python-dotenv · pytest · pathlib · logging · hashlib · uuid · json · dataclasses

**`requirements.txt` starts as:** `instaloader`, `streamlit`, `requests`, `google-api-python-client`, `google-auth`, `google-auth-oauthlib`, `pydantic`, `python-dotenv`, `pytest`. Versions get pinned after the first stable implementation. No unrelated packages.

**Deferred:** ffmpeg · OpenAI/Azure OpenAI · Whisper · OCR libraries · ChromaDB/FAISS/Qdrant/pgvector

### Rate limiting and request discipline

| ID | Requirement |
|----|-------------|
| NFR-1 | Conservative request behaviour; no aggressive loops |
| NFR-2 | Maximum 3 Instagram retries, then stop and report |
| NFR-3 | No indefinite retrying |

### Logging

Configured in `utils/logging_config.py`.

**INFO events:** download started · post resolved · media discovered · file downloaded · hash calculated · upload started · upload completed
**ERROR events:** download failed · upload failed

Example:

```
2026-09-21 10:00:00 INFO Resolving Instagram URL
2026-09-21 10:00:01 INFO Resolved shortcode=ABC123
2026-09-21 10:00:01 INFO Content type=reel
2026-09-21 10:00:02 INFO Downloading video
2026-09-21 10:00:05 INFO SHA256 calculated
2026-09-21 10:00:06 INFO Upload started
2026-09-21 10:00:12 INFO Upload completed
2026-09-21 10:00:12 INFO Manifest updated
```

**Never logged:** Instagram password · Google password · dashboard password · password hashes · access token · refresh token · client secret · any sensitive OAuth data

Login attempts are logged as `INFO Login succeeded user=naman` or `WARNING Login failed user=naman` — username and outcome only.

### State management

Multipage Streamlit app. Use `st.session_state` for anything that must survive a rerun. Never use module-level Python globals for user state.

### UI/UX

Clean, professional, restrained. Sidebar: Instagram · Google Drive · Settings. Main page: URL → content preview → download options → destination → progress → result. Functionality over visual effects; do not over-design.

### Accessibility

Clear labels throughout. Status must never be communicated by colour alone — always include text: Success · Warning · Failed · Uploading · Completed.

### Code quality

Type hints · clear docstrings · structured logging · expected exceptions caught · no raw stack traces surfaced to users · business logic independent of Streamlit · no single giant file.

### Idempotency and versioning

Running the same URL repeatedly must not create uncontrolled duplicates. `schema_version` in metadata and `application_version` in every manifest entry enable future schema migration.

---

## 15. Security and Compliance Rules

### Absolute prohibitions

The following must never be implemented:

- Instagram password storage
- Google password storage
- CAPTCHA bypass
- Instagram anti-bot bypass
- Proxy rotation intended to evade restrictions
- Fingerprint spoofing
- Automated login circumvention
- Rate-limit evasion

The application operates strictly within normal authorized access. Only content the user is authorized to download and use should be processed.

### Never committed

```
.env
.streamlit/secrets.toml
sessions/*
downloads/*
data/manifest.db
__pycache__/
*.pyc
```

`secrets.toml` now also contains dashboard password hashes, which makes keeping it out of source control doubly important.

Committed and allowed: `.env.example`, `.streamlit/secrets.toml.example`

### Token storage

Local development may use a protected local token cache. Production must use a proper secret/token storage mechanism. Never SQLite.

---

## 16. Application Access Control

Separate from Instagram and Google Drive authentication, the dashboard itself requires a sign-in. This exists so that an unattended browser tab, or a machine shared with others, does not give anyone the ability to trigger downloads, browse archived content, or reach connected Google Drive accounts.

### 16.1 Three distinct logins — do not conflate them

This is the single most confusable part of the system, so it is worth stating plainly:

| Login | Purpose | Who holds the credential | Stored how |
|-------|---------|-------------------------|------------|
| **App login** (new) | Gate access to the dashboard | The operator of this tool | Salted password hash in local config |
| **Instagram session** | Reach non-public content | Instagram | Instaloader session file, optional |
| **Google OAuth** | Upload to Google Drive | Google | Token in a secure cache |

The app login grants nothing on Instagram or Google Drive. It only unlocks the UI.

### 16.2 Requirements

| ID | Requirement |
|----|-------------|
| FR-16.1 | Every page requires an authenticated session. Unauthenticated requests render the login form instead of page content |
| FR-16.2 | Authentication state lives in `st.session_state`, never in a module global |
| FR-16.3 | Passwords are never stored in plaintext anywhere — not in config, not in the database, not in logs |
| FR-16.4 | Store only a salted hash using PBKDF2-HMAC-SHA256 from `hashlib`, with a per-user random salt and a high iteration count |
| FR-16.5 | Compare hashes with a constant-time comparison to avoid timing leaks |
| FR-16.6 | Failed attempts log the username and timestamp only, never the submitted password |
| FR-16.7 | Lock out for a cooling-off period after 5 consecutive failures |
| FR-16.8 | A generic failure message for both unknown user and wrong password: *"Incorrect username or password."* Do not reveal which was wrong |
| FR-16.9 | Sessions expire after a configurable idle timeout, defaulting to 60 minutes |
| FR-16.10 | A visible **Sign out** control in the sidebar clears the session |
| FR-16.11 | The signed-in username is recorded as `jobs.created_by` on every ingestion |
| FR-16.12 | Settings that affect the whole install are only editable by an admin-role account |

### 16.3 Login UI

```
┌─────────────────────────────────┐
│   Instagram Knowledge Base      │
│                                 │
│   Username  [______________]    │
│   Password  [______________]    │
│                                 │
│         [    Sign in    ]       │
│                                 │
│   Incorrect username or         │
│   password.                     │
└─────────────────────────────────┘
```

The sidebar, once signed in, shows the current user and a sign-out control:

```
Signed in as naman  (admin)
[ Sign out ]
```

### 16.4 User configuration

Users live in `.streamlit/secrets.toml` alongside the Google Drive accounts. No user-management UI in V1 — accounts are provisioned by editing config.

```toml
[auth]
session_timeout_minutes = 60
max_failed_attempts = 5
lockout_minutes = 15

[auth.users.naman]
display_name = "Naman"
role = "admin"
# PBKDF2-HMAC-SHA256, generated by scripts/hash_password.py
password_hash = "pbkdf2_sha256$260000$SALT$HASH"

[auth.users.viewer]
display_name = "Read Only"
role = "viewer"
password_hash = "pbkdf2_sha256$260000$SALT$HASH"
```

A small helper, `scripts/hash_password.py`, generates a hash to paste into config. It must never write the plaintext password to disk or to the shell history file.

### 16.5 Roles

| Role | Can do |
|------|--------|
| `admin` | Everything, including Settings and Google Drive account connection |
| `viewer` | Browse History and job detail. No downloads, no uploads, no settings changes |

Two roles are sufficient for V1. The role field exists so finer permissions can be added without a schema change.

### 16.6 Security boundaries — read this before exposing the app

A Streamlit-level login is a convenience gate, **not** a hardened security boundary. It protects against casual access on a shared machine. It does not make the application safe to expose to a network.

| ID | Requirement |
|----|-------------|
| NFR-16.1 | Bind to localhost by default |
| NFR-16.2 | If the app is ever reachable beyond localhost, it must sit behind HTTPS and a reverse proxy that performs its own authentication |
| NFR-16.3 | The README must state plainly that this gate is not sufficient for internet exposure |

Deferred to a later version: SSO via the existing Google identity, multi-factor authentication, a user-management UI, and password self-service reset.

---

## 17. Configuration

### `.streamlit/secrets.toml.example` — Google Drive accounts

```toml
[gdrive.personal]
display_name = "Personal Google Drive"
client_id = "YOUR_CLIENT_ID"
client_secret = "YOUR_CLIENT_SECRET"

[gdrive.work]
display_name = "Work Google Drive"
client_id = "YOUR_CLIENT_ID"
client_secret = "YOUR_CLIENT_SECRET"
```

The same file also holds the dashboard user accounts described in §16.4.

Real values never go into the `.example` file. That includes password hashes — the example ships with obvious placeholders.

### `.env.example` — application settings

```
APP_ENV=development
DOWNLOAD_DIR=downloads
TEMP_DIR=temp
LOG_LEVEL=INFO
DEFAULT_DRIVE_FOLDER=Instagram-Knowledge-Base
UPLOAD_CHUNK_SIZE=5242880
MAX_UPLOAD_RETRIES=3
SESSION_TIMEOUT_MINUTES=60
MAX_FAILED_LOGIN_ATTEMPTS=5
LOCKOUT_MINUTES=15
```

### Per-account setup steps

1. Create or configure the appropriate Google Cloud OAuth client
2. Add the client ID to secrets
3. Add the client secret
4. Authenticate from the application
5. Verify the connection
6. Assign a friendly display name

The UI hides all technical identifiers from the user.

### README must document

Project purpose · architecture · installation · virtual environment · dependencies · Google Cloud project setup · Google Drive configuration · optional Instagram authentication · **creating the first dashboard user** · running Streamlit · folder structure · usage · troubleshooting · security · testing · future LLM pipeline.

Google Cloud specifics to document: enabling the Drive API, the OAuth consent screen and test users, creating a Desktop app OAuth client, the `drive.file` scope, and development vs production configuration. Nothing hard-coded.

---

## 18. Error Handling Catalogue

### Custom exceptions

`InstagramDownloadError` · `InstagramAuthenticationError` · `InstagramContentUnavailableError` · `InstagramRateLimitError` · `GoogleDriveAuthenticationError` · `GoogleDriveUploadError` · `ManifestError` · `ValidationError` · `AppAuthenticationError`

### Handling policy

Display a friendly Streamlit error. Log the detailed technical error. Never show a raw stack trace to a normal user.

### Standard user-facing messages

| Situation | Message shown |
|-----------|---------------|
| Invalid URL | "Please enter a valid Instagram post, reel or video URL." |
| Instagram denies access | "Instagram could not provide this content. The post may be unavailable, require authentication, or Instagram may have temporarily limited access." |
| Authentication appears required | "Try enabling authenticated Instagram session." |
| Already downloaded | "This Instagram post has already been downloaded." |
| Bad dashboard credentials | "Incorrect username or password." |
| Too many failed logins | "Too many failed attempts. Please try again in 15 minutes." |
| Session expired | "Your session has timed out. Please sign in again." |
| Action needs admin | "This action requires an administrator account." |

When access is denied, the application reports and stops. It does not attempt to work around the restriction.

---

## 19. Testing Strategy

### Principle

The unit test suite must be fully deterministic and must never touch a live service. Mock Instaloader with fake `Post` objects. Mock Google Drive API calls. No real Google Drive account required.

### Coverage required before V1 is called complete

URL validation · shortcode extraction · content type detection · carousel detection · metadata generation · hash calculation · folder generation · duplicate detection · Google Drive path generation · manifest creation · manifest updates · retry behaviour · error handling · job lifecycle transitions · job history filtering · password hash generation and verification · lockout after repeated failures · session expiry · role enforcement

### Test files

`tests/test_instagram_resolver.py` · `tests/test_metadata.py` · `tests/test_paths.py` · `tests/test_manifest.py` · `tests/test_jobs.py` · `tests/test_app_auth.py`

Auth tests must assert that no plaintext password appears in any stored value or log record.

### Integration tests

Optional, under `tests/integration/`, gated behind an explicit flag:

```
RUN_INTEGRATION_TESTS=true
```

They never run automatically.

---

## 20. Build Plan: 21 Phases

Implemented strictly in order, one phase at a time, with tests run after each major phase. No jumping ahead to a complete application.

| Phase | Deliverable |
|-------|-------------|
| 1 | Project skeleton |
| 2 | Pydantic models |
| 3 | URL validation |
| 4 | Instaloader client |
| 5 | Instagram content resolver |
| 6 | Media downloader |
| 7 | Metadata generation |
| 8 | Hashing |
| 9 | SQLite manifest, including the `jobs` table |
| 10 | Google Drive authentication |
| 11 | Google Drive folder manager |
| 12 | Google Drive small-file upload |
| 13 | Google Drive large-file upload sessions |
| 14 | **App login gate, roles, and password hashing helper** |
| 15 | Streamlit Download page |
| 16 | History page with job list and metadata drill-down |
| 17 | Google Drive page |
| 18 | Settings page |
| 19 | Error handling |
| 20 | Testing |
| 21 | README |

The login gate lands at Phase 14, immediately before the first page is built, so every page is written against an already-authenticated session rather than being retrofitted later.

### Rules during implementation

1. Inspect the repository before creating files
2. Never overwrite an existing file without understanding it
3. Create the requested structure if the project is new
4. One phase at a time; run tests after each major phase
5. Never invent API methods — use the official Instaloader API and Google Drive API
6. Use Streamlit's native functionality for UI
7. Keep credentials out of source code; store no passwords; implement no bypasses
8. Type hints, clear docstrings, structured logging, expected exceptions caught
9. Keep business logic independent of Streamlit, and Google Drive logic independent of Instagram logic

**Post-V1 report must cover:** files created · files modified · dependencies installed · configuration required · tests executed · known limitations · how to run the application.

---

## 21. Definition of Done

V1 is complete only when every item below is true.

**Access control**
- [ ] Unauthenticated users see the login form instead of page content
- [ ] Valid credentials sign in successfully
- [ ] Invalid credentials show a generic failure message
- [ ] Repeated failures trigger lockout
- [ ] Idle sessions expire
- [ ] Sign out clears the session
- [ ] No plaintext password exists in config, database, or logs
- [ ] Viewer role cannot start downloads or change settings

**Core flow**
- [ ] Streamlit starts successfully
- [ ] User can enter an Instagram URL
- [ ] URL is validated
- [ ] Public Instagram content can be analyzed without credentials where Instagram makes it accessible
- [ ] Optional authenticated Instaloader session works

**Download coverage**
- [ ] Image posts download
- [ ] Reels download as video
- [ ] Reel cover image downloads
- [ ] Reel video + cover downloads
- [ ] Carousel images work
- [ ] Carousel videos work
- [ ] Mixed carousel works

**Metadata**
- [ ] Caption saved
- [ ] Author saved
- [ ] URL saved
- [ ] Timestamp saved
- [ ] `metadata.json` created
- [ ] `document.txt` created
- [ ] SHA-256 hashes generated

**Manifest**
- [ ] SQLite manifest works
- [ ] Duplicate detection works

**Jobs and history**
- [ ] Every ingestion attempt creates a job record
- [ ] Job status reflects the real outcome, including `PARTIAL`
- [ ] Activity summary counts are accurate
- [ ] Job list displays and sorts newest first
- [ ] All filters work
- [ ] Job detail view shows source, creator, caption, per-file size, full SHA-256, local path, remote path, and upload status
- [ ] `metadata.json` and `document.txt` are viewable from the detail view
- [ ] Missing local files are flagged
- [ ] Re-run creates a new job and preserves the original
- [ ] Deleting a record defaults to keeping local media
- [ ] Jobs are attributed to the signed-in user

**Google Drive**
- [ ] Account selection works
- [ ] Google authentication works
- [ ] Folder creation works
- [ ] Small files upload
- [ ] Large videos use upload sessions
- [ ] Upload progress displayed
- [ ] Upload retry works
- [ ] Failed uploads retryable from History
- [ ] Local files not deleted after failed uploads

**Project hygiene**
- [ ] Secrets not committed
- [ ] Unit tests pass
- [ ] README contains setup instructions

---

## 22. Future Roadmap

V1 is the ingestion layer only. The metadata format must support what follows, but none of it gets built yet.

### Target pipeline

```
Google Drive
   │
   ▼
Content Scanner
   │
   ├──────────────┐
   ▼              ▼
Images         Videos
   │              │
   ▼              ▼
  OCR      Audio Extraction
   │              │
   │              ▼
   │        Transcription
   └──────┬───────┘
          ▼
   Text + Frames  +  Metadata  +  Caption
          │
          ▼
      Documents
          │
          ▼
       Chunking
          │
          ▼
      Embeddings
          │
          ▼
    Vector Database
          │
          ▼
         RAG
          │
          ▼
         LLM
```

### Future per-post artifacts

**From a video:**
```
video.mp4
audio.wav
transcript.txt
frames/
    frame_0001.jpg
    frame_0002.jpg
video_metadata.json
```

**From an image:**
```
image.jpg
ocr.txt
image_metadata.json
embedding.json
```

### Future unified RAG document

```json
{
  "document_id": "instagram_ABC123",
  "source": "instagram",
  "author": "example_user",
  "url": "...",
  "caption": "...",
  "transcript": "...",
  "ocr": "...",
  "media": ["video.mp4", "cover.jpg"],
  "created_at": "...",
  "metadata": {}
}
```

### Frame extraction options (future)

First frame · every 5 seconds · every 10 seconds · custom interval · scene detection

### Extension points to keep open

Instagram profile ingestion · batch URL import · CSV import · playlist-style ingestion · scheduling · OCR · transcription · frame extraction · AI summaries · hashtag extraction · entity extraction · embeddings · vector database · semantic search · RAG chat interface · content classification · automatic tagging · semantic duplicate detection · cloud deployment · Google SSO for the dashboard · multi-factor authentication · user-management UI · per-user content visibility

---

## 23. Open Questions

| # | Question | Impact |
|---|----------|--------|
| 1 | Should the local virtual environment and dependencies be installed during implementation so each phase can be verified by running tests and launching Streamlit? | Affects whether phases are verified or only written |
| 2 | Target a specific Python 3.11+ patch version, or use whatever the machine provides? | Environment reproducibility |
| 3 | Confirm config split: Google Drive accounts in `.streamlit/secrets.toml`, app settings in `.env`. | Configuration surface |
| 4 | Check in after Phases 1–3, or run all 21 phases autonomously and report at the end? | Feedback cadence |
| 5 | Single dashboard user, or several with roles? §16 specs multiple with admin/viewer — confirm that is wanted rather than a single passphrase. | Login complexity |
| 6 | Is a local username + password gate the right fit, or would reusing the Google sign-in for dashboard access be preferable? | Auth architecture |
| 7 | Will the app ever be reached from another machine, or is it strictly localhost? | Determines whether HTTPS and a reverse proxy are mandatory |

---

## 24. Glossary

| Term | Meaning |
|------|---------|
| **Shortcode** | The unique Instagram identifier in a post URL (e.g. `ABC123` in `/reel/ABC123/`). The primary content identifier throughout this system. |
| **Carousel / Sidecar** | A multi-item Instagram post containing several images and/or videos. |
| **Reel cover** | The thumbnail/display image of a Reel. In V1 this is what "Reel image" means. |
| **Staging directory** | The local folder where media lands before upload. Used even in Google Drive-only mode. |
| **Manifest** | The SQLite record of everything ingested — posts, files, and upload outcomes. |
| **Upload session** | A Google Drive API mechanism for resumable, chunked uploads of large files. |
| **Instaloader session** | A saved, reusable Instagram authentication state, loaded via `load_session_from_file()`. |
| **RAG** | Retrieval-Augmented Generation — retrieving relevant documents to ground an LLM's answer. |
| **Idempotency** | Running the same operation repeatedly produces the same result without duplicates. |
| **Job** | One run of the ingestion pipeline for one URL. Created before any network call so failures remain visible. |
| **PARTIAL** | A job whose media downloaded successfully but whose upload did not. Recoverable by retrying the upload alone. |
| **App login** | The dashboard sign-in. Unrelated to Instagram or Google credentials; it only unlocks the UI. |
| **PBKDF2** | A deliberately slow key-derivation function used here to hash dashboard passwords via `hashlib`. |
