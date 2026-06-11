"""SQLite хранилище данных."""
import sqlite3, json, os
from datetime import datetime

DB_PATH = os.getenv("DB_PATH", "data/novator.db")

def _norm(name: str) -> str:
    """Нормализует имя контрагента — так же, как _classify в classifier.py."""
    return ' '.join((name or '').lower().strip().split())

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
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        conn.commit()

_init()


def save_setting(key: str, value: str):
    with _conn() as conn:
        conn.execute("""
            INSERT INTO settings (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """, (key, value))
        conn.commit()


def get_setting(key: str, default: str = None) -> str:
    with _conn() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row['value'] if row else default

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


def save_contractor_mapping(contractor: str, cat: str):
    """Сохраняет маппинг контрагент → категория в БД."""
    with _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS contractor_map (
                contractor TEXT PRIMARY KEY,
                cat        TEXT NOT NULL,
                updated_at TEXT
            )
        """)
        conn.execute("""
            INSERT INTO contractor_map (contractor, cat, updated_at)
            VALUES (?, ?, datetime('now'))
            ON CONFLICT(contractor) DO UPDATE SET cat=excluded.cat, updated_at=excluded.updated_at
        """, (_norm(contractor), cat))
        conn.commit()


def get_contractor_mappings() -> dict:
    """Загружает все сохранённые маппинги контрагент → категория."""
    with _conn() as conn:
        try:
            rows = conn.execute("SELECT contractor, cat FROM contractor_map").fetchall()
            return {r['contractor']: r['cat'] for r in rows}
        except Exception:
            return {}


def get_contractor_details() -> dict:
    """Загружает сохранённые детали контрагентов (комментарии, дата добавления)."""
    with _conn() as conn:
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS contractor_details (
                    contractor TEXT PRIMARY KEY,
                    comment    TEXT DEFAULT '',
                    added_at   TEXT
                )
            """)
            conn.commit()
            rows = conn.execute("SELECT contractor, comment, added_at FROM contractor_details").fetchall()
            return {r['contractor']: {'comment': r['comment'] or '', 'added_at': r['added_at'] or ''} for r in rows}
        except Exception:
            return {}


def save_contractor_comment(contractor: str, comment: str):
    """Сохраняет комментарий к контрагенту."""
    with _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS contractor_details (
                contractor TEXT PRIMARY KEY,
                comment    TEXT DEFAULT '',
                added_at   TEXT
            )
        """)
        conn.execute("""
            INSERT INTO contractor_details (contractor, comment, added_at)
            VALUES (?, ?, datetime('now'))
            ON CONFLICT(contractor) DO UPDATE SET comment=excluded.comment
        """, (_norm(contractor), comment))
        conn.commit()


def merge_month_data(month: str, new_data: dict):
    """
    Добавляет операции из new_data в существующие данные месяца.
    Операции за даты, уже присутствующие в БД, заменяются новыми.
    Старые даты (загруженные вручную ранее) сохраняются.
    """
    existing = get_month_data(month)
    if not existing or not existing.get('ops'):
        save_month_data(month, new_data)
        return

    # Даты, покрытые новой выпиской
    new_dates = {op.get('date') for op in new_data.get('ops', []) if op.get('date')}

    # Сохраняем старые операции за даты, которых нет в новой выписке
    kept_ops = [op for op in existing['ops'] if op.get('date') not in new_dates]

    # Объединяем: старые (без новых дат) + все новые
    merged_ops = kept_ops + new_data.get('ops', [])

    # Пересчитываем итоги из объединённого списка
    weeks_out  = [0.0] * 5
    weeks_in   = [0.0] * 5
    cat_totals: dict = {}

    for op in merged_ops:
        amt = op.get('amount', 0) or 0
        wi  = op.get('week', -1)
        if op.get('is_debit'):
            if 0 <= wi < 5:
                weeks_out[wi] += amt
            cat = op.get('cat', '')
            if cat:
                cat_totals[cat] = cat_totals.get(cat, 0) + amt
        else:
            if 0 <= wi < 5:
                weeks_in[wi] += amt

    merged = {
        **existing,
        'ops':       merged_ops,
        'weeks_out': [round(x) for x in weeks_out],
        'weeks_in':  [round(x) for x in weeks_in],
        'cats':      [
            {'name': k, 'fact': round(v)}
            for k, v in sorted(cat_totals.items(), key=lambda x: -x[1])
        ],
        'total_ops': len(merged_ops),
        'updated_at': datetime.now().isoformat(),
        'source':    'merged',
    }

    save_month_data(month, merged)



# ── План ──────────────────────────────────────────────────────────────────────

def save_plan(month: str, plan: dict):
    """Сохраняет план по категориям для месяца."""
    with _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS plan (
                month      TEXT PRIMARY KEY,
                data       TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            INSERT INTO plan (month, data, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(month) DO UPDATE
            SET data=excluded.data, updated_at=excluded.updated_at
        """, (month, json.dumps(plan, ensure_ascii=False), datetime.now().isoformat()))
        conn.commit()


def get_plan(month: str) -> dict:
    """Возвращает план для месяца или {} если не задан."""
    with _conn() as conn:
        try:
            row = conn.execute("SELECT data FROM plan WHERE month=?", (month,)).fetchone()
            return json.loads(row['data']) if row else {}
        except Exception:
            return {}


def get_all_plans() -> dict:
    """Возвращает планы по всем месяцам."""
    with _conn() as conn:
        try:
            rows = conn.execute("SELECT month, data, updated_at FROM plan").fetchall()
            return {r['month']: {**json.loads(r['data']), 'updated_at': r['updated_at']} for r in rows}
        except Exception:
            return {}
