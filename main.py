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
