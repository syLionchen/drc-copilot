# Tests for engine/eligibility.py, ai/eligibility_advisor.py and the "Apply for DRC" page.
# No API call, no network; the page test renders the Streamlit page headlessly on files in a temporary folder.
#
# Run (from the project root):
#   python -m unittest tests.test_eligibility -v
import copy
import json
import sys
import tempfile
import unittest
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ai import eligibility_advisor as adv  # noqa: E402
from engine import eligibility as elig  # noqa: E402

PARAMS = {"health_trigger_drop_pct": 20, "health_window_short_days": 7, "health_window_long_days": 30,
          "reduced_rate": 0.03, "reduced_rate_max_days": 30, "funding_date": "2026-11-06"}
RULES = elig.load_rules()
AS_OF = date(2026, 11, 6)


def make_profile(days=90, opened_days_ago=90, paypal_share=0.70, paypal_sales=None, sales_period_days=90,
                 documents="default", excluded_share=0.0, tail_factor=1.0):
    """A merchant that meets all nine conditions; keyword arguments break one thing at a time."""
    start = AS_OF - timedelta(days=days)
    dates = [(start + timedelta(days=i)).isoformat() for i in range(days)]
    revenue = [Decimal("1000.00")] * days
    paypal = [(Decimal("1000.00") * Decimal(str(paypal_share))).quantize(Decimal("0.01"))] * days
    net = [Decimal("970.00")] * days
    for i in range(days - 7, days):                      # the last 7 days, scaled
        net[i] = (Decimal("970.00") * Decimal(str(tail_factor))).quantize(Decimal("0.01"))
    raw = sum(revenue, Decimal(0))
    if documents == "default":
        documents = [
            {"file": "z_report_recent.pdf", "type": "z_report", "period_start": "2026-10-20", "period_end": "2026-10-20",
             "validation_passed": True, "reconciliation_status": "OK", "review_resolved": False},
            {"file": "vat_return_october.pdf", "type": "vat_return", "period_start": "2026-10-01",
             "period_end": "2026-10-31", "validation_passed": True, "reconciliation_status": "OK", "review_resolved": False}]
    return {
        "id": "test-shop", "name": "Test Shop", "sample": True, "description": "test", "as_of": AS_OF.isoformat(),
        "paypal": {"account_opened": (AS_OF - timedelta(days=opened_days_ago)).isoformat(),
                   "sales_eur": str(paypal_sales if paypal_sales is not None else sum(paypal, Decimal(0))),
                   "sales_period_days": sales_period_days},
        "daily": {"dates": dates, "revenue": [str(x) for x in revenue], "paypal_revenue": [str(x) for x in paypal],
                  "verified_net": [str(x) for x in net]},
        "documents": documents,
        "screening": {"raw_gross_eur": str(raw), "excluded_eur": str((raw * Decimal(str(excluded_share))).quantize(Decimal("0.01"))),
                      "excluded_txns": 3, "total_txns": 1000},
    }


def by_id(checks):
    return {c["id"]: c for c in checks}


def evaluate(profile, consents=None):
    return by_id(elig.evaluate(profile, RULES, PARAMS, consents))


