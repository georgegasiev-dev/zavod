"""
Telegram-репортёр Новатора.
Утренний отчёт 8:00 МСК — итоги вчерашнего дня.
Вечерний отчёт 16:30 МСК — движение за сегодня с % от плана.
"""
import os
import re
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

# Категории где раскрываем поставщиков
DETAIL_CATS = {"Лес", "Перевозка леса", "Смола", "Плёнка"}


# ── Утилиты ───────────────────────────────────────────────────────────────────

def _tg_send(text: str) -> bool:
    if not TG_TOKEN or not TG_CHAT_ID:
        log.warning("TG_TOKEN или TG_CHAT_ID не заданы")
        return False
    try:
        url  = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        data = json.dumps({"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML"}).encode()
        req  = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status == 200
    except Exception as e:
        log.error("Ошибка отправки в Telegram: %s", e)
        return False


def _fmt(n: float) -> str:
    """1234567 → 1 234 567"""
    return f"{int(round(n)):,}".replace(",", "\u00a0")


def _pct(fact: float, plan: float) -> str | None:
    if not plan:
        return None
    return f"{fact / plan * 100:.0f}%"


# Полные названия → аббревиатуры
_LEGAL_FORMS = [
    (r"общество с ограниченной ответственностью", "ООО"),
    (r"закрытое акционерное общество",            "ЗАО"),
    (r"открытое акционерное общество",            "ОАО"),
    (r"публичное акционерное общество",           "ПАО"),
    (r"акционерное общество",                     "АО"),
    (r"индивидуальный предприниматель",           "ИП"),
    (r"общество с дополнительной ответственностью","ОДО"),
]


def _clean_name(name: str) -> str:
    """
    Нормализует название контрагента:
    - Разворачивает полные юрформы → аббревиатуры
    - Убирает ', ИНН: XXXXXXXXXX'
    - Убирает 'р/с XXXXXXXX ...'
    """
    name = name.strip()
    # Нормализуем юридические формы (регистронезависимо)
    for pattern, abbr in _LEGAL_FORMS:
        name = re.sub(pattern, abbr, name, flags=re.IGNORECASE)
    # Убираем ', ИНН: ...' и ' ИНН ...' и ' ИНН —' (с тире вместо цифр)
    name = re.sub(r",?\s*ИНН[:\s\—\-]*\d*", "", name, flags=re.IGNORECASE)
    # Убираем 'р/с ...' или 'p/c ...' и всё после
    name = re.sub(r"\s+[рpрp]/[сc]\s+\S+.*", "", name, flags=re.IGNORECASE)
    # Убираем лишние пробелы и запятые в конце
    name = name.strip(" ,")
    return name


def _short_contractor(name: str) -> str:
    """Форматирует имя контрагента для отчёта."""
    name = _clean_name(name)
    # Только цифры — ИНН
    if name.replace(" ", "").isdigit():
        return f"ИНН {name}"
    # ИП — сокращаем до "ИП Фамилия"
    if re.match(r"^ип\s", name, re.IGNORECASE):
        parts = name.split()
        fam = parts[1] if len(parts) > 1 else ""
        return f"ИП {fam}" if fam else name
    # Физлицо ЗАГЛАВНЫМИ (не ООО/ЗАО/АО/ПАО/ИП) — берём первое слово (фамилия)
    if name.isupper() and " " in name and not re.match(r"^(ооо|зао|оао|пао|ао|ип)\b", name, re.IGNORECASE):
        return name.split()[0].capitalize()
    # Всё остальное — полное название без обрезки
    return name



def _month_income_total(month: str) -> float:
    """Считает сумму всех поступлений за месяц."""
    try:
        from database import get_month_data
        data = get_month_data(month)
        if not data or not data.get("ops"):
            return 0.0
        return sum(op.get("amount", 0) for op in data["ops"] if not op.get("is_debit"))
    except Exception:
        return 0.0


def _get_balance() -> str | None:
    try:
        from database import get_setting
        balance = get_setting("account_balance")
        updated = get_setting("balance_updated_at")
        if balance is None:
            return None
        b = float(balance)
        if updated:
            try:
                dt = datetime.fromisoformat(updated) + timedelta(hours=3)
                upd = dt.strftime("%d.%m %H:%M")
            except Exception:
                upd = updated[:16]
            return f"{_fmt(b)} ₽  <i>(на {upd} МСК)</i>"
        return f"{_fmt(b)} ₽"
    except Exception:
        return None


