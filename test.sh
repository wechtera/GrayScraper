# 1. Inspect table schemas (check which columns are PRIMARY KEY)
sqlite3 library.db ".schema models"
sqlite3 library.db ".schema model_files"
sqlite3 library.db ".schema downloaded_files"
sqlite3 library.db ".schema external_libraries"

# 2. Check sample rows to see what values are populating id / file_id
sqlite3 library.db "SELECT id, name, source, vault_path FROM models LIMIT 5;"
sqlite3 library.db "SELECT file_id, model_id, filename FROM model_files LIMIT 5;"
sqlite3 library.db "SELECT file_id, model_id, cleaned_filename, storage_tier FROM downloaded_files LIMIT 5;"
