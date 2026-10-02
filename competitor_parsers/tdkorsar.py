"""
Парсер цен tdkorsar.ru (ТД «Корсар», Нижний Новгород) — ламинированная фанера.

Главная цена товара на странице отдаётся в формате "N Р за л" (отличается
от формата "N Р / л" в блоках "С этим товаром покупают" / "Похожие товары",
что позволяет не спутать их регулярным выражением).
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
    ("18мм 2440x1220 F/F гл/гл береза",
     "https://tdkorsar.ru/catalog/fanera-laminirovannaya/fanera-laminirovannaya-2440-1220-18mm-f-f-gl-gl-bereza/"),
]


@dataclass
class PriceRow:
    site: str
    variant: str
    price_per_sheet: int
    in_stock: bool
    parsed_at: str
    url: str


def fetch_html(url: str) -> str:
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    return resp.text


def parse_sku_page(html: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    full_text = soup.get_text(" ", strip=True)
    result = {"price": None, "in_stock": None}

    m = re.search(r"(\d[\d\s]{2,7})\s*Р\s*за\s*л\b", full_text)
    if m:
        result["price"] = int(re.sub(r"\s", "", m.group(1)))

    result["in_stock"] = "Нет в наличии" not in full_text.split("В корзину")[0] if "В корзину" in full_text else None

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
                site="tdkorsar.ru",
                variant=name,
                price_per_sheet=data["price"],
                in_stock=data["in_stock"],
                parsed_at=now,
                url=url,
            ))
        except requests.RequestException as e:
            print(f"[ERROR] Не смог загрузить '{name}' ({url}): {e}")
    return rows


if __name__ == "__main__":
    for r in fetch_all():
        stock = " [в наличии]" if r.in_stock else (" [нет в наличии]" if r.in_stock is False else "")
        print(f"{r.site} | {r.variant} | {r.price_per_sheet}₽/лист{stock} | {r.parsed_at}")
