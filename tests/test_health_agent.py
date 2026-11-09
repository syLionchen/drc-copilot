# Tests for engine/health_agent.py. No API call, no network, nothing outside a temporary folder.
#
# Run (from the project root):
#   python -m unittest tests.test_health_agent -v
import json
import sys
import tempfile
import unittest
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine import health_agent as ha  # noqa: E402

PARAMS = {"health_trigger_drop_pct": 20, "health_window_short_days": 7, "health_window_long_days": 30,
          "reduced_rate": 0.03, "reduced_rate_max_days": 30, "requires_investor_approval": True,
          "tiers": [{"up_to": 703.26, "rate": 0.05}, {"up_to": None, "rate": 0.03}],
          "baseline_threshold_amount": 703.26}
START = date(2026, 9, 1)


def days(values, start=START):
    return [(start + timedelta(days=i), Decimal(str(v))) for i, v in enumerate(values)]


def terms(short=7, long_=30):
    return ha.health_terms({**PARAMS, "health_window_short_days": short, "health_window_long_days": long_})


def series_with_decline(decline):
    """10 days, windows 5/10: ma_long is exactly 100 and ma_short is exactly 100 - decline."""
    short_mean = Decimal(100) - Decimal(str(decline))
    return days([Decimal(200) - short_mean] * 5 + [short_mean] * 5)


class TriggerThreshold(unittest.TestCase):
    def check(self, decline):
        series = series_with_decline(decline)
        return ha.compute_metrics(series, series[-1][0], terms(5, 10))

    def test_a_fall_of_19_9_percent_does_not_trigger(self):
        m = self.check("19.9")
        self.assertEqual(m["decline_pct"], Decimal("19.9"))
        self.assertFalse(m["triggered"])

    def test_a_fall_of_20_1_percent_triggers(self):
        self.assertTrue(self.check("20.1")["triggered"])

    def test_exactly_the_threshold_does_not_trigger(self):          # "more than 20%"
        self.assertFalse(self.check("20")["triggered"])

    def test_the_trigger_uses_the_exact_value_not_the_rounded_one(self):
        series = days([Decimal("120")] * 5 + [Decimal("79.996")] * 5)   # fall 20.002%, displays as 20.00
        m = ha.compute_metrics(series, series[-1][0], terms(5, 10))
        self.assertEqual(ha.q2(m["decline_pct"]), Decimal("20.00"))
        self.assertTrue(m["triggered"])

    def test_threshold_and_windows_come_from_the_contract(self):
        with self.assertRaises(ha.HealthError):
            ha.health_terms({k: v for k, v in PARAMS.items() if k != "health_trigger_drop_pct"})
        t = ha.health_terms({**PARAMS, "health_trigger_drop_pct": 30})
        self.assertFalse(ha.compute_metrics(series_with_decline("25"), date(2026, 9, 10), {**t, "health_window_short_days": 5, "health_window_long_days": 10})["triggered"])

    def test_the_threshold_is_shown_without_an_exponent(self):
        series = series_with_decline("25")
        shown = ha.display_metrics(ha.compute_metrics(series, series[-1][0], terms(5, 10)), terms(5, 10))
        self.assertEqual(shown["trigger_decline_pct"], "20")
        self.assertEqual(ha.plain_terms(terms())["reduced_rate"], "0.03")

    def test_not_enough_history_gives_no_metrics(self):
        series = days([100] * 29)
        self.assertIsNone(ha.compute_metrics(series, series[-1][0], terms()))

    def test_scan_lists_the_triggering_days(self):
        series = days([100] * 30 + [40] * 7)
        triggered = [m["as_of"] for m in ha.scan(series, terms()) if m["triggered"]]
        self.assertTrue(triggered)
        self.assertEqual(triggered[-1], series[-1][0])
        self.assertNotIn(series[29][0], triggered)

    def test_unknown_day_is_an_error(self):
        with self.assertRaises(ha.HealthError):
            ha.compute_metrics(days([100] * 30), date(2030, 1, 1), terms())


