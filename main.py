"""
Новатор — платёжный мониторинг. FastAPI backend.
Запуск: uvicorn main:app --reload --port 8000
"""
from contextlib import asynccontextmanager
from fastapi import FastAPI, UploadFile, File, Depends, HTTPException, status, Form, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.responses import JSONResponse
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import pandas as pd, io, json, secrets, os, logging
from datetime import datetime
from classifier import classify_operations
from database import (save_month_data, merge_month_data, get_month_data, get_all_months,
                       get_last_upload, save_contractor_mapping, save_contractor_comment)


logging.basicConfig(level=logging.INFO)
log = logging.getLogger("main")

# ── планировщик ───────────────────────────────────────────────────────────────
scheduler = AsyncIOScheduler(timezone="Europe/Moscow")

async def scheduled_gmail_sync():
    """Запускается по расписанию — тянет выписку из Gmail."""
    log.info("⏰ Плановая синхронизация Gmail...")
    try:
        from gmail_fetcher import fetch_and_upload
        result = fetch_and_upload()
        log.info("Gmail sync result: %s", result)
    except Exception as e:
        log.error("Ошибка плановой синхронизации: %s", e)

# Время синхронизации: каждый день в 10:00 по Москве
# Настраивается через переменную SYNC_HOUR (по умолчанию 10)
SYNC_HOUR = int(os.getenv("SYNC_HOUR", "10"))
SYNC_MINUTE = int(os.getenv("SYNC_MINUTE", "0"))

@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler.add_job(
        scheduled_gmail_sync,
        CronTrigger(hour=SYNC_HOUR, minute=SYNC_MINUTE),
        id="gmail_sync",
        replace_existing=True,
    )
    scheduler.start()
    log.info("Scheduler started. Gmail sync at %02d:%02d Moscow time.", SYNC_HOUR, SYNC_MINUTE)
    yield
    scheduler.shutdown()

app = FastAPI(title="Новатор — Платёжный мониторинг", version="1.0.0", lifespan=lifespan)
security = HTTPBasic()

# CORS — разрешаем все источники явно
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASS = os.getenv("ADMIN_PASS", "novator2026")

def verify_admin(creds: HTTPBasicCredentials = Depends(security)):
    ok = (secrets.compare_digest(creds.username.encode(), ADMIN_USER.encode()) and
          secrets.compare_digest(creds.password.encode(), ADMIN_PASS.encode()))
    if not ok:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            headers={"WWW-Authenticate": "Basic"})
    return creds.username

# ── публичные эндпоинты ───────────────────────────────────────────────────────
@app.get("/api/data")
def get_data(month: str = None):
    if month:
        return get_month_data(month)
    return get_all_months()

@app.get("/api/status")
def get_status():
    return get_last_upload()

@app.get("/api/health")
def health():
    return {"status": "ok", "time": datetime.now().isoformat()}

