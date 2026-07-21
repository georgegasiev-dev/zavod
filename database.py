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
        conn.execute("""
            CREATE TABLE IF NOT EXISTS fssp_seen (
                ip_number  TEXT PRIMARY KEY,
                first_seen TEXT NOT NULL
            )
        """)  # ponytail: только номер ИП + дата обнаружения
        conn.commit()

_init()


def fssp_get_seen() -> set:
    with _conn() as conn:
        rows = conn.execute("SELECT ip_number FROM fssp_seen").fetchall()
        return {r["ip_number"] for r in rows}

def fssp_mark_seen(ip_numbers: list):
    with _conn() as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO fssp_seen (ip_number, first_seen) VALUES (?, datetime('now'))",
            [(t,) for t in ip_numbers]
        )
        conn.commit()


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


# ── Разрешённые пользователи Telegram ────────────────────────────────────────

def _ensure_users_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tg_allowed_users (
            chat_id    TEXT PRIMARY KEY,
            added_at   TEXT NOT NULL
        )
    """)


def add_allowed_user(chat_id: str):
    """Добавляет пользователя в список разрешённых."""
    with _conn() as conn:
        _ensure_users_table(conn)
        conn.execute("""
            INSERT INTO tg_allowed_users (chat_id, added_at)
            VALUES (?, datetime('now'))
            ON CONFLICT(chat_id) DO NOTHING
        """, (str(chat_id),))
        conn.commit()


def get_allowed_users() -> list[str]:
    """Возвращает список разрешённых chat_id."""
    with _conn() as conn:
        try:
            _ensure_users_table(conn)
            conn.commit()
            rows = conn.execute("SELECT chat_id FROM tg_allowed_users").fetchall()
            return [r['chat_id'] for r in rows]
        except Exception:
            return []


def remove_allowed_user(chat_id: str):
    """Удаляет пользователя из списка разрешённых."""
    with _conn() as conn:
        try:
            conn.execute("DELETE FROM tg_allowed_users WHERE chat_id=?", (str(chat_id),))
            conn.commit()
        except Exception:
            pass


# ── Лог доступа ───────────────────────────────────────────────────────────────

def log_access(action: str, ip: str = "—", details: str = ""):
    """Записывает событие в лог доступа."""
    with _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS access_log (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                action     TEXT NOT NULL,
                ip         TEXT DEFAULT '—',
                details    TEXT DEFAULT '',
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            INSERT INTO access_log (action, ip, details, created_at)
            VALUES (?, ?, ?, datetime('now'))
        """, (action, ip, details))
        conn.commit()


def get_access_log(limit: int = 50) -> list[dict]:
    """Возвращает последние записи лога доступа."""
    with _conn() as conn:
        try:
            rows = conn.execute("""
                SELECT id, action, ip, details, created_at
                FROM access_log
                ORDER BY id DESC
                LIMIT ?
            """, (limit,)).fetchall()
            return [dict(r) for r in rows]
        except Exception:
            return []


# ── Балансы по неделям ────────────────────────────────────────────────────────

def save_week_balance(date_from: str, opening: float, closing: float):
    """Сохраняет баланс на начало и конец периода выписки."""
    with _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS week_balance (
                date_from   TEXT PRIMARY KEY,
                opening     REAL NOT NULL,
                closing     REAL NOT NULL,
                updated_at  TEXT NOT NULL
            )
        """)
        conn.execute("""
            INSERT INTO week_balance (date_from, opening, closing, updated_at)
            VALUES (?, ?, ?, datetime('now'))
            ON CONFLICT(date_from) DO UPDATE
            SET opening=excluded.opening, closing=excluded.closing,
                updated_at=excluded.updated_at
        """, (date_from, opening, closing))
        conn.commit()


def get_week_balance(date_from: str) -> dict | None:
    """Возвращает баланс для периода или None."""
    with _conn() as conn:
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS week_balance (
                    date_from TEXT PRIMARY KEY, opening REAL, closing REAL, updated_at TEXT
                )
            """)
            row = conn.execute(
                "SELECT opening, closing FROM week_balance WHERE date_from=?", (date_from,)
            ).fetchone()
            return dict(row) if row else None
        except Exception:
            return None