def _get_week_plan(month: str, week_idx: int) -> dict:
    try:
        from database import get_plan
        plan = get_plan(month)
        if not plan:
            from main import DEFAULT_PLAN
            plan = DEFAULT_PLAN
        return {k: (v[week_idx] if isinstance(v, list) and week_idx < len(v) else 0)
                for k, v in plan.items()}
    except Exception:
        return {}


def _week_fact_by_cat(ops: list, week_idx: int) -> dict[str, float]:
    """Суммы расходов по категориям за всю неделю."""
    totals: dict[str, float] = {}
    for op in ops:
        if op.get("is_debit") and op.get("week") == week_idx:
            cat = op.get("cat", "")
            totals[cat] = totals.get(cat, 0) + op.get("amount", 0)
    return totals


def _date_label(dt: datetime) -> str:
    return f"{dt.day} {MONTH_NAMES_GEN[dt.month]}"


def _ops_for_date(date_str: str):
    """Возвращает (credit_ops, debit_ops, all_ops, data) для даты dd.mm.yyyy."""
    from database import get_month_data
    try:
        dt = datetime.strptime(date_str, "%d.%m.%Y")
    except ValueError:
        return [], [], [], {}
    month = MONTH_NAMES.get(dt.month, "")
    data  = get_month_data(month)
    if not data or not data.get("ops"):
        return [], [], [], data
    all_ops    = data.get("ops", [])
    day_ops    = [op for op in all_ops if op.get("date", "") == date_str]
    credit_ops = [op for op in day_ops if not op.get("is_debit")]
    debit_ops  = [op for op in day_ops if op.get("is_debit")]
    return credit_ops, debit_ops, all_ops, data


# ── Блоки отчёта ──────────────────────────────────────────────────────────────

def _block_income(credit_ops: list) -> list[str]:
    lines = []
    if not credit_ops:
        return lines
    totals: dict[str, float] = {}
    for op in credit_ops:
        c = _clean_name(op.get("contractor", "—"))
        totals[c] = totals.get(c, 0) + op.get("amount", 0)
    lines.append("<b>Поступления от клиентов:</b>")
    for c, s in sorted(totals.items(), key=lambda x: -x[1]):
        lines.append(f"  {c} — {_fmt(s)} руб.")
    lines.append("")
    return lines


def _block_expenses(debit_ops: list, all_ops: list, week_idx: int | None,
                    week_plan: dict) -> list[str]:
    lines = []
    if not debit_ops:
        return lines

    cat_totals: dict[str, float] = {}
    cat_contrs: dict[str, dict]  = {}
    for op in debit_ops:
        cat = op.get("cat", "Прочее")
        amt = op.get("amount", 0)
        cat_totals[cat] = cat_totals.get(cat, 0) + amt
        c = op.get("contractor", "—")
        cat_contrs.setdefault(cat, {})
        cat_contrs[cat][c] = cat_contrs[cat].get(c, 0) + amt

    week_fact = _week_fact_by_cat(all_ops, week_idx) if week_idx is not None else {}
    total_out = sum(cat_totals.values())

    lines.append("<b>Расходы:</b>")
    for cat, s in sorted(cat_totals.items(), key=lambda x: -x[1]):
        plan_key   = CAT_TO_PLAN_KEY.get(cat)
        plan_val   = week_plan.get(plan_key, 0) if plan_key else 0
        week_total = week_fact.get(cat, 0)

        cat_line = f"  {cat} — {_fmt(s)} руб."
        # Если в неделе накоплено больше чем за день — показываем недельный итог
        if week_total > s:
            cat_line += f" (Всего {_fmt(week_total)} с начала недели)"
        elif plan_val:
            pct = _pct(s, plan_val)
            if pct:
                cat_line += f" ({pct} от плана недели)"
        lines.append(cat_line)

        # Детализация поставщиков
        contrs = cat_contrs.get(cat, {})
        if cat in DETAIL_CATS or len(contrs) > 1:
            for c, v in sorted(contrs.items(), key=lambda x: -x[1]):
                lines.append(f"    {_short_contractor(c)} — {_fmt(v)} руб.")

    return lines


