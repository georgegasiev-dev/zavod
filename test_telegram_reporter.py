"""Financial report regression tests with synthetic operations and no network."""
import copy
from datetime import datetime
import types
import unittest
from unittest.mock import patch

import telegram_reporter as reporter


class FrozenDateTime(datetime):
    @classmethod
    def now(cls):
        return cls(2026, 11, 3, 12)


class CarrierReportTests(unittest.TestCase):
    def render(self, builder, ops):
        database = types.ModuleType('database')
        month_numbers = {name: number for number, name in reporter.MONTH_NAMES.items()}
        database.get_month_data = lambda month: {
            'ops': [op for op in ops if int(op['date'][3:5]) == month_numbers[month]]}
        database.get_week_balance = lambda date: {'opening': 1000000, 'closing': 500000}
        with patch.dict('sys.modules', {'database': database}), \
                patch.object(reporter, 'datetime', FrozenDateTime), \
                patch.object(reporter, '_get_balance', return_value=None), \
                patch.object(reporter, '_month_income_total', return_value=700000), \
                patch.object(reporter, '_get_week_plan', return_value={}):
            if builder in ('build_morning_report', 'build_evening_report'):
                return getattr(reporter, builder)('2026-11-02')
            return getattr(reporter, builder)()

    def test_carrier_details_and_totals_in_every_financial_report(self):
        builders = ('build_evening_report', 'build_morning_report',
                    'build_weekly_summary_report', 'build_babki_report')
        for builder in builders:
            for category in ('Перевозчик', 'Перевозчики'):
                for multiple in (False, True):
                    with self.subTest(builder=builder, category=category, multiple=multiple):
                        date = '26.10.2026' if builder == 'build_weekly_summary_report' else '02.11.2026'
                        def op(cat, name, amount, debit=True):
                            return dict(date=date, cat=cat, contractor=name,
                                        amount=amount, is_debit=debit, week=0)
                        ops = [op(category, 'ООО «Транспорт-1»', 200000),
                               op(category, 'ООО «Транспорт-1»', 150000),
                               op('ГСМ', 'ООО «Топливо»', 50000),
                               op('Поступления от клиентов', 'ООО «Клиент»', 700000, False)]
                        if multiple:
                            ops.append(op(category, 'ООО «Логистика»', 220000))
                        original_ops = copy.deepcopy(ops)
                        # Reproduce the previous formatting policy to compare all
                        # existing report lines, including totals and balances.
                        old_categories = reporter.DETAIL_CATS - {'Перевозчик', 'Перевозчики'}
                        with patch.object(reporter, 'DETAIL_CATS', old_categories):
                            before = self.render(builder, ops)
                        after = self.render(builder, ops)
                        self.assertIn('    ООО «Транспорт-1» — 350\u00a0000 руб.', after)
                        self.assertEqual(after.count('    ООО «Транспорт-1» —'), 1)
                        carrier_total = '570\u00a0000' if multiple else '350\u00a0000'
                        self.assertIn(f'{category} — {carrier_total} руб.', after)
                        self.assertIn('620\u00a0000' if multiple else '400\u00a0000', after)
                        self.assertIn('700\u00a0000', after)
                        if multiple:
                            self.assertIn('    ООО «Логистика» — 220\u00a0000 руб.', after)
                            self.assertEqual(before, after)
                        else:
                            # Weekly category headings become bold when expanded.
                            def existing_lines(text):
                                return [line.replace('<b>', '').replace('</b>', '')
                                        for line in text.splitlines()
                                        if line.strip() and not line.startswith('    ')]
                            self.assertEqual(existing_lines(before), existing_lines(after))
                        self.assertEqual(ops, original_ops)

    def test_existing_supplier_details_and_weekly_progress_are_preserved(self):
        day = [dict(cat='Перевозчик', contractor='ООО «Транспорт»', amount=350000,
                    is_debit=True, week=0),
               dict(cat='Лес', contractor='ООО «Лес»', amount=100000,
                    is_debit=True, week=0)]
        earlier = dict(day[0], amount=150000)
        lines = reporter._block_expenses(day, day + [earlier], 0, {'les': 200000})
        self.assertIn('  Перевозчик — 350\u00a0000 руб. (Всего 500\u00a0000 с начала недели)', lines)
        self.assertIn('  Лес — 100\u00a0000 руб. (50% от плана недели)', lines)
        self.assertIn('    ООО «Лес» — 100\u00a0000 руб.', lines)

    def test_empty_reports(self):
        for builder in ('build_evening_report', 'build_morning_report',
                        'build_weekly_summary_report', 'build_babki_report'):
            with self.subTest(builder=builder):
                self.assertNotIn('Перевозчик', self.render(builder, []))


if __name__ == '__main__':
    unittest.main()
