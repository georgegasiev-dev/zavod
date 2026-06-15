"""
AI-агент Новатора на базе Claude API.
Понимает свободный текст, работает с данными выписки.
"""
import os
import json
import logging
import urllib.request
from datetime import datetime, timedelta

log = logging.getLogger("ai_agent")

CLAUDE_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL   = "claude-sonnet-4-6"

MONTH_NAMES = {
    1:"Январь",2:"Февраль",3:"Март",4:"Апрель",5:"Май",6:"Июнь",
    7:"Июль",8:"Август",9:"Сентябрь",10:"Октябрь",11:"Ноябрь",12:"Декабрь"
}

# ── Инструменты для агента ────────────────────────────────────────────────────

TOOLS = [
    {
        "name": "search_operations",
        "description": (
            "Поиск операций по выписке. Можно фильтровать по контрагенту, "
            "категории, дате, сумме, типу (приход/расход). "
            "Возвращает список операций с датой, суммой, контрагентом, категорией."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Поисковый запрос — имя контрагента или часть названия"
                },
                "category": {
                    "type": "string",
                    "description": "Категория расхода: Лес, Смола, Плёнка, ЗП, Аренда и т.д."
                },
                "date_from": {
                    "type": "string",
                    "description": "Начало периода в формате DD.MM.YYYY"
                },
                "date_to": {
                    "type": "string",
                    "description": "Конец периода в формате DD.MM.YYYY"
                },
                "direction": {
                    "type": "string",
                    "enum": ["приход", "расход", "все"],
                    "description": "Тип операции"
                },
                "limit": {
                    "type": "integer",
                    "description": "Максимум операций в ответе (по умолчанию 50)"
                }
            }
        }
    },
    {
        "name": "get_summary",
        "description": (
            "Получить сводку за период: общие поступления, расходы, "
            "разбивку по категориям, топ контрагентов."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "date_from": {
                    "type": "string",
                    "description": "Начало периода DD.MM.YYYY"
                },
                "date_to": {
                    "type": "string",
                    "description": "Конец периода DD.MM.YYYY"
                },
                "month": {
                    "type": "string",
                    "description": "Месяц по-русски: Январь, Февраль и т.д."
                }
            }
        }
    },
    {
        "name": "get_balance",
        "description": "Получить текущий баланс счёта.",
        "input_schema": {
            "type": "object",
            "properties": {}
        }
    },
    {
        "name": "get_contractor_history",
        "description": "Вся история операций с конкретным контрагентом за всё время.",
        "input_schema": {
            "type": "object",
            "required": ["contractor_name"],
            "properties": {
                "contractor_name": {
                    "type": "string",
                    "description": "Имя или часть имени контрагента"
                }
            }
        }
    }
]

SYSTEM_PROMPT = """Ты финансовый ассистент компании Новатор. 
Отвечаешь только на русском языке. Кратко и по делу.
Работаешь с банковской выпиской: поступления от клиентов, расходы по категориям.
Основные категории расходов: Лес, Смола, Плёнка, ЗП, Аренда, ГСМ, Расходники, Электроэнергия.
Сегодня {today}. Текущий месяц: {month}.
Когда пользователь спрашивает про операции — используй инструменты для поиска данных.
Форматируй суммы с пробелами: 1 234 567 ₽."""


# ── Реализация инструментов ───────────────────────────────────────────────────

def _get_all_ops() -> list:
    """Возвращает все операции из всех месяцев."""
    from database import get_all_months, get_month_data
    all_ops = []
    try:
        months = get_all_months()
        for m in months:
            month = m.get("month") or m.get("name", "")
            data  = get_month_data(month)
            if data and data.get("ops"):
                for op in data["ops"]:
                    op["_month"] = month
                    all_ops.append(op)
    except Exception as e:
        log.error("_get_all_ops error: %s", e)
    return all_ops


def _parse_date(s: str) -> datetime | None:
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d.%m.%y"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _fmt(n: float) -> str:
    return f"{int(round(n)):,}".replace(",", "\u00a0")


def tool_search_operations(query="", category="", date_from="",
                            date_to="", direction="все", limit=50) -> dict:
    ops = _get_all_ops()
    results = []

    dt_from = _parse_date(date_from) if date_from else None
    dt_to   = _parse_date(date_to)   if date_to   else None
    q       = query.lower().strip()
    cat_q   = category.lower().strip()

    for op in ops:
        contractor = op.get("contractor", "")
        cat        = op.get("cat", "")
        date_str   = op.get("date", "")
        is_debit   = op.get("is_debit", False)

        # Фильтр по типу
        if direction == "приход" and is_debit:
            continue
        if direction == "расход" and not is_debit:
            continue

        # Фильтр по контрагенту
        if q and q not in contractor.lower():
            continue

        # Фильтр по категории
        if cat_q and cat_q not in cat.lower():
            continue

        # Фильтр по дате
        op_dt = _parse_date(date_str)
        if dt_from and op_dt and op_dt < dt_from:
            continue
        if dt_to and op_dt and op_dt > dt_to:
            continue

        results.append({
            "date":       date_str,
            "amount":     op.get("amount", 0),
            "contractor": contractor,
            "category":   cat,
            "direction":  "расход" if is_debit else "приход",
            "month":      op.get("_month", "")
        })

    # Сортируем по дате (новые первые)
    results.sort(key=lambda x: x["date"], reverse=True)
    total = sum(r["amount"] for r in results)

    return {
        "count":   len(results),
        "total":   total,
        "results": results[:limit]
    }


