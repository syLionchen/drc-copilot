# Tests for scripts/export_demo_snapshot.py: the safety check, id masking, and that the exported
# ledger rows and receipts carry no batch ids, e-mail addresses or payer ids. No network.
#
# Run (from the project root):
#   python -m unittest discover -s tests -p "test_export_demo_snapshot.py" -v
import json
import sys
import tempfile
import unittest
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine import ledger  # noqa: E402
from scripts import export_demo_snapshot as exp  # noqa: E402


class SafetyCheck(unittest.TestCase):
    def test_clean_text_passes(self):
        exp.check_clean({"a.json": '{"payer_name": "Anna Schmidt", "net": "11.91"}'})

    def test_each_forbidden_string_stops_the_export(self):
        for bad in ("anna@example.test", "key sk-ant-123", "client_secret=abc", "Access_Token: x"):
            with self.assertRaises(exp.UnsafeExport, msg=bad):
                exp.check_clean({"out.json": f"harmless {bad} harmless"})

    def test_the_message_names_the_file(self):
        with self.assertRaises(exp.UnsafeExport) as ctx:
            exp.check_clean({"clean.json": "ok", "dirty.json": "mail me at x@y.z"})
        self.assertIn("dirty.json", str(ctx.exception))
        self.assertNotIn("clean.json", str(ctx.exception))


class Masking(unittest.TestCase):
    def test_only_the_last_four_characters_of_a_transaction_id_are_kept(self):
        self.assertEqual(exp.mask_id("70N46584PG3145712"), "…5712")
        self.assertEqual(exp.mask_id("AB12"), "AB12")


