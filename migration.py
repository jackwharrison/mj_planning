"""
One-time migration: adds new columns to the recipe table.
Run once with: python migrate.py
"""
import sqlite3, os

db_path = os.environ.get('DATABASE_URL', '').replace('sqlite:///', '') or 'jm.db'

# If DATA_DIR is set (Render), look there
data_dir = os.environ.get('DATA_DIR', '')
if data_dir:
    db_path = os.path.join(data_dir, 'jm.db')

print(f'Migrating: {db_path}')

conn = sqlite3.connect(db_path)
cur  = conn.cursor()

# Get existing columns
cur.execute("PRAGMA table_info(recipe)")
existing = {row[1] for row in cur.fetchall()}
print(f'Existing columns: {existing}')

migrations = [
    ("star_rating", "INTEGER DEFAULT 0"),
    ("kcal",        "INTEGER"),
    ("protein_g",   "REAL"),
    ("carbs_g",     "REAL"),
    ("fat_g",       "REAL"),
]

for col, typedef in migrations:
    if col not in existing:
        cur.execute(f"ALTER TABLE recipe ADD COLUMN {col} {typedef}")
        print(f'  + Added column: {col}')
    else:
        print(f'  . Skipped (exists): {col}')

conn.commit()
conn.close()
print('Done!')