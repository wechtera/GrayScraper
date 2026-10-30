# SCRAPEPAGE — Application Archeology

**Purpose:** Establish what this application does, who uses it and how, what it is built from, and what is weak, so the codebase can be re-platformed with no functional gaps and no inherited vulnerabilities.
**Method:** Static read of the working tree (including uncommitted changes), the live `library.db` schema and row counts, and a run of the existing test suite. No network calls were made, and no secret values are reproduced here.
**Snapshot:** 2026-10-30, branch `main`, HEAD `6b98e60`. About 21 tracked or modified files are uncommitted, so the working tree is ahead of git history.
**Confidence key:** *Verified* means read in code or observed in the DB. *Inferred* means deduced from naming or docs. *Unverified* needs a runtime check before you rely on it.

---

## 1. What the application is

A single-user, local-first **personal archive and asset manager for a MyMiniFactory (MMF) 3D-printing library**. The repo is named `scrapepage`, and the product brands itself **"DESYNC 3D Vault"** in code and UI.

It does four things:

1. **Discovers** every model the user owns on MMF. It drives a real browser (Playwright) through the Cloudflare-protected library page and harvests model IDs.
2. **Catalogs** each model. It pulls metadata from the MMF REST API v2, stores the raw JSON on disk, and normalizes it into SQLite.
3. **Archives** the physical files. It downloads STL/ZIP/RAR archives into fast local staging and then offloads them to a NAS "vault" in a structured layout.
4. **Curates and serves** the collection:
   - A browser dashboard lets the user explore, audit and download.
   - A "stash" feature builds slicer-ready folders for printing.
   - An indexer adopts non-MMF STL collections in place.

**Scale in the live DB (Verified):**

| Measure | Value |
|---|---|
| Models in catalog | 4,893 (4,868 `mmf`, 25 `external`) |
| `model_files` rows | 23,572 |
| `downloaded_files` rows | 496, so roughly 2% of files are physically archived |
| `metadata/` JSON backups | about 4,870 files |
| `previews/` | about 4,870 entries |
| `library.db` | about 16 MB |

The download pipeline has barely started. Most of the value so far is catalog, analytics and previews.

### Provenance of the project (from `Archival_DEV/ai/savepoint.md`)

The project was built iteratively with an AI assistant. The stated principles are:
- Zero-bloat and standard-library simplicity.
- Defensive parsing.
- A self-contained UI with no web framework.

The refactor should deliberately revisit these. They produced a working system, but they are also the root of most of the security and maintainability problems in §8.

---

## 2. Use cases

### 2.1 Primary personas

- **The Collector (the only real user today).** Owns thousands of MMF items, mostly from Patreon/Tribe/Kickstarter-style monthly drops. Wants a durable offline copy, a way to find things, and assurance nothing is missing.
- **The Printer.** Wants to assemble a print job ("a pile of gray") of specific models, sub-models, or individual STLs, filtered by pre-supported or unsupported. They want it delivered as a clean folder for a slicer.
- **The Operator.** Is the same person, running long background jobs: scrape, ingest, download, reconcile. They want visibility into progress, failure and storage health.

### 2.2 Use-case catalog

| # | Use case | Trigger | Outcome | Main code |
|---|---|---|---|---|
| UC1 | Discover owned library | UI "start scrape" or `node scrape.js` | `model_ids.txt` and `models.json` in a timestamped `scrapes/scrape_*` dir | `scrape.js`, `TaskManager._run_scrape_worker` |
| UC2 | Ingest metadata | UI, from a scrape folder or pasted IDs | Raw JSON in `metadata/`, normalized rows in SQLite, schema-drift anomalies logged | `TaskManager._async_ingest` |
| UC3 | Authenticate to MMF | Settings, OAuth connect, or paste cookies | Bearer token (implicit grant) or Cloudflare cookies stored in `app_settings` | `ui/routes/auth.py`, `auth_service.py` |
| UC4 | Download files | Queue models in the UI | Staged on local disk, validated, offloaded to the NAS vault, recorded in `downloaded_files` | `DownloadManager` |
| UC5 | Browse and search catalog | Models, Creators and Releases views | Filtered, paged views; model detail with files and previews | `routes/catalog.py`, `views/models.js` |
| UC6 | Audit release continuity | Releases view, `/api/audit` | Missing months, low- and high-volume outliers per creator, bundle and bonus separation | `services/analytics.py` |
| UC7 | Peek inside archives | After download, or backfill | Sub-model discovery, member lists and preview images extracted from ZIP/RAR/7z | `ArchivePeeker` |
| UC8 | Build and export a stash | Stash modal, then Export | Slicer-ready folder with manifest. Surgical single-member extraction, with filter modes `all`, `presupported`, `unsupported` | `StashService`, `StashExporter` |
| UC9 | Render a preview of a mesh | Per-model "render view" | Blender-rendered JPEG, ephemeral or cached. Only for external models, not MMF | `MeshRenderer`, `scripts/blender_render_mesh.py` |
| UC10 | Adopt an external library in place | Settings, add library path, scan | Read-only crawl; synthetic model and file IDs (`>= 2,000,000,000`); `source='external'` | `ExternalLibraryIndexer` |
| UC11 | Reconcile vault and DB | Reconcile view | Forward and reverse scan; orphan directories; staging garbage; path drift realign; SHA-256 and STL validation | `VaultReconciler` |
| UC12 | Operate and tune | Settings view | Storage paths, concurrency, circuit breaker, log levels and layers, log and stash purge, directory usage | `routes/settings.py`, `logger.py` |