def sample_inputs():
    series = days([100 + i for i in range(23)] + [60, 61, 62, 63, 64, 65, 66])
    metrics = ha.compute_metrics(series, series[-1][0], terms())
    note = "SAMPLE. The road works started on 20 September and will take about 6 weeks."
    return ha.build_inputs(series, metrics, terms(), note), metrics


def good_recommendation(inputs):
    m = inputs["metrics"]
    return {
        "recommendation": "approve_reduced_rate", "suggested_duration_days": 30,
        "conditions": ["Review every week; restore the normal rate when the short mean is back near the long mean."],
        "reasons": [
            {"reason": "Revenue fell below the trigger.",
             "evidence": f"The 7-day mean is {m['ma_short']} against a 30-day mean of {m['ma_long']}, "
                         f"a fall of {m['decline_pct']}% (trigger: more than {m['trigger_decline_pct']}%)."},
            {"reason": "The merchant's claim fits the dates.",
             "evidence": f"The lowest day is {m['lowest_day']} with {m['lowest_day_net']}; the note says about 6 weeks."}],
        "risks": ["The works may take longer than the maximum reduced-rate period."],
        "confidence": "medium",
    }


class NumberCheck(unittest.TestCase):
    def test_numbers_and_dates_are_extracted(self):
        numbers, dates = ha.numbers_in("EUR 1,234.50 on 2026-10-20 (-19.9%), 7-day mean")
        self.assertEqual(numbers, [Decimal("1234.50"), Decimal("19.9"), Decimal("7")])
        self.assertEqual(dates, ["2026-10-20"])

    def test_a_correct_recommendation_is_valid(self):
        inputs, _ = sample_inputs()
        self.assertEqual(ha.validate_recommendation(good_recommendation(inputs), inputs, terms()), [])

    def test_rounding_difference_of_one_cent_is_allowed(self):
        inputs, _ = sample_inputs()
        rec = good_recommendation(inputs)
        rec["reasons"][0]["evidence"] = f"The 7-day mean is {Decimal(inputs['metrics']['ma_short']) + Decimal('0.01')}."
        self.assertEqual(ha.validate_recommendation(rec, inputs, terms()), [])

    def test_an_invented_number_is_caught(self):
        inputs, _ = sample_inputs()
        rec = good_recommendation(inputs)
        rec["reasons"][0]["evidence"] += " Weekend takings were EUR 1,234.56."
        problems = ha.validate_recommendation(rec, inputs, terms())
        self.assertTrue(any("1234.56" in p and "reason 1" in p for p in problems), problems)

    def test_a_number_off_by_more_than_a_cent_is_caught(self):
        inputs, _ = sample_inputs()
        rec = good_recommendation(inputs)
        rec["reasons"][0]["evidence"] = f"The 7-day mean is {Decimal(inputs['metrics']['ma_short']) + Decimal('0.05')}."
        self.assertTrue(ha.validate_recommendation(rec, inputs, terms()))

    def test_an_invented_date_is_caught(self):
        inputs, _ = sample_inputs()
        rec = good_recommendation(inputs)
        rec["reasons"][1]["evidence"] += " Revenue first dropped on 2026-01-01."
        problems = ha.validate_recommendation(rec, inputs, terms())
        self.assertTrue(any("2026-01-01" in p for p in problems), problems)

    def test_numbers_from_the_merchant_note_may_be_quoted(self):
        inputs, _ = sample_inputs()
        rec = good_recommendation(inputs)
        rec["reasons"][1]["evidence"] = "The merchant says the works started on 20 September."
        self.assertEqual(ha.validate_recommendation(rec, inputs, terms()), [])

    def test_a_reason_without_evidence_is_invalid(self):
        inputs, _ = sample_inputs()
        rec = good_recommendation(inputs)
        rec["reasons"][0]["evidence"] = ""
        self.assertTrue(ha.validate_recommendation(rec, inputs, terms()))


