"""
Raiffeisen Business API — генерация и скачивание выписок в формате Excel.

Переменные окружения (Railway → Variables):
  RAIFFEISEN_CLIENT_ID     — Client ID из API-оркестратора РБО
  RAIFFEISEN_CLIENT_SECRET — Client Secret из API-оркестратора РБО
  RAIFFEISEN_REFRESH_TOKEN — Refresh-токен, выпущенный в РБО
  RAIFFEISEN_ACCOUNT       — номер счёта (опционально)
  RAIFFEISEN_CNUM          — CNUM клиента (опционально, ищем автоматически)
"""
import os, json, logging, base64, time
from datetime import datetime, timedelta

log = logging.getLogger("raiffeisen_api")

CLIENT_ID     = os.getenv("RAIFFEISEN_CLIENT_ID",     "")
CLIENT_SECRET = os.getenv("RAIFFEISEN_CLIENT_SECRET",  "")
ACCOUNT_NUM   = os.getenv("RAIFFEISEN_ACCOUNT",        "")
CNUM          = os.getenv("RAIFFEISEN_CNUM",           "")

# Правильные URL из документации
SSO_URL          = "https://sso.rbo.raiffeisen.ru/token"
ACCOUNTS_API     = "https://api.openapi.raiffeisen.ru"
STATEMENTS_API   = "https://api.raiffeisen.ru/bank-statements"


# ── хранилище токенов ─────────────────────────────────────────────────────────

def save_token(token_data: dict):
    from database import _conn
    token_data["saved_at"] = datetime.now().isoformat()
    with _conn() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS kv_store
            (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)""")
        conn.execute("""INSERT INTO kv_store (key, value, updated_at) VALUES (?,?,datetime('now'))
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
            ("raiffeisen_token", json.dumps(token_data, ensure_ascii=False)))
        conn.commit()