### 2.3 Business rules worth preserving

These are domain knowledge, not accidents. They must survive the rewrite.

- **ID sanitization:** model IDs must match `^\d+$`. MMF returns `object-`-prefixed URLs that 404 on the API.
- **Double-count trap:** scraped payloads mix library-purchase IDs with model IDs. Extract `object_id`, or parse the canonical `3d-print-*-(\d+)` URL.
- **Bonus segregation:** a model is `is_bonus=1` if its title or tags match `bonus` or `reward`. These count toward `bonus_count` but not `model_count`.
- **Bundle heuristic:** `is_likely_bundle=1` if there are 4 or more regular models, or the text or tags contain `patreon`, `tribe`, `welcome pack`, `bundle`, `release`, `heroes`, `pack`.
- **Outlier math:** low outlier if `count < mean − 1.5σ` **or** `count ≤ 0.5·mean` with a delta of at least 3. The percentage clause exists because big Kickstarter months inflate σ and make the pure-σ bound negative. High outlier if `count > mean + 1.5σ`.
- **Schema drift detection:** compare the incoming top-level keys against the `KNOWN_TOP_LEVEL_KEYS` allow-list, and require `id`, `name` and `files`.
- **Two-tier storage:** `local` staging, then `nas` vault. Staging uses `.part` files, a validation step (ZIP CRC check), and an atomic move.
- **Circuit breaker:** a 401/403 from the download stream trips a persisted breaker and halts all workers until reset. It exists to avoid account flags.
- **Pacing:** batch size and cooldown, a worker concurrency setting, and 429 backoff with a 5-wide async semaphore for ingest.
- **Filename hygiene:** size prefix and suffix detection and cleaning in `PathSanitizer.inspect_filename`.
- **Directory layout:** `Creator/Category/[ID] ModelName/`. `sanitize_segment` collapses anything outside `[a-zA-Z0-9_-]`.
- **External models:** IDs are allocated from a high range so they never collide with MMF IDs.
- **Read-only guarantees:** the external indexer and the stash exporter promise not to modify source archives. This is asserted in docstrings and tests (`test_stash_guardrails.py`).
- **Stash guardrail:** only already-downloaded items can be added to a stash (`_verify_*_downloaded`).

---

## 3. Architecture as found

```
 Browser (vanilla ES-module SPA)  ──HTTP/JSON──►  ui/server.py  (http.server + ThreadingTCPServer, 0.0.0.0:8080)
   ui/index.html, vault.css                          │  RequestHandler → routes.dispatch_route (regex if-chain)
   static/js/{app,api,state,utils}.js                │  one new sqlite3 connection per request
   static/js/views/*.js                              ▼
                                           ┌──────────── services/ ─────────────┐
                                           │ TaskManager (singleton, threads)   │──subprocess──► node scrape.js (Playwright + stealth)
                                           │   scrape / ingest / reconcile /    │──httpx async──► MMF API v2 /objects/{id}
                                           │   peek / stash_export / ext index  │
                                           │ DownloadManager (worker threads)   │──httpx stream──► MMF download URLs
                                           │ VaultReconciler (singleton)        │
                                           │ ArchivePeeker, StashExporter,      │──subprocess──► unrar / 7z / 7zz
                                           │ MeshRenderer, ExternalIndexer,     │──subprocess──► Blender (headless)
                                           │ StashService, analytics, logger    │
                                           └────────────────┬───────────────────┘
                                                            ▼
       SQLite library.db (single file)     Disk: metadata/ previews/ scrapes/ logs/ staging/ + NAS vault + stash export dir
```

