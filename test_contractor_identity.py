import copy
import gc
import importlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

from contractor_identity import (canonicalize_data, ensure_schema, extract_inn,
                                 resolve, valid_inn)
from migrate_contractors import apply_reviewed, payment_fingerprint

AYRA = 'ИП Айрапетян Арсен Камоевич'
STAR = 'ООО "Стандарт-Старвуд"'


def op(name, amount=10.25, **extra):
    return dict(date='13.10.2026', contractor=name, amount=amount, desc='bank reference',
                is_debit=True, cat='Лес', week=2, **extra)


class IdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'novator.db'
        self.data = {'ops': [op(AYRA, manual_cat=True),
                             op(AYRA + ', ИНН: 522802685180', cat_marker='keep'),
                             op('ООО  "Стандарт-Старвуд"'),
                             op(STAR + ', ИНН: 7820078617'),
                             op('ООО "Стандарт-Старуд"')],
                     'unknown': [{'contractor': AYRA, 'amount': 12}],
                     'weeks_out': [0, 0, 51.25, 0, 0], 'custom': {'history': [1, 2]}}
        with sqlite3.connect(self.path) as conn:
            conn.execute('CREATE TABLE month_data(month TEXT PRIMARY KEY, data TEXT, updated_at TEXT)')
            conn.execute('INSERT INTO month_data VALUES (?, ?, ?)', ('Октябрь', json.dumps(self.data), 'unchanged'))
            conn.execute('CREATE TABLE contractor_details(contractor TEXT PRIMARY KEY, comment TEXT, added_at TEXT)')
            conn.executemany('INSERT INTO contractor_details VALUES (?, ?, ?)', [
                (AYRA.lower(), 'first note', '2026-01-01'),
                ((AYRA + ', ИНН: 522802685180').lower(), 'second note', '2026-02-01')])
            conn.execute('CREATE TABLE contractor_map(contractor TEXT PRIMARY KEY, cat TEXT, updated_at TEXT)')
            conn.execute('INSERT INTO contractor_map VALUES (?, ?, ?)', (AYRA.lower(), 'Лес', 'old'))
            conn.execute('CREATE TABLE bank_token(secret TEXT)')
            conn.execute('INSERT INTO bank_token VALUES (?)', ('fixture-token',))

    def tearDown(self):
        gc.collect()
        self.tmp.cleanup()

    def test_validators(self):
        self.assertTrue(valid_inn('522802685180'))
        self.assertTrue(valid_inn('7820078617'))
        for value in ('0000000000', '522802685181', '', '123', '５２２８０２６８５１８０'):
            self.assertFalse(valid_inn(value))
        self.assertIsNone(extract_inn('invoice 522802685180'))
        self.assertIsNone(extract_inn('ИНН: 522802685180 ИНН: 7820078617'))

    def test_backup_migration_invariants_metadata_and_idempotence(self):
        result = apply_reviewed(self.path)
        self.assertEqual(result['status'], 'applied')
        report = result['report']
        self.assertEqual(report['operations_updated'], 4)
        with sqlite3.connect(report['backup_path']) as conn:
            self.assertEqual(conn.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            self.assertEqual(json.loads(conn.execute('SELECT data FROM month_data').fetchone()[0]), self.data)
            self.assertEqual(conn.execute('SELECT secret FROM bank_token').fetchone()[0], 'fixture-token')
            self.assertIsNone(conn.execute("SELECT 1 FROM sqlite_master WHERE name='contractor_entities'").fetchone())
        with sqlite3.connect(self.path) as conn:
            data, updated = conn.execute('SELECT data, updated_at FROM month_data').fetchone()
            data = json.loads(data)
            self.assertEqual(updated, 'unchanged')
            self.assertEqual(payment_fingerprint(data), payment_fingerprint(self.data))
            self.assertEqual(data['ops'][0]['contractor_id'], data['ops'][1]['contractor_id'])
            self.assertEqual(data['ops'][2]['contractor_id'], data['ops'][3]['contractor_id'])
            self.assertEqual(data['ops'][4], self.data['ops'][4])
            self.assertEqual(data['ops'][0]['contractor_original'], AYRA)
            comment = conn.execute('SELECT comment FROM contractor_details WHERE contractor=?',
                                   (data['ops'][0]['contractor'].lower(),)).fetchone()[0]
            self.assertIn('first note', comment)
            self.assertIn('second note', comment)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM contractor_merge_log').fetchone()[0], 1)
        self.assertEqual(apply_reviewed(self.path)['status'], 'already_applied')
        self.assertEqual(len(list(self.path.parent.glob('backups/*.db'))), 1)

    def test_same_name_different_inn_aborts_without_changes(self):
        bad = copy.deepcopy(self.data)
        bad['ops'].append(op(AYRA + ', ИНН: 7820078617'))
        with sqlite3.connect(self.path) as conn:
            conn.execute('UPDATE month_data SET data=?', (json.dumps(bad),))
        with self.assertRaises(ValueError):
            apply_reviewed(self.path)
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(json.loads(conn.execute('SELECT data FROM month_data').fetchone()[0]), bad)

    def test_transaction_rolls_back_after_backup_on_alias_conflict(self):
        with sqlite3.connect(self.path) as conn:
            ensure_schema(conn)
            conn.execute('INSERT INTO contractor_entities VALUES (?, ?, ?)', ('inn:7820078617', '7820078617', 'other'))
            conn.execute('INSERT INTO contractor_aliases VALUES (?, ?, ?)', (AYRA.lower(), 'inn:7820078617', 'fixture'))
        with self.assertRaises(ValueError):
            apply_reviewed(self.path)
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(json.loads(conn.execute('SELECT data FROM month_data').fetchone()[0]), self.data)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM contractor_merge_log').fetchone()[0], 0)
        self.assertEqual(len(list(self.path.parent.glob('backups/*.db'))), 1)

    def test_import_same_inn_and_ambiguous_names(self):
        apply_reviewed(self.path)
        with sqlite3.connect(self.path) as conn:
            data = canonicalize_data(conn, {'ops': [op(AYRA), op('renamed, ИНН: 522802685180'),
                                                    op('another company'), op('another company, ИНН: 7820078617')]})
            self.assertEqual(data['ops'][0]['contractor_id'], data['ops'][1]['contractor_id'])
            self.assertNotIn('contractor_id', data['ops'][2])
            self.assertNotEqual(data['ops'][1]['contractor_id'], data['ops'][3]['contractor_id'])
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM contractor_entities WHERE inn='522802685180'").fetchone()[0], 1)
            with self.assertRaises(ValueError):
                resolve(conn, AYRA, '7820078617', True)

    def test_identical_names_with_separate_inns_stay_distinct(self):
        with sqlite3.connect(self.path) as conn:
            data = canonicalize_data(conn, {'ops': [
                op('same legal name', contractor_inn='522802685180'),
                op('same legal name', contractor_inn='7820078617'),
                op('same legal name')]})
            self.assertNotEqual(data['ops'][0]['contractor'], data['ops'][1]['contractor'])
            self.assertNotEqual(data['ops'][0]['contractor_id'], data['ops'][1]['contractor_id'])
            self.assertNotIn('contractor_id', data['ops'][2])

    def test_database_import_preserves_manual_categories(self):
        apply_reviewed(self.path)
        previous = os.environ.get('DB_PATH')
        os.environ['DB_PATH'] = str(self.path)
        try:
            import database
            database = importlib.reload(database)
            before = database.get_month_data('Октябрь')
            incoming = copy.deepcopy(before)
            incoming['ops'][0]['contractor'] = AYRA
            incoming['ops'][0]['cat'] = 'Вывоз мусора'
            incoming['ops'][0].pop('manual_cat')
            database.merge_month_data('Октябрь', incoming)
            after = database.get_month_data('Октябрь')
            self.assertEqual(after['ops'][0]['cat'], 'Лес')
            self.assertTrue(after['ops'][0]['manual_cat'])
            self.assertEqual(len(after['ops']), len(before['ops']))
            self.assertEqual(sum(o['amount'] for o in after['ops']), sum(o['amount'] for o in before['ops']))
            database.save_contractor_comment(AYRA, 'edited through old name')
            canonical = after['ops'][0]['contractor'].lower()
            self.assertEqual(database.get_contractor_details()[canonical]['comment'], 'edited through old name')
            long_name = 'ООО "Очень длинное название организации для проверки прежнего обрезанного имени", ИНН: 7820078617'
            legacy_op = op(long_name[:60], manual_cat=True)
            database.save_month_data('legacy-only', {'ops': [legacy_op]})
            fresh_op = op(long_name)
            fresh_op['cat'] = 'Вывоз мусора'
            database.merge_month_data('legacy-only', {'ops': [fresh_op]})
            restored = database.get_month_data('legacy-only')['ops']
            self.assertEqual(len(restored), 1)
            self.assertEqual(restored[0]['cat'], 'Лес')
            self.assertTrue(restored[0]['manual_cat'])
        finally:
            if previous is None:
                os.environ.pop('DB_PATH', None)
            else:
                os.environ['DB_PATH'] = previous


if __name__ == '__main__':
    unittest.main()