# ── Вечерний отчёт (16:30) ────────────────────────────────────────────────────

def build_evening_report(target_date: str | None = None) -> str:
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

    credit_ops, debit_ops, all_ops, data = _ops_for_date(date_str)
    if not credit_ops and not debit_ops:
        return f"📭 Отчёт о движении денег за {_date_label(dt)}\n\nОпераций в выписке нет."

    total_in  = sum(op.get("amount", 0) for op in credit_ops)
    total_out = sum(op.get("amount", 0) for op in debit_ops)

    week_idx  = (credit_ops or debit_ops)[0].get("week")
    week_plan = _get_week_plan(month, week_idx) if week_idx is not None else {}
    plan_in   = week_plan.get("prikhod", 0)
    plan_out  = sum(v for k, v in week_plan.items() if k not in ("prikhod", "proizvod"))

    lines = [f"<b>Отчёт о движении денег за {_date_label(dt)}</b>", ""]

    balance = _get_balance()
    month_total_in = _month_income_total(month)
    if balance:
        lines.append(f"На счету: {balance}")
        if month_total_in:
            lines.append(f"  Всего поступило с начала месяца: {_fmt(month_total_in)} ₽")
        lines.append("")

    in_line  = f"Поступлений — {_fmt(total_in)} ₽"
    out_line = f"Расходов — {_fmt(total_out)} ₽"
    pct_in   = _pct(total_in, plan_in)
    pct_out  = _pct(total_out, plan_out)
    if pct_in:
        in_line  += f" ({pct_in} от плана недели)"
    if pct_out:
        out_line += f" ({pct_out} от плана недели)"
    lines += [in_line, out_line, ""]

    lines += _block_income(credit_ops)
    lines += _block_expenses(debit_ops, all_ops, week_idx, week_plan)

    return "\n".join(lines)


# ── Утренний отчёт (8:00) ─────────────────────────────────────────────────────

def build_morning_report(target_date: str | None = None) -> str:
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

    credit_ops, debit_ops, all_ops, _ = _ops_for_date(date_str)
    total_in  = sum(op.get("amount", 0) for op in credit_ops)
    total_out = sum(op.get("amount", 0) for op in debit_ops)

    lines = [f"<b>Отчёт на утро {_date_label(today_dt)}</b>", ""]

    balance  = _get_balance()
    dt_month = MONTH_NAMES.get(dt.month, "")
    month_total_in = _month_income_total(dt_month)
    if balance:
        lines.append(f"На счету: {balance}")
        if month_total_in:
            lines.append(f"  Всего поступило с начала месяца: {_fmt(month_total_in)} ₽")
        lines.append("")

    if not credit_ops and not debit_ops:
        lines.append(f"За {_date_label(dt)} операций в выписке нет.")
        return "\n".join(lines)

    lines += [
        f"За {_date_label(dt)} поступило от клиентов: {_fmt(total_in)} ₽",
        f"Расходы за {_date_label(dt)}: {_fmt(total_out)} ₽",
        "",
    ]

    lines += _block_income(credit_ops)

    week_idx = (credit_ops or debit_ops)[0].get("week") if (credit_ops or debit_ops) else None
    lines += _block_expenses(debit_ops, all_ops, week_idx, {})

    return "\n".join(lines)


# ── Отправка ──────────────────────────────────────────────────────────────────

def send_evening_report(target_date: str | None = None) -> dict:
    if not TG_TOKEN or not TG_CHAT_ID:
        return {"status": "skip"}
    try:
        text = build_evening_report(target_date)
        ok   = _tg_send(text)
        return {"status": "ok" if ok else "error", "length": len(text)}
    except Exception as e:
        log.error("send_evening_report: %s", e)
        return {"status": "error", "reason": str(e)}


def send_morning_report(target_date: str | None = None) -> dict:
    if not TG_TOKEN or not TG_CHAT_ID:
        return {"status": "skip"}
    try:
        text = build_morning_report(target_date)
        ok   = _tg_send(text)
        return {"status": "ok" if ok else "error", "length": len(text)}
    except Exception as e:
        log.error("send_morning_report: %s", e)
        return {"status": "error", "reason": str(e)}