### Process and concurrency model (Verified)

- One Python process runs a thread-per-request HTTP server (`daemon_threads`).
- `TaskManager` and `VaultReconciler` are singletons that use background threads.
- Only **one active pipeline task** at a time (409 otherwise).
- `DownloadManager` runs N download threads and M offload threads on SQLite-backed job queues. Jobs are claimed with a "claim next" query.
- Ingest uses `asyncio` and `httpx.AsyncClient` inside a worker thread.
- A **separate Node process** is spawned for scraping, with stdout parsed by regex for progress (`Cycle N: M unique models`). That coupling is fragile.

### Data model (Verified from live DB)

| Table | Role |
|---|---|
| `models` | Catalog. Columns include `id`, `name`, `designer_*`, categories, `release_month`, `is_bonus`, `vault_path`, `archive_download_url`, `source` (`mmf` or `external`), `raw_backup_path`. |
| `model_tags`, `model_files` | Normalized tags and file records with `download_url`. |
| `releases` | Materialized cluster per `(designer, month)`, rebuilt at every server start. |
| `schema_anomalies` | Upstream drift log. |
| `download_jobs`, `downloaded_files`, `download_audit_log` | Download pipeline state and audit. |
| `pipeline_tasks` | Task history with accumulated log text. |
| `reconciliation_runs` | Reconciler snapshots. |
| `file_archive_submodels`, `file_archive_members` | Archive-peek results. |
| `stashes`, `stash_items` | Curated print piles. `item_type` is one of model, file, submodel or archive_member. A COALESCE-based unique index prevents duplicates. |
| `external_libraries` | Registered in-place libraries. |
| `app_settings` | Key/value config **and secrets**: OAuth client ID and secret, access token, Cloudflare and PHP session cookies, paths, concurrency. |

Schema is created by `CREATE TABLE IF NOT EXISTS` blocks scattered across at least six modules (`db.py`, `task_manager`, `reconciler`, `archive_peeker`, `stash_service`, `external_indexer`, `settings.py`). Migrations are hand-rolled `PRAGMA table_info` plus `ALTER TABLE`. There is no migration framework and no single schema source of truth. Code also contains "self-healing schema" shims that add the `source` column on demand in request handlers.

### API surface (about 60 routes, all under `/api`)

Dispatch is one long `if method == … and path == …` chain in `ui/routes/__init__.py`, with several aliases per route.

- **Catalog:** metrics, models, models/{id}, models/{id}/files, creators, releases, audit, models/{id}/render-view, render/stl
- **Archives:** archives/{id}/submodels, archives/{id}/raw-tree, reconcile/archive-peek
- **Downloads:** downloads/status, history, queue (POST/DELETE), dequeue, control
- **Settings and storage:** settings (GET/POST), settings/directories, system/browse, storage/status, storage/config, settings/libraries (+scan, delete), settings/logging, settings/logs/purge, settings/stashes/purge
- **Auth:** auth/status, config, login, callback, token_callback, disconnect
- **Tasks:** tasks/status, scrapes/list, scrapes/download, scraper/start, tasks/{cancel,kill,clear}, ingest/start
- **Reconcile:** scan, status, cancel, reset, delete-orphans, ingest-untracked, staging, staging/purge, drift, drift/realign, catalog-previews
- **Stashes:** CRUD, active stash, items, bulk add, export
- **Other:** `/previews/*` static image streaming with an SVG placeholder fallback

The routes carry no API versioning, schema validation or OpenAPI. Handler signatures are uniform `(conn, download_manager, body, params)`, and return shapes vary (2-tuple, 3-tuple, raw string, bytes).

### Frontend (Verified)

Roughly 9k lines. `index.html` is 1.4k lines, `vault.css` is 3k lines, and the JS modules (`models.js`, `downloads.js`, `settings.js` and others) range from 600 to 1.3k lines each. It is a hash-routed SPA of native ES modules with no build step and no framework. State lives in a small `state.js`. Rendering is by string-concatenated `innerHTML` (97 call sites across the view modules). A shared `escapeHtml` exists, but it is not applied everywhere.

