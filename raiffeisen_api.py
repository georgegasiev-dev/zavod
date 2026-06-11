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
        "intraday": True,   # включает операции текущего дня в реальном времени
    }
    headers = _auth_headers(access_token, id_token)
    resp = _post(f"{STATEMENTS_API}/v1/reports/excel", body, headers)
    report_id = resp.get("reportId")
    if not report_id:
        raise RuntimeError(f"Не получен reportId: {resp}")
    log.info("Raiffeisen: reportId=%s", report_id)
    return report_id


def wait_for_report(report_id: str, access_token: str, id_token: str,
                    max_wait: int = 300) -> str:
    """Ждёт готовности отчёта, возвращает финальный статус."""
    import httpx
    headers = {k: v for k, v in _auth_headers(access_token, id_token).items()
               if k != "Content-Type"}
    url = f"{STATEMENTS_API}/v1/reports/{report_id}/status"
    deadline = time.time() + max_wait
    last_status = "UNKNOWN"
    last_body   = ""
    consecutive_errors = 0
    with httpx.Client(timeout=15) as client:
        while time.time() < deadline:
            try:
                resp = client.get(url, headers=headers)
                last_body = resp.text[:300]
                if not resp.is_success:
                    consecutive_errors += 1
                    log.warning("Status check %s → %s: %s (err #%d)",
                                report_id, resp.status_code, last_body, consecutive_errors)
                    if consecutive_errors >= 3:
                        raise RuntimeError(
                            f"Статус-опрос отчёта {report_id} отказывает: "
                            f"HTTP {resp.status_code}: {last_body}"
                        )
                    time.sleep(5)
                    continue
                consecutive_errors = 0
                data = resp.json()
                last_status = data.get("status", "UNKNOWN")
                log.info("Raiffeisen report status: %s", last_status)
                if last_status.upper() == "COMPLETED":
                    return last_status
                if last_status.upper() in ("FAILED", "STOPPED", "CANCELLED"):
                    raise RuntimeError(
                        f"Генерация отчёта завершилась со статусом {last_status}: {last_body}"
                    )
            except RuntimeError:
                raise
            except Exception as e:
                consecutive_errors += 1
                log.warning("Status poll exception #%d: %s", consecutive_errors, e)
                if consecutive_errors >= 3:
                    raise RuntimeError(f"Статус-опрос упал 3 раза подряд: {e}")
            time.sleep(5)
    raise RuntimeError(
        f"Таймаут ({max_wait}s) ожидания отчёта {report_id}. "
        f"Последний статус: {last_status}. Тело: {last_body}"
    )


def download_report(report_id: str, access_token: str, id_token: str) -> bytes:
    """Скачивает файл отчёта, возвращает байты Excel."""
    headers = {k: v for k, v in _auth_headers(access_token, id_token).items()
               if k not in ("Content-Type",)}
    return _get(f"{STATEMENTS_API}/v1/reports/{report_id}/file", headers)


# ── Основная точка входа ──────────────────────────────────────────────────────

def _normalize_date(d: str) -> str:
    """Приводит дату к формату YYYY-MM-DD. Принимает DD.MM.YYYY или YYYY-MM-DD."""
    if not d:
        return d
    d = d.strip()
    if len(d) == 10 and d[4] == "-":
        return d  # уже ISO
    # DD.MM.YYYY → YYYY-MM-DD
    parts = d.replace("/", ".").split(".")
    if len(parts) == 3:
        dd, mm, yyyy = parts
        if len(yyyy) == 4:
            return f"{yyyy}-{mm.zfill(2)}-{dd.zfill(2)}"
    raise ValueError(f"Неизвестный формат даты: {d!r}. Ожидается YYYY-MM-DD или DD.MM.YYYY")


def _current_week_range() -> tuple[str, str]:
    """Возвращает (понедельник, сегодня) для текущей недели в формате YYYY-MM-DD."""
    import datetime as _dt
    today     = _dt.date.today()
    monday    = today - _dt.timedelta(days=today.weekday())   # weekday(): 0=пн, 6=вс
    sunday    = monday + _dt.timedelta(days=6)
    week_end  = min(sunday, today)                             # не выходим за сегодня
    return monday.strftime("%Y-%m-%d"), week_end.strftime("%Y-%m-%d")


def fetch_statements(date_from: str = None, date_to: str = None) -> bytes:
    """Запрашивает, ждёт и скачивает Excel-выписку. Возвращает байты файла."""
    if not date_from and not date_to:
        date_from, date_to = _current_week_range()
        log.info("Период не задан — берём текущую неделю: %s … %s", date_from, date_to)
    elif not date_from:
        date_from = _normalize_date(date_to)
    elif not date_to:
        date_to = _normalize_date(date_from)
    date_from = _normalize_date(date_from)
    date_to   = _normalize_date(date_to)

    access_token, id_token = get_valid_tokens()
    account_key = _get_account_key(access_token, id_token)
    log.info("Raiffeisen: account_key=%s, %s → %s", account_key, date_from, date_to)

    report_id = generate_excel_report(account_key, date_from, date_to, access_token, id_token)
    wait_for_report(report_id, access_token, id_token)
    return download_report(report_id, access_token, id_token)


