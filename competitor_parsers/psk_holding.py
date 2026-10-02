"""
Парсер цен psk-holding.ru (ГК «ПромСтройКонтракт») — ламинированная фанера.

ВАЖНО: страницы этого сайта (1C-Bitrix) отдают гигантское меню каталога
перед основным контентом, из-за чего верстку карточки товара не удалось
визуально проверить при написании парсера. Логика ниже использует два
независимых способа найти цену:
  1) структурированные данные Schema.org (JSON-LD, <script type="application/ld+json">
     с "@type": "Product"/"Offer") — стандартный для интернет-магазинов способ,
     не завязанный на конкретную вёрстку;
  2) текстовый регекс на "цена ... руб" как запасной вариант.
Если оба способа не сработают, товар просто не попадёт в отчёт (с WARN в лог) —
это не уронит остальной сбор. Если после первого реального запуска выяснится,
что цена не подхватилась, пришлите один прогон с выводом — поправим по месту.
"""
import json
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
    ("Семеновский ФЗ 18x1220x2440", "https://psk-holding.ru/products/fanera-laminirovannaya-semenovskiy-fz-18x1220x2440/"),
    ("18x1220x2440 плёнка 220 YUPM Kyummene Чудово", "https://psk-holding.ru/products/fanera-laminirovannaya-18kh1220kh2440-mm-plenka-220-yupm-kyummene-chudovo-/"),
    ("18мм 1220x2440 плёнка 120 СВЕЗА без лого", "https://psk-holding.ru/products/laminirovannaya-fanera-18-mm-1220kh2440-plenka-120-sveza-bez-logo/"),
    ("сорт 1/1 СВЕЗА 18x1220x2440", "https://psk-holding.ru/products/fanera-laminirovannaya-sort-1-1-sveza-18kh1220kh2440-mm/"),
    ("продольная, Сявский ФЗ 18x2440x1220 плёнка 120", "https://psk-holding.ru/products/fanera-prodolnaya-laminirovannaya-syavskiy-fz-18kh2440kh1220-mm-plenka-120/"),
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


def _price_from_jsonld(soup: BeautifulSoup) -> int | None:
    for tag in soup.find_all("script", {"type": "application/ld+json"}):
        try:
            data = json.loads(tag.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        candidates = data if isinstance(data, list) else [data]
        for c in candidates:
            if not isinstance(c, dict):
                continue
            offers = c.get("offers")
            if isinstance(offers, dict) and offers.get("price"):
                try:
                    return int(float(offers["price"]))
                except (TypeError, ValueError):
                    pass
            if isinstance(offers, list):
                for o in offers:
                    if isinstance(o, dict) and o.get("price"):
                        try:
                            return int(float(o["price"]))
                        except (TypeError, ValueError):
                            pass
    return None


def _price_from_text(soup: BeautifulSoup) -> int | None:
    full_text = soup.get_text(" ", strip=True)
    m = re.search(r"(\d[\d\s]{2,7})\s*(?:руб|₽)", full_text, re.IGNORECASE)
    if m:
        return int(re.sub(r"\s", "", m.group(1)))
    return None


def parse_sku_page(html: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    price = _price_from_jsonld(soup)
    if price is None:
        price = _price_from_text(soup)
    return {"price": price}


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
                site="psk-holding.ru",
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
