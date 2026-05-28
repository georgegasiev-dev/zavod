"""
Новатор — MCP сервер.
Даёт Claude-коллеге доступ к платёжному мониторингу через инструменты.
"""
from mcp.server.fastmcp import FastMCP
from database import (get_month_data, get_all_months,
                       get_contractor_mappings, save_contractor_mapping,
                       save_month_data)

mcp = FastMCP("Новатор — Платёжный мониторинг")

VALID_CATS = [
    'Лес', 'Перевозка леса', 'Смола', 'Плёнка', 'ГСМ', 'Расходники',
    'Свет', 'Вывоз мусора', 'НДС', 'НДФЛ', 'Упаковка', 'Перевозчик',
    'ЗП', 'Аренда', 'Адм.', 'Поступления от клиентов', 'Прочие нераспознанные',
]


# ── 1. Обзор по всем месяцам ──────────────────────────────────────────────────

@mcp.tool()
def get_overview() -> str:
    """
    Обзор по всем загруженным месяцам: итоговые расходы, поступления, число операций.
    Используй как стартовую точку — чтобы понять какие месяцы загружены.
    """
    all_data = get_all_months()
    if not all_data:
        return "Данных нет. Выписки ещё не загружены."

    lines = ["# Платёжный мониторинг Новатора — обзор по месяцам\n"]
    for month, data in all_data.items():
        total_out = sum(data.get('weeks_out', []))
        total_in  = sum(data.get('weeks_in',  []))
        ops       = data.get('total_ops', 0)
        updated   = (data.get('updated_at') or '')[:10]
        unknown   = len(data.get('unknown', []))
        lines.append(f"## {month}")
        lines.append(f"- Расходы: {total_out:,.0f} ₽")
        lines.append(f"- Поступления: {total_in:,.0f} ₽")
        lines.append(f"- Операций: {ops}  |  Нераспознанных: {unknown}")
        lines.append(f"- Обновлено: {updated}\n")

    return "\n".join(lines)


# ── 2. Сводка за месяц по категориям ─────────────────────────────────────────

@mcp.tool()
def get_monthly_summary(month: str) -> str:
    """
    Сводка расходов по категориям за конкретный месяц, с разбивкой по неделям.
    month: Май | Июнь | Июль | Август | Сентябрь | Октябрь | Ноябрь | Декабрь
    """
    data = get_month_data(month)
    if not data:
        return f"Данных за {month} нет. Возможно выписка ещё не загружена."

    cats      = data.get('cats', [])
    weeks_out = data.get('weeks_out', [])
    weeks_in  = data.get('weeks_in',  [])
    total_out = sum(weeks_out)
    total_in  = sum(weeks_in)

    lines = [f"# {month} 2026 — сводка платежей\n"]
    lines.append(f"**Итого расходов:** {total_out:,.0f} ₽")
    lines.append(f"**Итого поступлений:** {total_in:,.0f} ₽")
    lines.append(f"**Операций:** {data.get('total_ops', 0)}\n")

    lines.append("## Расходы по категориям")
    for cat in cats:
        if cat['name'] == 'Поступления от клиентов':
            continue
        lines.append(f"- {cat['name']}: {cat['fact']:,.0f} ₽")

    lines.append("\n## Расходы по неделям")
    for i, w in enumerate(weeks_out):
        lines.append(f"- Неделя {i+1}: {w:,.0f} ₽")

    lines.append("\n## Поступления по неделям")
    for i, w in enumerate(weeks_in):
        lines.append(f"- Неделя {i+1}: {w:,.0f} ₽")

    unknown = data.get('unknown', [])
    if unknown:
        lines.append(f"\n⚠️ Нераспознанных платежей: {len(unknown)} шт. — используй get_unknown_payments для деталей.")

    return "\n".join(lines)


# ── 3. Детали по неделе ───────────────────────────────────────────────────────

@mcp.tool()
def get_week_details(month: str, week: int, category: str = "") -> str:
    """
    Все операции за конкретную неделю месяца.
    month: Май | Июнь | … | Декабрь
    week: номер недели в месяце, 1–5
    category: фильтр по категории (необязательно, например "Лес" или "ЗП")
    """
    data = get_month_data(month)
    if not data:
        return f"Данных за {month} нет."

    wi  = week - 1
    ops = [op for op in data.get('ops', [])
           if op.get('week') == wi and op.get('is_debit')]

    if category:
        ops = [op for op in ops if op.get('cat', '') == category]

    if not ops:
        suffix = f" по категории «{category}»" if category else ""
        return f"Операций за неделю {week}{suffix} нет."

    total = sum(op.get('amount', 0) for op in ops)
    lines = [f"# {month}, неделя {week}{' — '+category if category else ''}"]
    lines.append(f"{len(ops)} операций | {total:,.0f} ₽\n")
    for op in sorted(ops, key=lambda x: x.get('date', '')):
        lines.append(
            f"- {op.get('date','')} | {op.get('contractor','')[:45]} | "
            f"{op.get('cat','')} | {op.get('amount',0):,.0f} ₽"
        )

    return "\n".join(lines)


# ── 4. Нераспознанные платежи ─────────────────────────────────────────────────

