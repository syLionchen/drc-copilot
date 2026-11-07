# Tests for engine/irr_tracker.py: XIRR on simple known cash flows, the projection rules
# (cap, term end, cycling, fee estimate) and the ledger reader. No network, no chart.
#
# Run (from the project root):
#   python -m unittest discover -s tests -p "test_irr_tracker.py" -v
import math
import sys
import unittest
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine import irr_tracker as irr  # noqa: E402
from engine import ledger  # noqa: E402
from engine import split_engine as se  # noqa: E402

D0 = date(2026, 1, 1)


class XirrKnownFlows(unittest.TestCase):
    def test_minus_100_then_plus_110_after_a_year_is_10_percent(self):
        self.assertAlmostEqual(irr.xirr([(D0, -100), (D0 + timedelta(days=365), 110)]), 0.10, places=9)

    def test_two_years_121_is_10_percent_a_year(self):
        self.assertAlmostEqual(irr.xirr([(D0, -100), (D0 + timedelta(days=730), 121)]), 0.10, places=9)

    def test_a_loss_gives_a_negative_rate(self):
        self.assertAlmostEqual(irr.xirr([(D0, -100), (D0 + timedelta(days=365), 90)]), -0.10, places=9)

    def test_actual_days_over_365_are_used(self):
        flows = [(D0, -100), (D0 + timedelta(days=366), 110)]
        self.assertAlmostEqual(irr.xirr(flows), 1.10 ** (365 / 366) - 1, places=9)

    def test_two_equal_instalments_match_the_closed_form(self):
        # 100 = 60 / (1 + r) + 60 / (1 + r)^2   ->   x = 1 / (1 + r) solves 60x^2 + 60x - 100 = 0
        x = (-60 + math.sqrt(60 ** 2 + 4 * 60 * 100)) / (2 * 60)
        expected = 1 / x - 1
        flows = [(D0, -100), (D0 + timedelta(days=365), 60), (D0 + timedelta(days=730), 60)]
        self.assertAlmostEqual(irr.xirr(flows), expected, places=9)

    def test_decimal_amounts_and_unsorted_input_are_fine(self):
        flows = [(D0 + timedelta(days=365), Decimal("110.00")), (D0, Decimal("-100.00"))]
        self.assertAlmostEqual(irr.xirr(flows), 0.10, places=9)

    def test_merchant_view_has_the_same_rate_with_inverted_signs(self):
        self.assertAlmostEqual(irr.xirr([(D0, 100), (D0 + timedelta(days=365), -110)]), 0.10, places=9)

    def test_undefined_cases_return_none(self):
        self.assertIsNone(irr.xirr([(D0, 100), (D0 + timedelta(days=365), 110)]))          # no outflow
        self.assertIsNone(irr.xirr([(D0, -100), (D0 + timedelta(days=365), -110)]))        # no inflow
        self.assertIsNone(irr.xirr([(D0, -100), (D0, 110)]))                               # all on one day


# ------------------------------------------------------------ projection
def params(cap, investment=100, term=24, rate=0.05, servicing=0.05, up_to=703.26, upper=0.03):
    return {"investment_amount": investment, "cap_amount": cap, "funding_date": "2026-11-06",
            "max_term_months": term, "servicing_fee_rate": servicing,
            "tiers": [{"up_to": up_to, "rate": rate}, {"up_to": None, "rate": upper}]}


FEE = Decimal("0.02")


def project(p, revenues, factor="1", actuals=()):
    return irr.project(p, [Decimal(r) for r in revenues], Decimal(factor), list(actuals), fee_rate=FEE)


