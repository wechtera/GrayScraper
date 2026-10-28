### 1. `README.md`

```markdown
# MyMiniFactory Library Archiver, Analytics & Release Explorer

An end-to-end local data pipeline and lightweight analytics dashboard built to crawl, archive, audit, and explore complete MyMiniFactory (MMF) 3D model libraries (~5,000+ items). 

The system bypasses Cloudflare security using stealth browser automation, archives raw API payloads to disk, normalizes data into a relational SQLite database, isolates loyalty/reward items from standard releases, audits chronological continuity (detecting missing creator drop months), and serves an offline dashboard with zero third-party UI dependencies.

---

## System Architecture

```text
                  [MyMiniFactory Web UI]
                            │
                            ▼  (Step 1: Node.js + Playwright Extra Stealth)
  [cookies.json] ──►  [scrape.js] ──► writes/appends ──► [model_ids.txt] (Delta Queue)
                                                                │
                            ┌───────────────────────────────────┘
                            ▼  (Step 2: Python 3.12 + httpx + SQLite)
                        [main.py]  (or offline: [seed_db.py])
                            │
            ┌───────────────┴───────────────┐
            ▼                               ▼
 [metadata/model_<id>.json]            [library.db]
  (Raw Archival Backups)          (Relational Store: models,
                                   tags, files, anomalies, releases)
                                            │
                                            ▼  (Step 3: Analytics & Local Dashboard)
                                      [ui/server.py] ◄── [ui/test_analytics.py]
                                            │
                                            ▼  (Standard Web Browser)
                                      [ui/index.html]

```

---

## File & Directory Manifest

| Path | Purpose | Runtime |
| --- | --- | --- |
| `scrape.js` | Crawls MMF library, pierces Shadow DOM, intercepts network calls, appends IDs. | Node.js (Playwright) |
| `cookies.json` | Stores browser session and Cloudflare clearance (`cf_clearance`, `PHPSESSID`). | Auth config |
| `model_ids.txt` | Newline-delimited queue of discovered numeric model IDs. | Disk Delta |
| `main.py` | Asynchronous metadata scraper; writes JSON backups and populates SQLite. | Python (`httpx`, `tqdm`) |
| `seed_db.py` | Offline batch importer to populate SQLite directly from existing `metadata/` JSONs. | Python (`tqdm`) |
| `library.db` | Backing SQLite relational database. | SQLite 3 |
| `metadata/` | Directory containing raw, ground-truth API JSON payloads (`model_<id>.json`). | Disk Archive |
| `ui/server.py` | Local HTTP/JSON server and startup analytics engine (zero external dependencies). | Python 3 Stdlib |
| `ui/index.html` | Dark-mode single-page application dashboard, audit panel, and pack viewer. | HTML5 / CSS / Vanilla JS |
| `ui/test_analytics.py` | Unit test suite verifying aggregation, heuristics, gap detection, and outliers. | Python `unittest` |
| `api_fetch.log` | API status, rate-limit backoffs, and schema drift warnings. | Logging |

---

## Database Architecture (`library.db`)

The relational database is fully indexed and normalized:

* **`models`**: Core model metadata, prices, image URLs, release month strings (`YYYY-MM`), pre-supported flags, and `is_bonus` indicators.
* **`model_tags`**: Normalized entity tags linked to model IDs (`model_id`, `tag`).
* **`model_files`**: Individual archive and STL parts (`file_id`, `model_id`, `filename`, `size_bytes`, `download_url`).
* **`releases`**: Materialized clusters aggregating models by `(designer_id, release_month)`. Isolates core models from loyalty/bonus models, tallies total payload sizes, and flags likely Patreon/Tribe bundles.
* **`schema_anomalies`**: Audit log capturing upstream MMF API schema changes or unexpected JSON fields.

---

## Prerequisites & Installation

### 1. Node.js Environment

```bash
npm init -y
npm install playwright playwright-extra puppeteer-extra-plugin-stealth
npx playwright install chromium

```

### 2. Python Environment (`uv`)

```bash
uv init
uv add httpx tqdm

```

### 3. Authentication Configuration

Export your active cookies from `myminifactory.com` using browser DevTools into `cookies.json`. Essential cookies:

* `cf_clearance` (Matches your IP and browser TLS fingerprint)
* `PHPSESSID`
* `REMEMBERME`

---

## Operations Guide

### Step 1: Scan Library for Model IDs

Run the browser crawler to scroll your library and populate `model_ids.txt`:

```bash
node scrape.js

```

* Appends new IDs immediately to disk.
* Automatically skips already-discovered IDs upon restart.

### Step 2: Ingest Metadata into Database

#### Live Network Ingestion:

```bash
uv run main.py

```

* Queries `https://www.myminifactory.com/api/v2/objects/{id}`.
* Detects schema drift and logs warnings to `schema_anomalies`.
* Saves raw JSON backups into `metadata/`.
* Inserts records into `library.db`.

#### Offline Ingestion (From Local JSON Backups):

If JSON files are already downloaded in `metadata/`, populate SQLite without making network calls:

```bash
uv run seed_db.py

```

### Step 3: Run the Analytics Dashboard

Launch the zero-dependency local dashboard:

```bash
uv run python ui/server.py

```

Navigate to **`http://localhost:8080`** in your browser.

* **Startup Analytics**: On launch, the server checks column schemas, categorizes bonuses, and refreshes the `releases` cluster table.
* **Monthly Releases Tab**: Groups models by creator and release month. Badges verified Patreon/Tribe packs.
* **Continuity & Gap Audit**: Computes historical coverage from a studio's oldest model to today. Flags:
* **Missing Months**: Hiatuses or scraper drops where 0 models exist for a calendar month.
* **Low Volume Outliers**: Drops where model count falls $\ge 50\%$ below expected average.
* **High Volume Outliers**: Surges where release count exceeds $\mu + 1.5\sigma$ (e.g. Kickstarter fulfillment).


* **Bonus & Reward Separation**: Isolates loyalty rewards, welcome packs, and bonus items so release metrics reflect actual monthly pack cadence.
* **Model Modal**: Click any monthly pack to view all miniatures that belong to that drop.

---

## Running Automated Tests

Run the complete test suite:

```bash
uv run python -m unittest ui/test_analytics.py

```

The test suite validates:

1. File count and cumulative byte size rollups.
2. Patreon/Tribe bundle heuristics ($\ge 4$ models or subscription keywords).
3. Loyalty reward and welcome bonus isolation.
4. Chronological continuity gap calculation across calendar years.
5. Average models per month calculation ($\mu$, $\sigma$).
6. Skew-resistant high and low volume outlier detection.

```

---

### 2. Context Save Point File (`CONTEXT_SAVEPOINT.md`)

```markdown
# CONTEXT SAVE POINT: MyMiniFactory Library Archiver & Analytics

**Current Date Context**: October 2026
**Target Scale**: ~4,868 Models, 100+ Creators, tens of thousands of STLs/archives.

https://www.myminifactory.com/api-doc/index.html

