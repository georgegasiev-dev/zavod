"""
Отчёт /rynok — мониторинг цен конкурентов на ламинированную фанеру
18мм 2440x1220 (merani.ru, prom23.ru), по требованию из Telegram.

Хранит собственную небольшую историю в SQLite рядом с основной БД проекта,
чтобы показывать разницу с прошлым запросом (↑/↓).
"""
import sqlite3
from pathlib import Path
from datetime import datetime

from competitor_parsers import merani, prom23

# Карта (сайт, название позиции) -> прямая ссылка на карточку товара, для кликабельных ссылок на фронтенде
_URL_MAP = {}
for _name, _url in merani.SKU_PAGES:
    _URL_MAP[("merani.ru", _name)] = _url
for _name, _url in prom23.SKU_PAGES:
    _URL_MAP[("prom23.ru", _name)] = _url

DB_PATH = Path(__file__).parent / "competitor_prices.sqlite3"

SCHEMA = """
CREATE TABLE IF NOT EXISTS competitor_prices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    site TEXT NOT NULL,
    variant TEXT NOT NULL,
    price_per_sheet INTEGER,
    in_stock INTEGER,
    parsed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rynok_site_variant_time
    ON competitor_prices (site, variant, parsed_at);
"""


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def _collect_rows() -> list[dict]:
    rows = []
    now = datetime.now().isoformat()

    try:
        for r in merani.fetch_all():
            rows.append({
                "site": r.site, "variant": r.variant,
                "price_per_sheet": r.price_per_sheet, "in_stock": None,
                "parsed_at": now,
            })
    except Exception as e:
        rows.append({"site": "merani.ru", "variant": f"[ОШИБКА: {e}]",
                      "price_per_sheet": None, "in_stock": None, "parsed_at": now})

    try:
        for r in prom23.fetch_all():
            rows.append({
                "site": r.site, "variant": r.variant,
                "price_per_sheet": r.price, "in_stock": 1 if r.in_stock else 0,
                "parsed_at": now,
            })
    except Exception as e:
        rows.append({"site": "prom23.ru", "variant": f"[ОШИБКА: {e}]",
                      "price_per_sheet": None, "in_stock": None, "parsed_at": now})

    return rows


def get_price_history() -> dict:
    """
    Возвращает историю цен конкурентов по дням для таблицы на сайте.
    Для каждого (site, variant) берём последнюю цену за каждый календарный день.
    """
    conn = _get_conn()
    cur = conn.execute("""
        SELECT site, variant, price_per_sheet, in_stock, parsed_at
        FROM competitor_prices
        ORDER BY site, variant, parsed_at ASC
    """)
    by_day: dict[tuple, dict[str, dict]] = {}
    for site, variant, price, in_stock, parsed_at in cur.fetchall():
        day = parsed_at[:10]  # YYYY-MM-DD
        key = (site, variant)
        by_day.setdefault(key, {})[day] = {"price": price, "in_stock": in_stock}
    conn.close()

    positions = []
    for (site, variant), days in sorted(by_day.items()):
        positions.append({
            "site": site,
            "variant": variant,
            "url": _URL_MAP.get((site, variant)),
            "days": days,  # {"2026-07-23": {"price": 2744, "in_stock": None}, ...}
        })
    return {"positions": positions}


def collect_and_save() -> int:
    """Собирает цены со всех сайтов и сохраняет в БД. Возвращает число сохранённых строк.
    Используется и планировщиком, и командой /rynok (через build_rynok_report)."""
    conn = _get_conn()
    rows = _collect_rows()
    valid_rows = [r for r in rows if r["price_per_sheet"] is not None]
    if valid_rows:
        conn.executemany(
            """INSERT INTO competitor_prices (site, variant, price_per_sheet, in_stock, parsed_at)
               VALUES (:site, :variant, :price_per_sheet, :in_stock, :parsed_at)""",
            valid_rows,
        )
        conn.commit()
    conn.close()
    return len(valid_rows)


def build_rynok_report() -> str:
    conn = _get_conn()
    rows = _collect_rows()

    valid_rows = [r for r in rows if r["price_per_sheet"] is not None]
    if valid_rows:
        conn.executemany(
            """INSERT INTO competitor_prices (site, variant, price_per_sheet, in_stock, parsed_at)
               VALUES (:site, :variant, :price_per_sheet, :in_stock, :parsed_at)""",
            valid_rows,
        )
        conn.commit()

    cur = conn.execute("""
        SELECT site, variant, price_per_sheet, in_stock, parsed_at
        FROM competitor_prices
        ORDER BY site, variant, parsed_at DESC
    """)
    latest_by_key: dict[tuple, list] = {}
    for site, variant, price, in_stock, parsed_at in cur.fetchall():
        latest_by_key.setdefault((site, variant), []).append((price, in_stock, parsed_at))
    conn.close()

    lines = [f"<b>📊 Рынок · цены конкурентов на 18мм 2440x1220</b>",
              f"<i>{datetime.now().strftime('%d.%m.%Y %H:%M')}</i>", ""]

    errored = [r for r in rows if r["price_per_sheet"] is None]
    by_site: dict[str, list] = {}
    for (site, variant), snapshots in latest_by_key.items():
        by_site.setdefault(site, []).append((variant, snapshots))

    for site in sorted(by_site):
        lines.append(f"<b>🏭 {site}</b>")
        for variant, snapshots in sorted(by_site[site]):
            price, stock, _ = snapshots[0]
            stock_note = " (нет в наличии)" if stock == 0 else ""
            if len(snapshots) > 1 and snapshots[1][0] is not None:
                diff = price - snapshots[1][0]
                if diff > 0:
                    trend = f" ↑+{diff}₽"
                elif diff < 0:
                    trend = f" ↓{diff}₽"
                else:
                    trend = ""
            else:
                trend = " (первое измерение)"
            lines.append(f"  {variant}: {price}₽{stock_note}{trend}")
        lines.append("")

    if errored:
        lines.append("⚠️ Не удалось собрать:")
        for r in errored:
            lines.append(f"  {r['site']}: {r['variant']}")

    return "\n".join(lines).strip()
