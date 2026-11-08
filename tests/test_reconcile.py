# Tests for engine/reconcile.py. No API, no network, nothing is paid.
#
#   * the planted inconsistency of the answer key must be flagged as ONE review item
#     (Z-B: card and gross_total MISMATCH, same root cause); everything else must be OK;
#   * the +-3 % boundary, root-cause merging, missing data, real-day PayPal data (injected);
#   * the saved data/merchant_docs/extracted.json gives the same result (skipped until it exists).
#
# Run (from the project root):
#   python -m unittest discover -s tests -p "test_reconcile.py" -v
import contextlib
import csv
import io
import json
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine import reconcile as rc  # noqa: E402

D = Decimal
NO_NUMBERS = {title: None for title in rc.CLAUSES}
Z_A, Z_B, UST = "z_bericht_A_2026-10-15.pdf", "z_bericht_B_2026-10-22.pdf", "ustva_2026-10.pdf"


def rows_of(path):
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def values_from_answer(file_name, answer):
    """What a correct extraction of each document contains (taken from the answer key)."""
    n = answer["numbers"]
    empty = {k: None for k in ("gross_total", "cash", "card", "paypal", "voids_count", "voids_amount",
                               "vat_19_net", "vat_7_net")}
    if file_name.startswith("z_bericht_A"):
        return {**empty, "doc_type": "z_report", "period_start": answer["date"], "period_end": answer["date"],
                "gross_total": n["umsatz_brutto"], "cash": n["bar"], "card": n["karte"], "paypal": n["paypal"],
                "vat_19_net": n["mwst_19"]["netto"], "vat_7_net": n["mwst_7"]["netto"], "currency": "EUR"}
    if file_name.startswith("z_bericht_B"):
        return {**empty, "doc_type": "z_report", "period_start": answer["date"], "period_end": answer["date"],
                "gross_total": n["gesamtumsatz_inkl_ust"], "cash": n["barzahlungen"],
                "card": n["kartenzahlungen"], "paypal": n["online_paypal"], "currency": "EUR"}
    return {**empty, "doc_type": "vat_return", "period_start": "2026-10-01", "period_end": "2026-10-31",
            "vat_19_net": n["kennzahl_81_bemessungsgrundlage"], "vat_7_net": n["kennzahl_86_bemessungsgrundlage"],
            "currency": "EUR"}


def status_of(report, file_name):
    return {c["check"]: c["status"] for c in report["documents"][file_name]["checks"]}


def check_of(report, file_name, name):
    return next(c for c in report["documents"][file_name]["checks"] if c["check"] == name)


