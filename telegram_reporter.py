"""
Telegram-репортёр Новатора.
Утренний отчёт 8:00 МСК — итоги вчерашнего дня.
Вечерний отчёт 16:30 МСК — движение за сегодня с % от плана.
"""
import os
import logging
import urllib.request
import json
from datetime import datetime, timedelta

log = logging.getLogger("telegram_reporter")

TG_TOKEN   = os.getenv("TG_TOKEN", "")
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "")

MONTH_NAMES = {
    1:"Январь",2:"Февраль",3:"Март",4:"Апрель",5:"Май",6:"Июнь",
    7:"Июль",8:"Август",9:"Сентябрь",10:"Октябрь",11:"Ноябрь",12:"Декабрь"
}

MONTH_NAMES_GEN = {
    1:"января",2:"февраля",3:"марта",4:"апреля",5:"мая",6:"июня",
    7:"июля",8:"августа",9:"сентября",10:"октября",11:"ноября",12:"декабря"
}

# Маппинг категория ops → ключ плана (должен совпадать с main.py CAT_TO_PLAN_KEY)
CAT_TO_PLAN_KEY = {
    "Лес":                    "les",
    "Долг лес":               "dolg_les",
    "Перевозка леса":         "perev_les",
    "Смола":                  "smola",
    "Плёнка":                 "plenka",
    "ГСМ":                    "gsm",
    "Расходники":             "rashod",
    "Электроэнергия":         "svet",
    "Вывоз мусора":           "vuvozmys",
    "НДС":                    "nds",
    "НДФЛ":                   "ndfl",
    "Упаковка":               "upakovka",
    "Перевозчики":            "perevozch",
    "ЗП аванс":               "zp_avans",
    "ЗП официальная":         "zp_ofic",
    "Больничные":             "zp_bolnich",
    "ЗП":                     "zp_avans",
    "Аренда помещений":       "arenda",
    "Административные":       "adm",
    "Прочие нераспознанные":  "prochie",
    "Поступления от клиентов":"prikhod",
}



def _short_contractor(c: str) -> str:
    """Форматирует имя контрагента для отчёта."""
    c = c.strip()
    # Если строка — только цифры (ИНН/КПП) — показываем как есть с пометкой
    if c.replace(" ", "").isdigit():
        return f"ИНН {c}"
    # Для ИП берём фамилию (последнее слово)
    if c.lower().startswith("ип "):
        return c.split()[-1]
    # Остальные — обрезаем до 45 символов
    return c[:45] + ("…" if len(c) > 45 else "")


def _tg_send(text: str) -> bool:
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
    return f"{int(round(n)):,}".replace(",", "\u00a0")


def _pct(fact: float, plan: float) -> str | None:
    if not plan:
        return None
    return f"{fact / plan * 100:.0f}%"


def _get_balance() -> str | None:
    """Возвращает последний известный баланс счёта из БД."""
    try:
        from database import get_setting
        balance = get_setting("account_balance")
        updated = get_setting("balance_updated_at")
        if balance is None:
            return None
        b = float(balance)
        if updated:
            try:
                dt = datetime.fromisoformat(updated)
                dt_msk = dt + timedelta(hours=3)
                upd_str = dt_msk.strftime("%d.%m %H:%M")
            except Exception:
                upd_str = updated[:16]
            return f"{_fmt(b)} ₽  <i>(на {upd_str} МСК)</i>"
        return f"{_fmt(b)} ₽"
    except Exception:
        return None


def _get_week_plan(month: str, week_idx: int) -> dict:
    """Возвращает план на неделю: {cat_key: сумма}."""
    try:
        from database import get_plan
        plan = get_plan(month)
        if not plan:
            # fallback к дефолтному плану из main
            try:
                from main import DEFAULT_PLAN
                plan = DEFAULT_PLAN
            except Exception:
                return {}
        return {k: (v[week_idx] if isinstance(v, list) and week_idx < len(v) else 0)
                for k, v in plan.items()}
    except Exception:
        return {}


def _ops_for_date(date_str: str) -> tuple[list, list, dict]:
    """
    Возвращает (credit_ops, debit_ops, data) для даты в формате dd.mm.yyyy.
    """
    from database import get_month_data

    try:
        dt = datetime.strptime(date_str, "%d.%m.%Y")
    except ValueError:
        return [], [], {}

    month = MONTH_NAMES.get(dt.month, "")
    data  = get_month_data(month)
    if not data or not data.get("ops"):
        return [], [], data

    ops        = data.get("ops", [])
    day_ops    = [op for op in ops if op.get("date", "") == date_str]
    credit_ops = [op for op in day_ops if not op.get("is_debit")]
    debit_ops  = [op for op in day_ops if op.get("is_debit")]
    return credit_ops, debit_ops, data