class Settlements(unittest.TestCase):
    def test_only_whitelisted_columns_are_exported(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "drc.db"
            conn = ledger.connect(path)
            ledger.insert_settlement(conn, {
                "date": "2026-11-06", "merchant_id": "kut", "verified_revenue": "166.84",
                "rate_applied": "0.050000", "drc_due": "8.34", "payout_amount": "8.34",
                "merchant_payout_fee": "0.17", "servicing_fee": "0.42", "investor_net": "7.92",
                "cumulative_due": "8.34", "sender_batch_id": "drc-kut-20261106",
                "payout_batch_id": "BATCH123", "status": "SUCCESS", "created_at": ledger.now_iso()})
            conn.close()
            rows = exp.build_settlements(path, "kut")
            self.assertEqual(len(rows), 1)
            self.assertEqual(tuple(rows[0]), exp.SETTLEMENT_COLUMNS)
            text = exp.dump(rows)
            self.assertNotIn("sender_batch_id", text)
            self.assertNotIn("BATCH123", text)
            self.assertNotIn("drc-kut-20261106", text)
            self.assertEqual(rows[0]["drc_due"], "8.34")
            self.assertEqual(exp.build_settlements(path, "someone-else"), [])

    def test_a_missing_ledger_gives_an_empty_list(self):
        self.assertEqual(exp.build_settlements(ROOT / "no-such-folder" / "drc.db", "kut"), [])


class FakeReporting:
    def fetch_trading_day_detailed(self, day):
        row = {"transaction_id": "70N46584PG3145712", "time_berlin": datetime(2026, 11, 6, 23, 33, 24),
               "payer_name": "Anna Schmidt", "payer_id": "buyer@example.test", "gross": Decimal("12.50"),
               "fee": Decimal("0.59"), "net": Decimal("11.91"), "currency": "EUR", "event_code": "T0000"}
        excluded = {"transaction_id": "9LE60910UW431634V", "time_berlin": datetime(2026, 11, 6, 20, 0, 0),
                    "event_code": "T1105", "amount": Decimal("1.02"), "currency": "EUR", "status": "S",
                    "payer_name": None, "reason": "event code T1105 is not a customer payment"}
        return ([row], [excluded]) if day == date(2026, 11, 6) else ([], [])


class Receipts(unittest.TestCase):
    def setUp(self):
        self.days = [date(2026, 11, 6), date(2026, 11, 7)]
        self.receipts = exp.build_receipts(self.days, FakeReporting())

    def test_ids_are_cut_and_payer_names_are_kept(self):
        tx = self.receipts[0]["transactions"][0]
        self.assertEqual(tx["transaction_id"], "…5712")          # 70N46584PG3145712 -> last 4: 5712
        self.assertEqual(tx["payer_name"], "Anna Schmidt")
        self.assertEqual(self.receipts[0]["excluded"][0]["transaction_id"], "…634V")

    def test_no_payer_id_or_email_is_exported(self):
        text = exp.dump(self.receipts)
        self.assertNotIn("payer_id", text)
        self.assertNotIn("@", text)
        exp.check_clean({"paypal_receipts.json": text})

    def test_summary_and_both_days(self):
        self.assertEqual([r["date"] for r in self.receipts], ["2026-11-06", "2026-11-07"])
        self.assertEqual(self.receipts[0]["summary"],
                         {"count": 1, "gross": "12.50", "fees": "0.59", "net": "11.91"})
        self.assertEqual(self.receipts[1]["summary"]["count"], 0)


class AnomalyDaily(unittest.TestCase):
    def test_flags_are_split_into_a_list(self):
        text = ("date,raw_gross,excluded_amount,at_risk_amount,verified_gross,est_fees,verified_net,"
                "flagged_txn_count,day_flags\n"
                "2026-09-22,1188.07,145.80,9.64,1042.27,30.82,1011.45,202,SELF_DEALING_DAY\n"
                "2026-10-27,683.43,0.00,24.33,683.43,30.05,653.38,3,CARD_REVENUE_MISSING;EVENING_SHARE_DROP\n"
                "2026-10-28,1087.26,0.00,14.08,1087.26,28.64,1058.62,2,\n")
        rows = exp.build_anomaly_daily(text)
        self.assertEqual(rows[0]["flags"], ["SELF_DEALING_DAY"])
        self.assertEqual(rows[1]["flags"], ["CARD_REVENUE_MISSING", "EVENING_SHARE_DROP"])
        self.assertEqual(rows[2]["flags"], [])
        self.assertEqual((rows[0]["raw"], rows[0]["excluded"], rows[0]["at_risk"], rows[0]["verified_gross"]),
                         ("1188.07", "145.80", "9.64", "1042.27"))
        self.assertEqual(rows[0]["flagged_txn_count"], 202)


class AnomalyMetricsAndFlagged(unittest.TestCase):
    FLAGGED = ("txn_id,timestamp,channel,customer_type,customer_id,amount_eur,list_price,discount_pct,status,"
               "price_overridden,anomaly_score,reasons,tier\n"
               "T1,2026-09-22 10:00:00,PAYPAL,INDIVIDUAL,PP-9001,0.80,0.80,0.0,COMPLETED,False,0.7,SELF_DEALING_SUSPECT,EXCLUDED\n"
               "T2,2026-09-22 10:01:00,PAYPAL,INDIVIDUAL,PP-9001,0.90,0.90,0.0,COMPLETED,False,0.7,SELF_DEALING_SUSPECT,EXCLUDED\n"
               "T9,2026-09-22 10:02:00,PAYPAL,INDIVIDUAL,PP-0001,9.00,9.00,0.0,COMPLETED,False,0.6,IF_OUTLIER,UNDER_REVIEW\n")
    INJECTED = ("anomaly_type,date,txn_id,detail\n"
                "SELF_DEALING_SMALL_ORDERS,2026-09-22,T1,x\n"
                "SELF_DEALING_SMALL_ORDERS,2026-09-22,T2,x\n"
                "SELF_DEALING_SMALL_ORDERS,2026-09-22,T3,x\n"
                "CARD_REVENUE_MISSING,2026-10-27,,x\n"
                "CARD_REVENUE_MISSING,2026-10-30,,x\n")
    DAILY = ("date,raw_gross,excluded_amount,at_risk_amount,verified_gross,est_fees,verified_net,"
             "flagged_txn_count,day_flags\n"
             "2026-09-22,1,0,0,1,0,1,3,SELF_DEALING_DAY\n"
             "2026-10-27,1,0,0,1,0,1,0,CARD_REVENUE_MISSING\n"
             "2026-10-28,1,0,0,1,0,1,0,CARD_REVENUE_MISSING\n")
    TRANSACTIONS = ("txn_id,customer_type\nT1,INDIVIDUAL\nB1,BUSINESS\nB2,BUSINESS\n")

    def test_precision_and_recall_per_detector(self):
        metrics = exp.build_anomaly_metrics(self.FLAGGED, self.INJECTED, self.DAILY, self.TRANSACTIONS)
        by_name = {d["detector"]: d for d in metrics["detectors"]}
        self_dealing = by_name["SELF_DEALING_SUSPECT"]               # predicted T1,T2; truth T1,T2,T3
        self.assertEqual((self_dealing["tp"], self_dealing["fp"], self_dealing["fn"]), (2, 0, 1))
        self.assertEqual((self_dealing["precision"], round(self_dealing["recall"], 3)), (1.0, 0.667))
        card = by_name["CARD_REVENUE_MISSING"]                       # predicted 10-27,10-28; truth 10-27,10-30
        self.assertEqual((card["tp"], card["fp"], card["fn"]), (1, 1, 1))
        self.assertEqual((card["precision"], card["recall"]), (0.5, 0.5))
        self.assertIsNone(by_name["PRICE_OVERRIDE_DEEP"]["precision"])   # nothing predicted: no precision
        self.assertEqual(metrics["business_orders"], {"total": 2, "false_positives": 0})

    def test_business_false_positives_are_counted(self):
        flagged = self.FLAGGED + "B1,2026-09-22 11:00:00,PAYPAL,BUSINESS,JP,120.00,120.00,0.0,COMPLETED,False,0.9,IF_OUTLIER,UNDER_REVIEW\n"
        metrics = exp.build_anomaly_metrics(flagged, self.INJECTED, self.DAILY, self.TRANSACTIONS)
        self.assertEqual(metrics["business_orders"]["false_positives"], 1)

    def test_flagged_rows_keep_reasons_as_a_list_and_no_emails(self):
        rows = exp.build_flagged(self.FLAGGED)
        self.assertEqual([r["tier"] for r in rows], ["EXCLUDED", "EXCLUDED", "UNDER_REVIEW"])
        self.assertEqual(rows[0]["reasons"], ["SELF_DEALING_SUSPECT"])
        exp.check_clean({"anomaly_flagged.json": exp.dump(rows)})


class MerchantDocs(unittest.TestCase):
    def test_nothing_is_exported_before_the_documents_were_read_and_reconciled(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(exp.build_merchant_docs(folder), {})

    def test_results_and_pdf_text_are_exported_and_pass_the_safety_check(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            (folder / "extracted.json").write_text(json.dumps({"documents": {"a.pdf": {}, "b.pdf": {}}}), encoding="utf-8")
            (folder / "reconciliation.json").write_text('{"review_required": []}', encoding="utf-8")
            for name in ("a.pdf", "b.pdf"):
                (folder / name).write_bytes(b"%PDF-1.4 fake")
            with mock.patch("ai.contract_parser.read_pdf_text", side_effect=lambda p: f"text of {Path(p).name} €"):
                files = exp.build_merchant_docs(folder)
        self.assertEqual(set(files), {"extracted.json", "reconciliation.json", "merchant_doc_texts.json"})
        texts = json.loads(files["merchant_doc_texts.json"])
        self.assertEqual(texts, {"a.pdf": "text of a.pdf €", "b.pdf": "text of b.pdf €"})
        exp.check_clean(files)

    def test_a_secret_in_the_document_text_stops_the_export(self):
        with self.assertRaises(exp.UnsafeExport):
            exp.check_clean({"merchant_doc_texts.json": '{"a.pdf": "contact: buyer@example.test"}'})


class MerchantHealth(unittest.TestCase):
    def test_nothing_is_exported_before_the_health_agent_ran(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(exp.build_health(folder), {})

    def test_the_recommendation_is_exported_as_it_is(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "recommendation.json").write_text('{"status": "VALID"}', encoding="utf-8")
            files = exp.build_health(folder)
        self.assertEqual(files, {"health_recommendation.json": '{"status": "VALID"}'})
        exp.check_clean(files)


class WholeExport(unittest.TestCase):
    def setUp(self):
        needed = (exp.se.PARAMS_PATH, exp.irr.VERIFIED_DAILY_PATH)
        if not all(p.exists() for p in needed):
            self.skipTest("data/contract_params.json or data/verified_daily.csv not found")

    def test_everything_but_paypal_exports_cleanly(self):
        with tempfile.TemporaryDirectory() as folder:
            files = exp.build_all(Path(folder) / "none.db", "kut", exp.se.load_params(),
                                  exp.irr.VERIFIED_DAILY_PATH, exp.DEFAULT_DAYS, skip_paypal=True)
        base = {"verified_daily.csv", "settlements.json", "projection.json",
                "anomaly_daily.json", "anomaly_flagged.json", "anomaly_metrics.json"}
        merchant = {"extracted.json", "reconciliation.json", "merchant_doc_texts.json"}
        health = {"health_recommendation.json"}
        self.assertIn(set(files) - health, (base, base | merchant))  # merchant documents and health are optional
        exp.check_clean(files)
        projection = json.loads(files["projection.json"])
        self.assertEqual([s["name"] for s in projection["scenarios"]], ["Baseline", "-20%", "-40%"])
        self.assertEqual(projection["contract"]["investor_breakeven_contributions"], "21052.63")
        for scenario in projection["scenarios"]:
            self.assertTrue(scenario["series"])
            self.assertIn("net_irr", scenario["metrics"])
        self.assertEqual(len(json.loads(files["anomaly_daily.json"])), 90)


if __name__ == "__main__":
    unittest.main()