def tool_get_summary(date_from="", date_to="", month="") -> dict:
    ops = _get_all_ops()

    dt_from = _parse_date(date_from) if date_from else None
    dt_to   = _parse_date(date_to)   if date_to   else None

    income_total  = 0.0
    expense_total = 0.0
    income_by_contractor: dict[str, float] = {}
    expense_by_cat:       dict[str, float] = {}

    for op in ops:
        if month and op.get("_month", "") != month:
            continue
        date_str = op.get("date", "")
        op_dt    = _parse_date(date_str)
        if dt_from and op_dt and op_dt < dt_from:
            continue
        if dt_to and op_dt and op_dt > dt_to:
            continue

        amt      = op.get("amount", 0)
        is_debit = op.get("is_debit", False)
        if is_debit:
            expense_total += amt
            cat = op.get("cat", "Прочее")
            expense_by_cat[cat] = expense_by_cat.get(cat, 0) + amt
        else:
            income_total += amt
            c = op.get("contractor", "—")
            income_by_contractor[c] = income_by_contractor.get(c, 0) + amt

    return {
        "income_total":          income_total,
        "expense_total":         expense_total,
        "income_by_contractor":  dict(sorted(income_by_contractor.items(), key=lambda x: -x[1])[:10]),
        "expense_by_category":   dict(sorted(expense_by_cat.items(), key=lambda x: -x[1])),
    }


def tool_get_balance() -> dict:
    try:
        from database import get_setting
        balance = get_setting("account_balance")
        updated = get_setting("balance_updated_at")
        if balance:
            return {"balance": float(balance), "updated_at": updated or ""}
    except Exception:
        pass
    return {"balance": None, "updated_at": ""}


def tool_get_contractor_history(contractor_name: str) -> dict:
    return tool_search_operations(query=contractor_name, limit=200)


def _run_tool(name: str, inputs: dict) -> str:
    try:
        if name == "search_operations":
            result = tool_search_operations(**inputs)
        elif name == "get_summary":
            result = tool_get_summary(**inputs)
        elif name == "get_balance":
            result = tool_get_balance()
        elif name == "get_contractor_history":
            result = tool_get_contractor_history(**inputs)
        else:
            result = {"error": f"Неизвестный инструмент: {name}"}
        return json.dumps(result, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


# ── Основная функция агента ───────────────────────────────────────────────────

def ask_agent(user_message: str) -> str:
    """
    Отправляет сообщение агенту и возвращает ответ.
    Агент может вызывать инструменты для работы с данными.
    """
    if not CLAUDE_API_KEY:
        return "⚠️ ANTHROPIC_API_KEY не задан. Добавь его в Railway Variables."

    today   = datetime.now()
    month   = MONTH_NAMES.get(today.month, "")
    system  = SYSTEM_PROMPT.format(
        today=today.strftime("%d.%m.%Y"),
        month=month
    )

    messages = [{"role": "user", "content": user_message}]

    # Агентный цикл — до 5 итераций (инструмент → ответ → инструмент...)
    for _ in range(5):
        payload = {
            "model":      CLAUDE_MODEL,
            "max_tokens": 2048,
            "system":     system,
            "tools":      TOOLS,
            "messages":   messages,
        }

        try:
            req = urllib.request.Request(
                "https://api.anthropic.com/v1/messages",
                data=json.dumps(payload).encode(),
                headers={
                    "Content-Type":      "application/json",
                    "x-api-key":         CLAUDE_API_KEY,
                    "anthropic-version": "2023-06-01",
                }
            )
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.loads(r.read().decode())
        except Exception as e:
            log.error("Claude API error: %s", e)
            return f"❌ Ошибка Claude API: {e}"

        stop_reason = data.get("stop_reason")
        content     = data.get("content", [])

        # Добавляем ответ ассистента в историю
        messages.append({"role": "assistant", "content": content})

        if stop_reason == "end_turn":
            # Финальный текстовый ответ
            for block in content:
                if block.get("type") == "text":
                    return block["text"]
            return "—"

        elif stop_reason == "tool_use":
            # Вызов инструментов
            tool_results = []
            for block in content:
                if block.get("type") == "tool_use":
                    tool_name   = block["name"]
                    tool_inputs = block.get("input", {})
                    tool_id     = block["id"]
                    log.info("Agent calls tool: %s(%s)", tool_name, tool_inputs)
                    result_str  = _run_tool(tool_name, tool_inputs)
                    tool_results.append({
                        "type":        "tool_result",
                        "tool_use_id": tool_id,
                        "content":     result_str
                    })

            messages.append({"role": "user", "content": tool_results})

        else:
            break

    return "⚠️ Агент не смог сформировать ответ."
