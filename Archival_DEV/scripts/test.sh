uv run python -c "
import sqlite3
from ui.services.db import init_persistent_tables
conn = sqlite3.connect('library.db')
init_persistent_tables(conn)
cols = [r[1] for r in conn.execute('PRAGMA table_info(pipeline_tasks)').fetchall()]
assert 'task_type' in cols and 'progress_pct' in cols and 'artifacts_json' in cols, f'Missing cols: {cols}'
setting = conn.execute(\"SELECT value FROM app_settings WHERE key = 'metadata_backup_path'\").fetchone()
assert setting is not None, 'metadata_backup_path setting missing'
print('✓ Database schema and app_settings verified!')
"
