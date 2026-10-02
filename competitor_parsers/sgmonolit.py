"""
Парсер цен sgmonolit.ru (1-я Опалубочная Компания, бренд «Сыктывкарский
фанерный завод») — ламинированная фанера.

ВАЖНО: сайт возвращает 404 на запросы с дата-центровых IP (похоже на
WAF/анти-бот защиту, маскирующуюся под "страница не найдена") — в браузере
страница открывается нормально. Это означает, что и этот скрипт, запущенный
с сервера на Timeweb, с высокой вероятностью не сможет получить страницу.
Разметку страницы вживую увидеть не удалось, поэтому используется только
общий текстовый regex на "цена ... руб/₽" без привязки к конкретной вёрстке.
Если позиция стабильно не появляется в отчёте — сайт блокирует сервер,
нужен другой подход (прокси, либо цена вручную).
"""
import re
import requests
from bs4 import BeautifulSoup
from dataclasses import dataclass
from datetime import datetime

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "ru-RU,ru;q=0.9",
}

SKU_PAGES = [
    ("Сыктывкарский ФЗ", "https://www.sgmonolit.ru/products/fanera-laminirovannaya/syktyvkarskii-fanernyi-zavod/"),
]


@dataclass
class PriceRow:
    site: str
    variant: str
    price_per_sheet: int
    parsed_at: str
    url: str


def fetch_html(url: str) -> str:
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    return resp.text


def parse_sku_page(html: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    full_text = soup.get_text(" ", strip=True)
    result = {"price": None}

    m = re.search(r"от\s+(\d[\d\s]{2,7})\s*(?:руб|₽)", full_text, re.IGNORECASE)
    if not m:
        m = re.search(r"(\d[\d\s]{2,7})\s*(?:руб|₽)\s*/?\s*лист", full_text, re.IGNORECASE)
    if m:
        result["price"] = int(re.sub(r"\s", "", m.group(1)))

    return result


def fetch_all() -> list[PriceRow]:
    rows = []
    now = datetime.now().isoformat()
    for name, url in SKU_PAGES:
        try:
            html = fetch_html(url)
            data = parse_sku_page(html)
            if data["price"] is None:
                print(f"[WARN] Не нашёл цену на странице '{name}' ({url}) — проверь вёрстку вручную")
                continue
            rows.append(PriceRow(
                site="sgmonolit.ru",
                variant=name,
                price_per_sheet=data["price"],
                parsed_at=now,
                url=url,
            ))
        except requests.RequestException as e:
            print(f"[ERROR] Не смог загрузить '{name}' ({url}): {e}")
    return rows


if __name__ == "__main__":
    for r in fetch_all():
        print(f"{r.site} | {r.variant} | {r.price_per_sheet}₽/лист | {r.parsed_at}")