---

## 4. Stack inventory

| Layer | Technology | Notes |
|---|---|---|
| Backend language | Python **3.12** (`.python-version`, `requires-python >= 3.12`) | Managed with **uv** (`uv.lock`) |
| HTTP server | stdlib `http.server` + `socketserver.ThreadingTCPServer` | No framework, no ASGI, no middleware |
| HTTP client | `httpx` ≥ 0.28.1 (sync, async, streaming) | `requests` ≥ 2.34.2 is also declared; check whether it is still imported |
| Persistence | SQLite 3 via stdlib `sqlite3` | Raw SQL, `row_factory=Row`, no ORM, no migrations. WAL mode not confirmed. |
| Browser automation | **Node** + `playwright` ^1.64.1, `playwright-extra` ^4.3.6, `puppeteer-extra-plugin-stealth` ^2.11.2. The Python `playwright` ≥ 1.64.1 is also a dependency. | Headed Chrome; manual Cloudflare Turnstile click |
| Progress bars | `tqdm` | Used by legacy CLI scripts |
| Archive handling | stdlib `zipfile`; external **`unrar`** and **7-Zip** binaries (hard-coded Homebrew fallbacks) | Both are shelled out |
| 3D rendering | **Blender** headless, driven by `scripts/blender_render_mesh.py` | `Archival_DEV/rnd/` holds R&D variants |
| Frontend | Vanilla JS ES modules, one HTML file, one CSS file | No bundler, no TypeScript, no tests |
| Tests | stdlib `unittest`, 11 files in `tests/` | **84 tests, all passing** (run 2026-10-30, about 6 s) |
| Platform assumptions | macOS (`/opt/homebrew/bin/*`), local NAS mount | No Docker, CI or deploy config |
| Logging | stdlib `logging` with a custom topic-layer filter (`http`, `sql`, `storage`, `archive`), an in-memory UI ring buffer, and `logs/vault.log` | `log_layer()` abstraction |
| External APIs | MMF REST v2 (`/api/v2/objects/{id}`, `/api/v2/user`), MMF OAuth authorize endpoint (`auth.myminifactory.com/web/authorize`) | Implicit grant |

### Dev-only and legacy material

- `Archival_DEV/scripts/` holds the original CLI pipeline: `main.py`, `seed_db.py`, `metadata_scrape.py`, `migrate_p1.py`, `survey_vault.py`, `validate_env.py`.
- `README.md` is **stale**. It still describes `main.py` and `seed_db.py` at the repo root and `ui/test_analytics.py`.
- `cookies.json` and `mmf_browser_profile/` are the pre-OAuth auth legacy. The last commit message is "begone cookies, hello oauth2", but cookie auth is still wired in.

---

## 5. External dependencies and upstream coupling

| Dependency | Why it is risky | Mitigation to design in |
|---|---|---|
| MMF library page DOM and XHR shape | Scraper depends on shadow-DOM traversal, a `3d-print-*-(\d+)` URL regex, and the heuristic "JSON array with more than 50 items". It also hard-codes a target count of 4,868 and a 4,000 early-exit. | Prefer the official API (an authenticated library or purchases endpoint) over DOM scraping. Make the expected count dynamic. |
| Cloudflare Turnstile / `cf_clearance` | Bound to IP and TLS fingerprint, with a manual click. Stealth plugins are an arms race. | Treat the official OAuth API as the primary path. Keep the browser path as an optional fallback. |
| MMF API v2 schema | Top-level key allow-list already shows drift is expected. | Keep drift detection. Store raw JSON as the source of truth. |
| MMF ToS | Scraping and stealth browsing for bulk archiving may breach the terms. | Confirm legal and ToS posture before investing further in the stealth path. |
| `unrar` / 7-Zip / Blender | Optional external binaries located with hard-coded paths. | Explicit configuration and a startup capability check. |

---

## 6. Data, state and storage layout