# ── Подписчики рассылки ───────────────────────────────────────────────────────

def add_broadcast_user(chat_id: str, name: str = ""):
    """Добавляет пользователя в список рассылки автоотчётов."""
    with _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS broadcast_users (
                chat_id    TEXT PRIMARY KEY,
                name       TEXT DEFAULT '',
                added_at   TEXT NOT NULL
            )
        """)
        conn.execute("""
            INSERT INTO broadcast_users (chat_id, name, added_at)
            VALUES (?, ?, datetime('now'))
            ON CONFLICT(chat_id) DO UPDATE SET name=excluded.name
        """, (str(chat_id), name))
        conn.commit()


def remove_broadcast_user(chat_id: str):
    with _conn() as conn:
        try:
            conn.execute("DELETE FROM broadcast_users WHERE chat_id=?", (str(chat_id),))
            conn.commit()
        except Exception:
            pass


def get_broadcast_users() -> list[dict]:
    """Возвращает всех подписчиков рассылки."""
    with _conn() as conn:
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS broadcast_users (
                    chat_id TEXT PRIMARY KEY, name TEXT DEFAULT '', added_at TEXT
                )
            """)
            conn.commit()
            rows = conn.execute("SELECT chat_id, name, added_at FROM broadcast_users").fetchall()
            return [dict(r) for r in rows]
        except Exception:
            return []


# ─── ЕОВР ──────────────────────────────────────────────────────────────────

def _init_eovr():
    with _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS eovr_cache (
                sheet_title TEXT PRIMARY KEY,
                year        INTEGER NOT NULL,
                month       INTEGER NOT NULL,
                totals      TEXT NOT NULL,
                updated_at  TEXT NOT NULL
            )
        """)
        conn.commit()

_init_eovr()


def save_eovr_month(sheet_title: str, year: int, month: int, totals: dict):
    """Сохранить итоги месяца ЕОВР (lush, sush, sborka, lam, obr)."""
    with _conn() as conn:
        conn.execute("""
            INSERT INTO eovr_cache (sheet_title, year, month, totals, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(sheet_title) DO UPDATE SET
                totals=excluded.totals, updated_at=excluded.updated_at
        """, (sheet_title, year, month, json.dumps(totals, ensure_ascii=False),
              datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
        conn.commit()


def get_eovr_month(sheet_title: str) -> dict | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT totals, updated_at FROM eovr_cache WHERE sheet_title=?",
            (sheet_title,)
        ).fetchone()
        if not row:
            return None
        return {**json.loads(row['totals']), '_updated_at': row['updated_at']}


def get_eovr_year(year: int) -> list[dict]:
    """Все месяцы года, отсортированные по месяцу."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT sheet_title, month, totals, updated_at FROM eovr_cache WHERE year=? ORDER BY month",
            (year,)
        ).fetchall()
        result = []
        for r in rows:
            t = json.loads(r['totals'])
            t['month'] = r['month']
            t['sheet_title'] = r['sheet_title']
            t['updated_at'] = r['updated_at']
            result.append(t)
        return result


def get_eovr_latest_updated() -> str | None:
    """Время последнего обновления любого листа ЕОВР."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT updated_at FROM eovr_cache ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()
        return row['updated_at'] if row else None


def _init_eovr_days():
    with _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS eovr_days (
                sheet_title TEXT NOT NULL,
                day         INTEGER NOT NULL,
                lush        REAL DEFAULT 0,
                sush        REAL DEFAULT 0,
                sborka      REAL DEFAULT 0,
                lam         REAL DEFAULT 0,
                obr         REAL DEFAULT 0,
                PRIMARY KEY (sheet_title, day)
            )
        """)
        conn.commit()