class AllMet(unittest.TestCase):
    def test_a_good_merchant_meets_all_nine_and_gets_an_indicative_amount(self):
        checks = elig.evaluate(make_profile(), RULES, PARAMS)
        self.assertEqual([c["status"] for c in checks], ["met"] * 9)
        self.assertEqual(elig.progress(checks), (9, 9))
        ind = elig.indicative_amount(elig.annual_verified_revenue(make_profile()), RULES)
        self.assertEqual(ind["amount"], Decimal("29200.00"))                  # 8% of EUR 365,000 a year, below the cap
        self.assertFalse(ind["capped"])
        self.assertIn("Indicative only, not an offer. The investor makes the final decision.", ind["disclaimer"])

    def test_indicative_amount_is_eight_percent_and_capped_at_fifty_thousand(self):
        self.assertEqual(elig.indicative_amount("100000", RULES)["amount"], Decimal("8000.00"))
        capped = elig.indicative_amount("1000000", RULES)
        self.assertEqual((capped["amount"], capped["capped"]), (Decimal("50000.00"), True))

    def test_every_rule_is_marked_illustrative_and_the_paypal_ones_name_their_source(self):
        for name, rule in RULES["rules"].items():
            self.assertTrue(rule["illustrative"], name)
        for name in ("paypal_account_age", "paypal_annual_sales"):
            self.assertIn("Businesskredit", RULES["rules"][name]["reference"])
        self.assertEqual(RULES["rules"]["paypal_account_age"]["min_days"], 90)
        self.assertEqual(RULES["rules"]["paypal_annual_sales"]["min_eur"], 12000)

    def test_every_check_has_the_required_fields(self):
        for c in elig.evaluate(make_profile(), RULES, PARAMS):
            for key in ("id", "number", "name", "status", "numbers", "how_to_fix", "detail", "requirement"):
                self.assertIn(key, c)
            self.assertIn(c["status"], elig.STATUSES)


