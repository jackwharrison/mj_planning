"""
Migration: adds new columns and tables.
Run once with: python migrate.py
"""
import sqlite3, os

data_dir = os.environ.get('DATA_DIR', '')
db_path  = os.path.join(data_dir, 'jm.db') if data_dir else 'jm.db'

print(f'Migrating: {db_path}')
conn = sqlite3.connect(db_path)
cur  = conn.cursor()

# New columns on existing tables
col_migrations = {
    'recipe': [
        ('star_rating', 'INTEGER DEFAULT 0'),
        ('kcal',        'INTEGER'),
        ('protein_g',   'REAL'),
        ('carbs_g',     'REAL'),
        ('fat_g',       'REAL'),
    ],
    'cal_event': [
        ('notes',          'TEXT DEFAULT ""'),
        ('project_id',     'INTEGER'),
        ('task_id',        'INTEGER'),
        ('end_date',       'DATE'),
        ('start_time',     'TEXT'),
        ('end_time',       'TEXT'),
        ('status',         'TEXT DEFAULT "confirmed"'),
        ('event_category', 'TEXT DEFAULT "event"'),
    ],
}

for table, cols in col_migrations.items():
    cur.execute(f'PRAGMA table_info({table})')
    existing = {row[1] for row in cur.fetchall()}
    for col, typedef in cols:
        if col not in existing:
            cur.execute(f'ALTER TABLE {table} ADD COLUMN {col} {typedef}')
            print(f'  + {table}.{col}')
        else:
            print(f'  . {table}.{col} (exists)')

# New daily_todo table (db.create_all handles this, but just in case)
cur.execute('''
    CREATE TABLE IF NOT EXISTS daily_todo (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id    TEXT    NOT NULL,
        text       TEXT    NOT NULL,
        done       INTEGER DEFAULT 0,
        pinned     INTEGER DEFAULT 0,
        priority   TEXT    DEFAULT "normal",
        for_date   DATE    NOT NULL,
        done_at    DATETIME,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        "order"    INTEGER DEFAULT 0
    )
''')
print('  . daily_todo table ready')

conn.commit()
conn.close()
print('Done!')