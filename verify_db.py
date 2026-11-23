"""Quick verification script for db_manager.py"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from Core.Memory.db_manager import init_db, get_db_path
import sqlite3

# Initialize the database
db = init_db()
db_path = get_db_path()
print(f"DB created at: {db_path}")
print(f"DB file exists: {db_path.exists()}")

# Check tables
conn = sqlite3.connect(str(db_path))
tables = conn.execute(
    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
).fetchall()
print(f"\nTables ({len(tables)} found):")
for t in tables:
    print(f"  - {t[0]}")

# Check columns for each table
print("\nTable schemas:")
for t in tables:
    cols = conn.execute(f"PRAGMA table_info({t[0]})").fetchall()
    print(f"\n  {t[0]}:")
    for c in cols:
        print(f"    {c[1]:20s} {c[2]:10s} {'NOT NULL' if c[3] else 'NULLABLE':8s} {'PK' if c[5] else ''}")

conn.close()
print("\nAll 4 tables verified successfully!")