def load_token() -> dict | None:
    from database import _conn
    try:
        with _conn() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS kv_store
                (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)""")
            row = conn.execute("SELECT value FROM kv_store WHERE key=?",
                               ("raiffeisen_token",)).fetchone()
        return json.loads(row["value"]) if row else None
    except Exception:
        return None


def _basic_auth() -> str:
    return "Basic " + base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()


def refresh_tokens(refresh_token: str) -> dict:
    import urllib.request, urllib.parse
    data = urllib.parse.urlencode({
        "client_id":     CLIENT_ID,
        "refresh_token": refresh_token,
        "grant_type":    "refresh_token",
    }).encode()
    req = urllib.request.Request(SSO_URL, data=data, headers={
        "Authorization": _basic_auth(),
        "Content-Type":  "application/x-www-form-urlencoded",
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        token = json.loads(r.read())
    save_token(token)
    log.info("Raiffeisen tokens обновлены")
    return token


def get_valid_tokens() -> tuple[str, str]:
    token = load_token()
    if not token:
        initial_rt = os.getenv("RAIFFEISEN_REFRESH_TOKEN", "")
        if not initial_rt:
            raise RuntimeError("Нет токена. Добавьте RAIFFEISEN_REFRESH_TOKEN в Railway Variables.")
        token = refresh_tokens(initial_rt)
    saved_at   = datetime.fromisoformat(token.get("saved_at", "2000-01-01"))
    expires_in = int(token.get("expires_in", 3600))
    if datetime.now() >= saved_at + timedelta(seconds=expires_in - 300):
        token = refresh_tokens(token["refresh_token"])
    return token["access_token"], token.get("id_token", "")


# ── Получение данных счёта ────────────────────────────────────────────────────

def _get_accounts(access_token: str, id_token: str) -> list[dict]:
    import urllib.request
    req = urllib.request.Request(
        f"{ACCOUNTS_API}/api/v1/accounts?fields=Id,Number,Name,Currency,Cnum",
        headers={
            "Authorization": f"Bearer {access_token}",
            "ID-Token": id_token,
            "Accept": "application/json",
        }
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.loads(r.read())
    return data if isinstance(data, list) else data.get("accounts", [])


def _get_account_key(access_token: str, id_token: str) -> str:
    """Возвращает accountKey в формате 'номерсчёта:CNUM'."""
    # Если заданы обе переменные — используем напрямую
    if ACCOUNT_NUM and CNUM:
        return f"{ACCOUNT_NUM}:{CNUM}"

    accounts = _get_accounts(access_token, id_token)
    # Ищем рублёвый расчётный счёт
    target = None
    for acc in accounts:
        currency = (acc.get("currency") or acc.get("Currency") or "").upper()
        if currency == "RUR":
            target = acc
            break
    if not target and accounts:
        target = accounts[0]
    if not target:
        raise RuntimeError("Не найден ни один счёт")

    number = target.get("number") or target.get("Number") or ACCOUNT_NUM
    cnum   = (target.get("cnum") or target.get("Cnum") or
              target.get("clientNumber") or target.get("ClientNumber") or CNUM)

    if not cnum:
        raise RuntimeError(
            f"Не найден CNUM для счёта {number}. "
            "Добавьте RAIFFEISEN_CNUM в Railway Variables. "
            "Найти в РБО: профиль → информация о компании → номер клиента."
        )
    return f"{number}:{cnum}"


# ── Генерация и скачивание выписки ───────────────────────────────────────────

def _auth_headers(access_token: str, id_token: str) -> dict:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type":  "application/json",
        "Accept":        "application/json",
    }
    if id_token:
        headers["Id-Token"] = id_token
    return headers


def _post(url: str, body: dict, headers: dict) -> dict:
    import httpx
    h = {k: v for k, v in headers.items() if k != "Content-Type"}
    with httpx.Client(timeout=30) as client:
        resp = client.post(url, json=body, headers=h)
        if not resp.is_success:
            raise RuntimeError(f"POST {url} → {resp.status_code}: {resp.text[:500]}")
        return resp.json() if resp.content else {}


def _get(url: str, headers: dict) -> bytes:
    import httpx
    h = {k: v for k, v in headers.items() if k != "Content-Type"}
    with httpx.Client(timeout=30) as client:
        resp = client.get(url, headers=h)
        resp.raise_for_status()
        return resp.content


def generate_excel_report(account_key: str, date_from: str, date_to: str,
                           access_token: str, id_token: str) -> str:
    """Запрашивает генерацию Excel-выписки, возвращает reportId."""
    body = {
        "accountKeys": [account_key],
        "from": date_from,
        "to":   date_to,
    }
    headers = _auth_headers(access_token, id_token)
    resp = _post(f"{STATEMENTS_API}/v1/reports/excel", body, headers)
    report_id = resp.get("reportId")
    if not report_id:
        raise RuntimeError(f"Не получен reportId: {resp}")
    log.info("Raiffeisen: reportId=%s", report_id)
    return report_id


def wait_for_report(report_id: str, access_token: str, id_token: str,
                    max_wait: int = 120) -> str:
    """Ждёт готовности отчёта, возвращает финальный статус."""
    import httpx
    headers = {k: v for k, v in _auth_headers(access_token, id_token).items()
               if k != "Content-Type"}
    url = f"{STATEMENTS_API}/v1/reports/{report_id}/status"
    deadline = time.time() + max_wait
    with httpx.Client(timeout=15) as client:
        while time.time() < deadline:
            resp = client.get(url, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            status = data.get("status", "UNKNOWN")
            log.info("Raiffeisen report status: %s", status)
            if status == "COMPLETED":
                return status
            if status in ("FAILED", "STOPPED", "CANCELLED"):
                raise RuntimeError(f"Генерация отчёта завершилась: {status}")
            time.sleep(5)
    raise RuntimeError(f"Таймаут ожидания отчёта {report_id}")


def download_report(report_id: str, access_token: str, id_token: str) -> bytes:
    """Скачивает файл отчёта, возвращает байты Excel."""
    headers = {k: v for k, v in _auth_headers(access_token, id_token).items()
               if k not in ("Content-Type",)}
    return _get(f"{STATEMENTS_API}/v1/reports/{report_id}/file", headers)


# ── Основная точка входа ──────────────────────────────────────────────────────

def fetch_statements(date_from: str = None, date_to: str = None) -> bytes:
    """Запрашивает, ждёт и скачивает Excel-выписку. Возвращает байты файла."""
    if not date_from:
        date_from = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d")
    if not date_to:
        # Берём позавчера — выписка за вчера может быть ещё не готова
        date_to = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d")

    access_token, id_token = get_valid_tokens()
    account_key = _get_account_key(access_token, id_token)
    log.info("Raiffeisen: account_key=%s, %s → %s", account_key, date_from, date_to)

    report_id = generate_excel_report(account_key, date_from, date_to, access_token, id_token)
    wait_for_report(report_id, access_token, id_token)
    return download_report(report_id, access_token, id_token)


def fetch_and_load(date_from: str = None, date_to: str = None) -> dict:
    """Получает выписку и сохраняет в БД."""
    import io
    import pandas as pd
    from classifier import classify_operations, month_for_date
    from database import merge_month_data

    xlsx_bytes = fetch_statements(date_from, date_to)
    df = pd.read_excel(io.BytesIO(xlsx_bytes))

    if df.empty:
        return {"processed": 0, "months": []}

    df["_target_month"] = df.iloc[:, 0].apply(lambda x: month_for_date(x, "Июнь"))
    total_ops, months_updated = 0, []
    for m, sub_df in df.groupby("_target_month"):
        sub_df = sub_df.drop(columns=["_target_month"])
        result = classify_operations(sub_df, m)
        result["source"] = "raiffeisen_api"
        merge_month_data(m, result)
        total_ops += result.get("total_ops", 0)
        months_updated.append(m)

    return {"processed": len(df), "ops_saved": total_ops, "months": months_updated}