class Boundaries(unittest.TestCase):
    def test_1_account_age_89_days_fails_and_90_passes(self):
        self.assertEqual(evaluate(make_profile(opened_days_ago=89))["paypal_account_age"]["status"], "not_met")
        self.assertEqual(evaluate(make_profile(opened_days_ago=90))["paypal_account_age"]["status"], "met")
        failing = evaluate(make_profile(opened_days_ago=45))["paypal_account_age"]
        self.assertEqual(failing["earliest_possible_days"], 45)

    def test_2_annual_sales_11999_99_fails_and_12000_passes(self):
        low = evaluate(make_profile(paypal_sales="11999.99", sales_period_days=365))["paypal_annual_sales"]
        high = evaluate(make_profile(paypal_sales="12000.00", sales_period_days=365))["paypal_annual_sales"]
        self.assertEqual((low["status"], high["status"]), ("not_met", "met"))

    def test_3_verified_history_89_trading_days_fails_and_90_passes(self):
        self.assertEqual(evaluate(make_profile(days=89))["verified_history"]["status"], "not_met")
        self.assertEqual(evaluate(make_profile(days=90))["verified_history"]["status"], "met")

    def test_4_revenue_fall_just_under_and_just_over_the_covenant_limit(self):
        self.assertEqual(evaluate(make_profile(tail_factor=0.7550))["stable_revenue"]["status"], "met")      # fall 19.8%
        self.assertEqual(evaluate(make_profile(tail_factor=0.7500))["stable_revenue"]["status"], "not_met")  # fall 20.6%

    def test_4_the_limit_is_read_from_the_contract_not_hard_coded(self):
        strict = {**PARAMS, "health_trigger_drop_pct": 10}
        checks = by_id(elig.evaluate(make_profile(tail_factor=0.8), RULES, strict))
        self.assertEqual(checks["stable_revenue"]["status"], "not_met")
        self.assertEqual(evaluate(make_profile(tail_factor=0.8))["stable_revenue"]["status"], "met")

    def test_5_verifiable_share_59_99_fails_and_60_passes(self):
        no_coverage = [{"file": "z.pdf", "type": "z_report", "period_start": "2026-10-20", "period_end": "2026-10-20",
                        "validation_passed": True, "reconciliation_status": None, "review_resolved": False},
                       {"file": "v.pdf", "type": "vat_return", "period_start": "2026-10-01", "period_end": "2026-10-31",
                        "validation_passed": True, "reconciliation_status": None, "review_resolved": False}]
        low = evaluate(make_profile(paypal_share=0.5999, documents=no_coverage))["verifiable_share"]
        high = evaluate(make_profile(paypal_share=0.60, documents=no_coverage))["verifiable_share"]
        self.assertEqual((low["status"], high["status"]), ("action_needed", "met"))

    def test_5_the_fix_explains_the_share_and_names_the_documents_that_would_close_the_gap(self):
        check = evaluate(make_profile(paypal_share=0.35))["verifiable_share"]       # VAT return covers October only
        n = check["numbers"]
        share, paypal, docs = (Decimal(n[k]) for k in ("Verifiable share (%)", "PayPal share of revenue (%)",
                                                        "Covered by reconciled documents (%)"))
        self.assertEqual(check["status"], "action_needed")
        self.assertEqual(share, paypal + docs)
        self.assertEqual(Decimal(n["Missing (percentage points)"]), Decimal("60") - share)
        fix = check["how_to_fix"]
        for text in (f"{share}%", f"{paypal}%", f"{docs}%", "(UStVA) for August 2026", "for September 2026",
                     "2026-08-08 to 2026-08-31", "2026-09-01 to 2026-09-30", "percentage points are missing"):
            self.assertIn(text, fix)
        self.assertNotIn("November 2026", fix)                           # the month of the check cannot be filed yet
        for month in ("August 2026", "September 2026"):
            adds, total = Decimal(n[f"Adds with the UStVA for {month} (%)"]), Decimal(n[f"Total with the UStVA for {month} (%)"])
            self.assertEqual(total, share + adds)
            self.assertGreaterEqual(total, Decimal("60"), month)         # either return alone closes the gap

    def test_5_nothing_is_suggested_when_the_share_is_enough(self):
        check = evaluate(make_profile(paypal_share=0.70))["verifiable_share"]
        self.assertEqual((check["status"], check["how_to_fix"]), ("met", ""))

    def test_5_reconciled_documents_add_to_the_verifiable_share(self):
        profile = make_profile(paypal_share=0.40)                      # the VAT return covers 31 of 90 days
        share = Decimal(evaluate(profile)["verifiable_share"]["numbers"]["Verifiable share (%)"])
        self.assertGreater(share, Decimal("60"))                       # 40% + 60% x 32/90 = 61.3%

    def test_6_documents_each_part_is_needed(self):
        good = make_profile()["documents"]
        no_vat = [d for d in good if d["type"] != "vat_return"]
        no_pos = [d for d in good if d["type"] != "z_report"]
        self.assertEqual(evaluate(make_profile(documents=no_vat))["documents_complete"]["status"], "action_needed")
        self.assertEqual(evaluate(make_profile(documents=no_pos))["documents_complete"]["status"], "action_needed")
        self.assertEqual(evaluate(make_profile(documents=[]))["documents_complete"]["status"], "action_needed")

    def test_6_age_limits_of_the_documents(self):
        def with_ends(pos_end, vat_end):
            docs = copy.deepcopy(make_profile()["documents"])
            docs[0]["period_end"], docs[1]["period_end"] = pos_end.isoformat(), vat_end.isoformat()
            return evaluate(make_profile(documents=docs))["documents_complete"]["status"]

        self.assertEqual(with_ends(AS_OF - timedelta(days=90), AS_OF - timedelta(days=45)), "met")
        self.assertEqual(with_ends(AS_OF - timedelta(days=91), AS_OF - timedelta(days=45)), "action_needed")
        self.assertEqual(with_ends(AS_OF - timedelta(days=90), AS_OF - timedelta(days=46)), "action_needed")

    def test_6_a_document_that_failed_the_ai_reading_checks_does_not_count(self):
        docs = copy.deepcopy(make_profile()["documents"])
        docs[0]["validation_passed"] = False
        check = evaluate(make_profile(documents=docs))["documents_complete"]
        self.assertEqual(check["status"], "action_needed")
        self.assertIn("z_report_recent.pdf", check["how_to_fix"])

    def test_7_an_unresolved_mismatch_needs_action_and_names_the_document_and_the_gap(self):
        docs = copy.deepcopy(make_profile()["documents"])
        docs[0].update({"reconciliation_status": "MISMATCH", "file": "z_b.pdf",
                        "mismatch": {"check": "card", "label": "card payments", "reported": "247.21", "reference": "301.47",
                                     "difference_eur": "-54.26", "percent": "-18.00"}})
        check = evaluate(make_profile(documents=docs))["reconciliation_clean"]
        self.assertEqual(check["status"], "action_needed")
        for text in ("z_b.pdf", "247.21", "301.47", "-54.26", "-18.00"):
            self.assertIn(text, check["detail"])
        docs[0]["review_resolved"] = True
        self.assertEqual(evaluate(make_profile(documents=docs))["reconciliation_clean"]["status"], "met")

    def test_8_excluded_share_4_99_percent_passes_and_5_fails(self):
        self.assertEqual(evaluate(make_profile(excluded_share=0.0499))["screening"]["status"], "met")
        self.assertEqual(evaluate(make_profile(excluded_share=0.05))["screening"]["status"], "not_met")


