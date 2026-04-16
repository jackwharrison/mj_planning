"""
Run this once to add the cost_estimate and cost_actual columns
to your existing jm.db database.

Usage:
    python migrate.py
"""
import sqlite3, os

# Find the database — SQLite puts it in instance/ or the project root
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

for col, typ in [('cost_estimate', 'REAL'), ('cost_actual', 'REAL')]:
    try:
        cur.execute(f'ALTER TABLE task ADD COLUMN {col} {typ}')
        print(f"  Added column: {col}")
    except sqlite3.OperationalError:
        print(f"  Already exists: {col} (skipped)")

conn.commit()
conn.close()
print("Done — restart Flask now.")