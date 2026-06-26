"""Еженедельный мониторинг ФССП по ИНН ООО Новатор."""
import os
import httpx
from database import fssp_get_seen, fssp_mark_seen
from telegram_reporter import _tg_send

FSSP_API_KEY = os.getenv("FSSP_API_KEY", "")
INN = os.getenv("NOVATOR_INN", "5235006626")


def _fetch() -> list:
    if not FSSP_API_KEY:
        raise RuntimeError("FSSP_API_KEY не задан")
    r = httpx.get(
        "https://parser-api.com/parser/fssp_api/search_ur_by_inn",
        params={"key": FSSP_API_KEY, "inn": INN},
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    return data.get("result", []) if data.get("done") == 1 else []


def check_fssp():
    """Запускается планировщиком раз в неделю."""
    try:
        records = _fetch()
    except Exception as e:
        _tg_send(f"⚠️ ФССП-монитор: ошибка запроса\n{e}")
        return

    seen = fssp_get_seen()
    new_items = [r for r in records if r.get("process_title") not in seen]

    if new_items:
        lines = [f"🔴 <b>ФССП — новые производства ({len(new_items)} шт.)</b>\n"]
        for r in new_items:
            lines.append(
                f"📋 <b>{r['process_title']}</b>\n"
                f"   Дата: {r.get('process_date', '—')}\n"
                f"   Документ: {str(r.get('document_title', '—'))[:100]}\n"
                f"   Взыскатель ИНН: {r.get('document_claimer_inn', '—')}"
            )
        _tg_send("\n\n".join(lines))
        fssp_mark_seen([r["process_title"] for r in new_items])
    else:
        total = len(records)
        _tg_send(f"✅ <b>ФССП</b>: новых производств нет. Активных всего: {total}")