class Consent(unittest.TestCase):
    def test_9_every_required_source_is_needed(self):
        for source in ("paypal", "pos", "vat"):
            consents = {"paypal": True, "pos": True, "vat": True, source: False}
            self.assertEqual(evaluate(make_profile(), consents)["consent"]["status"], "needs_consent", source)
        self.assertEqual(evaluate(make_profile(), {"paypal": True, "pos": True, "vat": True, "bank": False})["consent"]["status"], "met")

    def test_withdrawing_a_consent_shows_needs_consent_on_the_checks_that_use_that_data(self):
        checks = elig.evaluate(make_profile(), RULES, PARAMS)
        required = RULES["rules"]["consent"]["required_sources"]
        after = by_id(elig.apply_consent(checks, {"paypal": True, "pos": False, "vat": True}, required))
        self.assertEqual({i for i, c in after.items() if c["status"] == "needs_consent"},
                         {"verifiable_share", "documents_complete", "reconciliation_clean", "consent"})
        self.assertEqual(after["paypal_account_age"]["status"], "met")
        self.assertEqual(after["documents_complete"]["missing_consent"], ["pos"])
        no_paypal = by_id(elig.apply_consent(checks, {"paypal": False, "pos": True, "vat": True}, required))
        self.assertEqual(no_paypal["paypal_account_age"]["status"], "needs_consent")
        self.assertEqual(no_paypal["screening"]["status"], "needs_consent")
        self.assertEqual(no_paypal["documents_complete"]["status"], "met")

    def test_granting_the_consent_again_restores_the_original_result(self):
        required = RULES["rules"]["consent"]["required_sources"]
        base = elig.evaluate(make_profile(opened_days_ago=45), RULES, PARAMS)
        blocked = elig.apply_consent(base, {"paypal": False, "pos": True, "vat": True}, required)
        restored = elig.apply_consent(blocked, {"paypal": True, "pos": True, "vat": True}, required)
        self.assertEqual([c["status"] for c in restored], [c["status"] for c in base])
        self.assertEqual(by_id(restored)["paypal_account_age"]["status"], "not_met")

    def test_the_checks_survive_a_json_round_trip(self):
        required = RULES["rules"]["consent"]["required_sources"]
        checks = json.loads(json.dumps(elig.evaluate(make_profile(), RULES, PARAMS)))
        after = elig.apply_consent(checks, {"paypal": False, "pos": True, "vat": True}, required)
        self.assertEqual(elig.progress(after)[0], 2)                  # without PayPal data only conditions 6 and 7 stay met


