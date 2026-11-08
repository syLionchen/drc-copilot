# Tests for ai/doc_reader.py. They never call the Claude API:
#   * German number conversion
#   * the quote / value checks (shared with ai/contract_parser.py) on small synthetic extractions
#   * the saved data/merchant_docs/extracted.json against data/merchant_docs/answer_key.json
#     (skipped until `python -m ai.doc_reader --all` has been run)
#
# Run (from the project root):
#   python -m unittest discover -s tests -p "test_doc_reader.py" -v
import calendar
import json
import sys
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ai import contract_parser as cp  # noqa: E402
from ai import doc_reader as dr  # noqa: E402

D = Decimal


class GermanNumbers(unittest.TestCase):
    def test_thousands_dots_and_decimal_comma(self):
        for printed, expected in (("1.234,56", "1234.56"), ("997,13", "997.13"), ("12.016,00", "12016.00"),
                                  ("8.909,00", "8909.00"), ("0,50", "0.50"), ("1.234.567,89", "1234567.89"),
                                  ("2.283,04", "2283.04"), ("300", "300"), ("12.016", "12016")):
            self.assertEqual(dr.parse_german_number(printed), D(expected), printed)

    def test_currency_signs_spaces_and_minus_are_handled(self):
        self.assertEqual(dr.parse_german_number("1.234,56 €"), D("1234.56"))
        self.assertEqual(dr.parse_german_number("€ 1.234,56"), D("1234.56"))
        self.assertEqual(dr.parse_german_number(" 355,73 EUR "), D("355.73"))
        self.assertEqual(dr.parse_german_number("-54,26"), D("-54.26"))
        self.assertEqual(dr.parse_german_number("−54,26"), D("-54.26"))

    def test_english_and_broken_formats_are_rejected_not_guessed(self):
        for bad in ("1,234.56", "1234.56", "0.5", "12,3,4", "1.23,4.5", "abc", "", "1.234,"):
            with self.assertRaises(ValueError, msg=bad):
                dr.parse_german_number(bad)

    def test_result_has_no_float_error(self):
        self.assertEqual(str(dr.parse_german_number("0,10") + dr.parse_german_number("0,20")), "0.30")


# the text pdfplumber would return for a (shortened) Tagesabschluss
TEXT_B = """Tagesabschluss
Kassensystem B - Kaffee und Tee GmbH, Hannover
Donnerstag, 22.10.2026 | Abschluss-Nr. B-0069
Gesamtumsatz inkl. USt: 796,26 €
Barzahlungen: 156,43 €
Kartenzahlungen: 247,21 €
Online (PayPal): 392,62 €
Enthaltene USt 19 %: 76,21 €
Stornierte Vorgänge: 3 Stück
Stornosumme: 19,57 €
SAMPLE DOCUMENT FOR DEMONSTRATION ONLY"""


def field(value, quote=None, reason=None):
    return {"value": value, "source_quote": quote, "not_found_reason": reason}


def good_raw():
    raw = {name: field(None, None, "not in the document") for name in dr.FIELD_NAMES}
    raw.update({
        "doc_type": field("z_report", "Tagesabschluss"),
        "period_start": field("2026-10-22", "Donnerstag, 22.10.2026 | Abschluss-Nr. B-0069"),
        "period_end": field("2026-10-22", "Donnerstag, 22.10.2026 | Abschluss-Nr. B-0069"),
        "gross_total": field("796,26", "Gesamtumsatz inkl. USt: 796,26 €"),
        "cash": field("156,43", "Barzahlungen: 156,43 €"),
        "card": field("247,21", "Kartenzahlungen: 247,21 €"),
        "paypal": field("392,62", "Online (PayPal): 392,62 €"),
        "voids_count": field(3, "Stornierte Vorgänge: 3 Stück"),
        "voids_amount": field("19,57", "Stornosumme: 19,57 €"),
        "vat_19_net": field(None, None, "only the VAT amount is printed, not the net amount"),
        "currency": field("EUR", "Kartenzahlungen: 247,21 €"),
    })
    return raw


USAGE = {"api_calls": 1, "input_tokens": 3000, "output_tokens": 900, "api_seconds": 6.0}


