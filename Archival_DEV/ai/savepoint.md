### 2. Context Save Point File (`CONTEXT_SAVEPOINT.md`)

```markdown
# CONTEXT SAVE POINT: MyMiniFactory Library Archiver & Analytics

**Current Date Context**: October 2026
**Target Scale**: ~4,868 Models, 100+ Creators, tens of thousands of STLs/archives.

---

## 1. Current State & What Has Been Built

1. **Step 1 Scraper (`scrape.js`)**:
   - Uses `playwright-extra` with `puppeteer-extra-plugin-stealth`.
   - Intercepts background JSON payloads and penetrates nested open Shadow DOMs.
   - Parses URLs matching `3d-print-*-(\d+)` and `linkedObject` parameters.
   - Immediately appends discoveries to `model_ids.txt` (numeric only) and syncs `models.json`.

2. **Step 2 Ingestion Pipeline (`main.py` & `seed_db.py`)**:
   - Authenticated against MMF API v2 (`/api/v2/objects/{id}`) using browser cookies (`cookies.json`).
   - Normalizes data into `library.db` using transactions and thread locks.
   - Detects schema drift and unhandled payload keys, logging to `schema_anomalies`.
   - Backs up raw payloads to `metadata/model_<id>.json`.
   - `seed_db.py` allows offline initialization and ingestion straight from the `metadata/` folder.

3. **Database Architecture (`library.db`)**:
   - `models`: `(id, name, url, description, published_at, release_month, is_bonus, designer_id, designer_name, designer_username, primary_category, parent_category, pre_supported, featured, views, likes, price_value, price_currency, primary_image_url, raw_backup_path, updated_at)`
   - `model_tags`: `(model_id, tag)`
   - `model_files`: `(file_id, model_id, filename, size_bytes, download_url, created_at)`
   - `releases`: `(id, designer_name, designer_id, release_month, model_count, bonus_count, total_files, total_size_bytes, is_likely_bundle, sample_image_url, common_tags, computed_at)`
   - `schema_anomalies`: `(id, model_id, anomaly_type, details, detected_at)`

4. **Analytics & UI Engine (`ui/server.py`, `ui/index.html`)**:
   - Python standard library HTTP server (`http.server`) running on port 8080.
   - **Startup Analytics**: Backfills missing `release_month`, flags `is_bonus = 1` for models containing `bonus` or `reward`, and rebuilds `releases`.
   - **Continuity Audit (`/api/audit`)**:
     - Computes timeline from `oldest_month` to `max(latest_month, current_month)`.
     - Identifies missing calendar months.
     - Computes monthly average $\mu$ and sample standard deviation $\sigma$.
     - Detects low outliers ($\text{count} < \mu - 1.5\sigma$ or $\text{count} \le 0.5\mu$ with $\ge 3$ delta).
     - Detects high outliers ($\text{count} > \mu + 1.5\sigma$).
   - **UI**: Dark-mode dashboard with metrics overview, creator directory, monthly release cards, and release inspection modals.

5. **Test Suite (`ui/test_analytics.py`)**:
   - Fully passing `unittest` suite covering data aggregation, bundle classification, bonus segregation, calendar continuity, and anomaly math.

---

## 2. Key Heuristics & Rules

* **Bundle Detection**: Flagged as `is_likely_bundle = 1` if regular models $\ge 4$ OR description/tags contain: `patreon`, `tribe`, `tribes`, `welcome pack`, `bundle`, `release`, `heroes`, `pack`.
* **Bonus / Reward Segregation**: Flagged as `is_bonus = 1` if title or tags match `reward` or `bonus`. These models are incremented in `bonus_count` but excluded from `model_count` to preserve accurate monthly average metrics.
* **Numeric Sanitization**: All IDs must strictly match `^\d+$`. Upstream URLs with `object-` prefixes cause 404s on the MMF API router.

---

## 3. Solved Quirks & Lessons Learned

1. **Cloudflare Fingerprinting**: Direct Python `httpx` requests require exact headers and a valid `cf_clearance` cookie. If Cloudflare challenges increase, fallback to Node.js browser context fetching (`fetch_metadata.js` pattern).
2. **Double Counting in Scraper**: MMF returns library purchase IDs alongside model IDs in some payloads. Ensure extraction targets `object_id` or parses canonical URL patterns.
3. **Outlier Lower-Bound Underflow**: High surges (e.g. 60-model Kickstarter drops) expand $\sigma$, pushing $\mu - 1.5\sigma$ below 0. The outlier formula explicitly combines $\sigma$ thresholds with percentage drops ($\le 0.5\mu$) to catch low-volume drops reliably.

---

## 4. Pending / Next Logical Steps

1. **Downloader Pipeline**: Script to sequentially download physical STL/ZIP assets from `model_files.download_url` into organized directory structures (`Creator/Category/[ID] ModelName/`).
2. **Local Mesh Indexing**: Extracting STLs, calculating triangle counts, and generating local preview renders for slicer-ready asset management.
3. **Automated MMF Sync**: Cron or scheduled runner that checks library counts, runs `scrape.js` for deltas, fetches new metadata, and updates `library.db`.