class Profiles(unittest.TestCase):
    def test_the_new_merchant_fails_conditions_1_3_and_6(self):
        profile = elig.load_profile_file(elig.NEW_MERCHANT_PATH)
        self.assertTrue(profile["sample"])
        self.assertIn("SAMPLE", profile["SAMPLE"] + profile["description"])
        checks = by_id(elig.evaluate(profile, RULES, PARAMS))
        self.assertEqual(checks["paypal_account_age"]["status"], "not_met")
        self.assertEqual(checks["verified_history"]["status"], "not_met")
        self.assertEqual(checks["documents_complete"]["status"], "action_needed")
        self.assertEqual(checks["paypal_account_age"]["numbers"]["Account age (days)"], "45")
        self.assertIn("VAT return", checks["documents_complete"]["detail"])
        self.assertEqual(elig.progress(list(checks.values()))[0], 6)

    def test_kaffee_und_tee_has_an_unresolved_card_mismatch_in_condition_7(self):
        needed = (elig.DAILY_SUMMARY_PATH, elig.FLAGGED_PATH, ROOT / "data" / "verified_daily.csv",
                  elig.MERCHANT_DOCS_DIR / "extracted.json", elig.MERCHANT_DOCS_DIR / "reconciliation.json")
        if not all(p.exists() for p in needed):
            self.skipTest("run python -m data.generate_data, ai.anomaly, ai.doc_reader --all and engine.reconcile first")
        params = json.loads((ROOT / "data" / "contract_params.json").read_text(encoding="utf-8"))["params"]
        checks = by_id(elig.evaluate(elig.profile_demo_merchant(params), RULES, params))
        seven = checks["reconciliation_clean"]
        self.assertEqual(seven["status"], "action_needed")
        self.assertIn("z_bericht_B_2026-10-22.pdf", seven["detail"])
        self.assertEqual(seven["numbers"]["Difference (EUR)"], "-54.26")
        self.assertEqual(seven["numbers"]["Difference (%)"], "-18.00")


def unmet_inputs():
    profile = elig.load_profile_file(elig.NEW_MERCHANT_PATH)
    return adv.build_inputs(profile, elig.evaluate(profile, RULES, PARAMS))


def good_advice():
    return {"summary": "You meet 6 of 9 conditions; the account is 45 days old and a VAT return is missing.",
            "steps": [
                {"check_id": "paypal_account_age", "what_to_do": "Keep selling through PayPal; the account needs 45 more days.",
                 "materials": ["Nothing to upload"], "time_estimate_days": 45},
                {"check_id": "verified_history", "what_to_do": "Keep trading; you need 51 more trading days.",
                 "materials": ["Nothing to upload"], "time_estimate_days": 51},
                {"check_id": "documents_complete", "what_to_do": "Upload your latest VAT return as a PDF.",
                 "materials": ["Latest VAT return (UStVA) as a PDF"], "time_estimate_days": 7}]}