@mcp.tool()
def get_unknown_payments(month: str) -> str:
    """
    Список платежей, которые система не смогла автоматически классифицировать.
    Используй вместе с reclassify_contractor чтобы исправить категорию.
    month: Май | Июнь | … | Декабрь
    """
    data = get_month_data(month)
    if not data:
        return f"Данных за {month} нет."

    # Собираем из ops напрямую — unknown в БД может быть устаревшим
    ops = [op for op in data.get('ops', [])
           if op.get('cat') == 'Прочие нераспознанные' and op.get('is_debit')]

    if not ops:
        return f"Нераспознанных платежей за {month} нет — все классифицированы ✓"

    total = sum(op.get('amount', 0) for op in ops)
    lines = [f"# Нераспознанные платежи — {month}"]
    lines.append(f"{len(ops)} шт. | {total:,.0f} ₽\n")
    for op in sorted(ops, key=lambda x: -x.get('amount', 0)):
        lines.append(
            f"- **{op.get('contractor','')[:50]}** | {op.get('amount',0):,.0f} ₽"
            f"\n  Назначение: {op.get('desc','')[:80]}"
            f"\n  Дата: {op.get('date','')}"
        )

    lines.append(f"\n💡 Чтобы исправить — используй reclassify_contractor(contractor_name, new_category)")
    lines.append(f"Доступные категории: {', '.join(VALID_CATS)}")

    return "\n".join(lines)


# ── 5. Справочник контрагентов ────────────────────────────────────────────────

@mcp.tool()
def get_contractors(search: str = "", top: int = 30) -> str:
    """
    Справочник контрагентов с суммами платежей и категориями.
    search: поиск по имени (необязательно)
    top: сколько строк вернуть, максимум 100 (по умолчанию 30)
    """
    all_data   = get_all_months()
    db_map     = get_contractor_mappings()
    stats: dict = {}

    for month_data in all_data.values():
        for op in month_data.get('ops', []):
            if not op.get('is_debit'):
                continue
            c  = (op.get('contractor') or '').strip()
            cl = c.lower()
            if not cl:
                continue
            if cl not in stats:
                stats[cl] = {'name': c, 'total': 0,
                              'cat': db_map.get(cl, op.get('cat', ''))}
            stats[cl]['total'] += op.get('amount', 0)
            if cl in db_map:
                stats[cl]['cat'] = db_map[cl]

    items = sorted(stats.values(), key=lambda x: -x['total'])

    if search:
        sl    = search.lower()
        items = [i for i in items if sl in i['name'].lower()]

    items = items[:min(top, 100)]

    if not items:
        return f"Контрагентов по запросу «{search}» не найдено."

    lines = [f"# Контрагенты ({len(items)} из {len(stats)})\n"]
    for c in items:
        lines.append(f"- **{c['name']}** | {c['total']:,.0f} ₽ | {c['cat']}")

    return "\n".join(lines)


# ── 6. Реклассификация контрагента ────────────────────────────────────────────

@mcp.tool()
def reclassify_contractor(contractor_name: str, new_category: str) -> str:
    """
    Изменить категорию контрагента навсегда (сохраняется в справочнике сервера).
    После этого при каждой новой загрузке выписки он автоматически попадёт в нужную категорию.

    contractor_name: точное название контрагента (регистр не важен)
    new_category: одна из: Лес | Перевозка леса | Смола | Плёнка | ГСМ | Расходники |
                  Свет | Вывоз мусора | НДС | НДФЛ | Упаковка | Перевозчик |
                  ЗП | Аренда | Адм. | Поступления от клиентов | Прочие нераспознанные
    """
    if new_category not in VALID_CATS:
        return (f"Неверная категория: «{new_category}».\n"
                f"Доступные: {', '.join(VALID_CATS)}")

    norm = ' '.join(contractor_name.lower().strip().split())
    save_contractor_mapping(norm, new_category)

    # Обновляем во всех месяцах
    all_data = get_all_months()
    changed  = 0
    for month, data in all_data.items():
        updated = False
        for op in data.get('ops', []):
            op_norm = ' '.join((op.get('contractor') or '').lower().strip().split())
            if op_norm == norm:
                op['cat'] = new_category
                changed  += 1
                updated   = True
        if updated:
            save_month_data(month, data)

    return (f"✅ Контрагент **{contractor_name}** → **{new_category}**\n"
            f"Обновлено операций в базе: {changed} шт.\n"
            f"При следующей загрузке выписки классификация применится автоматически.")


# ── 7. Поиск операций ─────────────────────────────────────────────────────────

@mcp.tool()
def search_operations(query: str, month: str = "") -> str:
    """
    Поиск операций по имени контрагента или назначению платежа.
    query: строка поиска (часть названия контрагента или назначения)
    month: ограничить поиск одним месяцем (необязательно)
    """
    if month:
        months_data = {month: get_month_data(month)} if get_month_data(month) else {}
    else:
        months_data = get_all_months()

    if not months_data:
        return "Данных нет."

    ql      = query.lower()
    results = []

    for m, data in months_data.items():
        for op in data.get('ops', []):
            if not op.get('is_debit'):
                continue
            if (ql in (op.get('contractor') or '').lower()
                    or ql in (op.get('desc') or '').lower()):
                results.append({**op, '_month': m})

    if not results:
        return f"Ничего не найдено по запросу «{query}»."

    total = sum(r.get('amount', 0) for r in results)
    lines = [f"# Поиск: «{query}» — {len(results)} операций, {total:,.0f} ₽\n"]
    for r in sorted(results, key=lambda x: x.get('date', ''), reverse=True)[:50]:
        lines.append(
            f"- {r.get('date','')} | {r['_month']} | "
            f"{r.get('contractor','')[:40]} | {r.get('cat','')} | {r.get('amount',0):,.0f} ₽"
        )

    if len(results) > 50:
        lines.append(f"\n…ещё {len(results)-50} операций (показаны первые 50)")

    return "\n".join(lines)
