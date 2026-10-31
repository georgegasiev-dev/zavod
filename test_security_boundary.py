"""Security regression checks with synthetic credentials and no network calls."""
import ast
import asyncio
import io
import logging
import json
import types
from pathlib import Path
import unittest
from unittest.mock import patch

from security_logging import RedactingFormatter, redact


class SecurityBoundaryTests(unittest.TestCase):
    def test_log_redaction_includes_tracebacks_and_url_parameters(self):
        secret = 'synthetic-secret-not-a-real-token'
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(RedactingFormatter(logging.Formatter('%(message)s'), [secret]))
        logger = logging.Logger('isolated-security-test')
        logger.addHandler(handler)
        try:
            raise ValueError('https://example.test/callback?code=private-code&state=private-state '+secret)
        except ValueError:
            logger.exception('Credential: %s', secret)
        text = stream.getvalue()
        self.assertNotIn(secret, text)
        self.assertNotIn('private-code', text)
        self.assertNotIn('private-state', text)
        self.assertIn('ValueError', text)
        self.assertIn('[REDACTED]', text)

    def test_telegram_url_token_is_redacted_without_environment(self):
        result = redact('https://api.telegram.org/bot123456:synthetic_dummy/sendMessage', [])
        self.assertNotIn('123456:synthetic_dummy', result)

    def test_unknown_header_and_json_token_values_are_redacted(self):
        result = redact('Authorization: Basic synthetic-encoded-value {"refresh_token":"synthetic-value"}', [])
        self.assertNotIn('synthetic-encoded-value', result)
        self.assertNotIn('synthetic-value', result)

    def test_obsolete_bank_endpoints_fail_closed(self):
        source = ast.parse(Path(__file__).with_name('main.py').read_text(encoding='utf-8'))
        class HTTPError(Exception):
            def __init__(self, status_code, detail):
                self.status_code = status_code
        scope = {'HTTPException': HTTPError, 'Depends': lambda x: None, 'verify_api_access': object()}
        functions = {node.name: node for node in source.body
                     if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                     and node.name in ('raiffeisen_auth', 'raiffeisen_callback')}
        for name, node in functions.items():
            node.decorator_list = []
            exec(compile(ast.Module(body=[node], type_ignores=[]), '<bank-route-test>', 'exec'), scope)
        self.assertEqual(set(functions), {'raiffeisen_auth', 'raiffeisen_callback'})
        with self.assertRaises(HTTPError) as caught:
            scope['raiffeisen_auth']()
        self.assertEqual(caught.exception.status_code, 503)
        with self.assertRaises(HTTPError) as caught:
            asyncio.run(scope['raiffeisen_callback']())
        self.assertEqual(caught.exception.status_code, 410)

    def test_telegram_credentials_have_no_embedded_fallbacks(self):
        source = ast.parse(Path(__file__).with_name('raiffeisen_api.py').read_text(encoding='utf-8'))
        found = set()
        for node in ast.walk(source):
            if not isinstance(node, ast.Call) or ast.unparse(node.func) != 'os.getenv' or not node.args:
                continue
            if isinstance(node.args[0], ast.Constant) and node.args[0].value in ('TG_TOKEN', 'TG_CHAT_ID'):
                found.add(node.args[0].value)
                self.assertEqual(ast.literal_eval(node.args[1]), '')
        self.assertEqual(found, {'TG_TOKEN', 'TG_CHAT_ID'})

    def test_private_defaults_are_not_embedded_in_backend_code(self):
        source = ast.parse(Path(__file__).with_name('main.py').read_text(encoding='utf-8'))
        self.assertFalse(any(isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == 'DEFAULT_PLAN'
            for target in node.targets) for node in source.body))

    def test_eovr_proxy_has_fixed_target_and_validates_titles(self):
        source = ast.parse(Path(__file__).with_name('main.py').read_text(encoding='utf-8'))
        node = next(n for n in source.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'fetch_private_eovr_sheet')
        class HTTPError(Exception):
            pass
        class RouteError(Exception):
            def __init__(self, status_code, detail):
                self.status_code = status_code
        requests = []
        class Stream:
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            def raise_for_status(self): pass
            async def aiter_bytes(self): yield b'{"sheets":[]}'
        class Client:
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            def stream(self, method, url, params):
                requests.append((method,url,params))
                return Stream()
        fake = types.SimpleNamespace(AsyncClient=lambda **kwargs: Client(), HTTPError=HTTPError)
        scope = {'HTTPException':RouteError,'EOVR_API_KEY':'synthetic-key','EOVR_SHEET_ID':'configured-sheet','json':json,'log':types.SimpleNamespace(warning=lambda *args:None)}
        exec(compile(ast.Module(body=[node],type_ignores=[]),'<eovr-route-test>','exec'),scope)
        with patch.dict('sys.modules', {'httpx':fake}):
            result=asyncio.run(scope['fetch_private_eovr_sheet']("O'Brien 2026"))
            self.assertEqual(result, {'sheets':[]})
            self.assertTrue(requests[0][1].startswith('https://sheets.googleapis.com/v4/spreadsheets/configured-sheet/values/'))
            self.assertIn('%27O%27%27Brien%202026%27%21A1%3AAA110',requests[0][1])
            self.assertEqual(requests[0][2], {'key':'synthetic-key'})
            for title in ['', 'x'*101, 'bad\x00title']:
                with self.assertRaises(RouteError) as caught:
                    asyncio.run(scope['fetch_private_eovr_sheet'](title))
                self.assertEqual(caught.exception.status_code,400)
            self.assertEqual(len(requests),1)
            scope['EOVR_API_KEY']=''
            with self.assertRaises(RouteError) as caught:
                asyncio.run(scope['fetch_private_eovr_sheet']())
            self.assertEqual(caught.exception.status_code,503)
            self.assertEqual(len(requests),1)


if __name__ == '__main__':
    unittest.main()
