"""
Raiffeisen Business API — получение выписок через OAuth2.

Переменные окружения (Railway → Variables):
  RAIFFEISEN_CLIENT_ID     — Client ID из API-оркестратора
  RAIFFEISEN_CLIENT_SECRET — Client Secret из API-оркестратора
  RAIFFEISEN_ACCOUNT       — номер счёта (необязательно, если один счёт)
"""
import os, json, logging
from datetime import datetime, timedelta

log = logging.getLogger("raiffeisen_api")

CLIENT_ID     = os.getenv("RAIFFEISEN_CLIENT_ID",     "")
CLIENT_SECRET = os.getenv("RAIFFEISEN_CLIENT_SECRET",  "")
ACCOUNT_NUM   = os.getenv("RAIFFEISEN_ACCOUNT",        "")

BASE_URL      = "https://epa.raiffeisen.ru"
AUTH_URL      = f"{BASE_URL}/oauth/authorize"
TOKEN_URL     = f"{BASE_URL}/oauth/token"
STATEMENT_URL = f"{BASE_URL}/api/v1/accounts/{{account}}/transactions"

REDIRECT_URI  = os.getenv("RAIFFEISEN_REDIRECT_URI",
                           "https://zavod-production.up.railway.app/api/raiffeisen/callback")


# ── хранилище токенов (в БД) ──────────────────────────────────────────────────

def _token_key() -> str:
    return "raiffeisen_token"


def save_token(token_data: dict):
    """Сохраняет access/refresh токен в БД."""
    from database import _conn
    token_data["saved_at"] = datetime.now().isoformat()
    with _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS kv_store (
                key TEXT PRIMARY KEY, value TEXT, updated_at TEXT
            )
        """)
        conn.execute("""
            INSERT INTO kv_store (key, value, updated_at)
            VALUES (?, ?, datetime('now'))
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
        """, (_token_key(), json.dumps(token_data)))
        conn.commit()


def load_token() -> dict | None:
    """Загружает токен из БД."""
    from database import _conn
    try:
        with _conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS kv_store (
                    key TEXT PRIMARY KEY, value TEXT, updated_at TEXT
                )
            """)
            row = conn.execute(
                "SELECT value FROM kv_store WHERE key=?", (_token_key(),)
            ).fetchone()
        return json.loads(row["value"]) if row else None
    except Exception:
        return None


# ── OAuth2 ────────────────────────────────────────────────────────────────────

def get_auth_url() -> str:
    """Возвращает URL для авторизации в Райффайзен."""
    import urllib.parse
    params = {
        "response_type": "code",
        "client_id":     CLIENT_ID,
        "redirect_uri":  REDIRECT_URI,
        "scope":         "accounts:read",
        "state":         "novator2026",
    }
    return AUTH_URL + "?" + urllib.parse.urlencode(params)


def exchange_code(code: str) -> dict:
    """Обменивает authorization code на access + refresh токены."""
    import urllib.request, urllib.parse
    data = urllib.parse.urlencode({
        "grant_type":    "authorization_code",
        "code":          code,
        "client_id":     CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "redirect_uri":  REDIRECT_URI,
    }).encode()
    req = urllib.request.Request(TOKEN_URL, data=data,
                                  headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=30) as r:
        token = json.loads(r.read())
    save_token(token)
    log.info("Raiffeisen token получен, expires_in=%s", token.get("expires_in"))
    return token