# ----------------------------------------------- the planted inconsistency
class PlantedInconsistency(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        answer_path = rc.DOCS_DIR / "answer_key.json"
        if not (answer_path.exists() and rc.VERIFIED_DAILY.exists() and rc.DAILY_SUMMARY.exists()):
            raise unittest.SkipTest("needs data/merchant_docs/answer_key.json, verified_daily.csv and daily_summary.csv")
        cls.answer_key = json.loads(answer_path.read_text(encoding="utf-8"))["documents"]
        cls.documents = {name: {"values": values_from_answer(name, a)} for name, a in cls.answer_key.items()}
        cls.reference = rc.Reference(rows_of(rc.VERIFIED_DAILY), rows_of(rc.DAILY_SUMMARY))
        cls.report = rc.reconcile(cls.documents, cls.reference, numbers=NO_NUMBERS)

    def test_z_b_card_and_gross_total_are_mismatch_and_cash_and_paypal_are_ok(self):
        self.assertEqual(status_of(self.report, Z_B),
                         {"paypal": "OK", "card": "MISMATCH", "cash": "OK", "gross_total": "MISMATCH"})

    def test_the_gaps_are_under_reporting_of_about_18_and_6_percent(self):
        card, gross = check_of(self.report, Z_B, "card"), check_of(self.report, Z_B, "gross_total")
        self.assertEqual((card["direction"], gross["direction"]), ("under-reported", "under-reported"))
        self.assertEqual((card["reported"], card["reference"], card["difference"], card["percent"]),
                         ("247.21", "301.47", "-54.26", "-18.00"))
        self.assertEqual((gross["reported"], gross["reference"], gross["difference"], gross["percent"]),
                         ("796.26", "850.52", "-54.26", "-6.38"))
        self.assertIn("under-reporting", card["explanation"])

    def test_everything_else_is_ok(self):
        for name in (Z_A, UST):
            self.assertTrue(all(s == "OK" for s in status_of(self.report, name).values()), (name, status_of(self.report, name)))
        self.assertEqual(self.report["documents"][UST]["summary"]["ok"], 1)

    def test_the_vat_return_implies_nearly_the_verified_revenue(self):
        check = check_of(self.report, UST, "vat_vs_verified_revenue")
        self.assertEqual(check["reported"], "23831.67")            # 12016 x 1.19 + 8909 x 1.07
        self.assertEqual(check["reference"], "23833.61")
        self.assertLess(abs(D(check["percent"])), D("0.05"))

    def test_the_two_mismatches_are_one_review_record_with_one_root_cause(self):
        reviews = self.report["review_required"]
        self.assertEqual(len(reviews), 1)
        review = reviews[0]
        self.assertEqual(review["document"], Z_B)
        self.assertEqual(review["root_cause"], "card under-reported")
        self.assertEqual(review["affected_checks"], ["card", "gross_total"])
        self.assertEqual((review["difference_eur"], review["percent"]), ("-54.26", "-18.00"))
        self.assertEqual(review["status"], "review required")
        self.assertIn("ONE finding", review["explanation"])
        self.assertEqual(self.report["documents"][Z_B]["summary"]["mismatch"], 2)
        self.assertEqual(self.report["documents"][Z_B]["summary"]["review_items"], 1)

    def test_it_is_the_inconsistency_that_was_planted(self):
        planted = self.answer_key[Z_B]["planted_inconsistencies"][0]
        review = self.report["review_required"][0]
        self.assertEqual(planted["field"], "Kartenzahlungen")
        self.assertEqual(review["reported"], planted["reported"])
        self.assertEqual(review["reference"], planted["verified"])
        self.assertEqual(review["difference_eur"], planted["difference"])
        self.assertEqual([d for d, a in self.answer_key.items() if a["planted_inconsistencies"]], [Z_B])

    def test_a_review_never_changes_the_split(self):
        self.assertIn("none", self.report["review_required"][0]["automatic_action"])
        source = Path(rc.__file__).read_text(encoding="utf-8")
        self.assertNotIn("from engine", source)                    # no ledger, no split engine, no payouts
        self.assertNotIn("payouts", source)

    def test_the_terminal_table_lists_every_check_and_the_review(self):
        table = rc.format_table(self.report)
        for text in ("MISMATCH (under-reported)", "REVIEW REQUIRED REV-2026-10-22-card", "card under-reported",
                     "vat_vs_verified_revenue"):
            self.assertIn(text, table)
        self.assertEqual(table.count("REVIEW REQUIRED"), 1)

    def test_the_report_is_written_by_the_command_line(self):
        with tempfile.TemporaryDirectory() as folder:
            extracted = Path(folder) / "extracted.json"
            out = Path(folder) / "reconciliation.json"
            extracted.write_text(json.dumps({"documents": {
                name: {"values": doc["values"], "validation": {"passed": True}} for name, doc in self.documents.items()}}),
                encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                code = rc.main(["--extracted", str(extracted), "--out", str(out), "--no-live"])
            saved = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(code, 0)
        self.assertEqual(len(saved["review_required"]), 1)
        self.assertEqual(saved["tolerance_percent"], "3")

    def test_clause_numbers_are_read_from_the_contract(self):
        if not rc.CONTRACT_PDF.exists():
            self.skipTest("data/sample_contract.pdf not found")
        numbers = rc.clause_numbers()
        self.assertTrue(all(isinstance(numbers[t], int) for t in rc.CLAUSES), numbers)
        self.assertIn(f"Financial and Tax Reporting (Section {numbers['Financial and Tax Reporting']})",
                      rc.clause_text(numbers))


# ------------------------------------------------- the saved extraction
class SavedExtraction(unittest.TestCase):
    def test_the_saved_extracted_json_gives_the_same_result(self):
        if not (rc.EXTRACTED_PATH.exists() and rc.VERIFIED_DAILY.exists() and rc.DAILY_SUMMARY.exists()):
            self.skipTest("run `python -m ai.doc_reader --all` first")
        documents = json.loads(rc.EXTRACTED_PATH.read_text(encoding="utf-8"))["documents"]
        reference = rc.Reference(rows_of(rc.VERIFIED_DAILY), rows_of(rc.DAILY_SUMMARY))
        report = rc.reconcile(documents, reference, numbers=NO_NUMBERS)
        self.assertEqual(status_of(report, Z_B), {"paypal": "OK", "card": "MISMATCH", "cash": "OK", "gross_total": "MISMATCH"})
        for name in (Z_A, UST):
            self.assertTrue(all(s == "OK" for s in status_of(report, name).values()), name)
        self.assertEqual([r["affected_checks"] for r in report["review_required"]], [["card", "gross_total"]])


# --------------------------------------------------------- synthetic cases
VERIFIED = [{"date": "2026-10-22", "raw_gross": "1000.00", "verified_gross": "1000.00", "card_revenue_eur": "400.00"}]
SUMMARY = [{"date": "2026-10-22", "paypal_eur": "400.00", "pos_cash_reported_eur": "200.00"}]


def z_values(day="2026-10-22", **overrides):
    values = {"doc_type": "z_report", "period_start": day, "period_end": day, "gross_total": "1000.00",
              "cash": "200.00", "card": "400.00", "paypal": "400.00", "voids_count": None, "voids_amount": None,
              "vat_19_net": None, "vat_7_net": None, "currency": "EUR"}
    values.update(overrides)
    return values


def run(values, reference=None, tolerance=rc.TOLERANCE_PERCENT):
    reference = reference or rc.Reference(VERIFIED, SUMMARY)
    return rc.reconcile({"doc.pdf": {"values": values}}, reference, tolerance, numbers=NO_NUMBERS)


class Tolerance(unittest.TestCase):
    def test_exactly_three_percent_is_ok_and_beyond_it_is_a_mismatch(self):
        for card, expected in (("412.00", "OK"), ("412.01", "MISMATCH"), ("388.00", "OK"), ("387.99", "MISMATCH"),
                               ("400.00", "OK")):
            self.assertEqual(status_of(run(z_values(card=card)), "doc.pdf")["card"], expected, card)

    def test_over_reporting_is_labelled_over_reported(self):
        check = check_of(run(z_values(card="500.00")), "doc.pdf", "card")
        self.assertEqual((check["status"], check["direction"], check["percent"], check["difference"]),
                         ("MISMATCH", "over-reported", "25.00", "100.00"))
        self.assertIn("over-reporting", check["explanation"])

    def test_the_tolerance_can_be_changed(self):
        self.assertEqual(status_of(run(z_values(card="388.00"), tolerance=D("1")), "doc.pdf")["card"], "MISMATCH")


class RootCauses(unittest.TestCase):
    def test_a_gross_gap_that_equals_the_card_gap_is_one_finding(self):
        report = run(z_values(card="300.00", gross_total="900.00"))
        self.assertEqual(len(report["review_required"]), 1)
        self.assertEqual(report["review_required"][0]["affected_checks"], ["card", "gross_total"])

    def test_a_gross_gap_that_no_channel_explains_is_its_own_finding(self):
        report = run(z_values(gross_total="900.00"))                   # channels all match, total is short
        self.assertEqual([r["root_cause"] for r in report["review_required"]], ["gross_total under-reported"])

    def test_two_channels_with_the_same_gross_gap_are_still_one_finding(self):
        report = run(z_values(card="320.00", paypal="320.00", gross_total="840.00"))
        self.assertEqual(len(report["review_required"]), 1)
        self.assertEqual(report["review_required"][0]["root_cause"], "paypal under-reported; card under-reported")
        self.assertEqual(report["review_required"][0]["affected_checks"], ["paypal", "card", "gross_total"])

    def test_independent_channel_gaps_are_separate_findings(self):
        report = run(z_values(card="320.00", cash="280.00"))           # gross unchanged: the gaps cancel out
        self.assertEqual(sorted(r["root_cause"] for r in report["review_required"]),
                         ["card under-reported", "cash over-reported"])

    def test_no_mismatch_no_review(self):
        self.assertEqual(run(z_values())["review_required"], [])


class MissingData(unittest.TestCase):
    def test_not_reported_and_no_reference(self):
        report = run(z_values(paypal=None))
        self.assertEqual(status_of(report, "doc.pdf")["paypal"], "NOT_REPORTED")
        report = run(z_values(day="2026-01-01"))
        self.assertTrue(all(s == "NO_REFERENCE" for s in status_of(report, "doc.pdf").values()))
        self.assertEqual(report["review_required"], [])

    def test_real_days_use_transaction_search_and_simulated_days_do_not(self):
        calls = []

        def fetch(day):
            calls.append(day)
            return D("171.50")

        reference = rc.Reference(VERIFIED, SUMMARY, paypal_fetch=fetch)
        report = run(z_values(day="2026-11-06", paypal="171.50"), reference)       # a real day: not in the CSV files
        self.assertEqual(calls, ["2026-11-06"])
        paypal = check_of(report, "doc.pdf", "paypal")
        self.assertEqual((paypal["status"], paypal["reference"], paypal["reference_source"]),
                         ("OK", "171.50", "PayPal Transaction Search"))
        run(z_values(), reference)                                                  # a simulated day
        self.assertEqual(calls, ["2026-11-06"])                                      # no further API call


class VatReturn(unittest.TestCase):
    def reference(self):
        rows = [{"date": f"2026-10-0{i}", "raw_gross": "1000.00", "verified_gross": "1000.00", "card_revenue_eur": "0"}
                for i in (1, 2, 3)]
        return rc.Reference(rows, [])

    def vat(self, net19, net7):
        return {"doc_type": "vat_return", "period_start": "2026-10-01", "period_end": "2026-10-03",
                "gross_total": None, "cash": None, "card": None, "paypal": None, "voids_count": None,
                "voids_amount": None, "vat_19_net": net19, "vat_7_net": net7, "currency": "EUR"}

    def test_net_bases_are_converted_back_to_gross_with_1_19_and_1_07(self):
        check = check_of(run(self.vat("1500", "1100"), self.reference()), "doc.pdf", "vat_vs_verified_revenue")
        self.assertEqual((check["reported"], check["reference"], check["status"]), ("2962.00", "3000.00", "OK"))

    def test_a_vat_return_far_below_the_verified_revenue_is_flagged(self):
        report = run(self.vat("1500", "500"), self.reference())          # 1785 + 535 = 2320 against 3000
        check = check_of(report, "doc.pdf", "vat_vs_verified_revenue")
        self.assertEqual((check["status"], check["direction"], check["percent"]), ("MISMATCH", "under-reported", "-22.67"))
        self.assertEqual(len(report["review_required"]), 1)

    def test_missing_days_in_our_data_mean_no_reference(self):
        values = self.vat("1500", "1100")
        values["period_end"] = "2026-10-31"
        self.assertEqual(status_of(run(values, self.reference()), "doc.pdf")["vat_vs_verified_revenue"], "NO_REFERENCE")


if __name__ == "__main__":
    unittest.main()