class Advisor(unittest.TestCase):
    def test_a_correct_answer_is_valid(self):
        self.assertEqual(adv.validate_advice(good_advice(), unmet_inputs()), [])

    def test_the_advisor_can_quote_how_the_share_is_made_up_and_which_returns_would_help(self):
        profile = make_profile(paypal_share=0.35)
        inputs = adv.build_inputs(profile, elig.evaluate(profile, RULES, PARAMS))
        n = by_id(elig.evaluate(profile, RULES, PARAMS))["verifiable_share"]["numbers"]
        self.assertIn("UStVA for September 2026", adv.readable_inputs(inputs))
        advice = {"summary": f"Your verifiable share is {n['Verifiable share (%)']}%, below the {n['Required (%)']}% required.",
                  "steps": [{"check_id": "verifiable_share",
                             "what_to_do": f"{n['PayPal share of revenue (%)']}% of your revenue is visible in PayPal and "
                                           f"{n['Covered by reconciled documents (%)']}% is covered by documents. Submit the "
                                           f"VAT return for September 2026, which adds {n['Adds with the UStVA for September 2026 (%)']}%.",
                             "materials": ["VAT return (UStVA) for September 2026 as a PDF"], "time_estimate_days": 5}]}
        self.assertEqual(adv.validate_advice(advice, inputs), [])
        advice["steps"][0]["what_to_do"] += " That would add 25.17% to your share."
        self.assertTrue(any("25.17" in p for p in adv.validate_advice(advice, inputs)))

    def test_a_step_for_an_unknown_condition_id_is_invalid(self):
        advice = good_advice()
        advice["steps"][0]["check_id"] = "credit_score"
        problems = adv.validate_advice(advice, unmet_inputs())
        self.assertTrue(any("credit_score" in p and "does not exist" in p for p in problems), problems)

    def test_a_step_for_a_condition_that_is_already_met_is_invalid(self):
        advice = good_advice()
        advice["steps"][0]["check_id"] = "screening"
        self.assertTrue(any("already met" in p for p in adv.validate_advice(advice, unmet_inputs())))

    def test_an_invented_number_is_invalid(self):
        advice = good_advice()
        advice["steps"][2]["what_to_do"] = "Upload your VAT return; most shops need EUR 4,321.98 of sales first."
        problems = adv.validate_advice(advice, unmet_inputs())
        self.assertTrue(any("4321.98" in p for p in problems), problems)

    def test_an_invented_date_is_invalid(self):
        advice = good_advice()
        advice["summary"] += " Apply again on 2027-02-01."
        self.assertTrue(any("2027-02-01" in p for p in adv.validate_advice(advice, unmet_inputs())))

    def test_internal_field_names_are_invalid_but_check_id_may_have_them(self):
        advice = good_advice()
        advice["steps"][0]["what_to_do"] = "Wait until paypal_account_age is met."
        problems = adv.validate_advice(advice, unmet_inputs())
        self.assertTrue(any("paypal_account_age" in p and "internal field name" in p for p in problems), problems)

    def test_a_time_estimate_below_the_earliest_possible_time_is_invalid(self):
        advice = good_advice()
        advice["steps"][0]["time_estimate_days"] = 10
        self.assertTrue(any("shorter than the earliest possible" in p for p in adv.validate_advice(advice, unmet_inputs())))

    def test_a_document_may_be_named_by_its_file_name(self):
        profile = make_profile(documents=[
            {"file": "z_bericht_B_2026-10-22.pdf", "type": "z_report", "period_start": "2026-10-22",
             "period_end": "2026-10-22", "validation_passed": True, "reconciliation_status": "MISMATCH", "review_resolved": False,
             "mismatch": {"check": "card", "label": "card payments", "reported": "247.21", "reference": "301.47",
                          "difference_eur": "-54.26", "percent": "-18.00"}},
            {"file": "vat.pdf", "type": "vat_return", "period_start": "2026-10-01", "period_end": "2026-10-31",
             "validation_passed": True, "reconciliation_status": "OK", "review_resolved": False}])
        inputs = adv.build_inputs(profile, elig.evaluate(profile, RULES, PARAMS))
        advice = {"summary": "One document needs an explanation.",
                  "steps": [{"check_id": "reconciliation_clean",
                             "what_to_do": "Explain why z_bericht_B_2026-10-22.pdf shows card payments of EUR 247.21 instead of EUR 301.47.",
                             "materials": ["A short written explanation or a corrected report"], "time_estimate_days": 3}]}
        self.assertEqual(adv.validate_advice(advice, inputs), [])

    def test_structure_errors_are_reported(self):
        inputs = unmet_inputs()
        self.assertTrue(adv.validate_advice("text", inputs))
        self.assertTrue(adv.validate_advice({"summary": "", "steps": []}, inputs))
        bad = good_advice()
        bad["steps"][0]["time_estimate_days"] = "soon"
        self.assertTrue(adv.validate_advice(bad, inputs))

    def test_advise_uses_the_injected_call_and_validates(self):
        usage = {"api_calls": 1, "input_tokens": 1, "output_tokens": 1, "api_seconds": 0.1}
        bad = good_advice()
        bad["steps"][1]["check_id"] = "made_up"
        advice, _, problems = adv.advise(unmet_inputs(), call=lambda system, tool, prompt: (bad, usage))
        self.assertIs(advice, bad)
        self.assertTrue(problems)

    def test_the_prompt_is_readable_english_with_labelled_numbers(self):
        text = adv.readable_inputs(unmet_inputs())
        self.assertIn("Account age (days): 45", text)
        self.assertIn("Conditions met: 6 of 9", text)
        self.assertIn("Earliest this can be met: 45 days", text)
        self.assertIn("(id: paypal_account_age)", text)


