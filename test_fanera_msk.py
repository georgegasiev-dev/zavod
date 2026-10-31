"""Catalog traversal and durable history regressions, without network calls."""
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from dataclasses import asdict
from pathlib import Path
from unittest.mock import Mock, patch

from competitor_parsers import fanera_msk as parser
import competitor_prices as prices

FIXTURES = Path(__file__).parent / 'tests/fixtures/fanera-msk'


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.page1 = (FIXTURES / 'page1.html').read_text(encoding='utf-8')
        self.page2 = (FIXTURES / 'page2.html').read_text(encoding='utf-8')

    def test_live_markup_has_12_plus_3_cards_with_sheet_minimums(self):
        first, pages = parser.parse_catalog_page(self.page1)
        second, _ = parser.parse_catalog_page(self.page2)
        self.assertEqual((len(first), len(second)), (12, 3))
        self.assertEqual(pages, [parser.CATALOG_URL + '?PAGEN_1=2'])
        rows = first + second
        self.assertEqual(len({r.product_id for r in rows}), 15)
        self.assertEqual([r.price_per_sheet for r in first],
                         [2612, 2588, 2635, 2644, 2598, 3128, 2856, 2465, 2812, 2982, 2865, 2570])
        self.assertTrue(all(r.price_is_from for r in rows))
        self.assertFalse(first[0].in_stock)  # under order, despite cart button
        self.assertTrue(first[1].in_stock)
        self.assertTrue(all('?' not in r.url for r in rows))
        self.assertEqual(second[0].price_per_sheet, 2492)

    def test_load_more_pages_are_followed_and_duplicates_are_removed(self):
        # Source can repeat a card on the next page and backlink to page one.
        row, _ = parser.parse_catalog_page(self.page1)
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(self.page2, 'lxml')
        root = soup.select_one('.catalog_block_seonik_template')
        root.append(BeautifulSoup(self.page1, 'lxml').select_one('.catalog-block__item'))
        root.append(BeautifulSoup('<div class="bottom_nav"><a href="?PAGEN_1=2">2</a></div>', 'lxml'))
        responses = [Mock(text=self.page1), Mock(text=str(soup))]
        session = Mock()
        session.get.side_effect = responses
        with patch.object(parser.requests, 'Session') as factory:
            factory.return_value.__enter__.return_value = session
            rows = parser.fetch_all()
        self.assertEqual(len(rows), 15)
        self.assertEqual(session.get.call_count, 2)
        self.assertEqual(len({r.parsed_at for r in rows}), 1)
        self.assertEqual(rows[0].url, row[0].url)

    def test_recommendations_old_prices_and_unknown_prices_are_not_used(self):
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(self.page1, 'lxml')
        card = soup.select_one('.catalog-block__item')
        card.select_one('.price__new-val').string = 'Цена по запросу'
        card.append(BeautifulSoup('<span class="price__old-val">9999</span>', 'lxml'))
        soup.append(BeautifulSoup('<div class="catalog_block_template"><div class="catalog-block__item">'
                                  '<span class="price__new-val">123</span></div></div>', 'lxml'))
        rows, _ = parser.parse_catalog_page(str(soup))
        self.assertEqual(len(rows), 12)
        self.assertIsNone(rows[0].price_per_sheet)

    def test_failed_second_page_discards_partial_catalog(self):
        session = Mock()
        session.get.side_effect = [Mock(text=self.page1), parser.requests.Timeout('offline')]
        with patch.object(parser.requests, 'Session') as factory:
            factory.return_value.__enter__.return_value = session
            with self.assertRaises(parser.requests.Timeout):
                parser.fetch_all()

    def test_wrong_filter_or_missing_catalog_fails_explicitly(self):
        for html in ('<html>Access denied</html>', self.page1.replace('18 мм', '21 мм')):
            with self.assertRaises(ValueError):
                parser.parse_catalog_page(html)

    def test_product_links_cannot_escape_source(self):
        with self.assertRaises(ValueError):
            parser.canonical_url('https://example.com/catalog/other/')
        self.assertEqual(parser.canonical_url('/catalog/item/?oid=99&utm_source=test#x'),
                         'https://fanera-msk.ru/catalog/item/')


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'prices.sqlite3'
        self.db = patch.object(prices, 'DB_PATH', self.path)
        self.db.start()
        self.rows = parser.parse_catalog_page((FIXTURES / 'page1.html').read_text(encoding='utf-8'))[0]
        self.row = asdict(self.rows[0])
        self.row['parsed_at'] = '2026-10-22T08:15:00+03:00'

    def tearDown(self):
        self.db.stop()
        self.tmp.cleanup()

    def save(self, rows):
        conn = prices._get_conn()
        try:
            return prices._save_rows(conn, rows)
        finally:
            conn.close()

    def test_legacy_db_migration_and_restart_keep_history_and_links(self):
        conn = sqlite3.connect(self.path)
        conn.executescript(prices.SCHEMA)
        conn.execute("INSERT INTO competitor_prices(site,variant,price_per_sheet,parsed_at) "
                     "VALUES ('legacy.test','Original',100,'2026-10-21T08:15:00+03:00')")
        conn.commit()
        conn.close()
        self.save([self.row])
        positions = prices.get_price_history()['positions']
        self.assertEqual(len(positions), 2)
        current = next(r for r in positions if r['site'] == parser.SITE)
        self.assertEqual(current['url'], self.row['url'])
        self.assertTrue(current['days']['2026-10-22']['price_is_from'])
        self.assertEqual(next(r for r in positions if r['site'] == 'legacy.test')['days']['2026-10-21']['price'], 100)

    def test_same_snapshot_duplicate_and_renamed_card_keep_one_position(self):
        self.save([self.row, self.row])
        self.save([self.row])
        changed = {**self.row, 'variant': 'Новое название', 'price_per_sheet': 2700,
                   'parsed_at': '2026-10-23T08:15:00+03:00'}
        self.save([changed])
        positions = prices.get_price_history()['positions']
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0]['variant'], 'Новое название')
        self.assertEqual(len(positions[0]['days']), 2)
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM competitor_prices').fetchone()[0], 2)

    def test_price_change_is_compared_by_product_id_after_rename(self):
        self.save([self.row])
        changed = {**self.row, 'variant': 'Новое название', 'price_per_sheet': 2700,
                   'parsed_at': '2026-10-23T08:15:00+03:00'}
        with patch.object(prices, '_collect_rows', return_value=[changed]):
            report = prices.collect_and_report_changes()
        self.assertIn('2612 ₽ → 2700 ₽', report)
        with patch.object(prices, '_collect_rows', return_value=[changed]):
            self.assertIsNone(prices.collect_and_report_changes())

    def test_missing_price_is_unknown_and_does_not_destroy_previous_day(self):
        self.save([self.row])
        self.save([{**self.row, 'price_per_sheet': None, 'parsed_at': '2026-10-23T08:15:00+03:00'}])
        position = prices.get_price_history()['positions'][0]
        self.assertEqual(position['days']['2026-10-22']['price'], 2612)
        self.assertIsNone(position['days']['2026-10-23']['price'])

    def test_source_only_collection_saves_all_cards_without_notification(self):
        with patch.object(prices.fanera_msk, 'fetch_all', return_value=self.rows), \
                patch.object(prices, 'collect_and_notify_changes') as notify:
            self.assertEqual(prices.collect_fanera_msk(), 12)
            notify.assert_not_called()
        self.assertEqual(len(prices.get_price_history()['positions']), 12)

    def test_failure_does_not_write_a_partial_snapshot(self):
        self.save([self.row])
        with patch.object(prices.fanera_msk, 'fetch_all', side_effect=parser.requests.Timeout):
            with self.assertRaises(parser.requests.Timeout):
                prices.collect_fanera_msk()
        self.assertEqual(len(prices.get_price_history()['positions']), 1)

    def test_failed_source_keeps_other_sources_and_reports_staleness(self):
        self.save([self.row])
        legacy = Mock(site='merani.ru', variant='Existing', price_per_sheet=1234)
        with patch.object(prices.merani, 'fetch_all', return_value=[legacy]), \
                patch.object(prices.prom23, 'fetch_all', return_value=[]), \
                patch.object(prices.deltakom, 'fetch_all', return_value=[]), \
                patch.object(prices.tdkorsar, 'fetch_all', return_value=[]), \
                patch.object(prices.psk_holding, 'fetch_all', return_value=[]), \
                patch.object(prices.sgmonolit, 'fetch_all', return_value=[]), \
                patch.object(prices.fanera_msk, 'fetch_all', side_effect=parser.requests.Timeout), \
                self.assertLogs(prices.__name__):
            try:
                self.assertEqual(prices.collect_and_save(), 1)
                history = prices.get_price_history()
                self.assertEqual(len(history['positions']), 2)
                self.assertIn('fanera-msk.ru', history['refresh_error'])
            finally:
                prices._source_errors.clear()

    def test_source_only_background_refresh_does_not_notify(self):
        finished = threading.Event()
        original = threading.Thread.run
        def run(*args, **kwargs):
            try:
                return original(*args, **kwargs)
            finally:
                finished.set()
        prices._last_manual_collect = None
        with patch.object(prices.fanera_msk, 'fetch_all', return_value=self.rows), \
                patch.object(prices, 'collect_and_notify_changes') as notify, \
                patch.object(threading.Thread, 'run', run):
            try:
                self.assertTrue(prices.start_price_refresh(source=parser.SITE))
                self.assertTrue(finished.wait(3))
                notify.assert_not_called()
                self.assertEqual(len(prices.get_price_history()['positions']), 12)
            finally:
                prices._last_manual_collect = None


if __name__ == '__main__':
    unittest.main()
