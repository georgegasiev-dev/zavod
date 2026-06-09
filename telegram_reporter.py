"""
Telegram-репортёр Новатора.
Каждое утро в 10:00 МСК отправляет обзор прошедшей недели в Telegram.
"""
import os
import logging
import urllib.request
import urllib.parse
import json
from datetime import datetime, timedelta

log = logging.getLogger("telegram_reporter")

TG_TOKEN   = os.getenv("TG_TOKEN", "")
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "")

# Названия месяцев
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
    """Форматирует число как 14 787 332."""
    return f"{int(round(n)):,}".replace(",", " ")


def _pct(a: float, b: float) -> str:
    return f"{a/b*100:.1f}%" if b else "—"


def build_weekly_report() -> str:
    """Собирает текстовый обзор текущей/прошедшей недели из БД."""
    from database import get_month_data, get_all_months

    # Определяем текущий месяц и неделю
    today   = datetime.now()
    month_n = today.month
    month   = MONTH_NAMES.get(month_n, "Май")

    # Ищем данные за текущий месяц
    data = get_month_data(month)
    if not data or not data.get("ops"):
        return f"⚠️ Нет данных за {month} — выписка не загружена."

    ops       = data.get("ops", [])
    weeks_out = data.get("weeks_out", [0]*5)
    weeks_in  = data.get("weeks_in",  [0]*5)

    # Определяем последнюю неделю с данными
    last_wi = -1
    for wi in range(4, -1, -1):
        if weeks_out[wi] > 0 or weeks_in[wi] > 0:
            last_wi = wi
            break
    if last_wi == -1:
        return f"⚠️ За {month} данных по неделям пока нет."

    week_num = last_wi + 1  # 1-based

    # Операции за эту неделю
    week_ops = [op for op in ops if op.get("week") == last_wi]
    debit_ops  = [op for op in week_ops if op.get("is_debit")]
    credit_ops = [op for op in week_ops if not op.get("is_debit")]

    total_out = weeks_out[last_wi]
    total_in  = weeks_in[last_wi]
    balance   = total_in - total_out

    # Даты недели из операций
    dates = sorted([op.get("date","") for op in week_ops if op.get("date")])
    period = f"{dates[0]} — {dates[-1]}" if len(dates) >= 2 else (dates[0] if dates else "—")

    # Расходы по категориям
    cat_totals: dict[str, float] = {}
    for op in debit_ops:
        cat = op.get("cat", "Прочее")
        cat_totals[cat] = cat_totals.get(cat, 0) + op.get("amount", 0)
    cat_sorted = sorted(cat_totals.items(), key=lambda x: -x[1])

    # Топ поставщики по лесу
    les_ops = [op for op in debit_ops if op.get("cat") == "Лес"]
    les_by_contr: dict[str, float] = {}
    for op in les_ops:
        c = op.get("contractor","—")
        les_by_contr[c] = les_by_contr.get(c,0) + op.get("amount",0)
    les_sorted = sorted(les_by_contr.items(), key=lambda x: -x[1])

    # Поступления по клиентам
    client_totals: dict[str, float] = {}
    for op in credit_ops:
        c = op.get("contractor","—")
        client_totals[c] = client_totals.get(c,0) + op.get("amount",0)
    client_sorted = sorted(client_totals.items(), key=lambda x: -x[1])

    # ── Формируем текст ─────────────────────────────────────────────────────
    sign   = "+" if balance >= 0 else "−"
    b_abs  = abs(balance)

    lines = [
        f"<b>НОВАТОР · ИТОГИ НЕДЕЛИ {week_num}</b>",
        f"<i>{period} · {month} 2026</i>",
        "",
        "<b>Общий баланс</b>",
        f"Поступлений — {_fmt(total_in)} ₽",
        f"Расходов — {_fmt(total_out)} ₽",
        f"{'Профицит' if balance >= 0 else 'Дефицит'} — {sign}{_fmt(b_abs)} ₽",
        "",
    ]

    # Поступления
    if credit_ops:
        lines.append(f"<b>Поступления от клиентов — {_fmt(total_in)} ₽</b>")
        for c, s in client_sorted[:8]:
            short = c[:45] + ("…" if len(c) > 45 else "")
            lines.append(f"· {short} — {_fmt(s)} ₽")
        if len(client_sorted) > 8:
            lines.append(f"· …ещё {len(client_sorted)-8} клиентов")
        lines.append("")

    # Расходы
    lines.append(f"<b>Расходы — {_fmt(total_out)} ₽</b>")
    for cat, s in cat_sorted[:10]:
        pct = _pct(s, total_out)
        line = f"· {cat} — {_fmt(s)} ₽ ({pct})"

        # Для леса добавляем поставщиков
        if cat == "Лес" and les_sorted:
            contr_str = ", ".join(
                f"{c.split()[-1]} {_fmt(v)}" for c, v in les_sorted[:5]
            )
            line += f"\n  {contr_str}"
        lines.append(line)

    lines += [
        "",
        "<b>Вывод</b>",
        _auto_conclusion(balance, total_out, total_in, cat_totals),
    ]

    return "\n".join(lines)


def _auto_conclusion(balance: float, total_out: float,
                     total_in: float, cats: dict) -> str:
    """Генерирует короткий автоматический вывод."""
    parts = []

    if balance > 0:
        parts.append("Неделя закрылась в плюсе.")
    elif balance < -1_000_000:
        parts.append("Неделя закрылась с дефицитом — расходы превысили поступления.")
    else:
        parts.append("Неделя вышла почти в ноль.")

    if cats.get("ЗП", 0) > 0:
        parts.append("Зарплата выплачена.")

    if cats.get("Лес", 0) > 0 and cats.get("Смола", 0) > 0:
        parts.append("Производственный цикл запущен: лес и смола закуплены.")
    elif cats.get("Лес", 0) > 0:
        parts.append("Лес закуплен.")

    return " ".join(parts)


def send_weekly_report() -> dict:
    """Основная точка входа — формирует и отправляет отчёт."""
    if not TG_TOKEN or not TG_CHAT_ID:
        return {"status": "skip", "reason": "TG_TOKEN или TG_CHAT_ID не заданы"}

    try:
        text = build_weekly_report()
        ok   = _tg_send(text)
        log.info("Telegram report sent: %s", ok)
        return {"status": "ok" if ok else "error", "length": len(text)}
    except Exception as e:
        log.error("send_weekly_report error: %s", e)
        return {"status": "error", "reason": str(e)}
