# Tests for ai/contract_parser.py. They never call the Claude API:
#   * SavedContractParams  - assertions on the committed data/contract_params.json
#   * ValidationRules      - the validation functions on small synthetic inputs
#
# Run (from the project root):
#   python -m unittest discover -s tests -p "test_contract_parser.py" -v
import copy
import json
import sys
import unittest
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ai import contract_parser as cp  # noqa: E402


def D(x):
    return Decimal(str(x))


class SavedContractParams(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not cp.PARAMS_PATH.exists():
            raise AssertionError(f"{cp.PARAMS_PATH} not found. Run: python -m ai.contract_parser")
        saved = json.loads(cp.PARAMS_PATH.read_text(encoding="utf-8"))
        cls.params = saved["params"]
        cls.evidence = saved["evidence"]

    def test_all_fields_present(self):
        self.assertEqual(sorted(self.params), sorted(cp.FIELD_NAMES))
        self.assertEqual(sorted(self.evidence), sorted(cp.FIELD_NAMES))

    def test_headline_terms(self):
        p = self.params
        self.assertEqual(p["funding_date"], "2026-11-06")
        self.assertEqual(D(p["investment_amount"]), D(20000))
        self.assertEqual(D(p["cap_multiple"]), D("1.15"))
        self.assertEqual(D(p["cap_amount"]), D(23000))
        self.assertEqual(int(p["max_term_months"]), 24)
        self.assertEqual(D(p["reduced_rate"]), D("0.03"))

    def test_tiers_and_baseline(self):
        p = self.params
        tiers = p["tiers"]
        self.assertEqual(len(tiers), 2)
        self.assertEqual(D(tiers[0]["rate"]), D("0.05"))
        self.assertEqual(D(tiers[0]["up_to"]), D("703.26"))
        self.assertEqual(D(tiers[1]["rate"]), D("0.03"))
        self.assertIsNone(tiers[1]["up_to"])
        self.assertEqual(D(p["baseline_threshold_amount"]), D("703.26"))
        self.assertEqual(D(tiers[0]["up_to"]), D(p["baseline_threshold_amount"]))

    def test_baseline_method_quote_is_the_definition(self):
        self.assertTrue(self.params["baseline_threshold_method"])
        self.assertIn("Baseline Threshold", self.evidence["baseline_threshold_method"]["source_quote"])

    def test_other_terms(self):
        p = self.params
        self.assertEqual(p["day_basis"], "trading_day")
        self.assertEqual(p["timezone"], "Europe/Berlin")
        self.assertEqual(p["revenue_base"], "total_receipts_all_channels")
        self.assertEqual(p["settlement_frequency"], "daily")
        self.assertEqual(p["fallback_collection"], "SEPA direct debit")
        self.assertEqual(p["payout_fee_borne_by"], "merchant")
        self.assertEqual(D(p["servicing_fee_rate"]), D("0.05"))
        self.assertEqual(D(p["health_trigger_drop_pct"]), D(20))
        self.assertEqual((int(p["health_window_short_days"]), int(p["health_window_long_days"])), (7, 30))
        self.assertEqual(int(p["reduced_rate_max_days"]), 30)
        self.assertIs(p["requires_investor_approval"], True)
        self.assertEqual(int(p["card_gap_breach_days"]), 3)
        self.assertEqual(D(p["channel_share_drop_trigger_pp"]), D(15))
        self.assertEqual(int(p["monthly_report_days"]), 10)
        self.assertEqual(int(p["vat_copy_days"]), 14)
        self.assertEqual(int(p["annual_statement_months"]), 6)

    def test_saved_result_passes_all_validation_checks(self):
        """Re-validate the saved file against the PDF text (no API call)."""
        if not cp.PDF_PATH.exists():
            self.skipTest("data/sample_contract.pdf not found")
        try:
            pdf_text = cp.read_pdf_text(cp.PDF_PATH)
        except ImportError:
            self.skipTest("pdfplumber is not installed")
        failed = [(n, d) for n, ok, d in cp.validate(self.params, self.evidence, pdf_text) if not ok]
        self.assertEqual(failed, [])


class RunStatistics(unittest.TestCase):
    def test_cost_is_estimated_from_list_prices(self):
        # claude-sonnet-5-5: USD 2.00 per million input tokens, USD 10.00 per million output tokens
        self.assertEqual(cp.estimate_cost_usd("claude-sonnet-5-5", 10_000, 2_000), Decimal("0.0400"))
        self.assertEqual(cp.estimate_cost_usd("claude-sonnet-5-5", 0, 0), Decimal("0.0000"))

    def test_unknown_model_has_no_estimate(self):
        self.assertIsNone(cp.estimate_cost_usd("some-other-model", 1000, 1000))

    def test_meta_fields(self):
        usage = {"api_calls": 1, "input_tokens": 6000, "output_tokens": 3000, "api_seconds": 11.26}
        meta = cp.build_meta("claude-sonnet-5-5", 12.34, usage)
        self.assertEqual((meta["elapsed_seconds"], meta["api_seconds"]), (12.3, 11.3))
        self.assertEqual((meta["input_tokens"], meta["output_tokens"], meta["api_calls"]), (6000, 3000, 1))
        self.assertEqual(meta["estimated_cost_usd"], "0.0420")            # 6000 x 2 + 3000 x 10 per million
        self.assertEqual(meta["pricing_usd_per_mtok"], {"input": "2.00", "output": "10.00"})
        self.assertIsNone(cp.build_meta("unpriced", 1.0, usage)["estimated_cost_usd"])


GOOD = {
    "investment_amount": 20000, "cap_multiple": 1.15, "cap_amount": 23000,
    "tiers": [{"up_to": 703.26, "rate": 0.05}, {"up_to": None, "rate": 0.03}],
    "baseline_threshold_amount": 703.26, "servicing_fee_rate": 0.05, "reduced_rate": 0.03,
    "funding_date": "2026-11-06",
}


class ValidationRules(unittest.TestCase):
    def check(self, fn, **changes):
        params = copy.deepcopy(GOOD)
        params.update(changes)
        return fn(params)[0]

    def test_good_values_pass(self):
        for fn in (cp.check_rates, cp.check_cap, cp.check_tiers_ascending, cp.check_servicing,
                   cp.check_first_tier_baseline, cp.check_funding_date):
            self.assertTrue(self.check(fn), fn.__name__)

    def test_rate_outside_0_1_fails(self):
        self.assertFalse(self.check(cp.check_rates, tiers=[{"up_to": 703.26, "rate": 5}, {"up_to": None, "rate": 0.03}]))
        self.assertFalse(self.check(cp.check_rates, servicing_fee_rate=-0.01))

    def test_cap_must_equal_investment_times_multiple(self):
        self.assertFalse(self.check(cp.check_cap, cap_amount=22000))
        self.assertTrue(self.check(cp.check_cap, cap_multiple=1.1, cap_amount=22000))

    def test_tiers_must_ascend_and_end_open(self):
        self.assertFalse(self.check(cp.check_tiers_ascending, tiers=[
            {"up_to": 700, "rate": 0.05}, {"up_to": 500, "rate": 0.04}, {"up_to": None, "rate": 0.03}]))
        self.assertFalse(self.check(cp.check_tiers_ascending, tiers=[{"up_to": 703.26, "rate": 0.05}]))
        self.assertTrue(self.check(cp.check_tiers_ascending, tiers=[
            {"up_to": 500, "rate": 0.06}, {"up_to": 703.26, "rate": 0.05}, {"up_to": None, "rate": 0.03}]))

    def test_servicing_fee_must_be_below_20_percent(self):
        self.assertFalse(self.check(cp.check_servicing, servicing_fee_rate=0.2))
        self.assertTrue(self.check(cp.check_servicing, servicing_fee_rate=0.199))

    def test_first_tier_must_equal_baseline(self):
        self.assertFalse(self.check(cp.check_first_tier_baseline, baseline_threshold_amount=704.94))

    def test_quote_must_appear_in_pdf_text_ignoring_whitespace(self):
        pdf = cp.normalize("The Investor shall pay the Merchant\nEUR 20,000 (the \"Investment Amount\").")
        evidence = {"investment_amount": {"source_quote": 'The Investor shall pay the Merchant EUR 20,000 (the "Investment Amount").'}}
        self.assertTrue(cp.check_quote("investment_amount", GOOD, evidence, pdf)[0])
        evidence["investment_amount"]["source_quote"] = "The Investor shall pay the Merchant EUR 25,000."
        self.assertFalse(cp.check_quote("investment_amount", GOOD, evidence, pdf)[0])

    def test_non_null_value_without_quote_fails(self):
        self.assertFalse(cp.check_quote("investment_amount", GOOD, {"investment_amount": {"source_quote": None}}, "")[0])

    def test_null_field_needs_a_reason(self):
        params = {n: 1 for n in cp.FIELD_NAMES}
        params["vat_copy_days"] = None
        self.assertFalse(cp.check_null_reasons(params, {"vat_copy_days": {"not_found_reason": ""}})[0])
        self.assertTrue(cp.check_null_reasons(params, {"vat_copy_days": {"not_found_reason": "not in the contract"}})[0])


if __name__ == "__main__":
    unittest.main()