| Location | Contents | Sensitive | Git-ignored |
|---|---|---|---|
| `library.db` | Catalog plus **secrets** (§8) | **Yes** | Yes |
| `metadata/` | Raw API JSON, may include signed download URLs | Moderate | Yes |
| `previews/` | Cached images and renders | No | Yes |
| `scrapes/scrape_*/` | `model_ids.txt`, `models.json`, **a copy of `cookies.json`** | **Yes** | Yes |
| `cookies.json` (root) | Live Cloudflare and PHP session cookies | **Yes** | Yes |
| `mmf_browser_profile/` | Chromium profile with sessions | **Yes** | Yes |
| `logs/vault.log` | Rotating application log | Possible URL or token leakage (unverified) | Yes |
| NAS vault (path in settings) | Archived STL/ZIP/RAR, `metadata.json` per model | No | Outside repo |
| `staging/.work/` | In-flight `.part` files and staged files | No | Yes |
| Stash export dir (setting `stash_download_path`) | Materialized slicer folders and manifests | No | Outside repo |
| `tree_discovery.txt` | A 3 MB tree dump, **untracked** | Low | No. Delete or ignore it. |

---

## 7. Quality baseline

**Strengths**
- The tests pass: 84 tests across analytics, archive peek, downloader, API, stash service, routes, guardrails, exporter, indexer, renderer and settings.
- Real domain logic is captured in tests (outlier math, bundle and bonus rules, stash guardrails).
- Parameterized SQL is used almost everywhere. The f-string SQL found only interpolates `?` placeholder lists.
- Path-traversal guards exist on previews, orphan delete, staging purge and scrape-folder ingest.
- Downloads are staged, validated and atomically moved, with circuit-breaker protection.
- Schema-drift detection and an audit trail (`download_audit_log`) exist.

**Weaknesses**
- Several modules exceed 1,000 lines (`task_manager` 1.4k, `reconciler` 1.1k, `downloader` 1.1k) and mix orchestration, SQL, filesystem and HTTP.
- `sanitize_segment` is duplicated in at least four modules.
- The unrar and 7z binary discovery block is copy-pasted across `archive_peeker`, `stash_exporter` and `mesh_renderer`.
- `try/except ImportError` shims stub out `log_layer` in several files.
- Schema creation and migration are scattered (§3).
- No type checking, linting, CI or coverage.
- The frontend has zero tests and heavy `innerHTML` use.
- There is a latent bug in the cookie-file fallback in `_async_ingest`: the `elif isinstance(parsed, dict)` branch checks `parsed` where it should check `raw`, so the dict form is mishandled when the file-fallback path is taken.

---

## 8. Security and vulnerability register

Severity reflects a single-user local tool, then re-rated for what happens if the tool is ever reachable beyond `localhost`. Each item says how to verify and how to fix in the new stack. **All items are from static review. Confirm exploitability at runtime before treating the severity as final.**

### Critical

**V1. No authentication or authorization on any endpoint, bound to all interfaces.** *(Verified)*
`server.py` binds `0.0.0.0:8080`. No route checks identity. Anyone on the LAN can read the catalog, change settings, start tasks, queue downloads, and trigger deletions.
*Fix:* bind to `127.0.0.1` by default. Add real authentication for any remote use (session or token, plus CSRF protection) and a role model if multi-user is ever wanted.

**V2. Secrets exposed over unauthenticated HTTP.** *(Verified)*
- `GET /api/settings` returns **every** `app_settings` row. That includes the OAuth client secret, access token, and the Cloudflare and PHP session cookies.
- `GET /api/auth/status` returns `client_secret` in clear text.
- `GET /api/scrapes/download?path=cookies.json` is not blocked by the `..` check. It is only a root-relative path, so it returns the live cookie file, and also `scrapes/*/cookies.json` and `models.json`.
*Fix:* never return secrets (return `configured: true`). Use an allow-list for readable artifact paths. Move secrets to the OS keychain or an encrypted store.

**V3. Cross-site request forgery and DNS rebinding against localhost.** *(Verified in code)*
`Access-Control-Allow-Origin: *` is sent on every response. The server also parses POST bodies as JSON regardless of `Content-Type`, so a "simple" cross-origin form POST from any website the user visits can hit `localhost:8080`. There is no `Origin` or `Host` validation, so DNS rebinding also applies. The attacker can read the secrets from V2 or change storage paths.
*Fix:* remove wildcard CORS. Validate `Host` and `Origin`. Require a custom header or token on state-changing requests. Enforce `Content-Type: application/json`.

### High

**V4. Source code served as static files.** *(Verified)*
`SimpleHTTPRequestHandler(directory=ui/)` serves everything under `ui/`, including `server.py`, `routes/*.py` and `__pycache__`. It also ships directory listings by default for directories without an index.
*Fix:* serve only a dedicated `static/` directory, and disable listings.

