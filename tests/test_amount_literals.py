import math
import unittest

from app.agent.amount_literals import parse_amount_literal


class AmountLiteralTests(unittest.TestCase):
    def test_common_financial_omissions_supply_the_next_lower_place(self):
        cases = {
            '一百二': 120, '两百五': 250, '九百九': 990,
            '一千二': 1200, '一千二百三': 1230,
            '一万二': 12000, '两万二': 22000, '十万二': 102000,
            '一万二千三': 12300, '两万二千三百四': 22340,
            '一万零二百三': 10230, '一亿二': 120000000,
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(parse_amount_literal(raw), expected)

    def test_an_explicit_zero_prevents_the_omitted_place_interpretation(self):
        cases = {
            '一百零二': 102, '一千零二': 1002, '一千零二十': 1020,
            '一千二百零三': 1203, '一万零二': 10002,
            '两万零二': 20002, '一万二千零三': 12003,
            '一万零二百零三': 10203, '一亿零二': 100000002,
            '一亿零二万零三': 100020003,
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(parse_amount_literal(raw), expected)

    def test_complete_unit_forms_keep_their_literal_value(self):
        cases = {
            '零': 0, '一': 1, '两': 2, '十': 10, '十二': 12, '二十': 20,
            '三百': 300, '一千二百': 1200, '两千五百': 2500,
            '一万': 10000, '一万二百': 10200, '一万二千': 12000,
            '两万二千': 22000, '十二万': 120000,
            '二十三万四千五百六十七': 234567,
            '一亿二千万': 120000000,
            '十二亿三千四百五十六万七千八百九十': 1234567890,
            '一万亿': 1000000000000,
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(parse_amount_literal(raw), expected)

    def test_arabic_decimal_and_ten_thousand_forms_remain_supported(self):
        cases = {
            '0': 0, '300': 300, '0012': 12, '12000': 12000,
            '700.5': 700.5, '0.25': .25, '1万': 10000,
            '1.2万': 12000, '2.2万': 22000, '0.01万': 100,
            '1.2345万': 12345, '0.00001万': .1,
            '1,234': 1234, '12,345.67': 12345.67,
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(parse_amount_literal(raw), expected)

    def test_isolated_currency_suffixes_and_outer_whitespace_do_not_change_the_amount(self):
        for raw, expected in [(' 一万二 ', 12000), ('1200元', 1200),
                              ('两千五百元', 2500), ('一万二块', 12000),
                              ('两万二块钱', 22000), (' 1.2万元 ', 12000)]:
            with self.subTest(raw=raw):
                self.assertEqual(parse_amount_literal(raw), expected)

    def test_unknown_approximate_and_multiple_amounts_are_not_guessed(self):
        for raw in ['', '不知道', '几千', '数万', '大概一万', '一万左右', '一两万',
                    '三五百', '一万到两万', '一万二还是一万三', '1-2万', '1～2万',
                    '一半', '存到两万二', '一千二百多', '买一台', '壹万元',
                    '-100', '+100', '1e3', 'NaN', 'Infinity']:
            with self.subTest(raw=raw):
                self.assertIsNone(parse_amount_literal(raw))

    def test_malformed_digit_and_unit_sequences_are_rejected_as_a_whole(self):
        for raw in ['一二', '一二三', '一二万', '一万二三', '二百三四',
                    '一百百', '一千百', '十百', '二十百', '一千一千',
                    '一万万', '一亿亿', '一万二万', '万', '亿', '百', '千',
                    '零一', '零零', '一零', '一百零', '一百零零二', '一万零零二',
                    '1,23', '12,34,567', '1.2.3', '1万2', '一万2', '1千二',
                    '一 万 二', '一万二元整外', '元']:
            with self.subTest(raw=raw):
                self.assertIsNone(parse_amount_literal(raw))

    def test_non_literals_and_nonfinite_sizes_remain_unknown(self):
        for raw in [None, 0, 12.5, False, [], {}, '9' * 400, '9' * 400 + '万']:
            with self.subTest(raw=raw):
                self.assertIsNone(parse_amount_literal(raw))
        for raw in ['一万二', '1.2345万', '0.00001万']:
            self.assertTrue(math.isfinite(parse_amount_literal(raw)))