def result_for(raw, text=TEXT_B):
    return dr.build_result("z_bericht_B.pdf", text, raw, USAGE, 6.5)


def failed_checks(result):
    return [f["check"] for f in result["validation"]["failed"]]


class QuoteChecks(unittest.TestCase):
    def test_the_quote_check_is_the_one_of_the_contract_parser(self):
        self.assertIs(dr.check_quote, cp.check_quote)
        self.assertIs(dr.normalize, cp.normalize)

    def test_a_good_extraction_passes_and_converts_the_numbers(self):
        result = result_for(good_raw())
        self.assertTrue(result["validation"]["passed"], result["validation"]["failed"])
        values = result["values"]
        self.assertEqual((values["gross_total"], values["cash"], values["card"], values["paypal"]),
                         ("796.26", "156.43", "247.21", "392.62"))
        self.assertEqual((values["voids_count"], values["voids_amount"]), (3, "19.57"))
        self.assertIsNone(values["vat_19_net"])
        self.assertEqual(result["raw"]["gross_total"], "796,26")           # the printed form is kept too

    def test_a_quote_that_is_not_in_the_document_is_rejected(self):
        raw = good_raw()
        raw["card"]["source_quote"] = "Kartenzahlungen: 301,47 €"
        self.assertIn("field[card]", failed_checks(result_for(raw)))

    def test_quote_matching_ignores_whitespace(self):
        raw = good_raw()
        raw["card"]["source_quote"] = "Kartenzahlungen:   247,21\n€"
        self.assertTrue(result_for(raw)["validation"]["passed"])

    def test_the_number_must_appear_inside_its_quote(self):
        raw = good_raw()
        raw["card"]["value"] = "301,47"                                      # quote is real, value is not in it
        self.assertIn("field[card]", failed_checks(result_for(raw)))

    def test_null_needs_a_reason(self):
        raw = good_raw()
        raw["vat_19_net"]["not_found_reason"] = ""
        self.assertIn("field[vat_19_net]", failed_checks(result_for(raw)))

    def test_a_non_null_value_needs_a_quote(self):
        raw = good_raw()
        raw["cash"]["source_quote"] = None
        self.assertIn("field[cash]", failed_checks(result_for(raw)))

    def test_english_number_format_is_rejected(self):
        raw = good_raw()
        raw["card"].update(value="247.21", source_quote="Kartenzahlungen: 247,21 €")
        self.assertIn("field[card]", failed_checks(result_for(raw)))

    def test_wrong_enum_currency_and_dates_are_rejected(self):
        for name, value in (("doc_type", "invoice"), ("currency", "USD"), ("period_start", "22.10.2026"),
                            ("voids_count", "3")):
            raw = good_raw()
            raw[name]["value"] = value
            self.assertIn(f"field[{name}]", failed_checks(result_for(raw)), name)

    def test_period_must_not_end_before_it_starts(self):
        raw = good_raw()
        raw["period_start"]["value"] = "2026-10-23"
        self.assertIn("period_order", failed_checks(result_for(raw)))

    def test_missing_fields_fail_the_structure_check(self):
        raw = good_raw()
        del raw["card"]
        self.assertIn("structure", failed_checks(result_for(raw)))

    def test_a_document_that_does_not_add_up_is_reported_but_is_not_an_extraction_failure(self):
        raw = good_raw()
        text = TEXT_B.replace("Gesamtumsatz inkl. USt: 796,26", "Gesamtumsatz inkl. USt: 800,00")
        raw["gross_total"].update(value="800,00", source_quote="Gesamtumsatz inkl. USt: 800,00 €")
        result = result_for(raw, text)
        self.assertTrue(result["validation"]["passed"])
        self.assertIn("does not add up", result["validation"]["info"][0]["detail"])

    def test_meta_records_time_and_tokens(self):
        meta = result_for(good_raw())["meta"]
        self.assertEqual((meta["elapsed_seconds"], meta["input_tokens"], meta["output_tokens"]), (6.5, 3000, 900))
        if dr.config.CLAUDE_MODEL in dr.config.CLAUDE_PRICING_USD_PER_MTOK:
            self.assertEqual(meta["estimated_cost_usd"],
                             str(cp.estimate_cost_usd(dr.config.CLAUDE_MODEL, 3000, 900)))

    def test_the_tool_asks_for_every_field_with_a_quote(self):
        tool = dr.build_tool()
        self.assertEqual(sorted(tool["input_schema"]["required"]), sorted(dr.FIELD_NAMES))
        for name in dr.FIELD_NAMES:
            self.assertEqual(sorted(tool["input_schema"]["properties"][name]["required"]),
                             ["not_found_reason", "source_quote", "value"])


