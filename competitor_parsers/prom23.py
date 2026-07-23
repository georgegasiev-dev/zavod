"""
Парсер цен prom23.ru — ламинированная фанера, 18 мм, 2440x1220.

Этот сайт — самый удобный из всех: чистый UTF-8, цена лежит прямо в HTML
без JS-рендеринга, и на странице уже есть собственная метка времени
"Последнее обновление цены" — полезно для проверки свежести данных.
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

# Известные карточки 18мм / 2440x1220
SKU_PAGES = [
    ("TeaM GRID сорт 1/1", "https://www.prom23.ru/catalog/fanera-laminirovannaya/18-mm/id-faneralaminirovannaya2440kh1220kh18mmsort119067/"),
    ("СВЕЗА ДЭК сорт 1/1", "https://www.prom23.ru/catalog/fanera-laminirovannaya/18-mm/id-sveza-dek-2440-kh-1220-kh-18-mm-sort-1-1/"),
    ("ЖФК сорт 1/1", "https://www.prom23.ru/catalog/fanera-laminirovannaya/18-mm/id-zhfk-122-kh-244-kh-18-mm-sort-1-1/"),
    ("Плайтерра сорт 1/1", "https://www.prom23.ru/catalog/fanera-laminirovannaya/18-mm/id-playterra-1220-kh-2440-kh-18-mm/"),
    ("TeaM сорт 1/1", "https://www.prom23.ru/catalog/fanera-laminirovannaya/18-mm/id-fanerateamlaminirovannaya2440kh1220kh18mmberezasort11/"),
]


@dataclass
class PriceRow:
    site: str
    variant: str
    price: int
    old_price: int | None
    in_stock: bool
    price_updated_on_site: str | None  # метка времени самого сайта "Последнее обновление цены"
    parsed_at: str
    url: str


def fetch_html(url: str) -> str:
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    return resp.text


def parse_sku_page(html: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    full_text = soup.get_text(" ", strip=True)
    result = {"price": None, "old_price": None, "price_updated_on_site": None}

    # Цена обычно в виде: "3375 руб. 2 840 руб. ₽" (старая зачёркнутая, потом текущая)
    m = re.search(r"(\d[\d\s]{2,7})\s*руб\.\s*(\d[\d\s]{2,7})\s*руб\.\s*₽", full_text)
    if m:
        result["old_price"] = int(re.sub(r"\s", "", m.group(1)))
        result["price"] = int(re.sub(r"\s", "", m.group(2)))
    else:
        # если скидки нет — может быть только одна цена
        m2 = re.search(r"(\d[\d\s]{2,7})\s*руб\.\s*₽", full_text)
        if m2:
            result["price"] = int(re.sub(r"\s", "", m2.group(1)))

    # Метка времени, которую публикует сам сайт
    m3 = re.search(r"Последнее обновление цены:\s*([\d.: ]+)", full_text)
    if m3:
        result["price_updated_on_site"] = m3.group(1).strip()

    # Наличие: "Добавить в корзину" = в наличии, "Предзаказ" (без "Добавить в корзину" рядом) = нет в наличии
    result["in_stock"] = "Добавить в корзину" in full_text

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
                site="prom23.ru",
                variant=name,
                price=data["price"],
                old_price=data["old_price"],
                in_stock=data["in_stock"],
                price_updated_on_site=data["price_updated_on_site"],
                parsed_at=now,
                url=url,
            ))
        except requests.RequestException as e:
            print(f"[ERROR] Не смог загрузить '{name}' ({url}): {e}")
    return rows


if __name__ == "__main__":
    for r in fetch_all():
        old = f" (было {r.old_price}₽)" if r.old_price else ""
        upd = f" [цена на сайте обновлена: {r.price_updated_on_site}]" if r.price_updated_on_site else ""
        stock = " [ЕСТЬ В НАЛИЧИИ]" if r.in_stock else " [ПРЕДЗАКАЗ / НЕТ В НАЛИЧИИ]"
        print(f"{r.site} | {r.variant} | {r.price}₽/лист{old}{stock}{upd}")