class Wording(unittest.TestCase):
    def test_one_day_is_singular(self):
        self.assertEqual(elig.days_text(1), "1 day")
        self.assertEqual((elig.days_text(0), elig.days_text(2), elig.days_text(45)), ("0 days", "2 days", "45 days"))
        fix = evaluate(make_profile(opened_days_ago=89))["paypal_account_age"]["how_to_fix"]
        self.assertIn("needs 1 day more", fix)
        self.assertIn("1 more trading day of PayPal history is needed",
                      evaluate(make_profile(days=89))["verified_history"]["how_to_fix"])

    def test_condition_8_says_the_share_is_by_amount_and_shows_the_count_separately(self):
        profile = make_profile(excluded_share=0.0044)
        profile["screening"].update({"excluded_txns": 436, "total_txns": 10852})
        check = evaluate(profile)["screening"]
        self.assertEqual(check["numbers"]["Excluded share by amount (%)"], "0.44")
        self.assertEqual(check["numbers"]["Excluded share by count (%)"], "4.02")
        for text in ("By amount", "0.44%", "436 of 10,852 transactions", "4.02%", "the limit applies to the amount"):
            self.assertIn(text, check["detail"])
        self.assertIn("by amount", check["requirement"])

    def run_advisor(self, profiles):
        import contextlib
        import io
        from unittest import mock

        out = io.StringIO()
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(adv, "OUT_DIR", Path(folder)),                 mock.patch.object(adv.elig, "load_profiles", return_value=profiles),                 mock.patch.object(adv.se, "load_params", return_value=PARAMS), contextlib.redirect_stdout(out):
            adv.main(["--no-ai"])
        return out.getvalue()

    def test_the_disclaimer_is_printed_only_when_an_indicative_amount_exists(self):
        sentence = "Indicative only, not an offer"
        unmet = [elig.load_profile_file(elig.NEW_MERCHANT_PATH)]
        text = self.run_advisor(unmet)
        saved = [line for line in text.splitlines() if line.startswith("Saved")]
        self.assertEqual(len(saved), 1)
        self.assertNotIn(sentence, text)
        met_text = self.run_advisor([make_profile()])
        self.assertIn(sentence, [line for line in met_text.splitlines() if line.startswith("Saved")][0])


# ----------------------------------------------------------------------------------- the page
def page_script(folder):
    """Script that runs page_apply() with the demo folder replaced by `folder`."""
    app = str(ROOT / "app" / "streamlit_app.py")
    replacement = f"DEMO = Path({str(folder)!r})"
    lines = [f"src = open({app!r}, encoding='utf-8').read()",
             f"__file__ = {app!r}",
             f"src = src.replace({'DEMO = ROOT / ' + chr(34) + 'data' + chr(34) + ' / ' + chr(34) + 'demo' + chr(34)!r}, {replacement!r})",
             "exec(compile(src[:src.index('navigation = st.navigation')], __file__, 'exec'))",
             "page_apply()"]
    return "\n".join(lines) + "\n"


