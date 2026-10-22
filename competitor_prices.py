"""
Отчёт /rynok — мониторинг цен конкурентов на ламинированную фанеру
18мм 2440x1220 (merani.ru, prom23.ru), по требованию из Telegram.

Хранит собственную небольшую историю в SQLite рядом с основной БД проекта,
чтобы показывать разницу с прошлым запросом (↑/↓).
"""
import sqlite3
import threading
import logging
from dataclasses import asdict
from functools import wraps
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

MSK = ZoneInfo("Europe/Moscow")

def _now() -> datetime:
    return datetime.now(MSK)

from competitor_parsers import merani, prom23, deltakom, tdkorsar, psk_holding, sgmonolit, fanera_msk

# Карта (сайт, название позиции) -> прямая ссылка на карточку товара, для кликабельных ссылок на фронтенде
_URL_MAP = {}
for _name, _url in merani.SKU_PAGES:
    _URL_MAP[("merani.ru", _name)] = _url
for _name, _url in prom23.SKU_PAGES:
    _URL_MAP[("prom23.ru", _name)] = _url
for _name, _url in deltakom.SKU_PAGES:
    _URL_MAP[("ooodeltakom.ru", _name)] = _url
for _name, _url in tdkorsar.SKU_PAGES:
    _URL_MAP[("tdkorsar.ru", _name)] = _url
for _name, _url in psk_holding.SKU_PAGES:
    _URL_MAP[("psk-holding.ru", _name)] = _url