class DurationLimit(unittest.TestCase):
    def test_a_duration_above_the_contract_limit_is_rejected(self):
        inputs, _ = sample_inputs()
        rec = good_recommendation(inputs)
        rec["suggested_duration_days"] = 31
        problems = ha.validate_recommendation(rec, inputs, terms())
        self.assertTrue(any("31" in p and "30" in p for p in problems), problems)

    def test_the_limit_itself_is_allowed(self):
        inputs, _ = sample_inputs()
        rec = good_recommendation(inputs)
        rec["suggested_duration_days"] = 30
        self.assertEqual(ha.validate_recommendation(rec, inputs, terms()), [])

    def test_the_limit_is_read_from_the_contract(self):
        inputs, _ = sample_inputs()
        rec = good_recommendation(inputs)
        rec["suggested_duration_days"] = 20
        short_cap = ha.health_terms({**PARAMS, "reduced_rate_max_days": 14})
        self.assertTrue(ha.validate_recommendation(rec, inputs, short_cap))

    def test_approving_for_zero_days_or_a_wrong_type_is_rejected(self):
        inputs, _ = sample_inputs()
        for bad in (0, -1, "30", 12.5, True):
            rec = good_recommendation(inputs)
            rec["suggested_duration_days"] = bad
            self.assertTrue(ha.validate_recommendation(rec, inputs, terms()), bad)

    def test_unknown_recommendation_or_confidence_is_rejected(self):
        inputs, _ = sample_inputs()
        rec = good_recommendation(inputs)
        rec["recommendation"], rec["confidence"] = "pay_out_now", "certain"
        self.assertEqual(len(ha.validate_recommendation(rec, inputs, terms())), 2)

    def test_recommend_uses_the_injected_call_and_validates(self):
        inputs, _ = sample_inputs()
        bad = good_recommendation(inputs)
        bad["suggested_duration_days"] = 99
        usage = {"api_calls": 1, "input_tokens": 1, "output_tokens": 1, "api_seconds": 0.1}
        rec, got_usage, problems = ha.recommend(inputs, terms(), call=lambda system, tool, prompt: (bad, usage))
        self.assertIs(rec, bad)
        self.assertTrue(problems)

    def test_the_prompt_carries_the_contract_terms_and_note_in_readable_labels(self):
        inputs, _ = sample_inputs()
        text = ha.readable_inputs(inputs)
        m = inputs["metrics"]
        for expected in (f"7-day average: EUR {m['ma_short']}", f"30-day average: EUR {m['ma_long']}",
                         f"Fall vs 30-day average: {m['decline_pct']}%", "Baseline threshold: EUR 703.26",
                         "Reduced rate: 3%", "Maximum reduced-rate period: 30 days", "road works"):
            self.assertIn(expected, text)
        self.assertIn("5% up to EUR 703.26 a day, 3% on the rest", text)
        self.assertIn(text, ha.build_prompt(inputs))
        self.assertEqual(len(inputs["daily_verified_net"]), 30)

    def test_the_readable_input_has_no_internal_field_names_or_decimal_rates(self):
        inputs, _ = sample_inputs()
        text = ha.readable_inputs(inputs)
        self.assertEqual(ha._FIELD_NAME.findall(text), [])
        self.assertNotIn("0.03", text)
        self.assertNotIn("0.05", text)

    def test_every_number_shown_to_claude_can_be_quoted(self):
        """The 23 in 'the 23 days before the last 7' is shown to Claude, so quoting it must be valid."""
        inputs, _ = sample_inputs()
        self.assertIn("Average of the 23 days before the last 7", ha.readable_inputs(inputs))
        rec = good_recommendation(inputs)
        rec["reasons"][0]["evidence"] = (f"The last 7 days are {inputs['metrics']['short_vs_prior_decline_pct']}% below "
                                         f"the average of EUR {inputs['metrics']['ma_prior_days']} for the 23 days before them.")
        self.assertEqual(ha.validate_recommendation(rec, inputs, terms()), [])
        rec["reasons"][0]["evidence"] = "It is based on the 24 days before them."
        self.assertTrue(ha.validate_recommendation(rec, inputs, terms()))

    def test_the_number_check_still_uses_the_raw_numbers(self):
        inputs, _ = sample_inputs()
        rec = good_recommendation(inputs)
        rec["reasons"][0]["evidence"] = "The reduced rate is 3%, the baseline threshold EUR 703.26 and the period 30 days."
        self.assertEqual(ha.validate_recommendation(rec, inputs, terms()), [])


