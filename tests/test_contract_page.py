# Renders the Contract page of the Streamlit app headlessly (no browser, no Claude, no PayPal) and checks the
# display formats. Needs data/contract_params.json; skipped without it.
#
# Run (from the project root):
#   python -m unittest tests.test_contract_page -v
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PARAMS = ROOT / "data" / "contract_params.json"
APP = ROOT / "app" / "streamlit_app.py"

SCRIPT = f"""
src = open({str(APP)!r}, encoding="utf-8").read()
__file__ = {str(APP)!r}
exec(compile(src[:src.index("navigation = st.navigation")], __file__, "exec"))
page_contract()
"""


class ContractPage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not PARAMS.exists():
            raise unittest.SkipTest("data/contract_params.json not found")
        from streamlit.testing.v1 import AppTest

        cls.before = PARAMS.read_bytes()
        cls.at = AppTest.from_string(SCRIPT, default_timeout=60)
        cls.at.run()
        cls.contract = json.loads(PARAMS.read_text(encoding="utf-8"))
        cls.table = " ".join(m.value for m in cls.at.markdown if "<table" in m.value)
        cls.captions = [c.value for c in cls.at.caption]

    def cell(self, label):
        match = re.search(rf"<b>{re.escape(label)}</b></td><td>([^<]*)</td>", self.table)
        self.assertIsNotNone(match, label)
        return match.group(1)

    def test_the_page_renders_without_an_error(self):
        self.assertEqual([e.value for e in self.at.exception], [])

    def test_amounts_multiples_and_terms_are_readable(self):
        params = self.contract["params"]
        self.assertEqual(self.cell("Investment amount"), "EUR 20,000")
        self.assertEqual(self.cell("Cap amount"), "EUR 23,000")
        self.assertEqual(self.cell("Baseline threshold amount"), "EUR 703.26")
        self.assertEqual(self.cell("Cap multiple"), "1.15x")
        self.assertEqual(self.cell("Max term months"), "24 months")
        self.assertEqual(params["max_term_months"], 24)                    # the stored values are unchanged

    def test_the_tiers_show_the_threshold_in_eur(self):
        self.assertIn("up to EUR 703.26: 5%", self.cell("Tiers"))

    def test_the_stored_file_is_not_touched(self):
        self.assertEqual(PARAMS.read_bytes(), self.before)

    def test_the_extraction_line_has_time_and_cost_from_the_record(self):
        meta = self.contract.get("meta") or {}
        line = next(c for c in self.captions if c.startswith("Extracted by"))
        if meta.get("elapsed_seconds") is not None:
            self.assertIn(f"in {float(meta['elapsed_seconds']):.0f} s", line)
        if meta.get("estimated_cost_usd"):
            self.assertIn(f"~${float(meta['estimated_cost_usd']):.3f}", line)

    def test_the_revenue_base_row_has_the_grey_note_below_it(self):
        note = "Card and cash are part of Total Receipts, so the merchant’s POS reports and VAT returns are checked on the Merchant documents page."
        self.assertIn(note, self.table)
        self.assertLess(self.table.index("Revenue base"), self.table.index(note))
        self.assertLess(self.table.index(note), self.table.index("Card gap breach days"))


class ExtractionLine(unittest.TestCase):
    """The line is built from whatever the run record holds; missing parts are left out."""

    @classmethod
    def setUpClass(cls):
        src = APP.read_text(encoding="utf-8")
        start = src.index("def extraction_note")
        end = src.index("REVENUE_BASE_NOTE")
        cls.namespace = {}
        exec(src[start:end], cls.namespace)

    def test_without_a_record_nothing_is_invented(self):
        line = self.namespace["extraction_note"]({"model": "m", "params": {"a": 1}})
        self.assertEqual(line, "Extracted by m with tool use. 1 fields, each with a source quote.")

    def test_time_and_cost_are_added_when_present(self):
        contract = {"model": "m", "params": {}, "meta": {"elapsed_seconds": 23.0, "estimated_cost_usd": "0.0508"}}
        self.assertEqual(self.namespace["extraction_note"](contract),
                         "Extracted by m with tool use in 23 s, ~$0.051 (estimated from list prices). 0 fields, each with a source quote.")


if __name__ == "__main__":
    unittest.main()