# ── Алиасы ────────────────────────────────────────────────────────────────────
def build_daily_report(target_date=None):  return build_evening_report(target_date)
def build_weekly_report():                 return build_morning_report()
def send_daily_report(target_date=None):   return send_evening_report(target_date)
def send_weekly_report():                  return send_morning_report()


# ── Еженедельный отчёт (понедельник 7:45) ────────────────────────────────────

def build_weekly_summary_report() -> str:
    """
    Полный отчёт за прошлую неделю (пн–вс, календарная).
    """
    from database import get_month_data, get_week_balance

    # Определяем прошлую неделю (пн–вс)
    today    = datetime.now()
    last_mon = today - timedelta(days=today.weekday() + 7)
    last_sun = last_mon + timedelta(days=6)

    mon_label = _date_label(last_mon)
    sun_label = _date_label(last_sun)
    mon_iso   = last_mon.strftime("%Y-%m-%d")

    # Месяцы которые покрывает неделя
    months_needed = set()
    for i in range(7):
        dt = last_mon + timedelta(days=i)
        months_needed.add(MONTH_NAMES.get(dt.month, ""))

    # Все операции за неделю — перебираем каждый день явно
    all_credit: list = []
    all_debit:  list = []
    week_dates = set()
    for i in range(7):
        dt = last_mon + timedelta(days=i)
        week_dates.add(dt.strftime("%d.%m.%Y"))

    for month in months_needed:
        if not month:
            continue
        data = get_month_data(month)
        if not data or not data.get("ops"):
            continue
        for op in data.get("ops", []):
            if op.get("date", "") in week_dates:
                if op.get("is_debit"):
                    all_debit.append(op)
                else:
                    all_credit.append(op)

    if not all_credit and not all_debit:
        return f"📭 Итоги прошлой недели {mon_label}–{sun_label}\n\nОпераций в выписке нет."

    total_in  = sum(op.get("amount", 0) for op in all_credit)
    total_out = sum(op.get("amount", 0) for op in all_debit)

    # Балансы
    week_bal = get_week_balance(mon_iso)
    opening  = week_bal.get("opening") if week_bal else None
    closing  = week_bal.get("closing") if week_bal else None
    now_bal  = _get_balance()

    # ── Заголовок ────────────────────────────────────────────────────────────
    lines = [
        f"<b>НОВАТОР · ИТОГИ ПРОШЛОЙ НЕДЕЛИ</b>",
        f"<b>{mon_label} — {sun_label}</b>",
        "",
    ]

    # Балансы в начале
    if now_bal:
        lines.append(f"Баланс на данную минуту: {now_bal}")
    if opening is not None:
        lines.append(f"Баланс на начало прошлой недели: {_fmt(opening)} ₽")
    if closing is not None:
        lines.append(f"Баланс на конец прошлой недели: {_fmt(closing)} ₽")
    lines.append("")

    # Итоги
    lines += [
        f"Поступило за прошлую неделю — {_fmt(total_in)} ₽",
        f"Расходов за прошлую неделю — {_fmt(total_out)} ₽",
        "",
    ]

    # ── Поступления от клиентов ───────────────────────────────────────────────
    if all_credit:
        client_totals: dict[str, float] = {}
        for op in all_credit:
            c = _clean_name(op.get("contractor", "—"))
            client_totals[c] = client_totals.get(c, 0) + op.get("amount", 0)
        lines.append("<b>Поступления от клиентов:</b>")
        for c, s in sorted(client_totals.items(), key=lambda x: -x[1]):
            lines.append(f"  {c} — {_fmt(s)} руб.")
        lines.append("")

    # ── Расходы по категориям ────────────────────────────────────────────────
    if all_debit:
        cat_totals: dict[str, float] = {}
        cat_contrs: dict[str, dict]  = {}
        for op in all_debit:
            cat = op.get("cat", "Прочее")
            amt = op.get("amount", 0)
            cat_totals[cat] = cat_totals.get(cat, 0) + amt
            c = op.get("contractor", "—")
            cat_contrs.setdefault(cat, {})
            cat_contrs[cat][c] = cat_contrs[cat].get(c, 0) + amt

        lines.append("<b>Расходы:</b>")
        for cat, s in sorted(cat_totals.items(), key=lambda x: -x[1]):
            contrs = cat_contrs.get(cat, {})
            has_detail = cat in DETAIL_CATS or len(contrs) > 1

            if has_detail:
                # Категория с детализацией — выделяем жирным
                lines.append(f"<b>  {cat} — {_fmt(s)} руб.</b>")
                for c, v in sorted(contrs.items(), key=lambda x: -x[1]):
                    lines.append(f"    {_short_contractor(c)} — {_fmt(v)} руб.")
                lines.append("")  # пустая строка после блока
            else:
                lines.append(f"  {cat} — {_fmt(s)} руб.")
                lines.append("")  # пустая строка после категории

    return "\n".join(lines)


