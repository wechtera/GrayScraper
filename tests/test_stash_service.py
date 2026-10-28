"""
tests/test_stash_service.py - Isolated automated test suite for StashService ("A pile of gray").

Test Coverage:
1. Schema creation and migration idempotency.
2. Stash creation, description defaults ("A pile of gray"), and active flag assignment.
3. Immediate DB commit and persistence of active stash across simulated reconnects.
4. Polymorphic insertion across all 4 tiers (model, file, submodel, archive_member).
5. Duplicate prevention enforcing UNIQUE constraints and returning friendly rejection.
6. Bulk model insertion with mixed new and duplicate items.
7. Deletion safety: verifying stash item removal NEVER unlinks files on disk.
8. Active pointer fallback when an active stash is deleted.
"""

from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

# Ensure repository root is on sys.path
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Multi-path import resolution matching repository layout
try:
    from ui.services.stash_service import StashService
except ImportError:
    try:
        from services.stash_service import StashService
    except ImportError:
        raise ImportError("Cannot import StashService from ui.services or services.")


class TestStashServiceIsolated(unittest.TestCase):
    def setUp(self):
        # 1. Create fully isolated transient sandbox
        self.temp_dir_obj = tempfile.TemporaryDirectory()
        self.sandbox = Path(self.temp_dir_obj.name).resolve()
        self.db_path = self.sandbox / "test_library.db"

        # 2. Seed parent catalog tables required for foreign key constraints
        self._init_test_database()

        # 3. Instantiate StashService bound exclusively to the sandbox database
        self.svc = StashService(db_path=self.db_path)

    def tearDown(self):
        self.temp_dir_obj.cleanup()

    def _init_test_database(self):
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute("PRAGMA foreign_keys = ON;")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS models (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    designer_name TEXT DEFAULT 'Archvillain Games',
                    designer_username TEXT DEFAULT 'archvillain',
                    primary_category TEXT DEFAULT 'Monsters',
                    release_month TEXT DEFAULT '2026-10',
                    published_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    is_bonus BOOLEAN DEFAULT 0,
                    pre_supported BOOLEAN DEFAULT 1,
                    primary_image_url TEXT DEFAULT '',
                    url TEXT DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS model_files (
                    file_id INTEGER PRIMARY KEY,
                    model_id INTEGER NOT NULL,
                    filename TEXT NOT NULL,
                    size_bytes INTEGER DEFAULT 0,
                    download_url TEXT DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (model_id) REFERENCES models(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS file_archive_submodels (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    file_id INTEGER NOT NULL,
                    model_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    relative_dir TEXT,
                    preview_image_path TEXT,
                    total_files INTEGER DEFAULT 0,
                    total_bytes INTEGER DEFAULT 0,
                    has_presupported BOOLEAN DEFAULT 0,
                    has_unsupported BOOLEAN DEFAULT 0,
                    has_slicer_project BOOLEAN DEFAULT 0,
                    formats_json TEXT DEFAULT '[]',
                    scales_json TEXT DEFAULT '[]',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (file_id) REFERENCES model_files(file_id) ON DELETE CASCADE,
                    FOREIGN KEY (model_id) REFERENCES models(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS file_archive_members (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    submodel_id INTEGER,
                    file_id INTEGER NOT NULL,
                    internal_path TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    size_bytes INTEGER DEFAULT 0,
                    is_supported BOOLEAN DEFAULT 0,
                    file_extension TEXT DEFAULT '.stl',
                    FOREIGN KEY (submodel_id) REFERENCES file_archive_submodels(id) ON DELETE SET NULL,
                    FOREIGN KEY (file_id) REFERENCES model_files(file_id) ON DELETE CASCADE
                );

                -- Seed catalog data for testing
                INSERT INTO models (id, name, designer_name)
                VALUES (101, 'Vampire Lord', 'Artisan Guild'),
                       (102, 'Ghul Pack', 'Artisan Guild'),
                       (103, 'Dire Wolf', 'Artisan Guild');

                INSERT INTO model_files (file_id, model_id, filename, size_bytes)
                VALUES (501, 101, 'Vampire_Lord.zip', 150000000);

                INSERT INTO file_archive_submodels (id, file_id, model_id, name, total_bytes, total_files)
                VALUES (1, 501, 101, 'Vampire Lord Mounted', 75000000, 3);

                INSERT INTO file_archive_members (id, submodel_id, file_id, internal_path, filename, size_bytes, is_supported)
                VALUES (1001, 1, 501, 'Files/Vampire_Sword.stl', 'Vampire_Sword.stl', 12000000, 1);
            """)

    def test_schema_creation_and_idempotency(self):
        """Verifies schema creates necessary tables and can be called repeatedly without error."""
        self.svc.ensure_schema()
        self.svc.ensure_schema()

        with sqlite3.connect(str(self.db_path)) as conn:
            tables = {
                r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table';"
                ).fetchall()
            }
            self.assertIn("stashes", tables)
            self.assertIn("stash_items", tables)

            indices = {
                r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index';"
                ).fetchall()
            }
            self.assertIn("idx_stashes_active", indices)
            self.assertIn("idx_stash_items_unique", indices)

    def test_stash_creation_defaults_and_active_flag(self):
        """Validates default description 'A pile of gray', naming, and active flag assignment."""
        s1 = self.svc.create_stash("Batch 1")
        self.assertEqual(s1["name"], "Batch 1")
        self.assertEqual(s1["description"], "A pile of gray")
        self.assertFalse(s1["is_active"])

        s2 = self.svc.create_stash("Batch 2", description="Custom Notes", make_active=True)
        self.assertEqual(s2["name"], "Batch 2")
        self.assertEqual(s2["description"], "Custom Notes")
        self.assertTrue(s2["is_active"])

        # Creating s3 with make_active=True must demote s2
        s3 = self.svc.create_stash("Batch 3", make_active=True)
        self.assertTrue(s3["is_active"])

        active = self.svc.get_active_stash()
        self.assertIsNotNone(active)
        self.assertEqual(active["id"], s3["id"])

    def test_active_stash_persistence_across_reconnect(self):
        """Verifies active stash setting commits immediately and persists across new connection sessions."""
        stash = self.svc.create_stash("Persistent Stash", make_active=True)
        self.assertTrue(stash["is_active"])

        # Instantiate completely new service instance referencing the same SQLite database file
        new_svc_session = StashService(db_path=self.db_path)
        persisted_active = new_svc_session.get_active_stash()

        self.assertIsNotNone(persisted_active)
        self.assertEqual(persisted_active["id"], stash["id"])
        self.assertEqual(persisted_active["name"], "Persistent Stash")
        self.assertTrue(persisted_active["is_active"])

    def test_polymorphic_insertion_all_four_tiers(self):
        """Ensures polymorphic items across model, file, submodel, and archive_member can be added and resolved."""
        stash = self.svc.create_stash("Full Manifest", make_active=True)
        sid = stash["id"]

        # Tier 1: Entire Model
        res1 = self.svc.add_to_stash(sid, "model", model_id=101)
        self.assertTrue(res1["success"])

        # Tier 2: File Archive
        res2 = self.svc.add_to_stash(sid, "file", model_id=101, file_id=501)
        self.assertTrue(res2["success"])

        # Tier 3: Submodel Miniature
        res3 = self.svc.add_to_stash(sid, "submodel", model_id=101, file_id=501, submodel_id=1)
        self.assertTrue(res3["success"])

        # Tier 4: Raw Archive Member Mesh
        res4 = self.svc.add_to_stash(sid, "archive_member", model_id=101, file_id=501, archive_member_id=1001)
        self.assertTrue(res4["success"])

        detail = self.svc.get_stash_detail(sid)
        self.assertEqual(detail["total_items"], 4)
        self.assertEqual(detail["models_count"], 1)
        self.assertEqual(detail["files_count"], 1)
        self.assertEqual(detail["submodels_count"], 1)
        self.assertEqual(detail["archive_members_count"], 1)

        types = [item["item_type"] for item in detail["items"]]
        self.assertIn("model", types)
        self.assertIn("file", types)
        self.assertIn("submodel", types)
        self.assertIn("archive_member", types)

    def test_duplicate_prevention_friendly_rejection(self):
        """Verifies duplicate additions return already_exists=True with a friendly notification."""
        stash = self.svc.create_stash("Duplicate Test Stash")
        sid = stash["id"]

        first_add = self.svc.add_to_stash(sid, "model", model_id=102)
        self.assertTrue(first_add["success"])
        self.assertFalse(first_add["already_exists"])

        # Attempt to add exact same line item
        second_add = self.svc.add_to_stash(sid, "model", model_id=102)
        self.assertFalse(second_add["success"])
        self.assertTrue(second_add["already_exists"])
        self.assertIn("pending paint", second_add["message"].lower())

        # Verify only 1 line item exists in the database
        detail = self.svc.get_stash_detail(sid)
        self.assertEqual(detail["total_items"], 1)

    def test_bulk_add_models_mixed_new_and_duplicates(self):
        """Verifies bulk_add_models commits new items, skips duplicates, and returns accurate tallies."""
        stash = self.svc.create_stash("Bulk Test")
        sid = stash["id"]

        # Pre-seed model 101 into the stash
        self.svc.add_to_stash(sid, "model", model_id=101)

        # Bulk add models [101, 102, 103]
        report = self.svc.bulk_add_models(sid, [101, 102, 103])
        self.assertTrue(report["success"])
        self.assertEqual(report["added"], 2)
        self.assertEqual(report["duplicates"], 1)
        self.assertEqual(report["total"], 3)

        detail = self.svc.get_stash_detail(sid)
        self.assertEqual(detail["total_items"], 3)

    def test_deletion_safety_never_unlinks_files_on_disk(self):
        """Guarantees that deleting a stash item or stash manifest NEVER touches physical files on disk."""
        vault_file = self.sandbox / "storage_vault" / "models" / "Vampire_Lord.zip"
        vault_file.parent.mkdir(parents=True, exist_ok=True)
        vault_file.write_bytes(b"Simulated multi-gigabyte production 3D asset payload")

        stash = self.svc.create_stash("Deletion Safety Stash")
        sid = stash["id"]

        add_res = self.svc.add_to_stash(sid, "file", model_id=101, file_id=501)
        item_id = add_res["item_id"]

        # 1. Remove individual stash line item
        removed = self.svc.remove_from_stash(item_id)
        self.assertTrue(removed)
        self.assertTrue(vault_file.exists(), "Original physical file must remain intact on disk after item deletion")

        # 2. Add back and delete entire stash manifest
        self.svc.add_to_stash(sid, "file", model_id=101, file_id=501)
        stash_deleted = self.svc.delete_stash(sid)
        self.assertTrue(stash_deleted)
        self.assertTrue(vault_file.exists(), "Original physical file must remain intact on disk after stash deletion")

    def test_active_pointer_fallback_on_delete(self):
        """Verifies that deleting the currently active stash clears the active pointer without crashing."""
        active_stash = self.svc.create_stash("To Be Deleted", make_active=True)
        self.assertEqual(self.svc.get_active_stash()["id"], active_stash["id"])

        self.svc.delete_stash(active_stash["id"])

        # Active stash should gracefully resolve to None
        self.assertIsNone(self.svc.get_active_stash())

        # Remaining stashes are intact
        other_stash = self.svc.create_stash("Remaining Stash")
        stashes = self.svc.list_stashes()
        self.assertEqual(len(stashes), 1)
        self.assertEqual(stashes[0]["id"], other_stash["id"])

    def test_invalid_item_type_rejection(self):
        """Guarantees invalid item_type raises ValueError and does not commit corrupt state."""
        stash = self.svc.create_stash("Validation Stash")
        with self.assertRaises(ValueError) as ctx:
            self.svc.add_to_stash(stash["id"], "invalid_tier", model_id=101)
        self.assertIn("Invalid item_type", str(ctx.exception))

    def test_nonexistent_foreign_keys_rejection(self):
        """Guarantees foreign key references to nonexistent models or files raise ValueError."""
        stash = self.svc.create_stash("FK Validation Stash")
        with self.assertRaises(ValueError) as ctx:
            self.svc.add_to_stash(stash["id"], "model", model_id=999999)
        self.assertIn("does not exist", str(ctx.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
