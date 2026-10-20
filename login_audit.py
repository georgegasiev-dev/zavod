"""Login attempt telemetry for the Novator payment calendar.

Only use this on explicit password-check requests, not on every authenticated
API call. Never collect passwords, Authorization headers, cookies or tokens.
Client-supplied forwarding headers are diagnostic and may be spoofed.
"""
from datetime import datetime
from html import escape
from urllib.parse import urlsplit
from uuid import uuid4
from zoneinfo import ZoneInfo
import json
import logging

log = logging.getLogger(__name__)


def _clip(value, limit=300):
    return str(value or "").replace("\r", " ").replace("\n", " ").strip()[:limit]


def _safe_page(value):
    """Include referrer origin/path, never its query parameters or fragments."""
    try:
        uri = urlsplit(_clip(value, 600))
        if uri.scheme not in ("http", "https"):
            return ""
        return _clip(f"{uri.scheme}://{uri.netloc}{uri.path}", 300)
    except ValueError:
        return ""


def collect_login_attempt(request, username, accepted):
    headers = request.headers
    peer_ip = _clip(request.client.host if request.client else "", 128)
    forwarded = _clip(headers.get("x-forwarded-for"), 350)
    x_real_ip = _clip(headers.get("x-real-ip"), 128)
    cf_ip = _clip(headers.get("cf-connecting-ip"), 128)
    observed_ip = forwarded.split(",")[0].strip() if forwarded else (x_real_ip or cf_ip or peer_ip)
    now = datetime.now(ZoneInfo("Europe/Moscow"))
    return {
        "event_id": uuid4().hex[:12],
        "result": "success" if accepted else "failure",
        "time_msk": now.strftime("%d.%m.%Y %H:%M:%S МСК"),
        "timestamp": now.isoformat(),
        "username": _clip(username, 128) or "(не указан)",
        "ip": observed_ip or "—",
        "connection_ip": peer_ip or "—",
        "forwarded_for": forwarded,
        "x_real_ip": x_real_ip,
        "cloudflare_ip": cf_ip,
        "user_agent": _clip(headers.get("user-agent"), 600),
        "client_platform": _clip(headers.get("sec-ch-ua-platform"), 120),
        "client_browser_hints": _clip(headers.get("sec-ch-ua"), 250),
        "mobile_hint": _clip(headers.get("sec-ch-ua-mobile"), 16),
        "language": _clip(headers.get("accept-language"), 200),
        "origin": _safe_page(headers.get("origin")),
        "referer": _safe_page(headers.get("referer")),
        "request": f"{request.method} {request.url.path}",
    }


def format_login_alert(event):
    heading = "✅ <b>Успешный вход в платёжный календарь</b>" if event["result"] == "success" else "🚨 <b>Неудачная попытка входа в календарь</b>"
    attrs = (
        ("Время", "time_msk"),
        ("Логин", "username"),
        ("IP посетителя (по заголовкам)", "ip"),
        ("IP соединения с сервером", "connection_ip"),
        ("X-Forwarded-For", "forwarded_for"),
        ("X-Real-IP", "x_real_ip"),
        ("Cloudflare IP", "cloudflare_ip"),
        ("Браузер / устройство (User-Agent)", "user_agent"),
        ("Платформа", "client_platform"),
        ("Браузер (Client Hints)", "client_browser_hints"),
        ("Мобильное устройство", "mobile_hint"),
        ("Язык", "language"),
        ("Страница", "referer"),
        ("Origin", "origin"),
        ("Запрос", "request"),
        ("ID события", "event_id"),
    )
    lines = [heading]
    for label, key in attrs:
        value = event.get(key)
        if value:
            lines.append(f"<b>{escape(label)}:</b> {escape(str(value))}")
    lines.append("<i>IP и сведения об устройстве получены из HTTP-запроса и могут быть подменены. Пароль не сохраняется.</i>")
    return "\n".join(lines)[:3900]


def record_and_alert(request, username, accepted, write_log, send_alert):
    """Store one login attempt and send one Telegram alert, independently.

    A database failure must not prevent a notification or change login outcome.
    The Telegram sender performs asynchronous delivery.
    """
    event = collect_login_attempt(request, username, accepted)
    try:
        write_log("login_ok" if accepted else "login_failed",
                  event["ip"], json.dumps(event, ensure_ascii=False))
    except Exception as exc:
        log.error("Cannot persist login audit event %s: %s",
                  event["event_id"], type(exc).__name__)
    try:
        send_alert(format_login_alert(event))
    except Exception as exc:
        log.error("Cannot enqueue login alert %s: %s",
                  event["event_id"], type(exc).__name__)
    return event