def send_weekly_summary_report() -> dict:
    if not TG_TOKEN or not TG_CHAT_ID:
        return {"status": "skip"}
    try:
        text = build_weekly_summary_report()
        ok   = _tg_send(text)
        return {"status": "ok" if ok else "error", "length": len(text)}
    except Exception as e:
        log.error("send_weekly_summary_report: %s", e)
        return {"status": "error", "reason": str(e)}


# ── Отчёт /babki — поступления с начала недели ───────────────────────────────

def build_babki_report() -> str:
    """Текущий баланс + поступления с начала месяца + с начала недели + список."""
    from database import get_month_data

    today    = datetime.now()
    # Начало текущей недели (понедельник)
    week_mon = today - timedelta(days=today.weekday())
    month    = MONTH_NAMES.get(today.month, "")

    # Все даты текущей недели до сегодня включительно
    week_dates = set()
    for i in range((today - week_mon).days + 1):
        dt = week_mon + timedelta(days=i)
        week_dates.add(dt.strftime("%d.%m.%Y"))

    data = get_month_data(month)
    ops  = data.get("ops", []) if data else []

    # Поступления с начала месяца
    month_income = sum(op.get("amount", 0) for op in ops if not op.get("is_debit"))

    # Поступления с начала недели
    week_ops = [op for op in ops
                if not op.get("is_debit") and op.get("date", "") in week_dates]
    week_income = sum(op.get("amount", 0) for op in week_ops)

    # Группируем по контрагенту
    client_totals: dict[str, float] = {}
    for op in week_ops:
        c = _clean_name(op.get("contractor", "—"))
        client_totals[c] = client_totals.get(c, 0) + op.get("amount", 0)

    # Формируем отчёт
    mon_label = _date_label(week_mon)
    today_label = _date_label(today)

    lines = [f"<b>💰 БАБКИ · {today_label}</b>", ""]

    balance = _get_balance()
    if balance:
        lines.append(f"На счету: {balance}")

    lines.append(f"Всего поступило с начала месяца: {_fmt(month_income)} ₽")
    lines.append(f"Поступило с начала недели: {_fmt(week_income)} ₽")
    lines.append("")

    if client_totals:
        lines.append(f"<b>Поступления с {mon_label}:</b>")
        for c, s in sorted(client_totals.items(), key=lambda x: -x[1]):
            lines.append(f"  {c} — {_fmt(s)} руб.")
    else:
        lines.append("Поступлений с начала недели нет.")

    # Расходы с начала недели
    week_debit_ops = [op for op in ops
                     if op.get("is_debit") and op.get("date", "") in week_dates]
    week_expenses = sum(op.get("amount", 0) for op in week_debit_ops)

    if week_debit_ops:
        cat_totals: dict[str, float] = {}
        cat_contrs: dict[str, dict]  = {}
        for op in week_debit_ops:
            cat = op.get("cat", "Прочее")
            amt = op.get("amount", 0)
            cat_totals[cat] = cat_totals.get(cat, 0) + amt
            c = op.get("contractor", "—")
            cat_contrs.setdefault(cat, {})
            cat_contrs[cat][c] = cat_contrs[cat].get(c, 0) + amt

        lines.append("")
        lines.append(f"<b>Расходы с {mon_label} — {_fmt(week_expenses)} ₽:</b>")
        for cat, s in sorted(cat_totals.items(), key=lambda x: -x[1]):
            contrs   = cat_contrs.get(cat, {})
            has_detail = cat in DETAIL_CATS or len(contrs) > 1
            lines.append(f"  {cat} — {_fmt(s)} руб.")
            if has_detail:
                for c, v in sorted(contrs.items(), key=lambda x: -x[1]):
                    lines.append(f"    {_short_contractor(c)} — {_fmt(v)} руб.")
            lines.append("")

    return "\n".join(lines)


