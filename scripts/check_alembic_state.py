"""Check dev DB migration state: alembic_version + modes table columns."""
import sqlite3
from pathlib import Path

db = Path(__file__).resolve().parents[1] / "backend" / "data" / "zagent.db"
if not db.exists():
    print(f"NO DEV DB at {db}")
    raise SystemExit(0)

conn = sqlite3.connect(db)
try:
    version = conn.execute("SELECT version_num FROM alembic_version").fetchall()
except sqlite3.OperationalError as exc:
    version = f"NO alembic_version table ({exc})"
print("alembic_version:", version)
cols = [r[1] for r in conn.execute("PRAGMA table_info(modes)").fetchall()]
print("modes columns:", cols)
rows = conn.execute("SELECT name, topology, permission_mode FROM modes").fetchall()
print("mode rows:", rows)
conn.close()