class InternalFieldNames(unittest.TestCase):
    def test_ma_short_makes_the_recommendation_invalid(self):
        inputs, _ = sample_inputs()
        rec = good_recommendation(inputs)
        rec["reasons"][0]["evidence"] = f"The ma_short is {inputs['metrics']['ma_short']}."
        problems = ha.validate_recommendation(rec, inputs, terms())
        self.assertTrue(any("ma_short" in p and "evidence of reason 1" in p for p in problems), problems)

    def test_every_text_of_the_answer_is_scanned_and_the_word_is_named(self):
        inputs, _ = sample_inputs()
        for field, name in (("conditions", "reduced_rate_max_days"), ("risks", "baseline_threshold_amount")):
            rec = good_recommendation(inputs)
            rec[field] = [f"Check {name} every week."]
            problems = ha.validate_recommendation(rec, inputs, terms())
            self.assertTrue(any(name in p for p in problems), (field, problems))
        rec = good_recommendation(inputs)
        rec["reasons"][1]["reason"] = "The lowest_day is confirmed."
        self.assertTrue(any("lowest_day" in p for p in ha.validate_recommendation(rec, inputs, terms())))

    def test_plain_english_and_dates_are_not_flagged(self):
        inputs, _ = sample_inputs()
        self.assertEqual(ha.internal_names(good_recommendation(inputs)), [])
        self.assertEqual(ha.internal_names({"conditions": ["Restore 5% when the 7-day average is back (2026-10-20)."]}), [])


