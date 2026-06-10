"""
Telegram-репортёр Новатора.
Каждый вечер отправляет итоги дня в Telegram.
"""
import os
import logging
import urllib.request
import json
from datetime import datetime

log = logging.getLogger("telegram_reporter")

TG_TOKEN   = os.getenv("TG_TOKEN", "")
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "")

MONTH_NAMES = {
    1:"Январь",2:"Февраль",3:"Март",4:"Апрель",5:"Май",6:"Июнь",
    7:"Июль",8:"Август",9:"Сентябрь",10:"Октябрь",11:"Ноябрь",12:"Декабрь"
}


def _tg_send(text: str) -> bool:
    """Отправляет сообщение в Telegram."""
    if not TG_TOKEN or not TG_CHAT_ID:
        log.warning("TG_TOKEN или TG_CHAT_ID не заданы")
        return False
    try:
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        data = json.dumps({
            "chat_id": TG_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
        }).encode()
        req = urllib.request.Request(url, data=data,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status == 200
    except Exception as e:
        log.error("Ошибка отправки в Telegram: %s", e)
        return False


def _fmt(n: float) -> str:
    """Форматирует число: 14 787 332."""
    return f"{int(round(n)):,}".replace(",", " ")


def _pct(a: float, b: float) -> str:
    return f"{a/b*100:.1f}%" if b else "—"


def build_daily_report(target_date: str | None = None) -> str:
    """
    Собирает итоги дня из БД.
    target_date: 'YYYY-MM-DD' — если None, берёт сегодня.
    """
    from database import get_month_data

    today = datetime.now()
    if target_date:
        try:
            dt = datetime.strptime(target_date, "%Y-%m-%d")
        except ValueError:
            dt = today
    else:
        dt = today

    date_str   = dt.strftime("%Y-%m-%d")   # для сравнения с ops
    date_label = dt.strftime("%d.%m.%Y")   # для заголовка
    month      = MONTH_NAMES.get(dt.month, "")

    data = get_month_data(month)
    if not data or not data.get("ops"):
        return f"⚠️ Нет данных за {month} — выписка не загружена."

    ops = data.get("ops", [])

    # Операции только за нужный день
    day_ops    = [op for op in ops if op.get("date", "").startswith(date_str)]
    debit_ops  = [op for op in day_ops if op.get("is_debit")]
    credit_ops = [op for op in day_ops if not op.get("is_debit")]

    if not day_ops:
        return f"📭 За {date_label} операций в выписке нет."

    total_out = sum(op.get("amount", 0) for op in debit_ops)
    total_in  = sum(op.get("amount", 0) for op in credit_ops)

    # ── Заголовок ────────────────────────────────────────────────────────────
    lines = [
        f"<b>НОВАТОР · ИТОГИ ДНЯ {date_label}</b>",
        "",
        f"Поступлений — {_fmt(total_in)} ₽",
        f"Расходов — {_fmt(total_out)} ₽",
        "",
    ]

    # ── Поступления от клиентов ───────────────────────────────────────────────
    if credit_ops:
        client_totals: dict[str, float] = {}
        for op in credit_ops:
            c = op.get("contractor", "—")
            client_totals[c] = client_totals.get(c, 0) + op.get("amount", 0)
        client_sorted = sorted(client_totals.items(), key=lambda x: -x[1])

        lines.append("<b>Поступления от клиентов</b>")
        for c, s in client_sorted:
            short = c[:50] + ("…" if len(c) > 50 else "")
            lines.append(f"· {short} — {_fmt(s)} ₽")
        lines.append("")

    # ── Расходы по категориям ────────────────────────────────────────────────
    if debit_ops:
        cat_totals: dict[str, float] = {}
        for op in debit_ops:
            cat = op.get("cat", "Прочее")
            cat_totals[cat] = cat_totals.get(cat, 0) + op.get("amount", 0)
        cat_sorted = sorted(cat_totals.items(), key=lambda x: -x[1])

        # Топ поставщики по лесу
        les_by_contr: dict[str, float] = {}
        for op in debit_ops:
            if op.get("cat") == "Лес":
                c = op.get("contractor", "—")
                les_by_contr[c] = les_by_contr.get(c, 0) + op.get("amount", 0)
        les_sorted = sorted(les_by_contr.items(), key=lambda x: -x[1])

        lines.append("<b>Расходы</b>")
        for cat, s in cat_sorted:
            pct  = _pct(s, total_out)
            line = f"· {cat} — {_fmt(s)} ₽ ({pct})"
            if cat == "Лес" and les_sorted:
                contr_str = ", ".join(
                    f"{c} {_fmt(v)}" for c, v in les_sorted[:5]
                )
                line += f"\n  {contr_str}"
            lines.append(line)

    return "\n".join(lines)


# Алиас — чтобы main.py не ломался на старом имени
def build_weekly_report() -> str:
    return build_daily_report()


def send_daily_report(target_date: str | None = None) -> dict:
    """Основная точка входа — формирует и отправляет дневной отчёт."""
    if not TG_TOKEN or not TG_CHAT_ID:
        return {"status": "skip", "reason": "TG_TOKEN или TG_CHAT_ID не заданы"}
    try:
        text = build_daily_report(target_date)
        ok   = _tg_send(text)
        log.info("Telegram daily report sent: %s", ok)
        return {"status": "ok" if ok else "error", "length": len(text)}
    except Exception as e:
        log.error("send_daily_report error: %s", e)
        return {"status": "error", "reason": str(e)}


# Алиас для обратной совместимости с main.py
def send_weekly_report() -> dict:
    return send_daily_report()
