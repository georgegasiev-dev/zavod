"""
Raiffeisen Business API — выписки через Refresh-токен из РБО.

Переменные окружения (Railway → Variables):
  RAIFFEISEN_CLIENT_ID     — Client ID из API-оркестратора РБО
  RAIFFEISEN_CLIENT_SECRET — Client Secret из API-оркестратора РБО
  RAIFFEISEN_REFRESH_TOKEN — Refresh-токен, выпущенный в РБО
  RAIFFEISEN_ACCOUNT       — номер счёта (опционально)
"""
import os, json, logging, base64
from datetime import datetime, timedelta

log = logging.getLogger("raiffeisen_api")

CLIENT_ID     = os.getenv("RAIFFEISEN_CLIENT_ID",     "")
CLIENT_SECRET = os.getenv("RAIFFEISEN_CLIENT_SECRET",  "")
ACCOUNT_NUM   = os.getenv("RAIFFEISEN_ACCOUNT",        "")

SSO_URL  = "https://sso.rbo.raiffeisen.ru/token"
API_BASE = "https://api.openapi.raiffeisen.ru"


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


# ── обмен refresh_token на access_token ──────────────────────────────────────

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
    log.info("Raiffeisen tokens обновлены, expires_in=%s", token.get("expires_in"))
    return token


def get_valid_tokens() -> tuple[str, str]:
    """Возвращает (access_token, id_token), обновляя при необходимости."""
    token = load_token()
    if not token:
        initial_rt = os.getenv("RAIFFEISEN_REFRESH_TOKEN", "")
        if not initial_rt:
            raise RuntimeError(
                "Нет токена. Добавьте RAIFFEISEN_REFRESH_TOKEN в Railway Variables."
            )
        token = refresh_tokens(initial_rt)

    saved_at   = datetime.fromisoformat(token.get("saved_at", "2000-01-01"))
    expires_in = int(token.get("expires_in", 3600))
    if datetime.now() >= saved_at + timedelta(seconds=expires_in - 300):
        token = refresh_tokens(token["refresh_token"])

    return token["access_token"], token.get("id_token", "")


# ── получение счёта и выписки ─────────────────────────────────────────────────

def _get_account_id(access_token: str, id_token: str) -> str | None:
    """Возвращает UUID счёта для API. Если задан ACCOUNT_NUM — ищет по номеру, иначе берёт первый рублёвый."""
    import urllib.request
    try:
        url = f"{API_BASE}/api/v1/accounts?fields=Id,Number,Name,Currency"
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {access_token}",
            "ID-Token":      id_token,
            "Accept":        "application/json",
        })
        with urllib.request.urlopen(req, timeout=15) as r:
            accounts = json.loads(r.read())
        if not isinstance(accounts, list):
            accounts = accounts.get("accounts", [])

        if ACCOUNT_NUM:
            # Ищем по номеру счёта
            for acc in accounts:
                if acc.get("number") == ACCOUNT_NUM or acc.get("Number") == ACCOUNT_NUM:
                    return acc.get("id") or acc.get("Id")
        # Берём первый рублёвый счёт
        for acc in accounts:
            if (acc.get("currency") or acc.get("Currency") or "").upper() == "RUR":
                return acc.get("id") or acc.get("Id")
        # Фоллбэк — первый счёт
        if accounts:
            return accounts[0].get("id") or accounts[0].get("Id")
    except Exception as e:
        log.error("Ошибка получения счетов: %s", e)
    return None


def fetch_statements(date_from: str = None, date_to: str = None) -> list[dict]:
    import urllib.request
    if not date_from:
        date_from = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    if not date_to:
        date_to = datetime.now().strftime("%Y-%m-%d")

    access_token, id_token = get_valid_tokens()
    account_id = _get_account_id(access_token, id_token)
    if not account_id:
        raise RuntimeError("Не удалось определить UUID счёта")

    url = (f"{API_BASE}/api/v1/accounts/{account_id}/transactions"
           f"?startDate={date_from}&endDate={date_to}&showCurrency=RUR")
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {access_token}",
        "ID-Token":      id_token,
        "Accept":        "application/json",
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read())

    txs = data if isinstance(data, list) else data.get("transactions", data.get("items", []))
    log.info("Raiffeisen API: %d транзакций за %s–%s", len(txs), date_from, date_to)
    return txs


def transactions_to_df(transactions: list[dict]):
    import pandas as pd
    rows = []
    for tx in transactions:
        date_str = (tx.get("operationDate") or tx.get("date") or tx.get("valueDate") or "")[:10]
        amount   = float(tx.get("amount") or tx.get("sum") or 0)
        tx_type  = (tx.get("direction") or tx.get("transactionType") or "").lower()
        is_debit = tx_type in ("debit", "д", "out") if tx_type else amount < 0
        rows.append({
            "Дата операции":     date_str,
            "Тип операций":      "Дебет" if is_debit else "Кредит",
            "Контрагент":        tx.get("counterparty") or tx.get("payerName") or tx.get("recipientName") or "",
            "Назначение платежа": tx.get("paymentPurpose") or tx.get("purpose") or tx.get("description") or "",
            "Дебет":             abs(amount) if is_debit else 0,
            "Кредит":            abs(amount) if not is_debit else 0,
        })
    return pd.DataFrame(rows) if rows else pd.DataFrame()


def fetch_and_load(date_from: str = None, date_to: str = None) -> dict:
    from classifier import classify_operations, month_for_date
    from database import merge_month_data

    txs = fetch_statements(date_from, date_to)
    if not txs:
        return {"processed": 0, "months": []}

    df = transactions_to_df(txs)
    if df.empty:
        return {"processed": 0, "months": []}

    df["_target_month"] = df["Дата операции"].apply(lambda x: month_for_date(x, "Июнь"))
    total_ops, months_updated = 0, []
    for m, sub_df in df.groupby("_target_month"):
        sub_df = sub_df.drop(columns=["_target_month"])
        result = classify_operations(sub_df, m)
        result["source"] = "raiffeisen_api"
        merge_month_data(m, result)
        total_ops += result.get("total_ops", 0)
        months_updated.append(m)

    return {"processed": len(txs), "ops_saved": total_ops, "months": months_updated}
