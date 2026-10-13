import json
import unittest
from types import SimpleNamespace

from login_audit import collect_login_attempt, format_login_alert, record_and_alert


def fake_request(headers=None):
    return SimpleNamespace(
        headers={k.lower(): v for k, v in (headers or {}).items()},
        client=SimpleNamespace(host="127.0.0.1"),
        method="GET",
        url=SimpleNamespace(path="/api/raiffeisen/status"),
    )


class LoginAuditTests(unittest.TestCase):
    def test_success_and_failure_both_notify_without_deduplication(self):
        rows, messages = [], []
        req = fake_request({"X-Forwarded-For": "203.0.113.7, 192.0.2.1",
                            "User-Agent": "ExampleBrowser / ExampleOS"})
        for good in (False, False, True):
            event = record_and_alert(req, "admin", good,
                                     lambda *values: rows.append(values),
                                     messages.append)
            self.assertEqual(event["ip"], "203.0.113.7")
        self.assertEqual(len(rows), 3)
        self.assertEqual(len(messages), 3)
        self.assertEqual([x[0] for x in rows], ["login_failed", "login_failed", "login_ok"])
        self.assertIn("Неудачная", messages[0])
        self.assertIn("Успешный", messages[2])
        self.assertIn("ExampleBrowser", messages[2])

    def test_does_not_collect_password_or_sensitive_headers(self):
        password = "super-secret-never-include"
        token = "token-never-include"
        req = fake_request({
            "Authorization": f"Basic {password}",
            "Cookie": f"auth={token}",
            "X-Forwarded-For": "198.51.100.99",
            "Referer": f"https://example.org/login?session={token}",
            "User-Agent": "Browser",
            "Accept-Language": "ru-RU,ru",
        })
        event = collect_login_attempt(req, "admin", False)
        message = format_login_alert(event)
        all_data = json.dumps(event, ensure_ascii=False) + message
        self.assertNotIn(password, all_data)
        self.assertNotIn(token, all_data)
        self.assertIn("198.51.100.99", message)
        self.assertIn("https://example.org/login", message)

    def test_escapes_untrusted_strings_for_telegram_html(self):
        req = fake_request({"User-Agent": "<b>spoofed</b>",
                            "X-Forwarded-For": "<script>alert(1)</script>"})
        event = collect_login_attempt(req, "<admin>", False)
        message = format_login_alert(event)
        self.assertNotIn("<script>", message)
        self.assertIn("&lt;script&gt;", message)
        self.assertIn("&lt;admin&gt;", message)

    def test_database_failure_does_not_block_alert(self):
        sent = []
        def broken_log(*args):
            raise RuntimeError("DB down")
        record_and_alert(fake_request(), "admin", False, broken_log, sent.append)
        self.assertEqual(len(sent), 1)


if __name__ == "__main__":
    unittest.main()