**V5. OAuth implicit grant without state verification.** *(Verified)*
`/api/auth/login` stores a `state` value, but `/api/auth/token_callback` never compares it. Any caller can POST an arbitrary token, which is a login-CSRF and token-injection path. The implicit grant is also deprecated (OAuth 2.1). Access tokens travel in the URL fragment and are held in plaintext.
*Fix:* authorization-code flow with PKCE, strict `state` and redirect validation, refresh-token handling (the `mmf_refresh_token` field exists but is never populated), and encrypted storage. Confirm which grants MMF actually supports.

**V6. Arbitrary filesystem read and write via configurable paths.** *(Verified)*
- `GET /api/system/browse` lists directories anywhere the process user can read.
- `POST /api/storage/config` and `POST /api/settings` set the vault root, staging path, stash export path and metadata path to any location, creating directories with `mkdir(parents=True)`.
- `POST /api/settings/libraries` registers any directory for crawling.
- Combined with V1 and V3, a remote or cross-site caller can steer file writes and deletes (for example, orphan deletion is bounded to the configured NAS root, so redirecting that root widens the blast radius).
*Fix:* a fixed allow-listed set of base directories, set at install time and not changeable over the network. Validate every path against them.

**V7. Archive extraction and external command handling.** *(Inferred; verify)*
- The exporter and renderer pass archive-member names into `unrar` and `7z` argv. A member whose name starts with `-` can be taken as an option, which is argument injection. No `--` separator was seen.
- `7z -o{dir}` and `unrar x` unpack with whatever paths the archive declares. Zip-slip protection was not seen on the bulk-unpack path (`_unpack_archive_to_dest`).
- There are no limits on decompressed size, file count or ratio, so decompression bombs can exhaust disk or CPU.
- Subprocess timeouts exist for single-member extraction (60 s) and renders (120 s), but not for the bulk `x` calls.
- Archives come from third parties, so treat them as hostile.
*Fix:* an extraction library or sandbox with path normalization, `--` separators, quotas, timeouts and a non-privileged worker.

### Medium

**V8. Stored and DOM XSS in the UI.** *(Verified in part)*
`innerHTML` is used in 97 places. For example, `models.js:226` interpolates `err.message` unescaped. Model names, descriptions, tags and designer names come from a third party and from filenames, so they are untrusted. Some call sites escape and others do not. There is no Content-Security-Policy.
*Fix:* a templating or component layer that escapes by default (or `textContent`), plus a strict CSP and no inline scripts. Note that the OAuth callback page also uses inline script and injects `data.username` through `innerHTML`.

**V9. HTML and URL injection in the auth redirect page.** *(Verified)*
`handle_get_auth_login` builds an HTML `<meta refresh>` and an `<a href>` with `client_id` interpolated and not HTML-escaped. `client_id` is user-settable via `/api/auth/config`.
*Fix:* emit a real `302 Location` header and escape or encode values.

**V10. Secrets stored in plaintext in SQLite and on disk.** *(Verified)*
Tokens, the client secret and cookies sit in `app_settings`. Cookies are also copied into `scrapes/<run>/cookies.json` (kept indefinitely) and into `mmf_browser_profile/`. The DB file is 0644. `scrape.js` rewrites `cookies.json` with full cookie jars.
*Fix:* OS keychain or an encrypted secret store, short-lived run directories, `0600` permissions, and never copying secrets into artifact folders.

**V11. Unbounded request bodies and no input schema validation.** *(Verified)*
`Content-Length` is trusted and fully read into memory, with no maximum. Most handlers coerce fields ad hoc with `int()`, `str()` and `.get()`. Errors return Python exception text to the client (`"Internal Server Error: {e}"`).
*Fix:* body size limits, schema validation (for example Pydantic), and generic error messages with server-side logging.

**V12. Destructive operations are single-call and unauthenticated.** *(Verified)*
`delete-orphans` (`shutil.rmtree`), `staging/purge`, `reconcile/reset`, `tasks/clear` and `stashes/purge` have no confirmation token, no dry-run contract in the API, and no audit entry.
*Fix:* a two-step or dry-run pattern, an audit log, and authorization.

