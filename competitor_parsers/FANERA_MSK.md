# Fanera-MSK catalog monitoring

Source: https://fanera-msk.ru/catalog/laminirovannaya-fanera/18-1220kh2440/

The existing server scheduler collects this source daily at 08:15 Europe/Moscow,
alongside the other sources. The market page's refresh button also collects it.
No separate desktop automation or independent SQLite database is needed.

Every run discovers the main catalog's cards and follows its Bitrix PAGEN links.
These are the same pages used by the “Загрузить еще” button. On 22 October 2026
the catalog contained 15 cards, 12 + 3. The recommendations carousel is excluded.
New catalog cards are discovered automatically. Prices are the published minimum
(`от`) in rubles per sheet (`шт`); package offer IDs and alternative units are not
separate cards. Missing prices stay unknown, never zero.

Product IDs, names, canonical product links, availability and minimum-price flags
are persisted in the existing `competitor_prices.sqlite3`. The migration only
adds columns and an index, preserving older sources and observations. Identity is
site + product ID, so title changes and repeated cards do not split the history.
The same observation timestamp cannot be inserted twice for the same product.
Every collection is stored; the page displays the latest observation per day,
with changes relative to previous known daily prices. Earlier dates are not
backfilled. Historical rows remain even if a card later disappears from the source.

A failed page discards the entire source snapshot, retaining its old history and
reporting the error on the market page. Other sources can still be saved.

For source-only refresh/initial import, without Telegram notifications:
`POST /api/competitor-prices/refresh?source=fanera-msk.ru`.
Poll `GET /api/competitor-prices` until `refreshing` is false, check `refresh_error`,
and compare unique source rows and prices with the catalog. The normal refresh
retains the application's existing change-notification behavior.

Tests: `python -m unittest test_fanera_msk test_competitor_refresh test_psk_holding -v`.
Fixtures are reduced copies of the observed main catalog HTML, without images,
scripts, tracking or recommendations. The deployment workflow runs these tests
before restarting the service.