def _date_label(dt: datetime) -> str:
    """10 июня"""
    return f"{dt.day} {MONTH_NAMES_GEN[dt.month]}"


# ─────────────────────────────────────────────────────────────────────────────
# ВЕЧЕРНИЙ ОТЧЁТ (16:30) — движение за сегодня + % от плана
# ─────────────────────────────────────────────────────────────────────────────

def build_evening_report(target_date: str | None = None) -> str:
    """
    target_date: 'today' или None → сегодня, 'YYYY-MM-DD' / 'DD.MM.YYYY' → конкретная дата.
    """
    from database import get_month_data

    # Определяем дату
    if target_date and target_date != "today":
        try:
            dt = datetime.strptime(target_date, "%Y-%m-%d")
        except ValueError:
            try:
                dt = datetime.strptime(target_date, "%d.%m.%Y")
            except ValueError:
                dt = datetime.now()
    else:
        dt = datetime.now()

    date_str = dt.strftime("%d.%m.%Y")
    month    = MONTH_NAMES.get(dt.month, "")

    credit_ops, debit_ops, data = _ops_for_date(date_str)

    if not credit_ops and not debit_ops:
        return f"📭 Отчёт о движении денег за {_date_label(dt)}\n\nОпераций в выписке нет."

    total_in  = sum(op.get("amount", 0) for op in credit_ops)
    total_out = sum(op.get("amount", 0) for op in debit_ops)

    # Определяем индекс недели для плана
    week_idx = None
    if credit_ops:
        week_idx = credit_ops[0].get("week")
    elif debit_ops:
        week_idx = debit_ops[0].get("week")

    week_plan = _get_week_plan(month, week_idx) if week_idx is not None else {}

    plan_in  = week_plan.get("prikhod", 0)
    plan_out = sum(v for k, v in week_plan.items() if k != "prikhod" and k != "proizvod")

    pct_in  = _pct(total_in,  plan_in)
    pct_out = _pct(total_out, plan_out)

    # ── Заголовок ────────────────────────────────────────────────────────────
    lines = [f"<b>Отчёт о движении денег за {_date_label(dt)}</b>", ""]

    # Баланс
    balance = _get_balance()
    if balance:
        lines += [f"На счету: {balance}", ""]

    # Итоги
    in_str  = f"Поступлений — {_fmt(total_in)} ₽"
    out_str = f"Расходов — {_fmt(total_out)} ₽"
    if pct_in:
        in_str  += f" ({pct_in} от плана недели)"
    if pct_out:
        out_str += f" ({pct_out} от плана недели)"
    lines += [in_str, out_str, ""]

    # ── Поступления ───────────────────────────────────────────────────────────
    if credit_ops:
        client_totals: dict[str, float] = {}
        for op in credit_ops:
            c = op.get("contractor", "—")
            client_totals[c] = client_totals.get(c, 0) + op.get("amount", 0)
        lines.append("<b>Поступления от клиентов:</b>")
        for c, s in sorted(client_totals.items(), key=lambda x: -x[1]):
            short = c[:55] + ("…" if len(c) > 55 else "")
            lines.append(f"  {short} — {_fmt(s)} руб.")
        lines.append("")

    # ── Расходы ───────────────────────────────────────────────────────────────
    if debit_ops:
        cat_totals:    dict[str, float] = {}
        cat_contrs:    dict[str, dict]  = {}
        for op in debit_ops:
            cat = op.get("cat", "Прочее")
            amt = op.get("amount", 0)
            cat_totals[cat] = cat_totals.get(cat, 0) + amt
            c = op.get("contractor", "—")
            cat_contrs.setdefault(cat, {})
            cat_contrs[cat][c] = cat_contrs[cat].get(c, 0) + amt

        lines.append("<b>Расходы:</b>")
        for cat, s in sorted(cat_totals.items(), key=lambda x: -x[1]):
            plan_key = CAT_TO_PLAN_KEY.get(cat)
            plan_val = week_plan.get(plan_key, 0) if plan_key else 0
            pct_cat  = _pct(s, plan_val)

            cat_line = f"  {cat} — {_fmt(s)} руб."
            if pct_cat:
                cat_line += f" ({pct_cat} от плана недели)"
            lines.append(cat_line)

            # Детализация поставщиков для категорий с несколькими контрагентами
            contrs = cat_contrs.get(cat, {})
            if len(contrs) > 1 or cat in ("Лес", "Перевозка леса", "Смола", "Плёнка"):
                lines.append("       кому:")
                for c, v in sorted(contrs.items(), key=lambda x: -x[1]):
                    # Короткое имя: последнее значимое слово для ИП
                    short_c = _short_contractor(c)
                    lines.append(f"       · {short_c} {_fmt(v)}")

    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# УТРЕННИЙ ОТЧЁТ (8:00) — итоги вчерашнего дня
