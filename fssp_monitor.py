"""Еженедельный мониторинг ФССП по ИНН ООО Новатор — официальный API."""
import os
import time
import httpx
from database import fssp_get_seen, fssp_mark_seen
from telegram_reporter import _tg_send

FSSP_TOKEN = os.getenv("FSSP_TOKEN", "")       # токен с api-ip.fssprus.ru/register
INN        = os.getenv("NOVATOR_INN", "5235006626")
BASE       = "https://api-ip.fssprus.ru/api/v1.0"


def _request(task_id: str) -> list:
    """Подаём запрос → ждём статус 0 → забираем результат."""
    params = {"token": FSSP_TOKEN, "task": task_id}

    # Ждём завершения (статус 0), макс. 30 сек
    for _ in range(10):
        time.sleep(3)
        st = httpx.get(f"{BASE}/status", params=params, timeout=15).json()
        if st.get("response", {}).get("status") == 0:
            break
    else:
        raise RuntimeError("ФССП API: таймаут ожидания результата")

    res = httpx.get(f"{BASE}/result", params=params, timeout=15).json()
    # Официальный API возвращает список записей внутри response → data
    return res.get("response", {}).get("data", [])


def _fetch() -> list:
    if not FSSP_TOKEN:
        raise RuntimeError("FSSP_TOKEN не задан — зарегистрируйся на api-ip.fssprus.ru/register")

    # Поиск ЮЛ: name + address не нужны при поиске по ИНН через general-запрос
    # Официальный endpoint /search/legal принимает inn напрямую
    r = httpx.post(
        f"{BASE}/search/legal",
        data={"token": FSSP_TOKEN, "inn": INN},
        timeout=15,
    )
    r.raise_for_status()
    data = r.json()

    if data.get("code") != 0:
        raise RuntimeError(f"ФССП API ошибка: {data.get('exception', data)}")

    task_id = data["response"]["task"]
    return _request(task_id)


def check_fssp():
    """Запускается планировщиком раз в неделю (пт 9:00)."""
    try:
        records = _fetch()
    except Exception as e:
        _tg_send(f"⚠️ ФССП-монитор: ошибка\n{e}")
        return

    seen = fssp_get_seen()
    # Уникальный ключ — номер ИП (поле ip из официального API)
    new_items = [r for r in records if r.get("ip") not in seen]

    if new_items:
        lines = [f"🔴 <b>ФССП — новые производства ({len(new_items)} шт.)</b>\n"]
        for r in new_items:
            lines.append(
                f"📋 <b>{r.get('ip', '—')}</b>\n"
                f"   Дата возбуждения: {r.get('ip_date', '—')}\n"
                f"   Предмет: {str(r.get('exe_production', '—'))[:120]}\n"
                f"   Сумма долга: {r.get('debt', '—')} руб.\n"
                f"   Взыскатель: {r.get('name', '—')}"
            )
        _tg_send("\n\n".join(lines))
        fssp_mark_seen([r["ip"] for r in new_items if r.get("ip")])
    else:
        _tg_send(f"✅ <b>ФССП</b>: новых производств нет. Активных: {len(records)}")