class ReadsOnlyThePdf(unittest.TestCase):
    """The reader must never see the answer key: it is only for comparing results in these tests."""

    def test_the_module_does_not_mention_the_answer_key(self):
        self.assertNotIn("answer_key", Path(dr.__file__).read_text(encoding="utf-8"))

    def test_only_pdf_files_are_accepted(self):
        for name in ("answer_key.json", "extracted.json", "notes.txt"):
            with self.assertRaises(SystemExit):
                dr.process_document(name)

    def test_the_prompt_contains_only_the_pdf_text_and_no_other_file_is_opened(self):
        captured = {}

        def fake_call(system_prompt, tool, prompt):
            captured["prompt"] = prompt
            return good_raw(), USAGE

        def forbidden(*args, **kwargs):
            raise AssertionError("process_document opened a file other than the PDF")

        with mock.patch.object(dr, "read_pdf_text", return_value=TEXT_B) as read_pdf, \
                mock.patch.object(dr, "call_claude_tool", side_effect=fake_call), \
                mock.patch.object(Path, "read_text", side_effect=forbidden), \
                mock.patch("builtins.open", side_effect=forbidden):
            result = dr.process_document("data/merchant_docs/z_bericht_B_2026-10-22.pdf")
        read_pdf.assert_called_once_with("data/merchant_docs/z_bericht_B_2026-10-22.pdf")
        self.assertTrue(result["validation"]["passed"])
        self.assertIn(TEXT_B, captured["prompt"])
        self.assertNotIn("planted", captured["prompt"].lower())
        self.assertNotIn("verified_reference", captured["prompt"])


# ----------------------------------------------- saved results vs answer key
EXTRACTED_PATH, ANSWER_KEY_PATH = dr.EXTRACTED_PATH, dr.DOCS_DIR / "answer_key.json"

NONE_FIELDS = ("gross_total", "cash", "card", "paypal", "voids_count", "voids_amount", "vat_19_net", "vat_7_net")
AMOUNT_FIELDS = ("gross_total", "cash", "card", "paypal", "voids_amount", "vat_19_net", "vat_7_net")


def expected_values(file_name, answer):
    """What the extraction must contain for each document, taken from the answer key."""
    n = answer["numbers"]
    if file_name.startswith("z_bericht_A"):
        return {"doc_type": "z_report", "period_start": answer["date"], "period_end": answer["date"],
                "gross_total": n["umsatz_brutto"], "cash": n["bar"], "card": n["karte"], "paypal": n["paypal"],
                "voids_count": n["storno_anzahl"], "voids_amount": n["storno_betrag"],
                "vat_19_net": n["mwst_19"]["netto"], "vat_7_net": n["mwst_7"]["netto"], "currency": "EUR"}
    if file_name.startswith("z_bericht_B"):
        return {"doc_type": "z_report", "period_start": answer["date"], "period_end": answer["date"],
                "gross_total": n["gesamtumsatz_inkl_ust"], "cash": n["barzahlungen"],
                "card": n["kartenzahlungen"], "paypal": n["online_paypal"],
                "voids_count": n["stornierte_vorgaenge"], "voids_amount": n["stornosumme"],
                "vat_19_net": None, "vat_7_net": None, "currency": "EUR"}       # only the VAT amounts are printed
    year, month = (int(p) for p in answer["period"].split("-"))
    last_day = calendar.monthrange(year, month)[1]
    return {"doc_type": "vat_return", "period_start": f"{year}-{month:02d}-01",
            "period_end": f"{year}-{month:02d}-{last_day:02d}",
            **{name: None for name in NONE_FIELDS},
            "vat_19_net": n["kennzahl_81_bemessungsgrundlage"], "vat_7_net": n["kennzahl_86_bemessungsgrundlage"],
            "currency": "EUR"}


