from datetime import datetime
import math
import sqlite3
import unittest


def init_test_db(conn: sqlite3.Connection):
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.executescript("""
        CREATE TABLE models (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            designer_name TEXT,
            designer_id INTEGER,
            release_month TEXT,
            description TEXT,
            is_bonus BOOLEAN DEFAULT 0,
            primary_image_url TEXT
        );

        CREATE TABLE model_files (
            file_id INTEGER PRIMARY KEY,
            model_id INTEGER NOT NULL,
            filename TEXT,
            size_bytes INTEGER,
            FOREIGN KEY (model_id) REFERENCES models(id) ON DELETE CASCADE
        );

        CREATE TABLE model_tags (
            model_id INTEGER,
            tag TEXT NOT NULL,
            PRIMARY KEY (model_id, tag),
            FOREIGN KEY (model_id) REFERENCES models(id) ON DELETE CASCADE
        );

        CREATE TABLE releases (
            id TEXT PRIMARY KEY,
            designer_name TEXT NOT NULL,
            designer_id INTEGER,
            release_month TEXT NOT NULL,
            model_count INTEGER NOT NULL,
            bonus_count INTEGER NOT NULL DEFAULT 0,
            total_files INTEGER NOT NULL,
            total_size_bytes INTEGER NOT NULL,
            is_likely_bundle BOOLEAN DEFAULT 0,
            sample_image_url TEXT,
            common_tags TEXT
        );
    """)


