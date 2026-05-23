"""
Новатор — платёжный мониторинг. FastAPI backend.
Запуск: uvicorn main:app --reload --port 8000
"""
from fastapi import FastAPI, UploadFile, File, Depends, HTTPException, status, Form, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.responses import JSONResponse
import pandas as pd, io, json, secrets, os
from datetime import datetime
from classifier import classify_operations
from database import save_month_data, get_month_data, get_all_months, get_last_upload

app = FastAPI(title="Новатор — Платёжный мониторинг", version="1.0.0")
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
    save_month_data(month, result)
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
    contractor = (payload.get("contractor") or "").strip().lower()
    new_cat = payload.get("new_cat", "")

    if not month or not contractor or not new_cat:
        raise HTTPException(status_code=400, detail="month, contractor, new_cat обязательны")

    # 1. Обновляем ops в БД — меняем cat у всех ops с этим контрагентом
    data = get_month_data(month)
    changed = 0
    if data and data.get("ops"):
        for op in data["ops"]:
            if (op.get("contractor") or "").strip().lower() == contractor:
                op["cat"] = new_cat
                changed += 1
        if changed:
            save_month_data(month, data)

    # 2. Сохраняем в persistent справочник контрагентов
    save_contractor_mapping(contractor, new_cat)

    return {"status": "ok", "contractor": contractor, "new_cat": new_cat, "ops_updated": changed}
