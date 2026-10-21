"""Refresh stays responsive even when a source site stalls."""
import ast
import asyncio
from contextlib import closing
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import competitor_prices as prices


class RefreshTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(prices, "DB_PATH", Path(self.tmp.name) / "prices.db")
        self.db_patch.start()
        prices._last_manual_collect = None
        prices._refresh_running = False
        prices._refresh_error = None

    def tearDown(self):
        self.db_patch.stop()
        self.tmp.cleanup()

    def test_slow_collection_does_not_block_history_or_start_twice(self):
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()

        def collect():
            entered.set()
            release.wait(5)
            with closing(prices._get_conn()) as conn:
                conn.execute("INSERT INTO competitor_prices (site, variant, price_per_sheet, parsed_at) "
                             "VALUES ('example.test', '18mm', 100, '2026-10-21T08:15:00+03:00')")
                conn.commit()

        def wrapper(*args, **kwargs):
            try:
                return original(*args, **kwargs)
            finally:
                finished.set()

        original = threading.Thread.run
        with patch.object(prices, "collect_and_notify_changes", side_effect=collect) as worker, \
                patch.object(threading.Thread, "run", wrapper):
            try:
                self.assertTrue(prices.start_price_refresh())
                self.assertTrue(entered.wait(2))
                self.assertFalse(prices.start_price_refresh())
                self.assertEqual(prices.get_price_history(),
                                 {"positions": [], "refreshing": True, "refresh_error": None})
            finally:
                release.set()
                self.assertTrue(finished.wait(3))
            history = prices.get_price_history()
            self.assertFalse(history["refreshing"])
            self.assertEqual(history["positions"][0]["days"]["2026-10-21"]["price"], 100)
            self.assertFalse(prices.start_price_refresh())  # cooldown after success
            worker.assert_called_once()

    def test_background_failure_is_visible_and_can_retry(self):
        finished = threading.Event()
        original = threading.Thread.run

        def wrapper(*args, **kwargs):
            try:
                return original(*args, **kwargs)
            finally:
                finished.set()

        with patch.object(prices, "collect_and_notify_changes", side_effect=RuntimeError("offline")), \
                patch.object(threading.Thread, "run", wrapper), self.assertLogs(prices.__name__):
            self.assertTrue(prices.start_price_refresh())
            self.assertTrue(finished.wait(2))
        history = prices.get_price_history()
        self.assertFalse(history["refreshing"])
        self.assertIn("сохранённая история", history["refresh_error"])
        self.assertIsNone(prices._last_manual_collect)

    def test_daily_collection_yields_to_the_event_loop(self):
        # Load just the scheduled job, avoiding unrelated app/database startup.
        tree = ast.parse(Path(__file__).with_name("main.py").read_text(encoding="utf-8"))
        job = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef)
                   and n.name == "scheduled_competitor_prices")
        import logging
        scope = {"asyncio": asyncio, "log": logging.getLogger(__name__)}
        exec(compile(ast.Module(body=[job], type_ignores=[]), "main.py", "exec"), scope)
        entered, release = threading.Event(), threading.Event()

        def collect():
            entered.set()
            release.wait(2)

        async def check():
            task = asyncio.create_task(scope["scheduled_competitor_prices"]())
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                self.assertFalse(task.done())
            finally:
                release.set()
                await task

        with patch.object(prices, "collect_and_notify_changes", side_effect=collect):
            asyncio.run(check())


if __name__ == "__main__":
    unittest.main()