class ProjectionRules(unittest.TestCase):
    def test_runs_until_the_cap_is_reached(self):
        proj = project(params(cap="440.70"), ["1000"])           # 44.07 a day: exactly 10 days
        self.assertEqual(len(proj.rows), 10)
        self.assertEqual(proj.cap_reached_on, date(2026, 11, 15))
        self.assertEqual(proj.rows[-1]["cumulative"], Decimal("440.70"))
        self.assertEqual(irr.metrics(proj)["days_to_cap"], 9)

    def test_the_last_day_is_cut_to_reach_the_cap_exactly(self):
        proj = project(params(cap="440.00"), ["1000"])
        self.assertEqual(len(proj.rows), 10)
        self.assertEqual(proj.rows[-1]["drc_due"], Decimal("43.37"))        # 440.00 - 9 x 44.07
        self.assertEqual(proj.rows[-1]["cumulative"], Decimal("440.00"))

    def test_the_projection_uses_the_live_split_function(self):
        proj = project(params(cap="100000"), ["1000", "166.84", "703.26"])
        for row, revenue in zip(proj.rows[:3], ["1000", "166.84", "703.26"]):
            self.assertEqual(row["drc_due"], se.calculate_split(Decimal(revenue), params("100000")).drc_due)
        self.assertEqual([r["drc_due"] for r in proj.rows[:3]], [Decimal("44.07"), Decimal("8.34"), Decimal("35.16")])

    def test_revenue_values_are_cycled(self):
        proj = project(params(cap="100000"), ["100", "200", "300"])
        first_four = [r["drc_due"] for r in proj.rows[:4]]
        self.assertEqual(first_four, [Decimal("5.00"), Decimal("10.00"), Decimal("15.00"), Decimal("5.00")])

    def test_stress_factor_scales_revenue(self):
        base = project(params(cap="100000"), ["200"]).rows[0]["drc_due"]
        stressed = project(params(cap="100000"), ["200"], factor="0.5").rows[0]["drc_due"]
        self.assertEqual((base, stressed), (Decimal("10.00"), Decimal("5.00")))

    def test_stops_at_the_end_of_the_term_when_the_cap_is_not_reached(self):
        proj = project(params(cap="100000", term=1), ["100"])
        self.assertEqual(len(proj.rows), 30)                                # 2026-11-06 .. 2026-12-05
        self.assertEqual(proj.rows[-1]["date"], date(2026, 12, 5))
        self.assertIsNone(proj.cap_reached_on)
        m = irr.metrics(proj)
        self.assertFalse(m["repaid"])
        self.assertEqual(m["total_drc_due"], Decimal("150.00"))             # 30 x 5.00
        self.assertEqual(m["shortfall_vs_cap"], Decimal("99850.00"))

    def test_merchant_payout_fee_is_estimated_at_2_percent_of_drc_due(self):
        proj = project(params(cap="100000"), ["1000"])
        self.assertEqual(proj.rows[0]["fee"], Decimal("0.88"))              # 44.07 x 2% = 0.8814

    def test_projection_continues_after_the_last_actual_settlement(self):
        actual = {"date": date(2026, 11, 6), "drc_due": Decimal("8.34"), "investor_net": Decimal("7.92"),
                  "fee": Decimal("0.17"), "actual": True}
        proj = project(params(cap="440.70"), ["1000"], actuals=[actual])
        self.assertTrue(proj.rows[0]["actual"])
        self.assertEqual(proj.rows[1]["date"], date(2026, 11, 7))
        self.assertEqual(proj.rows[1]["cumulative"], Decimal("52.41"))      # 8.34 + 44.07
        self.assertEqual(proj.rows[-1]["cumulative"], Decimal("440.70"))
        self.assertEqual(proj.rows[0]["fee"], Decimal("0.17"))              # the real fee, not the estimate

    def test_principal_is_recovered_before_the_cap_is_reached(self):
        # 0.50 a day; the servicing fee 0.025 rounds half-up to 0.03, so investor_net is 0.47 a day.
        # Principal 100: day 213 (100.11). Cap 115: day 230. Counted from the funding date: 212 and 229.
        proj = project(params(cap="115", investment=100), ["10"])
        m = irr.metrics(proj)
        self.assertEqual(m["days_to_principal"], 212)
        self.assertEqual(m["days_to_cap"], 229)
        self.assertLess(m["principal_recovered_on"], m["cap_reached_on"])

    def test_net_multiple_is_investor_net_over_principal(self):
        proj = project(params(cap="115", investment=100), ["10"])
        m = irr.metrics(proj)
        self.assertEqual(m["total_drc_due"], Decimal("115.00"))
        self.assertEqual(m["net_multiple"], (m["total_investor_net"] / Decimal("100")).quantize(Decimal("0.001")))
        self.assertLess(m["net_multiple"], Decimal("1.150"))                # servicing fee comes off

    def test_gross_irr_is_above_net_irr_above_zero_and_merchant_apr_is_above_gross(self):
        proj = project(params(cap="115", investment=100), ["10"])
        m = irr.metrics(proj)
        self.assertGreater(m["gross_irr"], m["net_irr"])
        self.assertGreater(m["net_irr"], 0)
        self.assertGreater(m["merchant_apr"], m["gross_irr"])               # the merchant also pays payout fees

    def test_flows_have_the_right_signs(self):
        proj = project(params(cap="440.70"), ["1000"])
        gross, net, merchant = (irr.flows_of(proj, k) for k in ("gross", "net", "merchant"))
        self.assertEqual(gross[0], (date(2026, 11, 6), Decimal("-100.00")))
        self.assertEqual(merchant[0], (date(2026, 11, 6), Decimal("100.00")))
        self.assertTrue(all(a > 0 for _, a in gross[1:]) and all(a > 0 for _, a in net[1:]))
        self.assertTrue(all(a < 0 for _, a in merchant[1:]))
        self.assertEqual(merchant[1][1], -(Decimal("44.07") + Decimal("0.88")))   # drc_due + payout fee