_init_eovr_days()


def save_eovr_days(sheet_title: str, days: dict):
    """Сохранить посуточные данные ЕОВР. days = {1: {lush,sush,sborka,lam,obr}, ...}"""
    with _conn() as conn:
        conn.execute("DELETE FROM eovr_days WHERE sheet_title=?", (sheet_title,))
        for day, v in days.items():
            conn.execute("""
                INSERT INTO eovr_days (sheet_title, day, lush, sush, sborka, lam, obr)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (sheet_title, int(day),
                  v.get('lush',0), v.get('sush',0), v.get('sborka',0),
                  v.get('lam',0),  v.get('obr',0)))
        conn.commit()


def get_eovr_days(sheet_title: str) -> list[dict]:
    """Посуточные данные листа, отсортированные по дню."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT day, lush, sush, sborka, lam, obr FROM eovr_days WHERE sheet_title=? ORDER BY day",
            (sheet_title,)
        ).fetchall()
        return [dict(r) for r in rows]


# ── Месячное закрытие / Обязательства ────────────────────────────────────────

def _init_obligations():
    with _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS monthly_obligations (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                year       INTEGER NOT NULL DEFAULT 2026,
                month      TEXT NOT NULL,
                category   TEXT NOT NULL,
                opening_debt  REAL DEFAULT 0,
                closing_debt  REAL DEFAULT 0,
                planned_budget REAL DEFAULT 0,
                comment    TEXT DEFAULT '',
                updated_at TEXT,
                UNIQUE(year, month, category)
            )
        """)
        # Миграция со старой схемы (без year, UNIQUE был только (month, category)).
        # SQLite не умеет менять UNIQUE-констрейнт на лету — пересоздаём таблицу.
        cols = [r[1] for r in conn.execute("PRAGMA table_info(monthly_obligations)").fetchall()]
        if 'year' not in cols:
            conn.execute("ALTER TABLE monthly_obligations RENAME TO monthly_obligations_old")
            conn.execute("""
                CREATE TABLE monthly_obligations (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    year       INTEGER NOT NULL DEFAULT 2026,
                    month      TEXT NOT NULL,
                    category   TEXT NOT NULL,
                    opening_debt  REAL DEFAULT 0,
                    closing_debt  REAL DEFAULT 0,
                    planned_budget REAL DEFAULT 0,
                    comment    TEXT DEFAULT '',
                    updated_at TEXT,
                    UNIQUE(year, month, category)
                )
            """)
            # Старые записи (если были) — все относились к 2026 году, других не вели.
            conn.execute("""
                INSERT INTO monthly_obligations
                    (year, month, category, opening_debt, closing_debt, planned_budget, comment, updated_at)
                SELECT 2026, month, category, opening_debt, closing_debt, planned_budget, comment, updated_at
                FROM monthly_obligations_old
            """)
            conn.execute("DROP TABLE monthly_obligations_old")
        conn.commit()

_init_obligations()


def get_obligations(year: int, month: str) -> list:
    """Возвращает обязательства для месяца конкретного года."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM monthly_obligations WHERE year=? AND month=? ORDER BY id",
            (year, month)
        ).fetchall()
        return [dict(r) for r in rows]


def save_obligation(year: int, month: str, category: str, opening_debt: float,
                    closing_debt: float, planned_budget: float, comment: str):
    """Сохраняет/обновляет строку обязательств для (год, месяц, категория)."""
    with _conn() as conn:
        conn.execute("""
            INSERT INTO monthly_obligations (year, month, category, opening_debt, closing_debt, planned_budget, comment, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(year, month, category) DO UPDATE SET
                opening_debt=excluded.opening_debt,
                closing_debt=excluded.closing_debt,
                planned_budget=excluded.planned_budget,
                comment=excluded.comment,
                updated_at=excluded.updated_at
        """, (year, month, category, opening_debt, closing_debt, planned_budget, comment))
        conn.commit()