def _send_excel_to_telegram(xlsx_bytes: bytes, date_from: str, date_to: str):
    """Отправляет Excel-файл выписки в Telegram."""
    import httpx, os
    tg_token = os.getenv("TG_TOKEN", "8616497543:AAHo0UJBuRcbg-vElznHcIwkdBhL18DvVs0")
    chat_id  = os.getenv("TG_CHAT_ID", "269628847")
    filename = f"raiffeisen_{date_from}_{date_to}.xlsx"
    try:
        with httpx.Client(timeout=30) as client:
            client.post(
                f"https://api.telegram.org/bot{tg_token}/sendDocument",
                data={"chat_id": chat_id, "caption": f"📊 Выписка Райффайзен {date_from} — {date_to}"},
                files={"document": (filename, xlsx_bytes,
                       "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
            )
        log.info("Excel выписка отправлена в Telegram: %s", filename)
    except Exception as e:
        log.warning("Не удалось отправить Excel в Telegram: %s", e)


def fetch_and_load(date_from: str = None, date_to: str = None) -> dict:
    """Получает выписку и сохраняет в БД."""
    import io
    import pandas as pd
    from classifier import classify_operations, month_for_date
    from database import merge_month_data

    # Нормализуем даты для корректного имени файла
    d_from = _normalize_date(date_from) if date_from else              (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d")
    d_to   = _normalize_date(date_to)   if date_to   else d_from

    xlsx_bytes = fetch_statements(date_from, date_to)


    # ── Парсинг выписки Raiffeisen Excel ─────────────────────────────────
    # Структура файла:
    #   строки 0-8:  метаданные (банк, клиент, ИНН и т.д.)
    #   строка 9:    заголовки колонок
    #   строки 10-11: подзаголовки и нумерация — пропускаем
    #   строки 12+:  операции
    #   последние 4: итоговые строки (Обороты, Остаток и т.д.)
    raw = pd.read_excel(io.BytesIO(xlsx_bytes), header=None)
    log.info("Excel прочитан: %d строк", len(raw))

    # Данные с строки 12 (индекс 11), убираем последние 4 итоговые строки
    data = raw.iloc[12:-4].copy()
    data.columns = range(len(data.columns))

    # Переименовываем нужные колонки в понятные для classify_operations имена
    data = data.rename(columns={
        2:  "Дата операции",
        6:  "Контрагент",       # "Реквизиты корреспондента" → classifier ищет "контраг"
        9:  "Дебет",
        10: "Кредит",
        11: "Назначение платежа",
    })

    # Оставляем только нужные колонки
    keep = ["Дата операции", "Контрагент", "Дебет", "Кредит", "Назначение платежа"]
    df = data[keep].copy()

    # Фильтруем строки без валидной даты (мусорные строки)
    df["Дата операции"] = pd.to_datetime(df["Дата операции"], errors="coerce")
    df = df.dropna(subset=["Дата операции"])
    df = df[df["Дата операции"].dt.year >= 2020]

    # NaN → 0 чтобы сравнения raw_debit == 0 работали корректно
    df["Дебет"]  = pd.to_numeric(df["Дебет"],  errors="coerce").fillna(0)
    df["Кредит"] = pd.to_numeric(df["Кредит"], errors="coerce").fillna(0)

    if df.empty:
        return {"processed": 0, "months": []}

    log.info("После фильтрации: %d операций", len(df))

    df["_target_month"] = df["Дата операции"].apply(lambda x: month_for_date(x, "Июнь"))
    total_ops, months_updated = 0, []
    for m, sub_df in df.groupby("_target_month"):
        sub_df = sub_df.drop(columns=["_target_month"]).reset_index(drop=True)
        result = classify_operations(sub_df, m)
        result["source"] = "raiffeisen_api"
        merge_month_data(m, result)
        total_ops += result.get("total_ops", 0)
        months_updated.append(m)

    # ── Сверка итогов с банковскими данными ──────────────────────────────
    def _parse_balance(raw_val) -> float:
        """Парсит строку вида '163 582.93 (Кр/Cr)' → 163582.93"""
        try:
            s = str(raw_val).split('(')[0]          # убираем (Кр/Cr)
            s = s.replace(' ', '').replace(' ', '').replace(',', '.')
            return abs(float(s))
        except Exception:
            return 0.0

    recon = {}
    try:
        opening = _parse_balance(raw.iloc[7, 1])     # строка 7, кол 1
        closing = _parse_balance(raw.iloc[-1, 1])     # последняя строка, кол 1
        total_debit  = float(df["Дебет"].sum())
        total_credit = float(df["Кредит"].sum())
        expected_closing = round(opening + total_credit - total_debit, 2)
        diff = round(closing - expected_closing, 2)
        recon = {
            "opening_balance":   round(opening, 2),
            "closing_balance":   round(closing, 2),
            "total_debit":       round(total_debit, 2),
            "total_credit":      round(total_credit, 2),
            "expected_closing":  expected_closing,
            "discrepancy":       diff,
            "ok":                abs(diff) < 1.0,
        }
        if not recon["ok"]:
            log.warning("⚠️ Сверка не сошлась! Расхождение: %.2f ₽ (ожидалось %.2f, в банке %.2f)",
                        diff, expected_closing, closing)
        else:
            log.info("✅ Сверка OK: входящий %.2f + кредит %.2f − дебет %.2f = %.2f (банк: %.2f)",
                     opening, total_credit, total_debit, expected_closing, closing)
        # Сохраняем баланс счёта в БД для отображения на фронте
        try:
            from database import save_setting
            save_setting("account_balance", str(closing))
            save_setting("balance_updated_at", datetime.now().isoformat())
        except Exception as se:
            log.warning("Не удалось сохранить баланс: %s", se)
    except Exception as e:
        log.warning("Сверка не выполнена: %s", e)
        recon = {"ok": None, "error": str(e)[:100]}

    return {"processed": len(df), "ops_saved": total_ops, "months": months_updated,
            "reconciliation": recon}