class LedgerAndSummary(unittest.TestCase):
    def test_only_success_settlements_are_read(self):
        conn = ledger.connect(":memory:")
        for day, status, fee in ((date(2026, 11, 6), "SUCCESS", "0.17"), (date(2026, 11, 7), "DENIED", None),
                                 (date(2026, 11, 8), "PROCESSING", None)):
            ledger.insert_settlement(conn, {
                "date": day.isoformat(), "merchant_id": "kut", "verified_revenue": "166.84",
                "rate_applied": "0.050000", "drc_due": "8.34", "payout_amount": "8.34",
                "merchant_payout_fee": fee, "servicing_fee": "0.42", "investor_net": "7.92",
                "cumulative_due": "8.34", "sender_batch_id": f"drc-kut-{day:%Y%m%d}",
                "payout_batch_id": "X", "status": status, "created_at": ledger.now_iso()})
        rows = irr.load_actuals(ledger.connect(":memory:"), "kut")
        self.assertEqual(rows, [])                                          # a fresh ledger is empty
        rows = irr.load_actuals(conn, "kut")
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["date"], rows[0]["drc_due"], rows[0]["fee"]),
                         (date(2026, 11, 6), Decimal("8.34"), Decimal("0.17")))

    def test_irr_on_a_few_actual_days_is_not_meaningful(self):
        actual = {"date": date(2026, 11, 6), "drc_due": Decimal("8.34"), "investor_net": Decimal("7.92"),
                  "fee": Decimal("0.17"), "actual": True}
        info = irr.actual_summary(params(cap="23000", investment=20000), [actual])
        self.assertFalse(info["meaningful"])
        self.assertEqual((info["count"], info["span_days"]), (1, 0))
        self.assertIsNone(info["gross_irr"])

    def test_summary_says_when_the_contract_is_not_repaid_in_time(self):
        p = params(cap="100000", term=1)
        projections = [project(p, ["100"]), project(p, ["100"], factor="0.5")]
        projections[0].name, projections[1].name = "Baseline", "-50%"
        lines = irr.build_summary(projections, irr.actual_summary(p, []), p)
        text = "\n".join(lines)
        self.assertIn("NOT fully repaid within 1 months", text)
        self.assertIn("n/a - the data spans 0 day(s)", text)
        # both milestones have their own row, with the amount collected at maturity when not reached
        self.assertIn("Principal recovered (cum. investor_net >= EUR 100.00)", text)
        self.assertIn("Cap reached / contract ends (cum. drc_due = EUR 100,000.00)", text)
        self.assertIn("not reached (EUR 150.00)", text)      # baseline cap: 30 days x 5.00
        self.assertIn("not reached (EUR 75.00)", text)       # -50% cap: 30 days x 2.50
        self.assertIn("not reached (EUR 71.10)", text)       # -50% principal: 30 days x 2.37 investor_net
        self.assertNotIn("not within", text)

    def test_the_multiple_and_the_net_irr_are_shown_together_with_an_explanation(self):
        p = params(cap="115", investment=100)
        proj = project(p, ["10"])
        lines = irr.build_summary([proj], irr.actual_summary(p, []), p)
        text = "\n".join(lines)
        multiple = next(i for i, line in enumerate(lines) if line.startswith("Investor net recovery multiple"))
        net_irr = next(i for i, line in enumerate(lines) if line.startswith("Net IRR"))
        self.assertEqual(net_irr, multiple + 1)                              # adjacent rows
        self.assertIn("the investor's return is the IRR, not the multiple", text)
        self.assertIn("returns 1.081x net over 229 days", text)              # 230 days x 0.47 / 100 = 1.081
        self.assertNotIn("not reached", text)                                # everything was reached

    def test_no_explanation_note_when_nothing_is_repaid(self):
        p = params(cap="100000", term=1)
        text = "\n".join(irr.build_summary([project(p, ["100"])], irr.actual_summary(p, []), p))
        self.assertNotIn("the investor's return is the IRR", text)

    def test_the_chart_is_written(self):
        import tempfile
        p = params(cap="115", investment=100)
        projections = [project(p, ["10"]), project(p, ["10"], factor="0.5")]
        projections[1].name = "-50%"
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "chart.png"
            irr.plot_projection(projections, p, path=path)
            self.assertTrue(path.exists() and path.stat().st_size > 1000)


class WithTheRealContractAndData(unittest.TestCase):
    def setUp(self):
        if not (se.PARAMS_PATH.exists() and irr.VERIFIED_DAILY_PATH.exists()):
            self.skipTest("data/contract_params.json or data/verified_daily.csv not found")
        self.params = se.load_params()
        self.revenues = irr.load_daily_revenues()

    def test_baseline_repays_within_the_term_and_the_deep_stress_does_not(self):
        base = irr.project(self.params, self.revenues, Decimal("1.0"), [])
        stress = irr.project(self.params, self.revenues, Decimal("0.6"), [])
        self.assertIsNotNone(base.cap_reached_on)
        self.assertEqual(base.rows[-1]["cumulative"], Decimal("23000.00"))
        self.assertIsNone(stress.cap_reached_on)
        self.assertLess(stress.rows[-1]["cumulative"], Decimal("23000.00"))
        self.assertLess(stress.rows[-1]["date"], stress.end_date)

    def test_lower_revenue_never_pays_back_sooner(self):
        ends = [irr.project(self.params, self.revenues, f, []).rows[-1]["date"] for f in
                (Decimal("1.0"), Decimal("0.8"), Decimal("0.6"))]
        self.assertLessEqual(ends[0], ends[1])
        self.assertLessEqual(ends[1], ends[2])


if __name__ == "__main__":
    unittest.main()
