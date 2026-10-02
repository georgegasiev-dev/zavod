"""
Парсер цен ooodeltakom.ru (ООО «ТД СтимЛайн») — ламинированная фанера.

Страница содержит три ценовых тарифа (опт / опт-розница / розница) в виде
пар "цена за м3, цена за лист". Берём розничный тариф (от 1 листа) —
он сопоставим по смыслу с ценами у остальных конкурентов в этом отчёте.
"""
import re
import requests
from bs4 import BeautifulSoup
from dataclasses import dataclass
from datetime import datetime

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
}

SKU_PAGES = [
    ("Экспериментальная (Россия) 18мм 1220x2440 сорт 1/1", "https://ooodeltakom.ru/fanera/23/124.html"),
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

    # Ищем розничный тариф ("Розница, от 1 листа, Руб.") и берём пару
    # чисел сразу после него: первое — цена за м3, второе — цена за лист.
    idx = full_text.find("Розница")
    search_area = full_text[idx:idx + 300] if idx != -1 else full_text
    m = re.search(r"(\d[\d\s]{3,7}),\d\d\s+(\d[\d\s]{2,7}),\d\d", search_area)
    if m:
        result["price"] = int(re.sub(r"\s", "", m.group(2)))

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
                site="ooodeltakom.ru",
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
