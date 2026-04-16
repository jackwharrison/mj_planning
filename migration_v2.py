"""
Run this once to add/update the meal-planning tables.

Usage:
    python migration_v3.py
"""
import sqlite3, os

for path in [os.path.join('instance', 'jm.db'), 'jm.db']:
    if os.path.exists(path):
        db_path = path
        break
else:
    print("Could not find jm.db — have you run flask run at least once?")
    exit(1)

print(f"Migrating: {db_path}")
conn = sqlite3.connect(db_path)
cur  = conn.cursor()

tables = {
    'food_item': """
        CREATE TABLE IF NOT EXISTS food_item (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name VARCHAR(200) NOT NULL, category VARCHAR(100) DEFAULT 'other',
            quantity REAL DEFAULT 0, unit VARCHAR(50) DEFAULT 'pcs',
            low_stock REAL DEFAULT 0, created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )""",
    'recipe': """
        CREATE TABLE IF NOT EXISTS recipe (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name VARCHAR(300) NOT NULL, servings INTEGER DEFAULT 2,
            prep_mins INTEGER DEFAULT 0, cook_mins INTEGER DEFAULT 0,
            instructions TEXT DEFAULT '', source_url VARCHAR(2000) DEFAULT '',
            tags VARCHAR(500) DEFAULT '', created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )""",
    'recipe_ingredient': """
        CREATE TABLE IF NOT EXISTS recipe_ingredient (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recipe_id INTEGER NOT NULL REFERENCES recipe(id) ON DELETE CASCADE,
            food_item_id INTEGER REFERENCES food_item(id),
            name VARCHAR(200) NOT NULL, quantity REAL DEFAULT 0,
            unit VARCHAR(50) DEFAULT 'pcs'
        )""",
    'meal_plan': """
        CREATE TABLE IF NOT EXISTS meal_plan (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            plan_date DATE NOT NULL, meal_type VARCHAR(20) DEFAULT 'dinner',
            recipe_id INTEGER REFERENCES recipe(id),
            custom_name VARCHAR(300) DEFAULT '', assignee VARCHAR(10) DEFAULT 'both',
            cooked BOOLEAN DEFAULT 1, created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )""",
    'shopping_item': """
        CREATE TABLE IF NOT EXISTS shopping_item (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name VARCHAR(200) NOT NULL, quantity REAL DEFAULT 1,
            unit VARCHAR(50) DEFAULT 'pcs', is_food BOOLEAN DEFAULT 1,
            bought BOOLEAN DEFAULT 0, created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )""",
}

for name, ddl in tables.items():
    try:
        cur.execute(ddl)
        print(f"  Created table: {name}")
    except sqlite3.OperationalError as e:
        print(f"  Table {name}: {e}")

# Add new columns to existing tables (safe — skips if already present)
alter_cmds = [
    ('recipe', 'source_url', 'VARCHAR(2000) DEFAULT ""'),
    ('meal_plan', 'cooked', 'BOOLEAN DEFAULT 1'),
    ('shopping_item', 'is_food', 'BOOLEAN DEFAULT 1'),
]
for table, col, typ in alter_cmds:
    try:
        cur.execute(f'ALTER TABLE {table} ADD COLUMN {col} {typ}')
        print(f"  Added column: {table}.{col}")
    except sqlite3.OperationalError:
        print(f"  Already exists: {table}.{col} (skipped)")

conn.commit()
conn.close()
print("Done — restart Flask now.")