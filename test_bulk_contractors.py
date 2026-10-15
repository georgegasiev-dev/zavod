import gc
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from contextlib import closing

from bulk_contractors import apply_all,build_plan,load_records
from contractor_identity import canonicalize_data,identity_key,resolve
from migrate_contractors import payment_fingerprint


def op(name,inn=None,**extra):
    result={'contractor':name,'date':'13.10.2026','amount':19.37,'desc':'unique bank reference',
            'cat':'Перевозчик','manual_cat':True,'is_debit':True,'week':2}
    if inn: result['contractor_inn']=inn
    result.update(extra)
    return result


class BulkTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.path=Path(self.tmp.name)/'data.db'

    def tearDown(self):
        gc.collect()
        self.tmp.cleanup()

    def prepare(self,ops):
        data={'ops':ops,'total_ops':len(ops),'weeks_out':[0,0,19.37*len(ops),0,0],
              'weeks_in':[0]*5,'unknown':[],'other_history':['unchanged']}
        with closing(sqlite3.connect(self.path)) as conn:
            conn.execute('CREATE TABLE month_data(month TEXT PRIMARY KEY,data TEXT,updated_at TEXT)')
            conn.execute('INSERT INTO month_data VALUES (?,?,?)',('Октябрь',json.dumps(data),'original-time'))
            conn.execute('CREATE TABLE contractor_details(contractor TEXT PRIMARY KEY,comment TEXT,added_at TEXT)')
            for i,name in enumerate(dict.fromkeys(o['contractor'] for o in ops)):
                conn.execute('INSERT INTO contractor_details VALUES (?,?,?)',(name.lower(),'note-'+str(i),'2026-01-01'))
            conn.execute('CREATE TABLE token_store(value TEXT)')
            conn.execute("INSERT INTO token_store VALUES ('fixture-only')")
            conn.commit()
        return data

    def read(self):
        with closing(sqlite3.connect(self.path)) as conn:
            return json.loads(conn.execute('SELECT data FROM month_data').fetchone()[0])

    def test_every_named_counterparty_and_backup(self):
        before=self.prepare([
            op('ИП Грачёв Павел Иванович'),op('ИП Грачев Павел Иванович, INN: 522801666681'),
            op('ИП Параев Максим Владимирович'),op('ИП Параев Максим Владимирович, ИНН: 370603578002'),
            op('ИП Сироткин Сергей Сергеевич'),op('ИП Сироткин Сергей Сергеевич, ИНН: 522805498614')])
        result=apply_all(self.path)['report']
        after=self.read()
        self.assertEqual(payment_fingerprint(before),payment_fingerprint(after))
        self.assertEqual(result['cards_before'],6)
        self.assertEqual(result['cards_after'],3)
        self.assertEqual(result['remaining_duplicate_keys'],0)
        for index in (0,2,4):
            self.assertEqual(after['ops'][index]['contractor_id'],after['ops'][index+1]['contractor_id'])
        with closing(sqlite3.connect(result['backup_path'])) as conn:
            self.assertEqual(conn.execute('PRAGMA integrity_check').fetchone()[0],'ok')
            self.assertEqual(json.loads(conn.execute('SELECT data FROM month_data').fetchone()[0]),before)
            self.assertEqual(conn.execute('SELECT value FROM token_store').fetchone()[0],'fixture-only')
        self.assertEqual(apply_all(self.path)['status'],'already_applied')
        self.assertEqual(len(list(self.path.parent.glob('backups/*.db'))),1)

    def test_conflicting_inns_never_merge(self):
        self.prepare([op('ООО ОДНО ИМЯ'),op('ООО ОДНО ИМЯ, ИНН: 7820078617'),
                      op('ООО ОДНО ИМЯ, ИНН: 2311108272')])
        result=apply_all(self.path)['report']
        after=self.read()['ops']
        self.assertEqual(result['cards_after'],3)
        self.assertTrue(result['conflicts'])
        self.assertNotIn('contractor_id',after[0])
        self.assertNotEqual(after[1]['contractor_id'],after[2]['contractor_id'])
        with closing(sqlite3.connect(self.path)) as conn:
            fresh=canonicalize_data(conn,{'ops':[op('ООО ОДНО ИМЯ')]})
        self.assertNotIn('contractor_id',fresh['ops'][0])

    def test_cosmetic_and_legal_form_variants_and_no_inn(self):
        self.prepare([op('Общество с ограниченной ответственностью «Промышленник»'),
                      op('ООО ПРОМЫШЛЕННИК, ИНН: 2311108272'),
                      op('ООО  РК  КОРСАР'),op('ООО "РК "КОРСАР", ИНН: 5246056443'),
                      op('Индивидуальный предприниматель Яшин Сергей Сергеевич, ИНН: 7'),
                      op('Индивидуальный предприниматель Яшин Сергей Сергеевич')])
        result=apply_all(self.path)['report']
        after=self.read()['ops']
        self.assertEqual(result['cards_after'],3)
        for index in (0,2,4):
            self.assertEqual(after[index]['contractor'],after[index+1]['contractor'])
        self.assertNotIn('contractor_inn',after[4])

    def test_different_names_same_inn_and_preserved_notes(self):
        self.prepare([op('ООО ПРЕЖНЕЕ ИМЯ, ИНН: 7820078617'),op('ООО НОВОЕ ИМЯ, ИНН: 7820078617')])
        apply_all(self.path)
        after=self.read()['ops']
        self.assertEqual(after[0]['contractor'],after[1]['contractor'])
        with closing(sqlite3.connect(self.path)) as conn:
            note=conn.execute('SELECT comment FROM contractor_details WHERE contractor=?',
                              (after[0]['contractor'].lower(),)).fetchone()[0]
        self.assertIn('note-0',note)
        self.assertIn('note-1',note)

    def test_prefix_is_unique_and_company_suffixes_not_fuzzy(self):
        records=[op('ООО "ПРОМЫШЛЕННИК-М"'),op('ООО "ПРОМЫШЛЕННИК-КМВ"'),
                 op('ООО "ПРОМЫШЛЕННИК"'),op('ИП Солопов Александр Владимирович'),
                 op('ИП Логинов Александр Владимирович')]
        result=build_plan(records)
        self.assertEqual(len(result['groups']),5)

    def test_import_row_order_and_separate_inns(self):
        self.prepare([op('ИП Грачёв Павел Иванович'),op('ИП Грачёв Павел Иванович, ИНН: 522801666681')])
        apply_all(self.path)
        with closing(sqlite3.connect(self.path)) as conn:
            data=canonicalize_data(conn,{'ops':[op('ИП Грачев Павел Иванович'),
                op('ООО ДВОЙНИК',inn='7820078617'),op('ООО ДВОЙНИК',inn='2311108272'),
                op('ООО ДВОЙНИК')]})
            self.assertEqual(data['ops'][0]['contractor_inn'],'522801666681')
            self.assertNotEqual(data['ops'][1]['contractor_id'],data['ops'][2]['contractor_id'])
            self.assertNotIn('contractor_id',data['ops'][3])
            reverse=canonicalize_data(conn,{'ops':[op('ООО ЕДИНСТВЕННОЕ'),
                op('ООО ЕДИНСТВЕННОЕ, ИНН: 5246056443')]})
            self.assertEqual(reverse['ops'][0]['contractor_id'],reverse['ops'][1]['contractor_id'])

    def test_failed_migration_rolls_back_and_keeps_backup(self):
        from unittest.mock import patch
        before=self.prepare([op('ООО ИМЯ'),op('ООО ИМЯ, ИНН: 7820078617')])
        with patch('bulk_contractors.canonicalize_data',side_effect=RuntimeError('injected failure')):
            with self.assertRaises(RuntimeError): apply_all(self.path)
        self.assertEqual(self.read(),before)
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertIsNone(conn.execute("SELECT 1 FROM sqlite_master WHERE name='contractor_merge_log'").fetchone())
        self.assertEqual(len(list(self.path.parent.glob('backups/*.db'))),1)


if __name__=='__main__':
    unittest.main()

