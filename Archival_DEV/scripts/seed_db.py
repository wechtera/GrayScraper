import json
import logging
from pathlib import Path
import re
import sqlite3
from tqdm import tqdm

DB_FILE = Path("library.db")
METADATA_DIR = Path("metadata")
LOG_FILE = Path("seed_db.log")

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
    handlers=[logging.FileHandler(LOG_FILE, mode="w", encoding="utf-8")],
)
logger = logging.getLogger("seed_db")


def init_database() -> sqlite3.Connection:
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


def inspect_schema_drift(model_id: int, data: dict) -> list[tuple[str, str]]:
    anomalies = []
    incoming_keys = set(data.keys())

    unexpected = incoming_keys - KNOWN_TOP_LEVEL_KEYS
    if unexpected:
        msg = f"New unhandled fields: {sorted(unexpected)}"
        logger.warning(f"Model {model_id}: {msg}")
        anomalies.append(("UNEXPECTED_FIELDS", msg))

    missing_anchors = {"id", "name", "files"} - incoming_keys
    if missing_anchors:
        msg = f"Missing core fields: {sorted(missing_anchors)}"
        logger.warning(f"Model {model_id}: {msg}")
        anomalies.append(("MISSING_CORE_FIELDS", msg))

    return anomalies


def extract_normalized_records(model_id: int, data: dict, backup_path: Path):
    designer_data = data.get("designer") or {}
    designer_id = designer_data.get("id") if isinstance(designer_data, dict) else None
    designer_name = (
        designer_data.get("name")
        or designer_data.get("username")
        or "Unknown Designer"
    ) if isinstance(designer_data, dict) else "Unknown Designer"
    designer_username = designer_data.get("username") if isinstance(designer_data, dict) else None

    primary_category, parent_category = None, None
    categories_obj = data.get("categories") or {}
    if isinstance(categories_obj, dict):
        cat_items = categories_obj.get("items") or []
        if isinstance(cat_items, list) and len(cat_items) > 0 and isinstance(cat_items[0], dict):
            primary_category = cat_items[0].get("name")
            parent_obj = cat_items[0].get("parent") or {}
            if isinstance(parent_obj, dict):
                parent_category = parent_obj.get("name")

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

    price_obj = data.get("price") or {}
    price_val, price_curr = None, None
    if isinstance(price_obj, dict):
        try:
            price_val = float(price_obj.get("value")) if price_obj.get("value") is not None else None
        except (ValueError, TypeError):
            price_val = None
        price_curr = price_obj.get("currency")

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

    tag_rows = []
    raw_tags = data.get("tags") or []
    if isinstance(raw_tags, list):
        for t in raw_tags:
            if isinstance(t, str) and t.strip():
                tag_rows.append((model_id, t.strip().lower()))

    file_rows = []
    files_obj = data.get("files") or {}
    file_items = files_obj.get("items") if isinstance(files_obj, dict) else (files_obj if isinstance(files_obj, list) else [])
    if isinstance(file_items, list):
        for item in file_items:
            if isinstance(item, dict) and item.get("id"):
                try:
                    size_int = int(item.get("size")) if item.get("size") is not None else None
                except (ValueError, TypeError):
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


def main():
    if not METADATA_DIR.exists():
        print(f"Error: Directory '{METADATA_DIR}' not found.")
        return

    json_files = sorted(METADATA_DIR.glob("*.json"))
    if not json_files:
        print(f"No JSON files found in '{METADATA_DIR}/'.")
        return

    print(f"Initializing database '{DB_FILE}'...")
    conn = init_database()

    cursor = conn.cursor()
    cursor.execute("SELECT id FROM models")
    existing_db_ids = {row[0] for row in cursor.fetchall()}
    print(f"Found {len(json_files)} local JSON files. Existing in DB: {len(existing_db_ids)}.")

    models_inserted = 0
    anomalies_detected = 0

    # Process files within a transaction
    with conn:
        for json_path in tqdm(json_files, desc="Populating SQLite", unit="file"):
            try:
                with open(json_path, "r", encoding="utf-8") as f:
                    data = json.load(f)

                raw_id = data.get("id") or re.sub(r"\D", "", json_path.stem)
                if not raw_id:
                    continue
                model_id = int(raw_id)

                anomalies = inspect_schema_drift(model_id, data)
                model_row, tag_rows, file_rows = extract_normalized_records(model_id, data, json_path)

                conn.execute("""
                    INSERT OR REPLACE INTO models (
                        id, name, url, description, published_at, designer_id,
                        designer_name, designer_username, primary_category, parent_category,
                        pre_supported, featured, views, likes, price_value,
                        price_currency, primary_image_url, raw_backup_path
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, model_row)

                conn.execute("DELETE FROM model_tags WHERE model_id = ?", (model_id,))
                if tag_rows:
                    conn.executemany("INSERT OR IGNORE INTO model_tags (model_id, tag) VALUES (?, ?)", tag_rows)

                conn.execute("DELETE FROM model_files WHERE model_id = ?", (model_id,))
                if file_rows:
                    conn.executemany("""
                        INSERT OR REPLACE INTO model_files (
                            file_id, model_id, filename, size_bytes, download_url, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?)
                    """, file_rows)

                if anomalies:
                    anomalies_detected += len(anomalies)
                    conn.executemany("""
                        INSERT INTO schema_anomalies (model_id, anomaly_type, details)
                        VALUES (?, ?, ?)
                    """, [(model_id, a[0], a[1]) for a in anomalies])

                models_inserted += 1

            except Exception as e:
                logger.error(f"Failed processing {json_path.name}: {e}")

    cursor.execute("SELECT count(*) FROM models")
    total_models = cursor.fetchone()[0]
    cursor.execute("SELECT count(*) FROM model_tags")
    total_tags = cursor.fetchone()[0]
    cursor.execute("SELECT count(*) FROM model_files")
    total_files = cursor.fetchone()[0]
    conn.close()

    print("\nDatabase Population Summary:")
    print(f"- Processed & Indexed: {models_inserted} models")
    print(f"- Total Models in DB:  {total_models}")
    print(f"- Total Indexed Tags:  {total_tags}")
    print(f"- Total Model Files:   {total_files}")
    if anomalies_detected > 0:
        print(f"- Schema Drift Warnings: {anomalies_detected} (logged to 'schema_anomalies' and '{LOG_FILE}')")


if __name__ == "__main__":
    main()