**V13. Playwright launched with `--no-sandbox`, stealth plugins, and a spoofed user agent.** *(Verified)*
This weakens browser isolation while the browser loads remote content with authenticated cookies. The pinned user agent is also Chrome 130, which is stale.
*Fix:* drop `--no-sandbox`, use a dedicated unprivileged profile, and prefer the API route.

### Low and hygiene

- **V14. No TLS and no rate limiting** on the local server (acceptable on loopback, not otherwise).
- **V15. Logging may include sensitive URLs or tokens.** Review `log_layer` calls at DEBUG level (for example, token-bearing download URLs).
- **V16. SQLite concurrency.** A new connection per request plus many writer threads with a 45 s busy timeout. Confirm WAL mode and look for "database is locked" and lost-update risk in the job-claim logic.
- **V17. Supply chain.** Dependencies are fixed in `uv.lock` and `package-lock.json`, but there is no audit step (`pip-audit`, `npm audit`). The unused `requests` dependency should be removed.
- **V18. Singletons and global state** (`TaskManager.__new__`, module-level managers in `server.py`) make testing and multi-process deployment harder and hide lifecycle bugs.
- **V19. `Archival_DEV/` and root contain operational data and a 3 MB `tree_discovery.txt`** that should not live in the source tree.
- **V20. Git hygiene.** The working tree has about 20 modified files and many new untracked services and tests. Commit or branch before refactoring so there is a clean baseline.

---

## 9. Functional gaps and risks to close in the refactor

The work-in-progress state means several capabilities are partial or missing.

1. **Download pipeline is about 2% complete** (496 of 23,572 files). Real-scale behavior (disk, NAS latency, resume, rate limits, URL expiry) is unproven. The circuit-breaker state in the live DB suggests it has already tripped (the setting is present, the value was not inspected).
2. **No automated sync.** The savepoint lists a scheduled delta-sync as pending (scrape, diff, ingest, download). Today everything is manual.
3. **No resume or retry semantics documented** for ingest after a crash. Active tasks are tracked in-memory plus the DB, so confirm orphaned "running" tasks are recovered on restart.
4. **No integrity re-verification schedule.** The reconciler can hash and validate STLs, but it only runs on demand.
5. **No backup or restore** for `library.db` and `app_settings`. `Archival_DEV/backups/` holds only ad hoc dumps.
6. **Token lifecycle gaps.** Six-hour implicit tokens with no refresh mean downloads stop mid-run (the code trips the breaker on 401).
7. **External library IDs** are allocated from a counter. Confirm stability across unlink and rescan, since stashes reference model IDs.
8. **Single-writer assumptions** (one task at a time). Decide whether concurrency is a requirement.
9. **Observability.** Logs are text only. There are no metrics, health endpoint or structured task events.
10. **Portability.** macOS paths are hard-coded. The new stack should run in a container or on Linux.
11. **Documentation drift.** README, the savepoint and the actual layout disagree.
12. **Accessibility, responsive layout and i18n** are unassessed.

---

## 10. Recommendations for the new stack

The decision is yours. This section gives a recommendation and the reasoning, not a decision.

### Principles
- **Security by default:** loopback bind, authentication, CSRF protection, secrets never leave the server, path allow-lists.
- **One source of truth for schema:** versioned migrations.
- **Explicit boundaries:** API, domain services, job runner, storage adapters, and external-tool adapters (unrar, 7z, Blender, Playwright).
- **Preserve the domain logic in §2.3** with its tests, and port the 84 tests as characterization tests first.

### Suggested target (adapt as needed)