@app.post("/api/sync")
async def manual_sync(_: str = Depends(verify_admin)):
    """Ручной запуск синхронизации Gmail — без ожидания расписания."""
    try:
        from gmail_fetcher import fetch_and_upload
        result = fetch_and_upload()
        return {"status": "ok", **result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/sync/status")
def sync_status(_: str = Depends(verify_admin)):
    """Следующий запуск по расписанию."""
    job = scheduler.get_job("gmail_sync")
    if not job:
        return {"scheduled": False}
    return {
        "scheduled": True,
        "next_run": job.next_run_time.isoformat() if job.next_run_time else None,
        "sync_time": f"{SYNC_HOUR:02d}:{SYNC_MINUTE:02d} МСК",
    }

@app.get("/")
def root():
    return {"service": "Новатор Платёжный мониторинг", "status": "ok",
            "docs": "/docs", "health": "/api/health"}

# ── защищённые эндпоинты ──────────────────────────────────────────────────────
@app.post("/api/upload")
async def upload_statement(
    file: UploadFile = File(...),
    month: str = Form(...),
    _: str = Depends(verify_admin),
):
    contents = await file.read()
    try:
        df = pd.read_excel(io.BytesIO(contents))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Ошибка чтения файла: {e}")

    # Группируем строки по фактическому месяцу даты — операции с июньской датой
    # пойдут в Июнь, даже если при загрузке выбран Май
    from classifier import _find_col, month_for_date
    date_col = _find_col(df, 'дата')

    if date_col:
        df['_target_month'] = df[date_col].apply(lambda x: month_for_date(x, month))
    else:
        df['_target_month'] = month

    total_ops, total_unknown = 0, 0
    months_updated = []
    for m, sub_df in df.groupby('_target_month'):
        sub_df = sub_df.drop(columns=['_target_month'])
        result = classify_operations(sub_df, m)
        merge_month_data(m, result)
        total_ops    += result.get("total_ops", 0)
        total_unknown += len(result.get("unknown", []))
        months_updated.append(m)

    return {
        "status": "ok",
        "months": months_updated,
        "processed": total_ops,
        "unknown": total_unknown,
        "uploaded_at": datetime.now().isoformat(),
    }


# ── ручная классификация нераспознанных ───────────────────────────────────────
@app.post("/api/classify")
async def manual_classify(
    payload: dict,
    _: str = Depends(verify_admin),
):
    """
    Принимает список {contractor, cat} и обновляет данные месяца.
    payload = {month: "Май", corrections: [{contractor, cat, week, amount}, ...]}
    """
    month = payload.get("month")
    corrections = payload.get("corrections", [])
    if not month or not corrections:
        raise HTTPException(status_code=400, detail="month и corrections обязательны")

    data = get_month_data(month)
    if not data:
        raise HTTPException(status_code=404, detail=f"Данных за {month} нет")

    CAT_TO_ID = {
        'Лес':'les','Перевозка леса':'perev_les','Смола':'smola','Плёнка':'plenka',
        'ГСМ':'gsm','Расходники':'rashod','Свет':'svet','Вывоз мусора':'vuvozmys',
        'НДС':'nds','НДФЛ':'ndfl','Упаковка':'upakovka','Перевозчик':'perevozch',
        'ЗП':'zp_avans','Аренда':'arenda','Адм.':'adm',
    }

    ops = data.get("ops", [])
    for op in ops:
        for corr in corrections:
            if (op.get("contractor","").lower().strip() == corr["contractor"].lower().strip()
                    and op.get("cat") == "Прочее"):
                op["cat"] = corr["cat"]
                row_id = CAT_TO_ID.get(corr["cat"])
                if row_id and op.get("week", -1) >= 0:
                    wi = op["week"]
                    # пересчитываем weeks_out
                    pass  # пересчёт делается на фронте при следующем fetchData

    data["ops"] = ops
    # убираем из unknown те, что исправили
    corrected_names = {c["contractor"].lower().strip() for c in corrections}
    data["unknown"] = [u for u in data.get("unknown", [])
                       if u.get("contractor","").lower().strip() not in corrected_names]

    save_month_data(month, data)
    return {"status": "ok", "corrected": len(corrections)}


@app.post("/api/reclassify")
async def reclassify_op(
    payload: dict,
    _: str = Depends(verify_admin),
):
    """
    Меняет категорию платежа и запоминает контрагента в справочнике.
    payload = {month, contractor, new_cat}
    """
    month = payload.get("month")
    contractor = ' '.join((payload.get("contractor") or "").lower().strip().split())
    new_cat = payload.get("new_cat", "")

    if not month or not contractor or not new_cat:
        raise HTTPException(status_code=400, detail="month, contractor, new_cat обязательны")

    # 1. Обновляем ops в БД — меняем cat у всех ops с этим контрагентом (во всех месяцах)
    all_data = get_all_months()
    changed = 0
    for m, data in all_data.items():
        month_changed = False
        for op in data.get("ops", []):
            if ' '.join((op.get("contractor") or "").lower().strip().split()) == contractor:
                op["cat"] = new_cat
                changed += 1
                month_changed = True
        if month_changed:
            save_month_data(m, data)

    # 2. Сохраняем в persistent справочник контрагентов
    save_contractor_mapping(contractor, new_cat)

    return {"status": "ok", "contractor": contractor, "new_cat": new_cat, "ops_updated": changed}


# ── справочник контрагентов ───────────────────────────────────────────────────
@app.get("/api/contractors")
def get_contractors():
    """Полный справочник контрагентов с суммами из ops."""
    from database import get_contractor_mappings, get_all_months
    db_map = get_contractor_mappings()  # {contractor: cat}
    all_data = get_all_months()

    # Собираем суммы и даты по всем контрагентам из ops
    contractor_stats = {}  # {contractor: {total, first_seen, cat, comment}}
    for month_data in all_data.values():
        for op in month_data.get('ops', []):
            if not op.get('is_debit'):
                continue
            c = (op.get('contractor') or '').strip()
            if not c:
                continue
            cl = c.lower()
            if cl not in contractor_stats:
                contractor_stats[cl] = {
                    'name': c,
                    'total': 0,
                    'first_seen': op.get('date', ''),
                    'cat': op.get('cat', 'Прочие нераспознанные'),
                    'comment': '',
                }
            contractor_stats[cl]['total'] += op.get('amount', 0)
            # Берём самую раннюю дату
            d = op.get('date', '')
            if d and d < contractor_stats[cl]['first_seen']:
                contractor_stats[cl]['first_seen'] = d

    # Загружаем сохранённые данные (категории, комментарии)
    saved = get_contractor_details()
    for cl, stat in contractor_stats.items():
        if cl in db_map:
            stat['cat'] = db_map[cl]
        if cl in saved:
            stat['comment'] = saved[cl].get('comment', '')
            if saved[cl].get('added_at'):
                stat['first_seen'] = saved[cl]['added_at']

    result = sorted(contractor_stats.values(), key=lambda x: -x['total'])
    return result


@app.post("/api/contractors/update")
async def update_contractor(
    payload: dict,
    _: str = Depends(verify_admin),
):
    """Обновить категорию и/или комментарий контрагента."""
    contractor = ' '.join((payload.get("contractor") or "").lower().strip().split())
    new_cat = payload.get("cat")
    comment = payload.get("comment")
    if not contractor:
        raise HTTPException(status_code=400, detail="contractor обязателен")

    if new_cat is not None:
        save_contractor_mapping(contractor, new_cat)
        all_data = get_all_months()
        for month, data in all_data.items():
            changed = False
            for op in data.get('ops', []):
                if ' '.join((op.get('contractor') or '').lower().strip().split()) == contractor:
                    op['cat'] = new_cat
                    changed = True
            if changed:
                save_month_data(month, data)
    if comment is not None:
        save_contractor_comment(contractor, comment)

    return {"status": "ok"}


@app.post("/api/cleanup")
async def cleanup_duplicates(_: str = Depends(verify_admin)):
    """
    Разовая чистка БД: переносит операции в правильные месяцы по их датам
    и удаляет полные дубликаты (одинаковые date+contractor+amount+desc).
    """
    from classifier import month_for_date, MONTH_WEEK_RANGES
    import pandas as pd

    all_data = get_all_months()
    if not all_data:
        return {"status": "ok", "message": "База пуста"}

    # 1. Собираем все операции изо всех месяцев
    all_ops = []
    for month, data in all_data.items():
        for op in data.get('ops', []):
            all_ops.append(op)

    # 2. Группируем операции по их правильному месяцу (по дате)
    grouped: dict[str, list] = {}
    for op in all_ops:
        date_str = op.get('date', '')
        # date в формате DD.MM.YYYY
        try:
            parts = date_str.split('.')
            if len(parts) == 3:
                d = pd.Timestamp(year=int(parts[2]), month=int(parts[1]), day=int(parts[0]))
            else:
                d = pd.Timestamp(date_str)
            correct_month = month_for_date(d)
        except Exception:
            correct_month = 'Май'  # fallback

        if correct_month not in grouped:
            grouped[correct_month] = []
        grouped[correct_month].append(op)

    # 3. В каждом месяце удаляем дубликаты по ключу (date+contractor+amount+desc[:40])
    cleaned: dict[str, list] = {}
    removed_dups = 0
    for month, ops in grouped.items():
        seen_keys = set()
        unique = []
        # Используем такой же счётчик внутри месяца, как на фронтенде
        from collections import defaultdict
        counter = defaultdict(int)
        for op in ops:
            base = f"{op.get('date','')}|{op.get('contractor','')}|{op.get('amount',0)}|{op.get('desc','')[:40]}"
            counter[base] += 1
            key = f"{base}|#{counter[base]}"
            if key in seen_keys:
                removed_dups += 1
                continue
            seen_keys.add(key)
            unique.append(op)
        cleaned[month] = unique

    # 4. Пересчитываем weeks_out/weeks_in/cats/total_ops/unknown для каждого месяца
    from classifier import _get_week_index
    result_stats = {}
    for month, ops in cleaned.items():
        weeks_out = [0.0] * 5
        weeks_in  = [0.0] * 5
        cat_totals: dict = {}
        unknown = []

        for op in ops:
            # пересчитываем правильный week index для этого месяца
            date_str = op.get('date', '')
            try:
                parts = date_str.split('.')
                if len(parts) == 3:
                    d = pd.Timestamp(year=int(parts[2]), month=int(parts[1]), day=int(parts[0]))
                else:
                    d = pd.Timestamp(date_str)
                op['week'] = _get_week_index(d, month)
            except Exception:
                op['week'] = -1

            amt = op.get('amount', 0) or 0
            wi  = op.get('week', -1)
            if op.get('is_debit'):
                if 0 <= wi < 5:
                    weeks_out[wi] += amt
                cat = op.get('cat', '')
                if cat:
                    cat_totals[cat] = cat_totals.get(cat, 0) + amt
                if op.get('cat') == 'Прочие нераспознанные':
                    unknown.append({
                        'contractor': op.get('contractor','')[:80],
                        'desc': op.get('desc','')[:80],
                        'amount': op.get('amount', 0),
                    })
            else:
                if 0 <= wi < 5:
                    weeks_in[wi] += amt

        new_data = {
            'month': month,
            'weeks_out': [round(x) for x in weeks_out],
            'weeks_in':  [round(x) for x in weeks_in],
            'cats': [{'name': k, 'fact': round(v)}
                     for k, v in sorted(cat_totals.items(), key=lambda x: -x[1])],
            'ops': ops,
            'total_ops': len(ops),
            'unknown': unknown,
            'uploaded_at': datetime.now().isoformat(),
            'source': 'cleanup',
        }
        save_month_data(month, new_data)
        result_stats[month] = {'ops': len(ops), 'expenses': round(sum(weeks_out)),
                               'income': round(sum(weeks_in))}

    # 5. Удаляем «пустые» / устаревшие месяцы которых нет в cleaned (Февраль и т.п.)
    from database import _conn
    with _conn() as conn:
        for month in list(all_data.keys()):
            if month not in cleaned:
                conn.execute("DELETE FROM month_data WHERE month=?", (month,))
        conn.commit()

    return {
        "status": "ok",
        "removed_duplicates": removed_dups,
        "months": result_stats,
        "total_ops_in_db": sum(len(v) for v in cleaned.values()),
    }


# ── MCP сервер (SSE transport) ────────────────────────────────────────────────
from fastapi.responses import StreamingResponse
import asyncio, uuid

SESSIONS: dict[str, asyncio.Queue] = {}

MCP_TOOLS = [
    {"name": "get_overview",
     "description": "Обзор по всем загруженным месяцам: расходы, поступления, число операций.",
     "inputSchema": {"type": "object", "properties": {}, "required": []}},
    {"name": "get_monthly_summary",
     "description": "Сводка расходов по категориям за месяц с разбивкой по неделям.",
     "inputSchema": {"type": "object",
                     "properties": {"month": {"type": "string", "description": "Май | Июнь | Июль | Август | Сентябрь | Октябрь | Ноябрь | Декабрь"}},
                     "required": ["month"]}},
    {"name": "get_week_details",
     "description": "Все операции за конкретную неделю месяца.",
     "inputSchema": {"type": "object",
                     "properties": {
                         "month": {"type": "string"},
                         "week": {"type": "integer", "description": "1–5"},
                         "category": {"type": "string", "description": "Фильтр по категории (необязательно)"}},
                     "required": ["month", "week"]}},
    {"name": "get_unknown_payments",
     "description": "Список нераспознанных платежей за месяц.",
     "inputSchema": {"type": "object",
                     "properties": {"month": {"type": "string"}},
                     "required": ["month"]}},
    {"name": "get_contractors",
     "description": "Справочник контрагентов с суммами и категориями.",
     "inputSchema": {"type": "object",
                     "properties": {
                         "search": {"type": "string", "description": "Поиск по имени"},
                         "top": {"type": "integer", "description": "Сколько строк вернуть (до 100)"}}}},
    {"name": "reclassify_contractor",
     "description": "Изменить категорию контрагента навсегда (сохраняется в справочнике).",
     "inputSchema": {"type": "object",
                     "properties": {
                         "contractor_name": {"type": "string"},
                         "new_category": {"type": "string",
                             "description": "Лес | Перевозка леса | Смола | Плёнка | ГСМ | Расходники | Свет | Вывоз мусора | НДС | НДФЛ | Упаковка | Перевозчик | ЗП | Аренда | Адм. | Поступления от клиентов | Прочие нераспознанные"}},
                     "required": ["contractor_name", "new_category"]}},
    {"name": "search_operations",
     "description": "Поиск операций по имени контрагента или назначению платежа.",
     "inputSchema": {"type": "object",
                     "properties": {
                         "query": {"type": "string"},
                         "month": {"type": "string", "description": "Ограничить поиск месяцем (необязательно)"}},
                     "required": ["query"]}},
]

VALID_CATS = ['Лес','Перевозка леса','Смола','Плёнка','ГСМ','Расходники','Свет',
              'Вывоз мусора','НДС','НДФЛ','Упаковка','Перевозчик','ЗП','Аренда',
              'Адм.','Поступления от клиентов','Прочие нераспознанные']

def _norm(s): return ' '.join((s or '').lower().strip().split())

def _call_tool(name: str, args: dict) -> str:
    from database import (get_month_data, get_all_months, get_contractor_mappings,
                           save_contractor_mapping, save_month_data as _smd)
    if name == "get_overview":
        all_data = get_all_months()
        if not all_data: return "Данных нет."
        lines = ["# Обзор по месяцам\n"]
        for month, data in all_data.items():
            tout = sum(data.get('weeks_out', [])); tin = sum(data.get('weeks_in', []))
            lines += [f"## {month}", f"- Расходы: {tout:,.0f} ₽", f"- Поступления: {tin:,.0f} ₽",
                      f"- Операций: {data.get('total_ops',0)} | Нераспознанных: {len(data.get('unknown',[]))}\n"]
        return "\n".join(lines)

    if name == "get_monthly_summary":
        month = args.get("month","")
        data = get_month_data(month)
        if not data: return f"Данных за {month} нет."
        cats = data.get('cats',[]); wo = data.get('weeks_out',[]); wi2 = data.get('weeks_in',[])
        lines = [f"# {month} — сводка\n",
                 f"Расходы: {sum(wo):,.0f} ₽  |  Поступления: {sum(wi2):,.0f} ₽  |  Операций: {data.get('total_ops',0)}\n",
                 "## По категориям"]
        for c in cats:
            if c['name'] != 'Поступления от клиентов':
                lines.append(f"- {c['name']}: {c['fact']:,.0f} ₽")
        lines.append("\n## По неделям (расходы)")
        for i,w in enumerate(wo): lines.append(f"- Нед {i+1}: {w:,.0f} ₽")
        unk = data.get('unknown',[])
        if unk: lines.append(f"\n⚠️ Нераспознанных: {len(unk)} шт.")
        return "\n".join(lines)

    if name == "get_week_details":
        month = args.get("month",""); week = int(args.get("week",1)); cat = args.get("category","")
        data = get_month_data(month)
        if not data: return f"Данных за {month} нет."
        ops = [o for o in data.get('ops',[]) if o.get('week')==week-1 and o.get('is_debit')]
        if cat: ops = [o for o in ops if o.get('cat')==cat]
        if not ops: return f"Операций за неделю {week} нет."
        total = sum(o.get('amount',0) for o in ops)
        lines = [f"# {month}, неделя {week}{' — '+cat if cat else ''} ({len(ops)} оп., {total:,.0f} ₽)\n"]
        for o in sorted(ops, key=lambda x: x.get('date','')):
            lines.append(f"- {o.get('date','')} | {o.get('contractor','')[:40]} | {o.get('cat','')} | {o.get('amount',0):,.0f} ₽")
        return "\n".join(lines)

    if name == "get_unknown_payments":
        month = args.get("month","")
        data = get_month_data(month)
        if not data: return f"Данных за {month} нет."
        ops = [o for o in data.get('ops',[]) if o.get('cat')=='Прочие нераспознанные' and o.get('is_debit')]
        if not ops: return f"Нераспознанных за {month} нет ✓"
        lines = [f"# Нераспознанные — {month} ({len(ops)} шт., {sum(o.get('amount',0) for o in ops):,.0f} ₽)\n"]
        for o in sorted(ops, key=lambda x: -x.get('amount',0)):
            lines.append(f"- **{o.get('contractor','')[:50]}** | {o.get('amount',0):,.0f} ₽\n  {o.get('desc','')[:80]}")
        return "\n".join(lines)

    if name == "get_contractors":
        search = args.get("search",""); top = min(int(args.get("top",30)), 100)
        all_data = get_all_months(); db_map = get_contractor_mappings(); stats = {}
        for md in all_data.values():
            for op in md.get('ops',[]):
                if not op.get('is_debit'): continue
                c = (op.get('contractor') or '').strip(); cl = c.lower()
                if not cl: continue
                if cl not in stats: stats[cl] = {'name':c,'total':0,'cat':db_map.get(cl,op.get('cat',''))}
                stats[cl]['total'] += op.get('amount',0)
                if cl in db_map: stats[cl]['cat'] = db_map[cl]
        items = sorted(stats.values(), key=lambda x: -x['total'])
        if search: items = [i for i in items if search.lower() in i['name'].lower()]
        items = items[:top]
        if not items: return f"Контрагентов по запросу «{search}» не найдено."
        lines = [f"# Контрагенты ({len(items)})\n"]
        for c in items: lines.append(f"- **{c['name']}** | {c['total']:,.0f} ₽ | {c['cat']}")
        return "\n".join(lines)

    if name == "reclassify_contractor":
        cname = args.get("contractor_name",""); new_cat = args.get("new_category","")
        if new_cat not in VALID_CATS: return f"Неверная категория. Доступные: {', '.join(VALID_CATS)}"
        norm = _norm(cname); save_contractor_mapping(norm, new_cat)
        all_data = get_all_months(); changed = 0
        for month, data in all_data.items():
            upd = False
            for op in data.get('ops',[]):
                if _norm(op.get('contractor','')) == norm: op['cat'] = new_cat; changed += 1; upd = True
            if upd: _smd(month, data)
        return f"✅ {cname} → {new_cat}. Обновлено операций: {changed}"

    if name == "search_operations":
        query = args.get("query",""); month_filter = args.get("month","")
        ql = query.lower()
        all_data = {month_filter: get_month_data(month_filter)} if month_filter else get_all_months()
        results = []
        for m, data in all_data.items():
            if not data: continue
            for op in data.get('ops',[]):
                if not op.get('is_debit'): continue
                if ql in (op.get('contractor') or '').lower() or ql in (op.get('desc') or '').lower():
                    results.append({**op, '_month': m})
        if not results: return f"Ничего не найдено по «{query}»."
        total = sum(r.get('amount',0) for r in results)
        lines = [f"# Поиск «{query}» — {len(results)} оп., {total:,.0f} ₽\n"]
        for r in sorted(results, key=lambda x: x.get('date',''), reverse=True)[:50]:
            lines.append(f"- {r.get('date','')} | {r['_month']} | {r.get('contractor','')[:40]} | {r.get('cat','')} | {r.get('amount',0):,.0f} ₽")
        if len(results) > 50: lines.append(f"\n…ещё {len(results)-50} операций")
        return "\n".join(lines)

    return f"Неизвестный инструмент: {name}"


@app.get("/mcp/sse")
async def mcp_sse(request: Request):
    session_id = str(uuid.uuid4())
    queue: asyncio.Queue = asyncio.Queue()
    SESSIONS[session_id] = queue

    async def generator():
        try:
            yield f"event: endpoint\ndata: /mcp/messages/{session_id}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=15)
                    yield f"event: message\ndata: {json.dumps(msg, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        finally:
            SESSIONS.pop(session_id, None)

    return StreamingResponse(generator(), media_type="text/event-stream",
                             headers={"Cache-Control":"no-cache","Connection":"keep-alive",
                                      "Access-Control-Allow-Origin":"*"})


@app.post("/mcp/messages/{session_id}")
async def mcp_messages(session_id: str, request: Request):
    body = await request.json()
    method = body.get("method","")
    params = body.get("params", {})
    rid = body.get("id")

    if method == "initialize":
        result = {"protocolVersion": "2024-11-05",
                  "capabilities": {"tools": {}},
                  "serverInfo": {"name": "Новатор — Платёжный мониторинг", "version": "1.0.0"}}
    elif method == "notifications/initialized":
        return JSONResponse({"jsonrpc":"2.0","id":rid,"result":{}})
    elif method == "tools/list":
        result = {"tools": MCP_TOOLS}
    elif method == "tools/call":
        tool_name = params.get("name","")
        tool_args  = params.get("arguments", {})
        try:
            text = _call_tool(tool_name, tool_args)
        except Exception as e:
            text = f"Ошибка: {e}"
        result = {"content": [{"type": "text", "text": text}]}
    else:
        result = {}

    response = {"jsonrpc": "2.0", "id": rid, "result": result}

    # Отправляем в SSE-очередь если сессия жива
    if session_id in SESSIONS:
        await SESSIONS[session_id].put(response)

    return JSONResponse(response)