# ─────────────────────────────────────────────────────────────────────────────

def build_morning_report(target_date: str | None = None) -> str:
    """
    target_date: None → вчера, 'DD.MM.YYYY' или 'YYYY-MM-DD' → конкретная дата.
    """
    if target_date:
        try:
            dt = datetime.strptime(target_date, "%Y-%m-%d")
        except ValueError:
            try:
                dt = datetime.strptime(target_date, "%d.%m.%Y")
            except ValueError:
                dt = datetime.now() - timedelta(days=1)
    else:
        dt = datetime.now() - timedelta(days=1)

    date_str = dt.strftime("%d.%m.%Y")
    today_dt = datetime.now()

    credit_ops, debit_ops, _ = _ops_for_date(date_str)

    total_in  = sum(op.get("amount", 0) for op in credit_ops)
    total_out = sum(op.get("amount", 0) for op in debit_ops)

    # ── Заголовок ────────────────────────────────────────────────────────────
    lines = [f"<b>Отчёт на утро {_date_label(today_dt)}</b>", ""]

    # Баланс
    balance = _get_balance()
    if balance:
        lines += [f"На счету: {balance}", ""]

    if not credit_ops and not debit_ops:
        lines.append(f"За {_date_label(dt)} операций в выписке нет.")
        return "\n".join(lines)

    # Итоги вчера
    lines += [
        f"За {_date_label(dt)} поступило от клиентов: {_fmt(total_in)} ₽",
        f"Расходы за {_date_label(dt)}: {_fmt(total_out)} ₽",
        "",
    ]

    # ── Поступления ───────────────────────────────────────────────────────────
    if credit_ops:
        client_totals: dict[str, float] = {}
        for op in credit_ops:
            c = op.get("contractor", "—")
            client_totals[c] = client_totals.get(c, 0) + op.get("amount", 0)
        lines.append("<b>Поступления:</b>")
        for c, s in sorted(client_totals.items(), key=lambda x: -x[1]):
            short = c[:55] + ("…" if len(c) > 55 else "")
            lines.append(f"  {short} — {_fmt(s)} руб.")
        lines.append("")

    # ── Расходы ───────────────────────────────────────────────────────────────
    if debit_ops:
        cat_totals: dict[str, float] = {}
        cat_contrs: dict[str, dict]  = {}
        for op in debit_ops:
            cat = op.get("cat", "Прочее")
            amt = op.get("amount", 0)
            cat_totals[cat] = cat_totals.get(cat, 0) + amt
            c = op.get("contractor", "—")
            cat_contrs.setdefault(cat, {})
            cat_contrs[cat][c] = cat_contrs[cat].get(c, 0) + amt

        lines.append("<b>Расходы:</b>")
        for cat, s in sorted(cat_totals.items(), key=lambda x: -x[1]):
            lines.append(f"  {cat} — {_fmt(s)} руб.")
            contrs = cat_contrs.get(cat, {})
            if len(contrs) > 1 or cat in ("Лес", "Перевозка леса", "Смола", "Плёнка"):
                for c, v in sorted(contrs.items(), key=lambda x: -x[1]):
                    short_c = _short_contractor(c)
                    lines.append(f"    · {short_c} {_fmt(v)}")

    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Отправка
# ─────────────────────────────────────────────────────────────────────────────

def send_evening_report(target_date: str | None = None) -> dict:
    if not TG_TOKEN or not TG_CHAT_ID:
        return {"status": "skip", "reason": "TG_TOKEN или TG_CHAT_ID не заданы"}
    try:
        text = build_evening_report(target_date)
        ok   = _tg_send(text)
        return {"status": "ok" if ok else "error", "length": len(text)}
    except Exception as e:
        log.error("send_evening_report error: %s", e)
        return {"status": "error", "reason": str(e)}


def send_morning_report(target_date: str | None = None) -> dict:
    if not TG_TOKEN or not TG_CHAT_ID:
        return {"status": "skip", "reason": "TG_TOKEN или TG_CHAT_ID не заданы"}
    try:
        text = build_morning_report(target_date)
        ok   = _tg_send(text)
        return {"status": "ok" if ok else "error", "length": len(text)}
    except Exception as e:
        log.error("send_morning_report error: %s", e)
        return {"status": "error", "reason": str(e)}


# ── Алиасы для обратной совместимости ────────────────────────────────────────
def build_daily_report(target_date=None):
    return build_evening_report(target_date)

def build_weekly_report():
    return build_morning_report()

def send_daily_report(target_date=None):
    return send_evening_report(target_date)

def send_weekly_report():
    return send_morning_report()
