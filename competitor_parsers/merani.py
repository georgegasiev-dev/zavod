"""
Парсер цен merani.ru — ламинированная фанера, 18 мм, 1220x2440.

ВАЖНО про этот сайт:
- Страница-каталог с фильтром (?arrFilter_...) блокирует автообход через robots.txt.
- Но конкретные карточки товара (URL вида /katalog/fanery/laminirovannaya/18-mm-1220-2440-f-f/)
  доступны нормально — поэтому парсим точечно по списку известных SKU-страниц.
- У Merani несколько вариантов одной толщины/формата (разный сорт, разный производитель) —
  ниже список тех, что нас интересуют. Если появится новый вариант — добавь URL в SKU_PAGES.
- Кодировка страницы — windows-1251, не UTF-8. Это надо явно указать requests,
  иначе кириллица превратится в кракозябры.
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

# Известные карточки: (человекочитаемое название, URL)
SKU_PAGES = [
    ("18мм 1220x2440 сорт 1/1 F/F", "https://www.merani.ru/katalog/fanery/laminirovannaya/18-mm-1220-2440-f-f/"),
    ("18мм 1220x2440 сорт 1/2 F/F", "https://www.merani.ru/katalog/fanery/18mm/fanera-laminirovannaya-18-mm-1220-2440-sort-1-2/"),
    ("18мм 1220x2440 СВЕЗА дэк 350 сорт 1/1", "https://www.merani.ru/katalog/fanery/laminirovannaya/sveza-dek-18mm-sort-1-1-1220-2440/"),
    ("18мм 1220x2440 Китай F/F", "https://www.merani.ru/katalog/fanery/laminirovannaya-kitay/kitayskaya-18-mm-1220-2440-f-f/"),
]


@dataclass
class PriceRow:
    site: str
    variant: str
    price_per_sheet: int
    old_price_per_sheet: int | None
    discount_pct: int | None
    parsed_at: str
    url: str


def fetch_html(url: str) -> str:
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.encoding = "windows-1251"  # сайт отдаёт в этой кодировке, requests сам не всегда угадывает
    resp.raise_for_status()
    return resp.text


def parse_sku_page(html: str) -> dict:
    """
    Извлекает актуальную цену со страницы карточки товара.
    Ищем строку характеристик 'Цена за лист, руб' в таблице — она надёжнее,
    чем блок с текущей/старой ценой наверху (там встречается Vue-шаблонизация
    вида {{ item.PRICE }}, которая не всегда бывает уже отрендерена в исходном HTML).
    """
    soup = BeautifulSoup(html, "lxml")
    result = {"price": None, "old_price": None, "discount_pct": None}

    # 1) Пробуем найти цену в таблице характеристик товара
    for row in soup.find_all("tr"):
        cells = row.find_all("td")
        if len(cells) == 2 and "Цена за лист" in cells[0].get_text():
            digits = re.sub(r"[^\d]", "", cells[1].get_text())
            if digits:
                result["price"] = int(digits)

    # 2) Пробуем найти блок "2775 руб. / 2744 руб. -1%" в тексте страницы —
    #    там же обычно есть старая цена и процент скидки
    full_text = soup.get_text(" ", strip=True)
    m = re.search(r"(\d[\d\s]{2,7})\s*руб\.\s*(\d[\d\s]{2,7})\s*руб\.\s*-(\d+)%", full_text)
    if m:
        result["old_price"] = int(re.sub(r"\s", "", m.group(1)))
        result["price_from_discount_block"] = int(re.sub(r"\s", "", m.group(2)))
        result["discount_pct"] = int(m.group(3))
        if result["price"] is None:
            result["price"] = result["price_from_discount_block"]

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
                site="merani.ru",
                variant=name,
                price_per_sheet=data["price"],
                old_price_per_sheet=data.get("old_price"),
                discount_pct=data.get("discount_pct"),
                parsed_at=now,
                url=url,
            ))
        except requests.RequestException as e:
            print(f"[ERROR] Не смог загрузить '{name}' ({url}): {e}")
    return rows


if __name__ == "__main__":
    for r in fetch_all():
        discount = f" (было {r.old_price_per_sheet}₽, -{r.discount_pct}%)" if r.discount_pct else ""
        print(f"{r.site} | {r.variant} | {r.price_per_sheet}₽/лист{discount} | {r.parsed_at}")