for _name, _url in sgmonolit.SKU_PAGES:
    _URL_MAP[("sgmonolit.ru", _name)] = _url

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
    # Serialize the additive migration across history readers and collectors.
    conn.execute("BEGIN IMMEDIATE")
    columns = {r[1] for r in conn.execute("PRAGMA table_info(competitor_prices)")}
    for name, kind in (("product_id", "TEXT"), ("url", "TEXT"), ("price_is_from", "INTEGER NOT NULL DEFAULT 0")):
        if name not in columns:
            conn.execute(f"ALTER TABLE competitor_prices ADD COLUMN {name} {kind}")
    conn.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_rynok_product_snapshot
                    ON competitor_prices(site, product_id, parsed_at)
                    WHERE product_id IS NOT NULL""")
    conn.commit()
    return conn


def _fanera_rows() -> list[dict]:
    rows = [asdict(row) for row in fanera_msk.fetch_all()]
    now = _now().isoformat()
    for row in rows:
        row["parsed_at"] = now
    return rows


def _save_rows(conn: sqlite3.Connection, rows: list[dict]) -> list[dict]:
    unique = {}
    for row in rows:
        if row["price_per_sheet"] is None and row.get("product_id") is None:
            continue
        row = {"product_id": None, "url": None, "price_is_from": False, **row}
        unique[(row["site"], row["product_id"] or row["variant"])] = row
    saved = list(unique.values())
    conn.executemany("""INSERT INTO competitor_prices
        (site, variant, price_per_sheet, in_stock, parsed_at, product_id, url, price_is_from)
        VALUES (:site, :variant, :price_per_sheet, :in_stock, :parsed_at, :product_id, :url, :price_is_from)
        ON CONFLICT(site, product_id, parsed_at) WHERE product_id IS NOT NULL DO NOTHING""", saved)
    conn.commit()
    return saved


def collect_fanera_msk() -> int:
    """Refresh this catalog without sending notifications or querying other sites."""
    rows = _fanera_rows()  # complete traversal must succeed before any writes
    conn = _get_conn()
    try:
        count = len(_save_rows(conn, rows))
        with _refresh_state_lock:
            _source_errors.pop(fanera_msk.SITE, None)
        return count
    finally:
        conn.close()


def _collect_rows() -> list[dict]:
    rows = []
    now = _now().isoformat()

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
        for r in deltakom.fetch_all():
            rows.append({
                "site": r.site, "variant": r.variant,
                "price_per_sheet": r.price_per_sheet, "in_stock": None,
                "parsed_at": now,
            })
    except Exception as e:
        rows.append({"site": "ooodeltakom.ru", "variant": f"[ОШИБКА: {e}]",
                      "price_per_sheet": None, "in_stock": None, "parsed_at": now})

    try:
        for r in tdkorsar.fetch_all():
            rows.append({
                "site": r.site, "variant": r.variant,
                "price_per_sheet": r.price_per_sheet,
                "in_stock": (1 if r.in_stock else 0) if r.in_stock is not None else None,
                "parsed_at": now,
            })
    except Exception as e:
        rows.append({"site": "tdkorsar.ru", "variant": f"[ОШИБКА: {e}]",
                      "price_per_sheet": None, "in_stock": None, "parsed_at": now})

    try:
        for r in psk_holding.fetch_all():
            rows.append({
                "site": r.site, "variant": r.variant,
                "price_per_sheet": r.price_per_sheet, "in_stock": None,
                "parsed_at": now,
            })
    except Exception as e:
        rows.append({"site": "psk-holding.ru", "variant": f"[ОШИБКА: {e}]",
                      "price_per_sheet": None, "in_stock": None, "parsed_at": now})

    try:
        for r in sgmonolit.fetch_all():
            rows.append({
                "site": r.site, "variant": r.variant,
                "price_per_sheet": r.price_per_sheet, "in_stock": None,
                "parsed_at": now,
            })
    except Exception as e:
        rows.append({"site": "sgmonolit.ru", "variant": f"[ОШИБКА: {e}]",
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

    try:
        rows.extend(_fanera_rows())
        with _refresh_state_lock:
            _source_errors.pop(fanera_msk.SITE, None)
    except Exception:
        logging.getLogger(__name__).exception("Fanera-MSK collection failed; keeping saved history")
        with _refresh_state_lock:
            _source_errors[fanera_msk.SITE] = "fanera-msk.ru: обновление не удалось; показана сохранённая история."

    return rows


def get_price_history() -> dict:
    """
    Возвращает историю цен конкурентов по дням для таблицы на сайте.
    Для каждого (site, variant) берём последнюю цену за каждый календарный день.
    """
    conn = _get_conn()
    cur = conn.execute("""
        SELECT site, variant, price_per_sheet, in_stock, parsed_at, product_id, url, price_is_from
        FROM competitor_prices
        ORDER BY parsed_at ASC, id ASC
    """)
    by_day: dict[tuple, dict[str, dict]] = {}
    metadata = {}
    for site, variant, price, in_stock, parsed_at, product_id, url, is_from in cur.fetchall():
        day = parsed_at[:10]  # YYYY-MM-DD
        key = (site, product_id or variant)
        metadata[key] = (variant, url)
        by_day.setdefault(key, {})[day] = {"price": price, "in_stock": in_stock}
        if is_from:
            by_day[key][day]["price_is_from"] = True
    conn.close()

    positions = []
    for key, days in sorted(by_day.items()):
        site = key[0]
        variant, url = metadata[key]
        positions.append({
            "site": site,
            "variant": variant,
            "url": url or _URL_MAP.get((site, variant)),
            "days": days,  # {"2026-07-23": {"price": 2744, "in_stock": None}, ...}
        })
    with _refresh_state_lock:
        return {"positions": positions, "refreshing": _refresh_running,
                "refresh_error": _refresh_error or " ".join(_source_errors.values()) or None}


_last_manual_collect: datetime | None = None
_MANUAL_COLLECT_COOLDOWN_SECONDS = 120  # не чаще раза в 2 минуты на повторные клики "Обновить"
_refresh_state_lock = threading.Lock()
_refresh_running = False
_refresh_error = None
_source_errors = {}


def start_price_refresh(source: str | None = None) -> bool:
    """Reserve one background collection; readers never wait for source sites."""
    global _refresh_running, _refresh_error
    if source not in (None, fanera_msk.SITE):
        raise ValueError("Unsupported price source")
    with _refresh_state_lock:
        if _refresh_running:
            return False
        if (_last_manual_collect is not None and
                (_now() - _last_manual_collect).total_seconds() < _MANUAL_COLLECT_COOLDOWN_SECONDS):
            return False
        _refresh_running = True
        _refresh_error = None

    def run():
        global _refresh_running, _refresh_error
        error = None
        try:
            if source == fanera_msk.SITE:
                global _last_manual_collect
                with _collection_lock:
                    collect_fanera_msk()
                _last_manual_collect = _now()
            else:
                collect_and_save_if_due()
        except Exception:
            logging.getLogger(__name__).exception("Background competitor price collection failed")
            error = "Не удалось обновить цены. Показана сохранённая история."
        finally:
            with _refresh_state_lock:
                _refresh_error = error
                _refresh_running = False

    worker = threading.Thread(target=run, name="competitor-price-refresh", daemon=True)
    try:
        worker.start()
    except Exception:
        with _refresh_state_lock:
            _refresh_running = False
        raise
    return True


def collect_and_save_if_due() -> bool:
    """Как collect_and_save(), но с дедупликацией: если с прошлого ручного
    сбора прошло меньше _MANUAL_COLLECT_COOLDOWN_SECONDS — ничего не делает
    и возвращает False (фронтенд просто получит уже сохранённые данные)."""
    global _last_manual_collect
    now = _now()
    if _last_manual_collect is not None:
        elapsed = (now - _last_manual_collect).total_seconds()
        if elapsed < _MANUAL_COLLECT_COOLDOWN_SECONDS:
            return False
    collect_and_notify_changes()
    _last_manual_collect = now
    return True


def collect_and_save() -> int:
    """Собирает цены со всех сайтов и сохраняет в БД. Возвращает число сохранённых строк.
    Используется и планировщиком, и командой /rynok (через build_rynok_report)."""
    rows = _collect_rows()
    conn = _get_conn()
    try:
        return len(_save_rows(conn, rows))
    finally:
        conn.close()


def collect_and_notify_changes() -> bool:
    """Unified collection/notification path for scheduled, site and bot refreshes."""
    import logging
    from telegram_reporter import _tg_send
    report = collect_and_report_changes()
    if not report:
        return False
    sent = _tg_send(report)
    if not sent:
        logging.getLogger(__name__).error("Competitor price alert detected but Telegram delivery failed")
    return sent


_collection_lock = threading.Lock()

def _serialized_collection(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        with _collection_lock:
            return fn(*args, **kwargs)
    return wrapper


@_serialized_collection
def collect_and_report_changes() -> str | None:
    """
    Для ежедневной автоматической проверки (планировщик).
    Собирает свежие цены, сравнивает с последним известным значением по каждой позиции
    и возвращает текст уведомления в Telegram, ТОЛЬКО если хоть одна цена изменилась.
    Если изменений нет — возвращает None (уведомление не шлём, чтобы не спамить).
    """
    conn = _get_conn()

    # Цена по каждой позиции до текущего сбора
    prev_prices: dict[tuple, int] = {}
    cur = conn.execute("""
        SELECT site, COALESCE(product_id, variant), price_per_sheet FROM competitor_prices
        WHERE id IN (SELECT MAX(id) FROM competitor_prices GROUP BY site, COALESCE(product_id, variant))
    """)
    for site, variant, price in cur.fetchall():
        if price is not None:
            prev_prices[(site, variant)] = price

    try:
        rows = _collect_rows()
        valid_rows = _save_rows(conn, rows)
    finally:
        conn.close()

    changed_lines = []
    for r in valid_rows:
        key = (r["site"], r.get("product_id") or r["variant"])
        prev = prev_prices.get(key)
        if prev is not None and r["price_per_sheet"] is not None and prev != r["price_per_sheet"]:
            diff = r["price_per_sheet"] - prev
            arrow = f"↑ +{diff}₽" if diff > 0 else f"↓ {diff}₽"
            changed_lines.append(f"  {r['site']} — {r['variant']}: {prev}₽ → {r['price_per_sheet']}₽ ({arrow})")

    if not changed_lines:
        return None

    lines = [
        "<b>📊 Изменение цен конкурентов</b>",
        f"<i>{_now().strftime('%d.%m.%Y %H:%M')}</i>", "",
    ] + changed_lines
    return "\n".join(lines)


def build_rynok_report() -> str:
    collect_and_notify_changes()
    conn = _get_conn()

    cur = conn.execute("""
        SELECT site, variant, price_per_sheet, in_stock, parsed_at, product_id
        FROM competitor_prices
        ORDER BY parsed_at DESC, id DESC
    """)
    latest_by_key: dict[tuple, list] = {}
    names = {}
    for site, variant, price, in_stock, parsed_at, product_id in cur.fetchall():
        key = (site, product_id or variant)
        names.setdefault(key, variant)
        latest_by_key.setdefault(key, []).append((price, in_stock, parsed_at))
    conn.close()

    lines = [f"<b>📊 Рынок · цены конкурентов на 18мм 2440x1220</b>",
              f"<i>{_now().strftime('%d.%m.%Y %H:%M')}</i>", ""]

    by_site: dict[str, list] = {}
    for key, snapshots in latest_by_key.items():
        by_site.setdefault(key[0], []).append((names[key], snapshots))

    for site in sorted(by_site):
        lines.append(f"<b>🏭 {site}</b>")
        for variant, snapshots in sorted(by_site[site]):
            price, stock, _ = snapshots[0]
            if price is None:
                lines.append(f"  {variant}: цена по запросу")
                continue
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

    return "\n".join(lines).strip()