class ScenarioFile(unittest.TestCase):
    def test_make_scenario_copies_cuts_and_labels(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            src = folder / "verified_daily.csv"
            src.write_text("date,verified_net\n" + "".join(
                f"{(START + timedelta(days=i)).isoformat()},100.00\n" for i in range(70)), encoding="utf-8")
            before = src.read_bytes()
            dst = ha.make_scenario(src, folder / "health" / "dip.csv")
            self.assertEqual(src.read_bytes(), before)                # the original is untouched
            series, info = ha.load_series(dst)
            self.assertTrue(info["simulated"])
            self.assertIn("SIMULATED SCENARIO", dst.read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(len(series), 60)
            self.assertEqual(series[0][1], Decimal("100.00"))
            self.assertEqual(series[-8][1], Decimal("100.00"))
            self.assertEqual({v for _, v in series[-7:]}, {Decimal("65.00")})
            self.assertTrue(ha.compute_metrics(series, series[-1][0], terms())["triggered"])

    def test_the_size_of_the_dip_is_read_from_the_scenario_file(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            src = folder / "verified_daily.csv"
            src.write_text("date,verified_net\n" + "".join(
                f"{(START + timedelta(days=i)).isoformat()},{100 + i}\n" for i in range(70)), encoding="utf-8")
            dst = ha.make_scenario(src, folder / "dip.csv")
            self.assertEqual(ha.scenario_dip(dst), {"dip_pct": "35", "dip_days": 7})
            self.assertIsNone(ha.scenario_dip(src))                      # an ordinary file has no dip

    def test_base_history_summary_reports_days_triggers_and_the_largest_fall(self):
        with tempfile.TemporaryDirectory() as folder:
            flat = Path(folder) / "flat.csv"
            flat.write_text("date,verified_net\n" + "".join(
                f"{(START + timedelta(days=i)).isoformat()},{100 if i < 33 else 90}\n" for i in range(40)), encoding="utf-8")
            summary = ha.base_history_summary(terms(), flat)
        self.assertEqual(summary["days"], 40)
        self.assertEqual(summary["triggered_days"], 0)
        self.assertEqual(summary["largest_fall_date"], (START + timedelta(days=39)).isoformat())
        self.assertEqual(summary["largest_fall_pct"], "7.85")        # 7-day mean 90 vs 30-day mean 97.67

    def test_the_original_file_cannot_be_overwritten(self):
        with self.assertRaises(ha.HealthError):
            ha.make_scenario(ha.VERIFIED_DAILY_PATH, ha.VERIFIED_DAILY_PATH)

    def test_the_real_data_loads(self):
        if not ha.VERIFIED_DAILY_PATH.exists():
            self.skipTest("data/verified_daily.csv not found")
        series, info = ha.load_series(ha.VERIFIED_DAILY_PATH)
        self.assertFalse(info["simulated"])
        self.assertEqual(len(series), 90)


class OnlyRecommends(unittest.TestCase):
    def test_a_run_writes_only_the_recommendation_file(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            params_file = folder / "contract_params.json"
            params_file.write_text(json.dumps({"params": PARAMS}), encoding="utf-8")
            params_before = params_file.read_bytes()
            scenario = folder / "dip.csv"
            scenario.write_text("# SIMULATED SCENARIO\ndate,verified_net\n" + "".join(
                f"{(START + timedelta(days=i)).isoformat()},{100 if i < 53 else 40}\n" for i in range(60)),
                encoding="utf-8")
            note = folder / "note.txt"
            note.write_text("SAMPLE note: the road works take about 6 weeks.", encoding="utf-8")
            out = folder / "out" / "recommendation.json"
            usage = {"api_calls": 1, "input_tokens": 1000, "output_tokens": 200, "api_seconds": 3.0}

            def fake_recommend(inputs, terms_, call=None):
                return good_recommendation(inputs), usage, []

            with mock.patch.object(ha, "recommend", side_effect=fake_recommend):
                code = ha.main(["--scenario", str(scenario), "--note", str(note), "--params", str(params_file),
                                "--out", str(out)])
            self.assertEqual(code, 0)
            self.assertEqual(params_file.read_bytes(), params_before)           # contract parameters unchanged
            self.assertEqual(sorted(p.name for p in folder.rglob("*") if p.is_file()),
                             sorted(["contract_params.json", "dip.csv", "note.txt", "recommendation.json"]))
            record = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(record["status"], "VALID")
            self.assertTrue(record["simulated_scenario"])
            self.assertEqual(record["notice"], ha.NOTICE)
            self.assertEqual(record["meta"]["input_tokens"], 1000)
            self.assertNotIn(str(folder), json.dumps(record))                   # no absolute paths in the file

    def test_an_invalid_recommendation_is_saved_marked_invalid(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            params_file = folder / "contract_params.json"
            params_file.write_text(json.dumps({"params": PARAMS}), encoding="utf-8")
            scenario = folder / "dip.csv"
            scenario.write_text("# SIMULATED SCENARIO\ndate,verified_net\n" + "".join(
                f"{(START + timedelta(days=i)).isoformat()},{100 if i < 53 else 40}\n" for i in range(60)),
                encoding="utf-8")
            out = folder / "recommendation.json"
            usage = {"api_calls": 1, "input_tokens": 1, "output_tokens": 1, "api_seconds": 0.1}

            def fake_recommend(inputs, terms_, call=None):
                rec = good_recommendation(inputs)
                rec["suggested_duration_days"] = 45
                return rec, usage, ha.validate_recommendation(rec, inputs, terms_)

            with mock.patch.object(ha, "recommend", side_effect=fake_recommend):
                code = ha.main(["--scenario", str(scenario), "--params", str(params_file), "--out", str(out)])
            self.assertEqual(code, 2)
            record = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(record["status"], "INVALID")
            self.assertTrue(record["problems"])

    def test_nothing_is_asked_when_the_trigger_does_not_fire(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            params_file = folder / "contract_params.json"
            params_file.write_text(json.dumps({"params": PARAMS}), encoding="utf-8")
            flat = folder / "flat.csv"
            flat.write_text("date,verified_net\n" + "".join(
                f"{(START + timedelta(days=i)).isoformat()},100\n" for i in range(40)), encoding="utf-8")
            out = folder / "recommendation.json"
            with mock.patch.object(ha, "recommend", side_effect=AssertionError("must not be called")):
                code = ha.main(["--scenario", str(flat), "--params", str(params_file), "--out", str(out)])
            self.assertEqual(code, 0)
            self.assertFalse(out.exists())


if __name__ == "__main__":
    unittest.main()
