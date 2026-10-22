"""Discover Fanera-MSK catalog cards, including Bitrix's load-more pages.

The load-more button uses the same PAGEN_1 links as ordinary pagination.
Read only the main catalog template; recommendations contain unrelated sizes.
The displayed price is a minimum ("от"), per piece/sheet, not m² or m³.
"""
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

SITE = "fanera-msk.ru"
CATALOG_URL = "https://fanera-msk.ru/catalog/laminirovannaya-fanera/18-1220kh2440/"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; NovatorPriceMonitor/1.0)"}
MAX_PAGES = 100
log = logging.getLogger(__name__)


@dataclass
class PriceRow:
    site: str
    product_id: str
    variant: str
    price_per_sheet: int | None
    in_stock: bool | None
    parsed_at: str
    url: str
    price_is_from: bool


def canonical_url(value: str) -> str:
    parts = urlsplit(urljoin(CATALOG_URL, value))
    if parts.scheme not in ("http", "https") or parts.hostname not in (SITE, "www." + SITE):
        raise ValueError("Unexpected catalog host")
    if not parts.path.startswith("/catalog/"):
        raise ValueError("Unexpected product path")
    # oid selects a packaging offer, not a separate catalog card.
    return urlunsplit(("https", SITE, parts.path.rstrip("/") + "/", "", ""))


def _price(value: str) -> int | None:
    match = re.fullmatch(r"\s*(?:от\s*)?(\d[\d\s\xa0\u202f]*)(?:[.,]00)?\s*(?:₽|руб\.?)?\s*", value, re.I)
    if not match:
        return None
    price = int(re.sub(r"\s", "", match[1]))
    return price if price > 0 else None


def parse_catalog_page(html: str, page_url: str = CATALOG_URL) -> tuple[list[PriceRow], list[str]]:
    soup = BeautifulSoup(html, "lxml")
    root = soup.select_one(".catalog_block_seonik_template")
    if root is None:
        raise ValueError("Main Fanera-MSK catalog is missing")
    cards = root.select(".catalog-block__item")
    if not cards:
        raise ValueError("Fanera-MSK returned no catalog cards")
    rows = []
    for card in cards:
        title = card.select_one(".catalog-block__info-title a")
        identifier = re.search(r"_(\d+)$", card.get("id", ""))
        if title is None or not title.get("href") or identifier is None:
            raise ValueError("Incomplete Fanera-MSK product identity")
        name = title.get_text(" ", strip=True)
        # A changed/cleared SEO filter must not import the entire catalog.
        if not re.search(r"\b18\s*мм\b", name, re.I) or not re.search(r"1220\s*[хx×]\s*2440", name, re.I):
            raise ValueError(f"Unexpected size in Fanera-MSK catalog: {name}")
        price_tag = card.select_one(".price__new-val")
        price_text = price_tag.get_text(" ", strip=True) if price_tag else ""
        price = _price(price_text)
        # Unknown is deliberately not zero and never taken from an old price,
        # delivery fee, recommendations, or another unit's offer.
        stock_tag = card.select_one(".status-icon")
        stock_text = stock_tag.get_text(" ", strip=True).lower() if stock_tag else ""
        stock = False if any(s in stock_text for s in ("под заказ", "нет в наличии")) else (True if "в наличии" in stock_text else None)
        rows.append(PriceRow(SITE, identifier[1], name, price, stock, "",
                             canonical_url(title["href"]), bool(re.search(r"\bот\b", price_text, re.I))))

    pages = set()
    for link in root.select(".bottom_nav a[href]"):
        parts = urlsplit(urljoin(page_url, link["href"]))
        if canonical_url(urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))) != CATALOG_URL:
            continue
        for key, value in parse_qsl(parts.query):
            if re.fullmatch(r"PAGEN_\d+", key) and value.isdigit() and int(value) > 0:
                pages.add(CATALOG_URL + "?" + urlencode({key: int(value)}))
    return rows, sorted(pages)


def fetch_all() -> list[PriceRow]:
    pending, visited, by_id, by_url = [CATALOG_URL], set(), {}, {}
    with requests.Session() as session:
        session.headers.update(HEADERS)
        while pending:
            url = pending.pop(0)
            if url in visited:
                continue
            if len(visited) >= MAX_PAGES:
                raise ValueError("Fanera-MSK pagination limit exceeded; snapshot discarded")
            response = session.get(url, timeout=(5, 25))
            response.raise_for_status()
            response.encoding = "utf-8"
            rows, pages = parse_catalog_page(response.text, url)
            visited.add(url)
            for row in rows:
                other = by_url.get(row.url)
                if other is not None and other != row.product_id:
                    raise ValueError("Conflicting Fanera-MSK product identities")
                by_url[row.url] = row.product_id
                by_id[row.product_id] = row
            pending.extend(p for p in pages if p not in visited and p not in pending)
    now = datetime.now(timezone.utc).isoformat()
    for row in by_id.values():
        row.parsed_at = now
    log.info("Fanera-MSK: %s pages, %s unique products", len(visited), len(by_id))
    return list(by_id.values())