class ExtractedAgainstAnswerKey(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not (EXTRACTED_PATH.exists() and ANSWER_KEY_PATH.exists()):
            raise unittest.SkipTest("run `python -m data.make_merchant_docs` and `python -m ai.doc_reader --all` first")
        cls.extracted = json.loads(EXTRACTED_PATH.read_text(encoding="utf-8"))["documents"]
        cls.answers = json.loads(ANSWER_KEY_PATH.read_text(encoding="utf-8"))["documents"]

    def test_every_document_in_the_answer_key_was_extracted_and_validated(self):
        self.assertEqual(sorted(self.extracted), sorted(self.answers))
        for name, result in self.extracted.items():
            self.assertTrue(result["validation"]["passed"], (name, result["validation"]["failed"]))

    def test_extracted_numbers_equal_the_answer_key(self):
        for name, answer in self.answers.items():
            expected, values = expected_values(name, answer), self.extracted[name]["values"]
            for field_name, want in expected.items():
                got = values[field_name]
                if want is None:
                    self.assertIsNone(got, (name, field_name))
                elif field_name == "voids_count":
                    self.assertEqual(int(got), int(want), (name, field_name))
                elif field_name in AMOUNT_FIELDS:
                    self.assertEqual(D(got), D(want), (name, field_name))
                else:
                    self.assertEqual(got, want, (name, field_name))

    def test_german_formatting_was_converted_correctly(self):
        b = self.extracted["z_bericht_B_2026-10-22.pdf"]
        self.assertEqual(dr.parse_german_number(b["raw"]["gross_total"]), D(b["values"]["gross_total"]))
        v = self.extracted["ustva_2026-10.pdf"]
        self.assertEqual((v["values"]["vat_19_net"], v["values"]["vat_7_net"]), ("12016.00", "8909.00"))

    def test_every_quote_is_in_the_pdf_it_came_from(self):
        for name, result in self.extracted.items():
            pdf_path = dr.DOCS_DIR / name
            if not pdf_path.exists():
                self.skipTest(f"{pdf_path} not found")
            normalized = cp.normalize(cp.read_pdf_text(pdf_path))
            for field_name, evidence in result["evidence"].items():
                if result["values"][field_name] is not None:
                    self.assertIn(cp.normalize(evidence["source_quote"]), normalized, (name, field_name))

    def test_nulls_have_a_reason_and_nothing_is_calculated(self):
        for name, result in self.extracted.items():
            for field_name, value in result["values"].items():
                if value is None:
                    self.assertTrue(result["evidence"][field_name]["not_found_reason"], (name, field_name))
        b = self.extracted["z_bericht_B_2026-10-22.pdf"]["values"]               # printed: VAT amounts only
        self.assertIsNone(b["vat_19_net"])
        self.assertIsNone(b["vat_7_net"])

    def test_the_planted_card_gap_shows_up_against_the_verified_data(self):
        """Extraction alone finds the numbers; comparing them with verified_daily.csv finds the lie."""
        answer = self.answers["z_bericht_B_2026-10-22.pdf"]
        reported = D(self.extracted["z_bericht_B_2026-10-22.pdf"]["values"]["card"])
        verified = D(answer["verified_reference"]["card_revenue_eur"])
        deviation = (reported - verified) / verified * 100
        self.assertEqual(reported, D(answer["planted_inconsistencies"][0]["reported"]))
        self.assertAlmostEqual(float(deviation), -18.0, places=0)

    def test_the_other_documents_match_their_verified_references(self):
        a = self.extracted["z_bericht_A_2026-10-15.pdf"]["values"]
        ref = self.answers["z_bericht_A_2026-10-15.pdf"]["verified_reference"]
        self.assertEqual(D(a["card"]), D(ref["card_revenue_eur"]))
        self.assertEqual(D(a["gross_total"]), D(ref["verified_gross"]))

    def test_run_statistics_are_recorded(self):
        for name, result in self.extracted.items():
            meta = result["meta"]
            self.assertGreater(meta["input_tokens"], 0, name)
            self.assertGreater(meta["output_tokens"], 0, name)
            self.assertGreaterEqual(meta["elapsed_seconds"], 0, name)


if __name__ == "__main__":
    unittest.main()
