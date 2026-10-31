"""Security regression checks with synthetic credentials and no network calls."""
import ast
import asyncio
import io
import logging
from pathlib import Path
import unittest

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


if __name__ == '__main__':
    unittest.main()
