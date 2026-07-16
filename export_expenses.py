"""Выгрузка расходов 2026 для анализа себестоимости.
Запуск на сервере: cd /opt/zavod && venv/bin/python3 export_expenses.py
Результат: expense_report.json (суммы по категориям + все операции для ревизии)
"""
import sqlite3, json, os
from collections import defaultdict

DB_PATH = os.getenv("DB_PATH", "data/novator.db")

MONTHS_2026 = ['Январь','Февраль','Март','Апрель','Май','Июнь','Июль',
               'Август','Сентябрь','Октябрь','Ноябрь','Декабрь']

conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row

report = {"months": {}, "prochee_ops": [], "other_review_ops": []}

# Категории, по которым точно нужна ручная ревизия назначений
REVIEW_CATS = {'Прочее', 'Прочие нераспознанные'}

for month in MONTHS_2026:
    row = conn.execute("SELECT data FROM month_data WHERE month=?", (month,)).fetchone()
    if not row:
        continue
    data = json.loads(row['data'])
    ops = data.get('ops', [])
    cat_sums = defaultdict(float)
    n_debit = 0
    for op in ops:
        if not op.get('is_debit'):
            continue
        n_debit += 1
        cat = op.get('cat', '?')
        amt = op.get('amount', 0) or 0
        cat_sums[cat] += amt
        if cat in REVIEW_CATS:
            report["prochee_ops"].append({
                "month": month,
                "date": op.get('date',''),
                "amount": amt,
                "contractor": (op.get('contractor') or '')[:60],
                "desc": (op.get('desc') or '')[:100],
            })
    report["months"][month] = {
        "debit_ops": n_debit,
        "cats": {k: round(v) for k, v in sorted(cat_sums.items(), key=lambda x: -x[1])},
        "total_expenses": round(sum(cat_sums.values())),
    }

report["prochee_total_ops"] = len(report["prochee_ops"])

with open("expense_report.json", "w", encoding="utf-8") as f:
    json.dump(report, f, ensure_ascii=False, indent=1)

print(f"OK: {len(report['months'])} месяцев, {report['prochee_total_ops']} операций 'Прочее' на ревизию")
print("Файл: expense_report.json")