def refresh_access_token() -> dict | None:
    """Обновляет access token через refresh token."""
    import urllib.request, urllib.parse
    token = load_token()
    if not token or not token.get("refresh_token"):
        log.warning("Нет refresh_token — нужна повторная авторизация")
        return None
    data = urllib.parse.urlencode({
        "grant_type":    "refresh_token",
        "refresh_token": token["refresh_token"],
        "client_id":     CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    }).encode()
    try:
        req = urllib.request.Request(TOKEN_URL, data=data,
                                      headers={"Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=30) as r:
            new_token = json.loads(r.read())
        save_token(new_token)
        log.info("Raiffeisen token обновлён")
        return new_token
    except Exception as e:
        log.error("Ошибка обновления токена: %s", e)
        return None


def get_valid_token() -> str | None:
    """Возвращает актуальный access_token, при необходимости обновляет."""
    token = load_token()
    if not token:
        return None
    saved_at   = datetime.fromisoformat(token.get("saved_at", "2000-01-01"))
    expires_in = int(token.get("expires_in", 3600))
    # Обновляем за 5 минут до истечения
    if datetime.now() >= saved_at + timedelta(seconds=expires_in - 300):
        token = refresh_access_token()
    return (token or {}).get("access_token")


# ── Получение выписки ─────────────────────────────────────────────────────────

def fetch_statements(date_from: str = None, date_to: str = None) -> list[dict]:
    """
    Запрашивает транзакции через API Райффайзена.
    date_from / date_to — строки формата 'YYYY-MM-DD'. По умолчанию — вчерашний день.
    Возвращает список операций совместимых с нашим classifier.
    """
    import urllib.request

    access_token = get_valid_token()
    if not access_token:
        raise RuntimeError("Нет access token — нужна авторизация: /api/raiffeisen/auth")

    if not date_from:
        date_from = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    if not date_to:
        date_to = datetime.now().strftime("%Y-%m-%d")

    account = ACCOUNT_NUM or _get_first_account(access_token)
    if not account:
        raise RuntimeError("Не удалось определить номер счёта")

    url = (STATEMENT_URL.format(account=account)
           + f"?startDate={date_from}&endDate={date_to}&showCurrency=RUR")

    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {access_token}",
        "Accept":        "application/json",
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read())

    transactions = data if isinstance(data, list) else data.get("transactions", data.get("items", []))
    log.info("Raiffeisen API: получено %d транзакций", len(transactions))
    return transactions


def _get_first_account(access_token: str) -> str | None:
    """Получает первый счёт из API если RAIFFEISEN_ACCOUNT не задан."""
    import urllib.request
    try:
        req = urllib.request.Request(
            f"{BASE_URL}/api/v1/accounts",
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
        accounts = data if isinstance(data, list) else data.get("accounts", [])
        if accounts:
            acc = accounts[0]
            return acc.get("accountNumber") or acc.get("account") or acc.get("id")
    except Exception as e:
        log.error("Ошибка получения счетов: %s", e)
    return None


# ── Преобразование транзакций в наш формат ───────────────────────────────────

def transactions_to_df(transactions: list[dict]):
    """Конвертирует транзакции Райффайзена в DataFrame совместимый с classifier."""
    import pandas as pd

    rows = []
    for tx in transactions:
        # Поля могут называться по-разному в зависимости от версии API
        date_str = (tx.get("operationDate") or tx.get("date") or
                    tx.get("valueDate") or "")[:10]
        amount   = float(tx.get("amount") or tx.get("sum") or 0)
        tx_type  = tx.get("direction") or tx.get("transactionType") or ""
        is_debit = tx_type.lower() in ("debit", "д", "out") if tx_type else amount < 0

        rows.append({
            "Дата операции":   date_str,
            "Тип операций":    "Дебет" if is_debit else "Кредит",
            "Контрагент":      tx.get("counterparty") or tx.get("payerName") or tx.get("recipientName") or "",
            "Назначение платежа": tx.get("paymentPurpose") or tx.get("purpose") or tx.get("description") or "",
            "Дебет":           abs(amount) if is_debit else 0,
            "Кредит":          abs(amount) if not is_debit else 0,
        })

    return pd.DataFrame(rows) if rows else pd.DataFrame()


def fetch_and_load(date_from: str = None, date_to: str = None) -> dict:
    """
    Тянет выписку из API, классифицирует и сохраняет в БД.
    Используется планировщиком вместо gmail_fetcher.
    """
    from classifier import classify_operations, month_for_date
    from database import merge_month_data

    txs = fetch_statements(date_from, date_to)
    if not txs:
        return {"processed": 0, "skipped": 0, "details": []}

    df = transactions_to_df(txs)
    if df.empty:
        return {"processed": 0, "skipped": 0, "details": []}

    # Группируем по месяцу
    df["_target_month"] = df["Дата операции"].apply(lambda x: month_for_date(x, "Июнь"))

    total_ops, months_updated = 0, []
    for m, sub_df in df.groupby("_target_month"):
        sub_df = sub_df.drop(columns=["_target_month"])
        result = classify_operations(sub_df, m)
        result["source"] = "raiffeisen_api"
        merge_month_data(m, result)
        total_ops += result.get("total_ops", 0)
        months_updated.append(m)

    return {
        "processed": len(txs),
        "ops_saved": total_ops,
        "months": months_updated,
    }