def send_babki_report() -> dict:
    if not TG_TOKEN or not TG_CHAT_ID:
        return {"status": "skip"}
    try:
        text = build_babki_report()
        ok   = _tg_send(text)
        return {"status": "ok" if ok else "error", "length": len(text)}
    except Exception as e:
        log.error("send_babki_report: %s", e)
        return {"status": "error", "reason": str(e)}


# ─── ЕОВР отчёт ────────────────────────────────────────────────────────────

MONTH_FULL_RU = ['','Январь','Февраль','Март','Апрель','Май','Июнь',
                 'Июль','Август','Сентябрь','Октябрь','Ноябрь','Декабрь']
MONTH_SHORT_RU = ['','янв','фев','мар','апр','май','июн',
                  'июл','авг','сен','окт','ноя','дек']

_METRICS = [
    ('lush',   'Лущилка',   'л'),
    ('sush',   'Сушилка',   'л'),
    ('sborka', 'Сборка',    'л'),
    ('lam',    'Ламинация', 'м²'),
    ('obr',    'Обрезка',   'л'),
]

def _fmt_n(v: int) -> str:
    """12 386 → '12 386'"""
    return f"{v:,}".replace(',', '\u202f')  # узкий неразрывный пробел


def build_eovr_report(year: int | None = None, month: int | None = None) -> str:
    """
    Отчёт ЕОВР:
    - итоги с начала месяца по каждому переделу
    - детализация по дням для каждого передела
    """
    from database import get_eovr_year, get_eovr_days, get_eovr_latest_updated
    from datetime import datetime as dt

    now = dt.now()
    if year  is None: year  = now.year
    if month is None: month = now.month

    # Найти нужный лист
    months_data = get_eovr_year(year)
    target = next((m for m in months_data if m['month'] == month), None)

    if not target:
        return (f"📋 <b>ЕОВР {MONTH_FULL_RU[month]} {year}</b>\n\n"
                f"Данных нет. Запусти /sync_eovr для загрузки.")

    sheet_title = target['sheet_title']
    days = get_eovr_days(sheet_title)  # [{day, lush, sush, sborka, lam, obr}, ...]
    updated = (target.get('updated_at') or '')[:16]
    mon_short = MONTH_SHORT_RU[month]

    lines = [
        f"📋 <b>ЕОВР {MONTH_FULL_RU[month]} {year}</b>",
        f"<i>Обновлено: {updated}</i>",
        "",
        "━━━ Итого с начала месяца ━━━",
    ]

    for key, label, unit in _METRICS:
        val = int(target.get(key, 0))
        lines.append(f"{label}: <b>{_fmt_n(val)}</b> {unit}")

    from datetime import datetime as _dt
    today_dt = _dt.now()
    today = today_dt.day
    is_current_month = (year == today_dt.year and month == today_dt.month)
    show_from = max(1, today - 4) if is_current_month else 1

    lines.append("")
    if is_current_month:
        lines.append(f"━━━ Последние 5 дней ({show_from}.{month:02}–{today}.{month:02}) ━━━")
    else:
        lines.append("━━━ Детализация по дням ━━━")

    for key, label, unit in _METRICS:
        lines.append("")
        lines.append(f"<b>{label}:</b>")
        if not days:
            lines.append("  нет данных")
            continue
        for d in days:
            day_num = d['day']
            if is_current_month and day_num > today:
                continue
            if is_current_month and day_num < show_from:
                continue
            val = int(d.get(key, 0))
            val_str = _fmt_n(val) if val > 0 else '0'
            lines.append(f"  {day_num:2}.{month:02}  {val_str}")

    return "\n".join(lines)


def send_eovr_report(year: int | None = None, month: int | None = None) -> dict:
    if not TG_TOKEN or not TG_CHAT_ID:
        return {"status": "skip"}
    try:
        text = build_eovr_report(year, month)
        ok   = _tg_send(text)
        return {"status": "ok" if ok else "error", "length": len(text)}
    except Exception as e:
        log.error("send_eovr_report: %s", e)
        return {"status": "error", "reason": str(e)}