def flag_bonuses_and_compute_releases(conn: sqlite3.Connection):
    conn.execute("""
        UPDATE models
        SET is_bonus = 1
        WHERE LOWER(name) LIKE '%reward%'
           OR LOWER(name) LIKE '%bonus%'
           OR id IN (
               SELECT model_id FROM model_tags 
               WHERE LOWER(tag) LIKE '%reward%' OR LOWER(tag) LIKE '%bonus%'
           );
    """)

    conn.execute("DELETE FROM releases;")
    cluster_query = """
        SELECT 
            COALESCE(m.designer_id, 0) || '_' || m.release_month AS release_id,
            m.designer_name,
            m.designer_id,
            m.release_month,
            COUNT(DISTINCT CASE WHEN m.is_bonus = 0 THEN m.id END) AS regular_model_count,
            COUNT(DISTINCT CASE WHEN m.is_bonus = 1 THEN m.id END) AS bonus_model_count,
            COUNT(f.file_id) AS total_files,
            COALESCE(SUM(f.size_bytes), 0) AS total_size_bytes,
            MAX(m.primary_image_url) AS sample_image_url,
            GROUP_CONCAT(DISTINCT t.tag) AS all_tags,
            GROUP_CONCAT(m.description) AS all_descriptions
        FROM models m
        LEFT JOIN model_files f ON m.id = f.model_id
        LEFT JOIN model_tags t ON m.id = t.model_id
        WHERE m.release_month IS NOT NULL AND m.release_month != ''
        GROUP BY m.designer_name, m.release_month;
    """
    clusters = conn.execute(cluster_query).fetchall()
    bundle_keywords = {"patreon", "tribe", "tribes", "welcome pack", "bundle", "release", "heroes", "pack"}

    release_rows = []
    for c in clusters:
        reg_count = c[4] or 0
        bonus_count = c[5] or 0
        if reg_count == 0 and bonus_count == 0:
            continue

        tags_str = c[9] or ""
        desc_str = (c[10] or "").lower()
        combined_text = f"{tags_str.lower()} {desc_str}"
        is_bundle = 1 if (reg_count >= 4 or any(kw in combined_text for kw in bundle_keywords)) else 0

        release_rows.append((
            c[0], c[1], c[2], c[3], reg_count, bonus_count, c[6], c[7], is_bundle, c[8], tags_str
        ))

    conn.executemany("""
        INSERT INTO releases (
            id, designer_name, designer_id, release_month,
            model_count, bonus_count, total_files, total_size_bytes,
            is_likely_bundle, sample_image_url, common_tags
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, release_rows)


def detect_missing_months(start_month: str, end_month: str, present_months: list[str]) -> list[str]:
    start_dt = datetime.strptime(start_month, "%Y-%m")
    end_dt = datetime.strptime(end_month, "%Y-%m")
    
    expected = []
    cur_y, cur_m = start_dt.year, start_dt.month
    end_y, end_m = end_dt.year, end_dt.month
    
    while (cur_y, cur_m) <= (end_y, end_m):
        expected.append(f"{cur_y:04d}-{cur_m:02d}")
        cur_m += 1
        if cur_m > 12:
            cur_m = 1
            cur_y += 1

    present_set = set(present_months)
    return [m for m in expected if m not in present_set]


def calculate_metrics_and_anomalies(counts_by_month: dict[str, int]):
    counts = list(counts_by_month.values())
    n = len(counts)
    if n == 0:
        return 0.0, 0.0, [], []

    avg = sum(counts) / n
    variance = sum((c - avg) ** 2 for c in counts) / (n - 1) if n > 1 else 0.0
    std_dev = math.sqrt(variance)
    sensitivity = max(1.5 * std_dev, 3.0)

    highs = [m for m, cnt in counts_by_month.items() if cnt > avg + sensitivity]
    
    # Flag if drops below standard threshold OR drops by >= 50% from average with at least a 3-unit delta
    lows = [
        m for m, cnt in counts_by_month.items()
        if (cnt < avg - sensitivity) or (cnt <= (avg * 0.5) and (avg - cnt) >= 3.0)
    ]
    return round(avg, 2), round(std_dev, 2), highs, lows


class TestReleaseAnalytics(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        init_test_db(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_file_counts_and_sizes_aggregation(self):
        self.conn.execute("INSERT INTO models (id, name, designer_name, designer_id, release_month, description) VALUES (1, 'Warrior', 'Rescale Miniatures', 101, '2024-05', 'Warrior')")
        self.conn.execute("INSERT INTO models (id, name, designer_name, designer_id, release_month, description) VALUES (2, 'Mage', 'Rescale Miniatures', 101, '2024-05', 'Mage')")
        
        self.conn.execute("INSERT INTO model_files (file_id, model_id, filename, size_bytes) VALUES (10, 1, 'warrior.stl', 10485760)")
        self.conn.execute("INSERT INTO model_files (file_id, model_id, filename, size_bytes) VALUES (11, 1, 'warrior_sup.stl', 26214400)")
        self.conn.execute("INSERT INTO model_files (file_id, model_id, filename, size_bytes) VALUES (12, 2, 'mage.zip', 15728640)")

        flag_bonuses_and_compute_releases(self.conn)

        row = self.conn.execute("SELECT model_count, total_files, total_size_bytes FROM releases WHERE id = '101_2024-05'").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], 2)
        self.assertEqual(row[1], 3)
        self.assertEqual(row[2], 52428800)

    def test_bundle_heuristic_by_model_count_threshold(self):
        for i in range(1, 5):
            self.conn.execute(f"INSERT INTO models (id, name, designer_name, designer_id, release_month, description) VALUES ({i}, 'Prop {i}', 'PropStudio', 202, '2024-06', 'Basic prop')")
        
        flag_bonuses_and_compute_releases(self.conn)

        row = self.conn.execute("SELECT is_likely_bundle FROM releases WHERE id = '202_2024-06'").fetchone()
        self.assertEqual(row[0], 1)

    def test_bonus_and_reward_isolation(self):
        self.conn.execute("INSERT INTO models (id, name, designer_name, designer_id, release_month, description) VALUES (1, 'Warrior', 'Studio', 10, '2024-05', 'Hero')")
        self.conn.execute("INSERT INTO models (id, name, designer_name, designer_id, release_month, description) VALUES (2, 'Archer', 'Studio', 10, '2024-05', 'Hero')")
        self.conn.execute("INSERT INTO models (id, name, designer_name, designer_id, release_month, description) VALUES (3, 'Mage', 'Studio', 10, '2024-05', 'Hero')")
        self.conn.execute("INSERT INTO models (id, name, designer_name, designer_id, release_month, description) VALUES (4, '3-Month Loyalty Reward Dragon', 'Studio', 10, '2024-05', 'Loyalty drop')")
        self.conn.execute("INSERT INTO models (id, name, designer_name, designer_id, release_month, description) VALUES (5, 'Mimic Chest', 'Studio', 10, '2024-05', 'Chest')")
        self.conn.execute("INSERT INTO model_tags (model_id, tag) VALUES (5, 'patreon bonus')")

        flag_bonuses_and_compute_releases(self.conn)

        row = self.conn.execute("SELECT model_count, bonus_count FROM releases WHERE id = '10_2024-05'").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], 3)
        self.assertEqual(row[1], 2)

    def test_gap_continuity_detects_missing_month(self):
        present = ["2025-01", "2025-02", "2025-04", "2025-05"]
        missing = detect_missing_months("2025-01", "2025-05", present)
        self.assertEqual(missing, ["2025-03"])

    def test_average_models_per_month_metric(self):
        data = {
            "2024-01": 20,
            "2024-02": 22,
            "2024-03": 18,
            "2024-04": 20
        }
        avg, std_dev, highs, lows = calculate_metrics_and_anomalies(data)
        self.assertEqual(avg, 20.0)
        self.assertEqual(std_dev, 1.63)
        self.assertEqual(highs, [])
        self.assertEqual(lows, [])

    def test_outlier_detection_high_and_low(self):
        data = {
            "2024-01": 20,
            "2024-02": 19,
            "2024-03": 2,   # Drop: low anomaly
            "2024-04": 21,
            "2024-05": 60,  # Surge: high anomaly
            "2024-06": 20
        }
        avg, std_dev, highs, lows = calculate_metrics_and_anomalies(data)
        self.assertIn("2024-05", highs, "60 models must trigger a high outlier flag")
        self.assertIn("2024-03", lows, "2 models must trigger a low outlier flag")


if __name__ == "__main__":
    unittest.main()
