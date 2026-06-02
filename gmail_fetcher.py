"""
Gmail IMAP — автозагрузка выписки Райффайзен.

Переменные окружения (Railway → Variables):
  GMAIL_USER          — ваш адрес Gmail (например, yourname@gmail.com)
  GMAIL_APP_PASSWORD  — App Password из настроек Google (16 символов без пробелов)
  RAIFFEISEN_FROM     — адрес отправителя выписки (например, noreply@raiffeisen.ru)
                        или часть темы письма, если FROM неизвестен
  GMAIL_FOLDER        — папка для поиска (по умолчанию INBOX)
"""
import imaplib
import email
import os
import io
import logging
from datetime import datetime, timedelta
from email.header import decode_header

import pandas as pd

from classifier import classify_operations
from database import merge_month_data

log = logging.getLogger("gmail_fetcher")

# ── настройки ─────────────────────────────────────────────────────────────────
GMAIL_USER         = os.getenv("GMAIL_USER", "")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "")
RAIFFEISEN_FROM    = os.getenv("RAIFFEISEN_FROM", "raiffeisen")   # ищем вхождение в адресе отправителя или теме
GMAIL_FOLDER       = os.getenv("GMAIL_FOLDER", "INBOX")

MONTH_MAP = {
    1: "Январь", 2: "Февраль", 3: "Март", 4: "Апрель",
    5: "Май",    6: "Июнь",    7: "Июль", 8: "Август",
    9: "Сентябрь", 10: "Октябрь", 11: "Ноябрь", 12: "Декабрь",
}

# ── вспомогательные функции ───────────────────────────────────────────────────

def _decode_str(s: str | bytes) -> str:
    """Декодирует заголовок письма в строку."""
    if isinstance(s, bytes):
        return s.decode(errors="replace")
    parts = decode_header(s)
    result = ""
    for part, enc in parts:
        if isinstance(part, bytes):
            result += part.decode(enc or "utf-8", errors="replace")
        else:
            result += part
    return result


def _month_from_date(dt: datetime) -> str:
    """Возвращает русское название месяца по дате."""
    return MONTH_MAP.get(dt.month, "Май")


def _month_from_filename(filename: str) -> str | None:
    """
    Пробует вытащить месяц из имени файла.
    Ожидаемые форматы: statement_2026-05.xlsx, 2026_05_novator.xlsx и т.п.
    Возвращает None если не удалось.
    """
    import re
    m = re.search(r"20\d{2}[-_](\d{2})", filename)
    if m:
        month_num = int(m.group(1))
        if 1 <= month_num <= 12:
            return MONTH_MAP[month_num]
    return None


def _connect() -> imaplib.IMAP4_SSL:
    """Подключается к Gmail через IMAP SSL."""
    if not GMAIL_USER or not GMAIL_APP_PASSWORD:
        raise RuntimeError("GMAIL_USER и GMAIL_APP_PASSWORD не заданы в переменных окружения")
    imap = imaplib.IMAP4_SSL("imap.gmail.com", 993)
    imap.login(GMAIL_USER, GMAIL_APP_PASSWORD)
    return imap


# ── основная функция ──────────────────────────────────────────────────────────

def fetch_and_upload() -> dict:
    """
    Проверяет Gmail, скачивает новые выписки и загружает в БД.
    Возвращает словарь с результатом: {processed, skipped, errors, details}.
    """
    if not GMAIL_USER or not GMAIL_APP_PASSWORD:
        return {"processed": 0, "skipped": 0, "errors": ["GMAIL_USER / GMAIL_APP_PASSWORD не заданы"], "details": []}

    result = {"processed": 0, "skipped": 0, "errors": [], "details": []}

    try:
        imap = _connect()
    except Exception as e:
        result["errors"].append(f"Ошибка подключения к Gmail: {e}")
        log.error("IMAP connect error: %s", e)
        return result

    try:
        imap.select(GMAIL_FOLDER)

        # Ищем непрочитанные письма за последние 3 дня
        since_date = (datetime.now() - timedelta(days=3)).strftime("%d-%b-%Y")
        status_code, msg_ids = imap.search(None, f'(UNSEEN SINCE "{since_date}")')
        if status_code != "OK" or not msg_ids[0]:
            log.info("Новых писем не найдено")
            imap.logout()
            return result

        ids = msg_ids[0].split()
        log.info("Найдено непрочитанных писем: %d", len(ids))

        for msg_id in ids:
            try:
                _, msg_data = imap.fetch(msg_id, "(RFC822)")
                raw = msg_data[0][1]
                msg = email.message_from_bytes(raw)

                subject = _decode_str(msg.get("Subject", ""))
                sender  = _decode_str(msg.get("From", ""))
                log.info("Письмо: from=%s subject=%s", sender, subject)

                # Фильтр: письмо должно быть от Райффайзена
                raiff_kw = RAIFFEISEN_FROM.lower()
                if raiff_kw not in sender.lower() and raiff_kw not in subject.lower():
                    log.info("Пропускаем — не от Райффайзена")
                    result["skipped"] += 1
                    continue

                # Ищем .xlsx вложение
                xlsx_data = None
                xlsx_name = ""
                for part in msg.walk():
                    content_disp = part.get("Content-Disposition", "")
                    filename = part.get_filename()
                    if filename:
                        filename = _decode_str(filename)
                    if (filename and filename.lower().endswith((".xlsx", ".xls"))
                            and "attachment" in content_disp.lower()):
                        xlsx_data = part.get_payload(decode=True)
                        xlsx_name = filename
                        break

                if not xlsx_data:
                    log.info("Вложение .xlsx не найдено в письме")
                    result["skipped"] += 1
                    continue

                # Определяем месяц
                month = _month_from_filename(xlsx_name)
                if not month:
                    # берём месяц по дате письма
                    date_str = msg.get("Date", "")
                    try:
                        from email.utils import parsedate_to_datetime
                        msg_dt = parsedate_to_datetime(date_str)
                        month = _month_from_date(msg_dt)
                    except Exception:
                        month = _month_from_date(datetime.now())

                log.info("Обрабатываем файл '%s' → месяц '%s'", xlsx_name, month)

                # Читаем и группируем по фактическому месяцу даты
                df = pd.read_excel(io.BytesIO(xlsx_data))
                from classifier import _find_col, month_for_date
                date_col = _find_col(df, 'дата')
                if date_col:
                    df['_target_month'] = df[date_col].apply(lambda x: month_for_date(x, month))
                else:
                    df['_target_month'] = month

                processed_months = []
                total_ops, total_unknown = 0, 0
                for m, sub_df in df.groupby('_target_month'):
                    sub_df = sub_df.drop(columns=['_target_month'])
                    classified = classify_operations(sub_df, m)
                    classified["source"] = "gmail_auto"
                    merge_month_data(m, classified)
                    total_ops += classified.get("total_ops", 0)
                    total_unknown += len(classified.get("unknown", []))
                    processed_months.append(m)

                # Помечаем письмо прочитанным
                imap.store(msg_id, "+FLAGS", "\\Seen")

                result["processed"] += 1
                result["details"].append({
                    "file": xlsx_name,
                    "months": processed_months,
                    "ops": total_ops,
                    "unknown": total_unknown,
                })
                log.info("Загружено: %s → %s (%d операций)", xlsx_name, month, classified["total_ops"])

            except Exception as e:
                log.error("Ошибка обработки письма %s: %s", msg_id, e)
                result["errors"].append(str(e))

    finally:
        try:
            imap.logout()
        except Exception:
            pass

    return result
