"""SQLite хранилище данных."""
import sqlite3, json, os
from datetime import datetime

DB_PATH = os.getenv("DB_PATH", "data/novator.db")

def _conn():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def _init():
    with _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS month_data (
                month      TEXT PRIMARY KEY,
                data       TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS upload_log (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                month      TEXT,
                source     TEXT,
                ops_count  INTEGER,
                unknown    INTEGER,
                created_at TEXT
            )
        """)
        conn.commit()

_init()

def save_month_data(month: str, data: dict):
    now = datetime.now().isoformat()
    with _conn() as conn:
        conn.execute("""
            INSERT INTO month_data (month, data, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(month) DO UPDATE
            SET data=excluded.data, updated_at=excluded.updated_at
        """, (month, json.dumps(data, ensure_ascii=False), now))
        conn.execute("""
            INSERT INTO upload_log (month, source, ops_count, unknown, created_at)
            VALUES (?, ?, ?, ?, ?)
        """, (month, data.get('source','manual'), data.get('total_ops',0), len(data.get('unknown',[])), now))
        conn.commit()

def get_month_data(month: str) -> dict:
    with _conn() as conn:
        row = conn.execute("SELECT data FROM month_data WHERE month=?", (month,)).fetchone()
    return json.loads(row['data']) if row else {}

def get_all_months() -> dict:
    with _conn() as conn:
        rows = conn.execute("SELECT month, data, updated_at FROM month_data").fetchall()
    return {r['month']: {**json.loads(r['data']), 'updated_at': r['updated_at']} for r in rows}

def get_last_upload() -> dict:
    with _conn() as conn:
        row = conn.execute("SELECT * FROM upload_log ORDER BY id DESC LIMIT 1").fetchone()
    if not row:
        return {"status": "no_data"}
    return dict(row)
