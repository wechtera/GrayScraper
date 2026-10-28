import asyncio
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import httpx
from tqdm.asyncio import tqdm

# --- Configuration ---
CONCURRENCY_LIMIT = 5
REQUEST_DELAY = 0.15
DB_FILE = Path("library.db")
METADATA_DIR = Path("metadata")
MODEL_IDS_FILE = Path("model_ids.txt")
COOKIES_FILE = Path("cookies.json")
LOG_FILE = Path("api_fetch.log")

# Canonical v2 schema baseline used to detect payload drift
KNOWN_TOP_LEVEL_KEYS = {
    "id", "url", "name", "visibility", "visibility_name", "listed",
    "status", "status_name", "description", "description_html",
    "printing_details", "printing_details_html", "featured", "support",
    "complexity", "complexity_name", "dimensions", "material_quantity",
    "images", "views", "likes", "published_at", "tags", "licenses",
    "filters", "file_mode", "categories", "files", "prints", "designer",
    "license", "archive_download_url", "is_liked", "user_collections",
    "is_saved", "price", "purchase_url", "is_bought"
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, mode="w", encoding="utf-8"),
    ],
)
logger = logging.getLogger("mmf_pipeline")


def init_database() -> sqlite3.Connection:
    """Initializes tables and returns a connection with foreign keys enabled."""
    conn = sqlite3.connect(DB_FILE)
    conn.execute("PRAGMA foreign_keys = ON;")
    with conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS models (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                url TEXT,
                description TEXT,
                published_at TEXT,
                designer_id INTEGER,
                designer_name TEXT,
                designer_username TEXT,
                primary_category TEXT,
                parent_category TEXT,
                pre_supported BOOLEAN DEFAULT 0,
                featured BOOLEAN DEFAULT 0,
                views INTEGER DEFAULT 0,
                likes INTEGER DEFAULT 0,
                price_value REAL,
                price_currency TEXT,
                primary_image_url TEXT,
                raw_backup_path TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS model_tags (
                model_id INTEGER,
                tag TEXT NOT NULL,
                PRIMARY KEY (model_id, tag),
                FOREIGN KEY (model_id) REFERENCES models(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS model_files (
                file_id INTEGER PRIMARY KEY,
                model_id INTEGER NOT NULL,
                filename TEXT,
                size_bytes INTEGER,
                download_url TEXT,
                created_at TEXT,
                FOREIGN KEY (model_id) REFERENCES models(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS schema_anomalies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                model_id INTEGER NOT NULL,
                anomaly_type TEXT NOT NULL,
                details TEXT NOT NULL,
                detected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_models_designer ON models(designer_name);
            CREATE INDEX IF NOT EXISTS idx_models_category ON models(primary_category);
            CREATE INDEX IF NOT EXISTS idx_tags_tag ON model_tags(tag);
            CREATE INDEX IF NOT EXISTS idx_files_model ON model_files(model_id);
        """)
    return conn


def load_cookies() -> dict:
    if not COOKIES_FILE.exists():
        return {}
    with open(COOKIES_FILE, "r", encoding="utf-8") as f:
        raw = json.load(f)
    return {c["name"]: c["value"] for c in raw if "name" in c and "value" in c}


def inspect_schema_drift(model_id: int, data: dict) -> list[tuple[str, str]]:
    """Checks for structural anomalies or unhandled keys and logs warnings."""
    anomalies = []
    incoming_keys = set(data.keys())

    # Detect unexpected new keys introduced by MMF
    unexpected = incoming_keys - KNOWN_TOP_LEVEL_KEYS
    if unexpected:
        msg = f"New unhandled fields: {sorted(unexpected)}"
        logger.warning(f"Model {model_id}: {msg}")
        anomalies.append(("UNEXPECTED_FIELDS", msg))

    # Detect missing expected structural anchors
    missing_anchors = {"id", "name", "files"} - incoming_keys
    if missing_anchors:
        msg = f"Missing core fields: {sorted(missing_anchors)}"
        logger.warning(f"Model {model_id}: {msg}")
        anomalies.append(("MISSING_CORE_FIELDS", msg))

    return anomalies


def extract_normalized_records(model_id: int, data: dict, backup_path: Path):
    """
    Extracts every field supported by the SQLite schema.
    Uses defensive lookups so malformed payloads do not abort ingestion.
    """
    # 1. Designer details
    designer_data = data.get("designer") or {}
    designer_id = designer_data.get("id") if isinstance(designer_data, dict) else None
    designer_name = (
        designer_data.get("name")
        or designer_data.get("username")
        or "Unknown Designer"
    ) if isinstance(designer_data, dict) else "Unknown Designer"
    designer_username = designer_data.get("username") if isinstance(designer_data, dict) else None

    # 2. Categories
    primary_category, parent_category = None, None
    categories_obj = data.get("categories") or {}
    if isinstance(categories_obj, dict):
        cat_items = categories_obj.get("items") or []
        if isinstance(cat_items, list) and len(cat_items) > 0 and isinstance(cat_items[0], dict):
            primary_category = cat_items[0].get("name")
            parent_obj = cat_items[0].get("parent") or {}
            if isinstance(parent_obj, dict):
                parent_category = parent_obj.get("name")

    # 3. Primary image resolution
    primary_image_url = None
    images = data.get("images") or []
    if isinstance(images, list) and len(images) > 0:
        target_img = next((img for img in images if isinstance(img, dict) and img.get("is_primary")), images[0])
        if isinstance(target_img, dict):
            primary_image_url = (
                (target_img.get("large") or {}).get("url")
                or (target_img.get("standard") or {}).get("url")
                or (target_img.get("original") or {}).get("url")
                or (target_img.get("thumbnail") or {}).get("url")
            )

    # 4. Pricing
    price_obj = data.get("price") or {}
    price_val, price_curr = None, None
    if isinstance(price_obj, dict):
        try:
            price_val = float(price_obj.get("value")) if price_obj.get("value") is not None else None
        except ValueError:
            price_val = None
        price_curr = price_obj.get("currency")

    # 5. Core model record
    model_row = (
        model_id,
        data.get("name") or f"Model {model_id}",
        data.get("url"),
        data.get("description"),
        data.get("published_at"),
        designer_id,
        designer_name,
        designer_username,
        primary_category,
        parent_category,
        1 if data.get("support") else 0,
        1 if data.get("featured") else 0,
        data.get("views") or 0,
        data.get("likes") or 0,
        price_val,
        price_curr,
        primary_image_url,
        str(backup_path),
    )

    # 6. Tags
    tag_rows = []
    raw_tags = data.get("tags") or []
    if isinstance(raw_tags, list):
        for t in raw_tags:
            if isinstance(t, str) and t.strip():
                tag_rows.append((model_id, t.strip().lower()))

    # 7. Files
    file_rows = []
    files_obj = data.get("files") or {}
    file_items = files_obj.get("items") if isinstance(files_obj, dict) else (files_obj if isinstance(files_obj, list) else [])
    if isinstance(file_items, list):
        for item in file_items:
            if isinstance(item, dict) and item.get("id"):
                try:
                    size_int = int(item.get("size")) if item.get("size") is not None else None
                except ValueError:
                    size_int = None
                file_rows.append((
                    item["id"],
                    model_id,
                    item.get("filename") or item.get("path"),
                    size_int,
                    item.get("download_url"),
                    item.get("created_at"),
                ))

    return model_row, tag_rows, file_rows


async def process_model(
    client: httpx.AsyncClient,
    model_id: int,
    db_conn: sqlite3.Connection,
    db_lock: asyncio.Lock,
    semaphore: asyncio.Semaphore,
) -> bool:
    backup_file = METADATA_DIR / f"model_{model_id}.json"
    url = f"https://www.myminifactory.com/api/v2/objects/{model_id}"
    data = None

    # Load from backup JSON if it already exists; otherwise fetch from network
    if backup_file.exists() and backup_file.stat().st_size > 0:
        try:
            with open(backup_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = None

    if data is None:
        async with semaphore:
            for attempt in range(1, 4):
                try:
                    res = await client.get(url, timeout=20.0)
                    if res.status_code == 200:
                        data = res.json()
                        # Always persist the raw payload before modifying the database
                        with open(backup_file, "w", encoding="utf-8") as f:
                            json.dump(data, f, indent=2)
                        await asyncio.sleep(REQUEST_DELAY)
                        break
                    elif res.status_code == 429:
                        wait = int(res.headers.get("Retry-After", 5))
                        logger.warning(f"Rate limited (429) on {model_id}. Waiting {wait}s...")
                        await asyncio.sleep(wait)
                        continue
                    elif res.status_code in (403, 404):
                        logger.warning(f"Model {model_id} returned HTTP {res.status_code}")
                        return False
                except Exception as e:
                    logger.error(f"Error fetching {model_id} (attempt {attempt}/3): {e}")
                    await asyncio.sleep(1.5 * attempt)

    if not data:
        return False

    # Check for schema drift
    anomalies = inspect_schema_drift(model_id, data)
    model_row, tag_rows, file_rows = extract_normalized_records(model_id, data, backup_file)

    # Thread-safe database transaction
    async with db_lock:
        try:
            with db_conn:
                db_conn.execute("""
                    INSERT OR REPLACE INTO models (
                        id, name, url, description, published_at, designer_id,
                        designer_name, designer_username, primary_category, parent_category,
                        pre_supported, featured, views, likes, price_value,
                        price_currency, primary_image_url, raw_backup_path
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, model_row)

                db_conn.execute("DELETE FROM model_tags WHERE model_id = ?", (model_id,))
                if tag_rows:
                    db_conn.executemany("INSERT OR IGNORE INTO model_tags (model_id, tag) VALUES (?, ?)", tag_rows)

                db_conn.execute("DELETE FROM model_files WHERE model_id = ?", (model_id,))
                if file_rows:
                    db_conn.executemany("""
                        INSERT OR REPLACE INTO model_files (
                            file_id, model_id, filename, size_bytes, download_url, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?)
                    """, file_rows)

                if anomalies:
                    db_conn.executemany("""
                        INSERT INTO schema_anomalies (model_id, anomaly_type, details)
                        VALUES (?, ?, ?)
                    """, [(model_id, anom[0], anom[1]) for anom in anomalies])

            return True
        except sqlite3.Error as e:
            logger.error(f"Database error writing model {model_id}: {e}")
            return False


async def main():
    if not MODEL_IDS_FILE.exists():
        print(f"Error: {MODEL_IDS_FILE} not found. Run step 1 (scrape.js) first.")
        return

    METADATA_DIR.mkdir(exist_ok=True)
    conn = init_database()
    db_lock = asyncio.Lock()

    # Read current state from the database
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM models")
    existing_db_ids = {row[0] for row in cursor.fetchall()}

    # Read incoming IDs from step 1
    with open(MODEL_IDS_FILE, "r", encoding="utf-8") as f:
        incoming_ids = [
            int(digits) for line in f
            if (digits := re.sub(r"\D", "", line.strip()))
        ]

    # Calculate delta
    new_ids = [mid for mid in incoming_ids if mid not in existing_db_ids]

    print(f"Total IDs in delta file: {len(incoming_ids)}")
    print(f"Existing models in DB:   {len(existing_db_ids)}")
    print(f"New models to ingest:    {len(new_ids)}")

    if not new_ids:
        print("Database is up-to-date with current delta file.")
        conn.close()
        return

    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        "Accept": "application/json",
        "Referer": "https://www.myminifactory.com/library",
    }
    cookies = load_cookies()
    semaphore = asyncio.Semaphore(CONCURRENCY_LIMIT)

    async with httpx.AsyncClient(headers=headers, cookies=cookies, follow_redirects=True) as client:
        tasks = [
            process_model(client, mid, conn, db_lock, semaphore)
            for mid in new_ids
        ]
        results = await tqdm.gather(*tasks, desc="Ingesting Models into SQLite", unit="model")

    successful = sum(1 for r in results if r)
    cursor.execute("SELECT count(*) FROM models")
    total_now = cursor.fetchone()[0]

    cursor.execute("SELECT count(*) FROM schema_anomalies")
    total_anomalies = cursor.fetchone()[0]

    conn.close()

    print(f"\nIngestion Complete: {successful}/{len(new_ids)} new models inserted.")
    print(f"Total models in library.db: {total_now}")
    if total_anomalies > 0:
        print(f"Notice: {total_anomalies} schema warnings logged to 'schema_anomalies' and '{LOG_FILE}'.")


if __name__ == "__main__":
    asyncio.run(main())