| Concern | Suggestion | Why |
|---|---|---|
| API | **FastAPI** (Python 3.12+) with Pydantic models and OpenAPI | Keeps the existing domain code reusable, adds validation, auth dependencies and docs |
| Data | SQLite in **WAL** mode behind **SQLAlchemy 2 + Alembic** (Postgres only if multi-user is a goal) | Migrations and one schema definition |
| Jobs | A persistent queue with a single worker process (for example Dramatiq, RQ or Arq on SQLite or Redis), or a small in-house runner over a `jobs` table with proper state machine and recovery | Replaces the singleton-and-threads orchestration and survives restarts |
| Scraping | Prefer the **official authenticated MMF API**. Keep Playwright in an isolated worker or container as a fallback, returning structured JSON over a defined contract instead of regex over stdout | Removes the fragile stdout coupling and the stealth arms race |
| Downloads | Async `httpx` with resumable `Range` requests, a checksum step, and the same staged-then-atomic-move flow | Preserves the proven design |
| Archives | Isolated worker with extraction quotas, normalized paths, `--` separators, and a hard timeout | Addresses V7 |
| Frontend | A framework that escapes by default (SvelteKit, React with TypeScript, or server-rendered HTMX) plus Vite and a strict CSP | Addresses V8 and the 97 `innerHTML` sites |
| Auth | Local session login or a single API token for the loopback tool. OAuth 2 authorization-code with PKCE toward MMF. | Addresses V1, V3 and V5 |
| Secrets | OS keychain or an encrypted settings table with a key outside the DB | Addresses V10 |
| Packaging | Docker Compose (app, worker, optional browser worker), with volumes for the vault and the staging area | Removes macOS path assumptions |
| Quality | `ruff`, `mypy` or `pyright`, `pytest`, `pip-audit` and `npm audit`, Playwright end-to-end tests, GitHub Actions | None of this exists today |

### Suggested migration order
1. Commit or branch the current tree. Delete or ignore `tree_discovery.txt`. Update the README.
2. **Stop the bleeding first, on the current code:** bind to loopback, remove secrets from `/api/settings` and `/api/auth/status`, fix the `scrapes/download` allow-list, restrict static serving, drop wildcard CORS, and verify OAuth `state`. These are small changes and remove V1 to V5 immediately.
3. Freeze behavior: port the existing tests, and add characterization tests for the API contract (record real responses).
4. Stand up the new skeleton: schema and migrations imported from the live DB, auth, job runner.
5. Port services one at a time in this order: catalog and analytics (read-only, lowest risk), then ingest, then downloads, then reconcile and stash, then the archive-handling paths with the hardened extractor.
6. Rebuild the UI view by view against the new API.
7. Cut over with a DB migration rehearsal on a copy of `library.db`, and verify counts (4,893 models, 23,572 files, 496 downloads).

---

## 11. Open questions for you

1. Is this tool ever meant to be reachable from other machines, or is loopback-only acceptable? This decides how much of V1 to V3 needs a full auth system.
2. Is the MMF ToS position on automated library access settled? It shapes whether the stealth scraper survives the refactor.
3. Should the new system support more than one user or library?
4. Should the stack stay Python, or is a different backend language acceptable?
5. Is the NAS vault path stable, or should it move to a storage-adapter abstraction (local, SMB, S3)?
6. Should the non-MMF "external library" feature stay in scope?
7. Are `Archival_DEV/rnd/` Blender experiments a product requirement (better previews) or disposable?

---

## Appendix A. Key files

| Area | Files |
|---|---|
| Server and routing | `ui/server.py`, `ui/routes/__init__.py` and `ui/routes/{catalog,downloads,archives,reconcile,settings,stashes,storage,tasks,auth}.py` |
| Orchestration | `services/task_manager.py` |
| Download | `services/downloader.py` |
| Integrity | `services/reconciler.py` |
| Archives | `services/archive_peeker.py` |
| Stash | `services/stash_service.py`, `services/stash_exporter.py` |
| Rendering | `services/mesh_renderer.py`, `scripts/blender_render_mesh.py` |
| External libs | `services/external_indexer.py` |
| Analytics | `services/analytics.py` |
| Auth | `services/auth_service.py` |
| Schema and settings | `services/db.py` |
| Scraper | `scrape.js` |
| Frontend | `ui/index.html`, `ui/static/css/vault.css`, `ui/static/js/**` |
| Tests | `tests/*.py`, `test.sh` |
| Project history | `Archival_DEV/ai/savepoint.md`, `Archival_DEV/ai/prompt`, `Archival_DEV/scripts/*` |

## Appendix B. How this was gathered and what is not covered

- Read in full or in key parts: `server.py`, route dispatcher, auth, settings, tasks, stash routes, `db.py`, `auth_service.py`, `scrape.js`, and the scrape, ingest, download, reconcile and orphan-delete code paths.
- Surveyed by function listing and targeted search: the remaining service modules, the frontend views, and CSS.
- Not reviewed: line-by-line frontend logic, `archive_peeker` segmentation heuristics, the Blender script internals, the contents of `Archival_DEV/rnd/`, and the log files.
- Not done: running the server, exercising any endpoint, or inspecting secret values. The V-items above should be reproduced against a copy of the app before they are closed.