class ApplyPage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from streamlit.testing.v1 import AppTest

        cls.folder = tempfile.TemporaryDirectory()
        folder = Path(cls.folder.name)
        usage = {"api_calls": 1, "input_tokens": 100, "output_tokens": 50, "api_seconds": 2.0}
        from ai import contract_parser

        meta = contract_parser.build_meta("claude-sonnet-5-5", 12.0, usage)
        for profile, advice in ((elig.load_profile_file(elig.NEW_MERCHANT_PATH), good_advice()), (make_profile(), None)):
            checks = elig.evaluate(profile, RULES, PARAMS)
            block = None if advice is None else {"status": "VALID", "problems": [], "model": "m", "meta": meta,
                                                 "summary": advice["summary"], "steps": advice["steps"]}
            record = adv.build_record(profile, checks, RULES, block)
            (folder / f"eligibility_{profile['id']}.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
        cls.at = AppTest.from_string(page_script(folder), default_timeout=90)
        cls.at.run()

    @classmethod
    def tearDownClass(cls):
        cls.folder.cleanup()

    def table_text(self):
        return " ".join(m.value for m in self.at.markdown if "<table" in m.value)

    def test_the_page_renders_with_the_top_sentence_and_four_consent_boxes(self):
        self.assertEqual([e.value for e in self.at.exception], [])
        text = " ".join(m.value for m in self.at.markdown)
        self.assertIn("Financing based on verified data, not collateral. You choose what to share; nothing is stored in this demo.", text)
        labels = [c.label for c in self.at.checkbox]
        self.assertEqual(labels, ["PayPal transaction history (required)", "POS daily reports (required)",
                                  "VAT returns (required)", "Bank statements (optional)"])
        self.assertEqual([c.value for c in self.at.checkbox], [True, True, True, False])

    def test_both_profiles_are_offered_and_the_bakery_shows_its_progress(self):
        self.assertEqual(len(self.at.selectbox[0].options), 2)
        self.assertIn("Sample Bakery GmbH (SAMPLE)", self.at.selectbox[0].options)
        self.assertIn("6 of 9 conditions met", [e.proto.text for e in self.at.get("progress")])

    def test_withdrawing_a_required_consent_shows_needs_consent_and_hides_the_amount(self):
        from streamlit.testing.v1 import AppTest

        at = AppTest.from_string(page_script(self.folder.name), default_timeout=90)
        at.run()
        at.selectbox[0].select("Test Shop (SAMPLE)").run()
        self.assertIn("Indicative amount", [m.label for m in at.metric])
        self.assertIn("9 of 9 conditions met", [e.proto.text for e in at.get("progress")])
        at.checkbox(key="consent_pos").uncheck().run()
        self.assertNotIn("Indicative amount", [m.label for m in at.metric])
        table = " ".join(m.value for m in at.markdown if "<table" in m.value)
        self.assertEqual(table.count("Needs consent"), 4)                 # conditions 5, 6, 7 and 9
        self.assertIn("5 of 9 conditions met", [e.proto.text for e in at.get("progress")])
        self.assertTrue(any("still open" in w.value for w in at.warning))

    def test_one_day_is_shown_as_1_day_on_the_page(self):
        from streamlit.testing.v1 import AppTest

        record_path = Path(self.folder.name) / "eligibility_sample-bakery.json"
        record = json.loads(record_path.read_text(encoding="utf-8"))
        record["advice"]["steps"][0]["time_estimate_days"] = 1
        with tempfile.TemporaryDirectory() as other:
            (Path(other) / "eligibility_sample-bakery.json").write_text(json.dumps(record), encoding="utf-8")
            at = AppTest.from_string(page_script(other), default_timeout=90)
            at.run()
            text = " ".join(m.value for m in at.markdown)
        self.assertIn("about 1 day<", text)
        self.assertNotIn("about 1 days", text)

    def test_the_advice_is_shown_with_its_label_and_the_disclaimer_when_all_conditions_are_met(self):
        text = " ".join(m.value for m in self.at.markdown)
        self.assertIn("VALID", text)
        self.assertIn("Condition 1: PayPal business account history", text)
        self.assertIn("about 45 days", text)


if __name__ == "__main__":
    unittest.main()
