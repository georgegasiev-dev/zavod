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
from database import save_month_data, merge_month_data, get_month_data, get_all_months, get_last_upload

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

    result = classify_operations(df, month)
    merge_month_data(month, result)
    return {
        "status": "ok",
        "month": month,
        "processed": result["total_ops"],
        "unknown": len(result["unknown"]),
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
    contractor = (payload.get("contractor") or "").strip().lower()
    new_cat = payload.get("cat")
    comment = payload.get("comment")
    if not contractor:
        raise HTTPException(status_code=400, detail="contractor обязателен")

    from database import save_contractor_mapping, save_contractor_comment
    if new_cat is not None:
        save_contractor_mapping(contractor, new_cat)
        # Обновляем в ops всех месяцев
        from database import get_all_months, save_month_data
        all_data = get_all_months()
        for month, data in all_data.items():
            changed = False
            for op in data.get('ops', []):
                if (op.get('contractor') or '').strip().lower() == contractor:
                    op['cat'] = new_cat
                    changed = True
            if changed:
                save_month_data(month, data)
    if comment is not None:
        save_contractor_comment(contractor, comment)

    return {"status": "ok"}
