"""
IMAP-воркер: каждые N минут проверяет Gmail,
скачивает .xlsx вложения из писем банка,
классифицирует и сохраняет в БД.

Запуск: python email_watcher.py
"""
import imaplib, email, io, time, os, logging, re
from email.header import decode_header
import pandas as pd
from classifier import classify_operations
from database import save_month_data

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%d.%m.%Y %H:%M:%S',
)
log = logging.getLogger("watcher")

# ── конфиг из env ─────────────────────────────────────────────────────────────
IMAP_HOST      = os.getenv("IMAP_HOST",       "imap.gmail.com")
IMAP_PORT      = int(os.getenv("IMAP_PORT",   "993"))
EMAIL_USER     = os.getenv("EMAIL_USER",       "")   # ваш Gmail
EMAIL_PASS     = os.getenv("EMAIL_PASS",       "")   # App Password (не основной пароль!)
SENDER_FILTER  = os.getenv("SENDER_FILTER",   "")   # e.g. "@raiffeisen.ru" — только письма от банка
CHECK_EVERY    = int(os.getenv("CHECK_EVERY",  "900"))  # секунды (900 = 15 мин)

MONTH_KEYWORDS = {
    'янв':'Январь','фев':'Февраль','мар':'Март','апр':'Апрель',
    'май':'Май','мая':'Май','июн':'Июнь','июл':'Июль','авг':'Август',
    'сен':'Сентябрь','окт':'Октябрь','ноя':'Ноябрь','дек':'Декабрь',
    '01':'Январь','02':'Февраль','03':'Март','04':'Апрель','05':'Май',
    '06':'Июнь','07':'Июль','08':'Август','09':'Сентябрь',
    '10':'Октябрь','11':'Ноябрь','12':'Декабрь',
}

def _decode(val: str) -> str:
    parts = decode_header(val or '')
    out = ''
    for chunk, enc in parts:
        if isinstance(chunk, bytes):
            out += chunk.decode(enc or 'utf-8', errors='replace')
        else:
            out += str(chunk)
    return out

def _guess_month(subject: str, filename: str) -> str:
    text = (subject + ' ' + filename).lower()
    for kw, month in MONTH_KEYWORDS.items():
        if kw in text:
            return month
    return 'Май'  # fallback

def _process_message(msg) -> bool:
    subject = _decode(msg.get('Subject', ''))
    sender  = msg.get('From', '')

    if SENDER_FILTER and SENDER_FILTER.lower() not in sender.lower():
        log.debug(f"Пропускаем письмо от {sender!r} (не банк)")
        return False

    log.info(f"Обрабатываем: «{subject}» от {sender}")
    processed = False

    for part in msg.walk():
        filename = _decode(part.get_filename() or '')
        if not filename.lower().endswith(('.xlsx', '.xls')):
            continue
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        try:
            df = pd.read_excel(io.BytesIO(payload))
            month = _guess_month(subject, filename)
            result = classify_operations(df, month)
            result['source'] = 'email'
            save_month_data(month, result)
            log.info(f"✓ {month}: {result['total_ops']} операций, "
                     f"{len(result['unknown'])} неопознанных, файл «{filename}»")
            processed = True
        except Exception as e:
            log.error(f"Ошибка обработки «{filename}»: {e}")

    return processed

def watch():
    log.info(f"IMAP-воркер запущен. Ящик: {EMAIL_USER}, проверка каждые {CHECK_EVERY}с")
    if not EMAIL_USER or not EMAIL_PASS:
        log.error("EMAIL_USER / EMAIL_PASS не заданы! Проверь config.env")
        return

    while True:
        try:
            with imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT) as imap:
                imap.login(EMAIL_USER, EMAIL_PASS)
                imap.select("INBOX")
                _, ids = imap.search(None, 'UNSEEN')
                msg_ids = ids[0].split()
                if msg_ids:
                    log.info(f"Новых писем: {len(msg_ids)}")
                for mid in msg_ids:
                    _, data = imap.fetch(mid, '(RFC822)')
                    msg = email.message_from_bytes(data[0][1])
                    _process_message(msg)
                    imap.store(mid, '+FLAGS', '\\Seen')
        except imaplib.IMAP4.error as e:
            log.error(f"IMAP ошибка авторизации: {e}")
        except Exception as e:
            log.error(f"Ошибка соединения: {e}")
        time.sleep(CHECK_EVERY)

if __name__ == "__main__":
    watch()
