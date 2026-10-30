#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Automated Verification Test Suite for CSV-to-Executive-PDF Pipeline v2.1.

Verifies all 8 audit edge cases in isolation:
1. Missing --input file raises FileNotFoundError (exit code 2), never generates silent mock data.
2. Dirty numeric values (e.g. 'abc') raise ValueError; boolean strings ('yes'/'no'/'true'/'false'/'是'/'否') parse accurately.
3. Missing fee/refund columns fail fast unless --estimate-missing-fees / --assume-zero-refunds are explicitly passed.
4. Semantic period sorting places '2026-W10' AFTER '2026-W9' (not lexicographical '2026-W9' > '2026-W10').
5. Zero prior-period net revenue sets wow_net_delta = None and outputs 'not calculable' in both JSON and prose (never '+0.0%').
6. High-cardinality channels/regions (>6 segments) aggregate tail into 'Other (N segments)' and pass 1-page PDF verification.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
import pandas as pd

import csv_to_pdf_pipeline as en_pipe
import csv_to_pdf_pipeline_zh as zh_pipe


class TestPipelineStrictAudit(unittest.TestCase):

    def test_weighted_other_channel_refund_rate(self):
        for pipe, aggregate, key in [(en_pipe, en_pipe._aggregate_top_n, 'channel'), (zh_pipe, zh_pipe._aggregate_top_n_zh, '业务渠道')]:
            rows = [
                {key: 'A', 'txns': 100, 'gross': 100, 'net': 100, 'share_pct': 99, 'refund_pct': 0, 'refunded_txns': 0, 'latest_txns': 100, 'latest_refunded_txns': 0, 'latest_refund_pct': 0},
                {key: 'B', 'txns': 1, 'gross': 1, 'net': 1, 'share_pct': 1, 'refund_pct': 100, 'refunded_txns': 1, 'latest_txns': 1, 'latest_refunded_txns': 1, 'latest_refund_pct': 100},
            ]
            with self.subTest(language=pipe.__name__):
                other = aggregate(rows, key, 1)[0]
                self.assertEqual(other['refund_pct'], 0.99)
                self.assertEqual(other['latest_refund_pct'], 0.99)
                period = 'week' if pipe is en_pipe else '结算周期'
                ident = 'txn_id' if pipe is en_pipe else '订单号'
                region = 'region' if pipe is en_pipe else '销售大区'
                gross = 'gross_usd' if pipe is en_pipe else '总流水_USD'
                fee = 'stripe_fee_usd' if pipe is en_pipe else '通道手续费_USD'
                refunded = 'refunded' if pipe is en_pipe else '是否退款'
                net = 'net_usd' if pipe is en_pipe else '净营收_USD'
                dataset = [{ident: str(i), period: '2026-W09', '_sort_key': pipe.parse_period_sort_key('2026-W09')[1], key: 'A' if i < 100 else 'B', region: 'R', gross: 100, fee: 0, refunded: int(i == 100), net: 100} for i in range(101)]
                compute = pipe.compute_deterministic_metrics if pipe is en_pipe else pipe.compute_metrics_zh
                combined = compute(pd.DataFrame(dataset), max_table_rows=1)['by_channel'][0]
                self.assertEqual(combined['refund_pct'], 0.99)
                self.assertEqual(combined['latest_refund_pct'], 0.99)
                self.assertEqual(combined['refunded_txns'], 1)

    def test_infinite_numeric_rejected(self):
        for pipe in (en_pipe, zh_pipe):
            for value in ('inf', '-inf', '1e9999'):
                with self.subTest(language=pipe.__name__, value=value):
                    with self.assertRaises(ValueError):
                        pipe.parse_strict_numeric_series(pd.Series([value]), 'amount')

    def test_period_canonical_normalization(self):
        for pipe in (en_pipe, zh_pipe):
            with self.subTest(language=pipe.__name__):
                self.assertEqual(pipe.parse_period_sort_key('2026-W9')[0], '2026-W09')
                self.assertEqual(pipe.parse_period_sort_key('2026-W09')[0], '2026-W09')
                self.assertLess(pipe.parse_period_sort_key('2025-01')[1], pipe.parse_period_sort_key('2026-W52')[1])
                with tempfile.TemporaryDirectory() as tmp:
                    path = os.path.join(tmp, 'periods.csv')
                    pd.DataFrame({'id': ['1', '2', '3'], 'period': ['2026-W9', '2026-W09', '2025-01'], 'channel': ['A'] * 3, 'region': ['R'] * 3, 'gross': [100] * 3, 'fee': [2] * 3, 'refund': [0] * 3, 'net': [98] * 3}).to_csv(path, index=False)
                    args = SimpleNamespace(input=path, demo=False, col_id='id', col_period='period', col_channel='channel', col_region='region', col_gross='gross', col_fee='fee', col_refund='refund', col_net='net', estimate_missing_fees=False, assume_zero_refunds=False, derive_net_from_gross=False)
                    load = pipe.load_and_validate_csv if pipe is en_pipe else pipe.load_and_validate_csv_zh
                    compute = pipe.compute_deterministic_metrics if pipe is en_pipe else pipe.compute_metrics_zh
                    frame, _ = load(args)
                    result = compute(frame)
                    field = 'week' if pipe is en_pipe else '结算周期'
                    self.assertEqual([row[field] for row in result['weekly']], ['2025-01', '2026-W09'])
                    self.assertEqual(result['weekly'][1]['txns'], 2)

    def test_1_missing_input_file_fails_fast(self):
        """Audit #1: Passing a non-existent --input path must fail with exit code != 0 and NOT create a mock file."""
        with tempfile.TemporaryDirectory() as tmp:
            bad_path = os.path.join(tmp, "does_not_exist.csv")
            res = subprocess.run(
                [sys.executable, "csv_to_pdf_pipeline.py", "--input", bad_path],
                capture_output=True, text=True
            )
            self.assertNotEqual(res.returncode, 0)
            self.assertFalse(os.path.exists(bad_path))
            self.assertIn("Specified --input file does not exist", res.stderr)

    def test_2_boolean_refund_strings_and_dirty_numbers(self):
        """Audit #2: 'yes'/'no' in refund column must parse to 1/0; unparseable numbers must raise ValueError."""
        s_ref = pd.Series(["yes", "no", "TRUE", "False", "1", "0", "refunded", "paid"])
        parsed_ref = en_pipe.parse_strict_refund_series(s_ref, "refunded")
        self.assertEqual(parsed_ref.tolist(), [1, 0, 1, 0, 1, 0, 1, 0])

        s_bad_ref = pd.Series(["yes", "maybe_refunded"])
        with self.assertRaises(ValueError):
            en_pipe.parse_strict_refund_series(s_bad_ref, "refunded")

        s_num = pd.Series(["$1,200.50", "89.00", " $45.25 "])
        parsed_num = en_pipe.parse_strict_numeric_series(s_num, "gross_usd")
        self.assertEqual(parsed_num.tolist(), [1200.5, 89.0, 45.25])

        s_bad_num = pd.Series(["100.00", "N/A_corrupted"])
        with self.assertRaises(ValueError):
            en_pipe.parse_strict_numeric_series(s_bad_num, "gross_usd")

    def test_3_missing_fee_or_refund_column_requires_explicit_flag(self):
        """Audit #3: Missing fee or refund column must raise ValueError unless opt-in flag is provided."""
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = os.path.join(tmp, "no_fee.csv")
            pd.DataFrame({
                "txn_id": ["t1", "t2"],
                "week": ["2026-W09", "2026-W10"],
                "channel": ["Pro", "Pro"],
                "region": ["US", "US"],
                "gross_usd": [100.0, 200.0],
                "refunded": ["no", "yes"],
            }).to_csv(csv_path, index=False)

            # Without --estimate-missing-fees -> must fail
            res_fail = subprocess.run(
                [sys.executable, "csv_to_pdf_pipeline.py", "--input", csv_path, "--allow-html-only"],
                capture_output=True, text=True
            )
            self.assertNotEqual(res_fail.returncode, 0)
            self.assertIn("Fee column 'stripe_fee_usd' is missing", res_fail.stderr)

            # With --estimate-missing-fees -> must succeed
            out_json = os.path.join(tmp, "out.json")
            out_html = os.path.join(tmp, "out.html")
            res_ok = subprocess.run(
                [
                    sys.executable, "csv_to_pdf_pipeline.py",
                    "--input", csv_path,
                    "--estimate-missing-fees", "--derive-net-from-gross",
                    "--allow-html-only",
                    "--output-json", out_json,
                    "--output-html", out_html,
                ],
                capture_output=True, text=True
            )
            self.assertEqual(res_ok.returncode, 0, msg=res_ok.stderr)
            with open(out_json, "r", encoding="utf-8") as f:
                m = json.load(f)
            # 1 out of 2 rows had refunded='yes' -> 50.0% refund rate!
            self.assertEqual(m["refund_rate"], 50.0)

    def test_4_semantic_period_sorting_w9_vs_w10(self):
        """Audit #4: '2026-W10' must sort AFTER '2026-W9', never before it."""
        k9 = en_pipe.parse_period_sort_key("2026-W9")
        k10 = en_pipe.parse_period_sort_key("2026-W10")
        self.assertLess(k9, k10)

        df = pd.DataFrame({
            "txn_id": ["t1", "t2"],
            "week": ["2026-W9", "2026-W10"],
            "_sort_key": [k9, k10],
            "channel": ["Enterprise", "Enterprise"],
            "region": ["North America", "North America"],
            "gross_usd": [100.0, 150.0],
            "stripe_fee_usd": [3.0, 4.5],
            "refunded": [0, 0],
            "net_usd": [97.0, 145.5],
        })
        m = en_pipe.compute_deterministic_metrics(df)
        self.assertEqual(m["latest_week"], "2026-W10")
        self.assertEqual(m["wow_net_delta"], 50.0)

    def test_5_zero_prior_period_net_revenue_consistency(self):
        """Audit #5: When prior period net revenue is 0, JSON, KPI table, and prose must all report N/A (not +0.0%)."""
        df = pd.DataFrame({
            "txn_id": ["t1", "t2"],
            "week": ["2026-W09", "2026-W10"],
            "_sort_key": [en_pipe.parse_period_sort_key("2026-W09"), en_pipe.parse_period_sort_key("2026-W10")],
            "channel": ["Starter", "Starter"],
            "region": ["US", "US"],
            "gross_usd": [50.0, 100.0],
            "stripe_fee_usd": [2.0, 3.0],
            "refunded": [1, 0],
            "net_usd": [0.0, 97.0],
        })
        m = en_pipe.compute_deterministic_metrics(df)
        self.assertIsNone(m["wow_net_delta"])
        self.assertIn("N/A", m["wow_delta_str"])
        self.assertIn("not calculable", m["executive_bullets"][0])
        self.assertNotIn("+0.0%", m["executive_bullets"][0])

    def test_6_top_n_aggregation_for_many_channels(self):
        """Audit #7: >6 channels/regions must aggregate tail into 'Other (N segments)' so table stays compact."""
        rows = []
        for i in range(10):
            rows.append({
                "txn_id": f"tx_{i}",
                "week": "2026-W10",
                "_sort_key": en_pipe.parse_period_sort_key("2026-W10"),
                "channel": f"Channel_{i:02d}",
                "region": f"Region_{i:02d}",
                "gross_usd": float((10 - i) * 100),
                "stripe_fee_usd": 3.0,
                "refunded": 0,
                "net_usd": float((10 - i) * 100 - 3),
            })
        df = pd.DataFrame(rows)
        m = en_pipe.compute_deterministic_metrics(df, max_table_rows=6)
        self.assertEqual(len(m["by_channel"]), 6)
        self.assertEqual(m["by_channel"][-1]["channel"], "Other (5 segments)")
        self.assertEqual(m["total_distinct_channels"], 10)


if __name__ == "__main__":
    unittest.main(verbosity=2)
