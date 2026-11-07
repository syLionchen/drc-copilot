# Unit tests for engine/split_engine.py and engine/ledger.py.
# verified_revenue = verified_net of one Trading Day. No test touches PayPal: payouts are faked,
# the ledger is an in-memory SQLite database.
#
# Run (from the project root):
#   python -m unittest discover -s tests -p "test_split_engine.py" -v
import contextlib
import io
import sys
import unittest
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine import ledger  # noqa: E402
from engine import split_engine as se  # noqa: E402
from paypal import reporting  # noqa: E402

PARAMS = {
    "tiers": [{"up_to": 703.26, "rate": 0.05}, {"up_to": None, "rate": 0.03}],
    "cap_amount": 23000,
    "servicing_fee_rate": 0.05,
    "funding_date": "2026-11-06",
    "max_term_months": 24,
}
MERCHANT = "kut"
ESCROW = "escrow@example.test"
DAY1, DAY2, DAY3 = date(2026, 11, 6), date(2026, 11, 7), date(2026, 11, 8)


def calc(revenue, cumulative="0.00"):
    return se.calculate_split(Decimal(revenue), PARAMS, Decimal(cumulative))


class FakePayouts:
    """Stands in for paypal/payouts.py: PayPal reports the statuses in order and its actual fee."""

    def __init__(self, statuses=("PENDING", "PROCESSING", "SUCCESS"), fee="0.17"):
        self.statuses, self.fee, self.polls, self.created = list(statuses), fee, 0, []

    def create_payout(self, receiver_email, amount, sender_batch_id, note):
        self.created.append((receiver_email, amount, sender_batch_id, note))
        return {"batch_header": {"payout_batch_id": "FAKE123", "batch_status": "PENDING"}}

    def get_payout_status(self, batch_id):
        status = self.statuses[min(self.polls, len(self.statuses) - 1)]
        self.polls += 1
        header = {"payout_batch_id": batch_id, "batch_status": status}
        if status == "SUCCESS":
            header["fees"] = {"value": self.fee, "currency": "EUR"}
        return {"batch_header": header}


def run(conn, day, revenue, payouts, confirm=lambda prompt: True):
    return se.run_settlement(conn, PARAMS, day, Decimal(revenue), MERCHANT, ESCROW, payouts, confirm,
                             sleep=lambda seconds: None, out=lambda *args: None)


def seed(conn, day, drc_due, status="SUCCESS", payout_batch_id="SEED"):
    ledger.insert_settlement(conn, {
        "date": day.isoformat(), "merchant_id": MERCHANT, "verified_revenue": "0.00",
        "rate_applied": "0.000000", "drc_due": drc_due, "payout_amount": drc_due,
        "merchant_payout_fee": "0.00", "servicing_fee": "0.00", "investor_net": drc_due,
        "cumulative_due": drc_due, "sender_batch_id": ledger.make_sender_batch_id(MERCHANT, day),
        "payout_batch_id": payout_batch_id, "status": status, "created_at": ledger.now_iso(),
    })


class ProgressiveTiers(unittest.TestCase):
    def test_examples_from_the_spec(self):
        # 1,000 = 703.26 x 5% + 296.74 x 3% = 35.163 + 8.9022 = 44.0652 -> rounded once -> 44.07
        self.assertEqual(calc("1000").drc_due, Decimal("44.07"))
        self.assertEqual(calc("166.84").drc_due, Decimal("8.34"))      # below the threshold: 5% only
        self.assertEqual(calc("703.26").drc_due, Decimal("35.16"))     # exactly at the threshold
        self.assertEqual(calc("0").drc_due, Decimal("0.00"))

    def test_not_one_flat_rate_for_the_whole_day(self):
        self.assertNotEqual(calc("1000").drc_due, Decimal("50.00"))    # 5% of everything
        self.assertNotEqual(calc("1000").drc_due, Decimal("30.00"))    # 3% of everything

    def test_just_above_the_threshold(self):
        self.assertEqual(calc("703.27").drc_due, Decimal("35.16"))     # 35.163 + 0.0003 = 35.1633

    def test_tier_parts_split_at_the_threshold(self):
        parts = calc("1000").tier_parts
        self.assertEqual([p[0] for p in parts], [Decimal("703.26"), Decimal("296.74")])
        self.assertEqual([p[1] for p in parts], [Decimal("0.05"), Decimal("0.03")])

    def test_rate_applied_is_the_blended_rate(self):
        self.assertEqual(calc("166.84").rate_applied, Decimal("0.050000"))
        self.assertEqual(calc("1000").rate_applied, Decimal("0.044065"))
        self.assertEqual(calc("0").rate_applied, Decimal("0.000000"))

    def test_with_the_saved_contract_params(self):
        if not se.PARAMS_PATH.exists():
            self.skipTest("data/contract_params.json not found")
        params = se.load_params()
        self.assertEqual(se.calculate_split(Decimal("166.84"), params).drc_due, Decimal("8.34"))
        self.assertEqual(se.calculate_split(Decimal("703.26"), params).drc_due, Decimal("35.16"))
        self.assertEqual(se.calculate_split(Decimal("1000"), params).drc_due, Decimal("44.07"))


class FeeRules(unittest.TestCase):
    def test_payout_amount_equals_drc_due(self):
        for revenue in ("166.84", "703.26", "1000"):
            split = calc(revenue)
            self.assertEqual(split.payout_amount, split.drc_due)

    def test_escrow_split(self):
        split = calc("166.84")                                         # drc_due 8.34
        self.assertEqual(split.servicing_fee, Decimal("0.42"))         # 8.34 x 5% = 0.417
        self.assertEqual(split.investor_net, Decimal("7.92"))          # drc_due - servicing_fee
        self.assertEqual(split.servicing_fee + split.investor_net, split.drc_due)

    def test_merchant_total_outlay_is_drc_due_plus_payout_fee(self):
        self.assertEqual(se.merchant_total_outlay("8.34", "0.17"), Decimal("8.51"))

    def test_payout_fee_is_recorded_from_paypal_and_not_counted_toward_the_cap(self):
        conn = ledger.connect(":memory:")
        fake = FakePayouts(fee="0.17")
        row = run(conn, DAY1, "166.84", fake)
        self.assertEqual(row["status"], "SUCCESS")
        self.assertEqual(row["merchant_payout_fee"], "0.17")           # PayPal's actual fee
        self.assertEqual(row["payout_amount"], "8.34")
        self.assertEqual(row["cumulative_due"], "8.34")                # not 8.51
        self.assertEqual(ledger.cumulative_due(conn, MERCHANT), Decimal("8.34"))
        self.assertEqual(fake.created[0][1], Decimal("8.34"))          # the full drc_due is sent


class CapEnforcement(unittest.TestCase):
    def test_last_day_is_cut_to_reach_the_cap_exactly(self):
        split = calc("1000", cumulative="22990.00")                    # only 10.00 left, 44.07 would be due
        self.assertEqual(split.drc_due, Decimal("10.00"))
        self.assertTrue(split.truncated and split.terminates)
        self.assertEqual(split.cumulative_after, Decimal("23000.00"))
        self.assertEqual(split.servicing_fee + split.investor_net, Decimal("10.00"))

    def test_exactly_reaching_the_cap_terminates_without_truncation(self):
        split = calc("1000", cumulative="22955.93")                    # 23,000 - 44.07
        self.assertEqual(split.drc_due, Decimal("44.07"))
        self.assertFalse(split.truncated)
        self.assertTrue(split.terminates)

    def test_normal_day_does_not_terminate(self):
        self.assertFalse(calc("1000", cumulative="100.00").terminates)

    def test_nothing_more_is_due_once_the_cap_is_reached(self):
        with self.assertRaises(se.ContractTerminated):
            calc("1000", cumulative="23000.00")

    def test_termination_record_is_written_and_later_days_are_refused(self):
        conn = ledger.connect(":memory:")
        seed(conn, DAY1, "22990.00")
        row = run(conn, DAY2, "1000", FakePayouts())
        self.assertEqual(row["drc_due"], "10.00")
        self.assertEqual(row["cumulative_due"], "23000.00")
        self.assertIsNotNone(ledger.get_termination(conn, MERCHANT))
        fake = FakePayouts()
        with self.assertRaises(se.ContractTerminated):
            run(conn, DAY3, "1000", fake)
        self.assertEqual(fake.created, [])


class Idempotency(unittest.TestCase):
    def test_second_run_for_the_same_day_is_refused_before_paypal_is_called(self):
        conn = ledger.connect(":memory:")
        fake = FakePayouts()
        run(conn, DAY1, "166.84", fake)
        with self.assertRaises(se.AlreadySettled):
            run(conn, DAY1, "166.84", fake)
        self.assertEqual(len(fake.created), 1)
        self.assertEqual(len(ledger.list_settlements(conn, MERCHANT)), 1)

    def test_sender_batch_id_format_and_uniqueness(self):
        self.assertEqual(ledger.make_sender_batch_id(MERCHANT, DAY1), "drc-kut-20261106")
        conn = ledger.connect(":memory:")
        seed(conn, DAY1, "8.34")
        with self.assertRaises(ledger.DuplicateSettlement):
            seed(conn, DAY1, "8.34")

    def test_declined_confirmation_pays_and_records_nothing(self):
        conn = ledger.connect(":memory:")
        fake = FakePayouts()
        with self.assertRaises(se.PayoutNotConfirmed):
            run(conn, DAY1, "166.84", fake, confirm=lambda prompt: False)
        self.assertEqual(fake.created, [])
        self.assertIsNone(ledger.get_by_batch_id(conn, ledger.make_sender_batch_id(MERCHANT, DAY1)))

    def test_zero_revenue_day_records_no_payout_and_is_closed(self):
        conn = ledger.connect(":memory:")
        fake = FakePayouts()
        row = run(conn, DAY1, "0", fake)
        self.assertEqual((row["status"], row["drc_due"]), ("NO_PAYOUT", "0.00"))
        self.assertEqual(fake.created, [])
        with self.assertRaises(se.AlreadySettled):
            run(conn, DAY1, "0", fake)

    def test_only_success_counts_toward_the_cumulative_total(self):
        conn = ledger.connect(":memory:")
        run(conn, DAY1, "166.84", FakePayouts(statuses=("PENDING", "DENIED")))
        row = ledger.get_by_batch_id(conn, ledger.make_sender_batch_id(MERCHANT, DAY1))
        self.assertEqual(row["status"], "DENIED")
        self.assertEqual(ledger.cumulative_due(conn, MERCHANT), Decimal("0.00"))
        self.assertIsNone(row["merchant_payout_fee"])
        with self.assertRaises(se.UnresolvedAttempt):                  # PayPal would reject the same batch id
            run(conn, DAY1, "166.84", FakePayouts())

    def test_unfinished_payout_is_resumed_without_paying_again(self):
        conn = ledger.connect(":memory:")
        seed(conn, DAY1, "8.34", status="PROCESSING", payout_batch_id="FAKE1")
        fake = FakePayouts(statuses=("SUCCESS",), fee="0.17")
        row = run(conn, DAY1, "166.84", fake)
        self.assertEqual((row["status"], row["merchant_payout_fee"]), ("SUCCESS", "0.17"))
        self.assertEqual(fake.created, [])


class ContractTermAndOrder(unittest.TestCase):
    def test_before_funding_date_and_after_the_term_are_refused(self):
        conn = ledger.connect(":memory:")
        with self.assertRaises(se.OutsideContractTerm):
            se.plan_settlement(conn, MERCHANT, date(2026, 11, 5), Decimal("100"), PARAMS)
        with self.assertRaises(se.OutsideContractTerm):
            se.plan_settlement(conn, MERCHANT, date(2028, 11, 6), Decimal("100"), PARAMS)
        se.plan_settlement(conn, MERCHANT, date(2028, 11, 5), Decimal("100"), PARAMS)   # last day inside 24 months

    def test_days_must_be_settled_in_order(self):
        conn = ledger.connect(":memory:")
        seed(conn, DAY2, "10.00")
        with self.assertRaises(se.OutOfOrder):
            run(conn, DAY1, "166.84", FakePayouts())

    def test_add_months(self):
        self.assertEqual(se.add_months(date(2026, 11, 6), 24), date(2028, 11, 6))
        self.assertEqual(se.add_months(date(2026, 1, 31), 1), date(2026, 2, 28))


def live_row(transaction_id, gross, fee, payer_id=None, list_price=None):
    """A customer-payment row as paypal/reporting.py returns it."""
    row = {"transaction_id": transaction_id, "time_berlin": datetime(2026, 11, 6, 12, 0, 0),
           "payer_name": f"Buyer {transaction_id}", "payer_id": payer_id, "gross": Decimal(gross),
           "fee": Decimal(fee), "net": Decimal(gross) - Decimal(fee), "currency": "EUR", "event_code": "T0006"}
    if list_price is not None:
        row["list_price"] = Decimal(list_price)
    return row


# four ordinary payments: gross 171.50, fees 4.66, net 166.84 (the real 2026-11-06 sandbox day)
NORMAL = [live_row("N1", "12.50", "0.59", "P1"), live_row("N2", "8.40", "0.51", "P2"),
          live_row("N3", "100.00", "2.25", "P3"), live_row("N4", "50.60", "1.31", "P4")]


class FakeReporting:
    """Stands in for paypal.reporting: serves the given rows; daily_summary is the real, pure one."""

    def __init__(self, rows, excluded=()):
        self.rows, self.excluded, self.calls = rows, list(excluded), 0

    def fetch_trading_day_detailed(self, day):
        self.calls += 1
        return list(self.rows), list(self.excluded)

    daily_summary = staticmethod(reporting.daily_summary)


def gather(rows, **kwargs):
    lines = []
    revenue = se.gather_revenue(DAY1, reporting_module=FakeReporting(rows), out=lines.append, **kwargs)
    return revenue, lines


class RevenueSource(unittest.TestCase):
    def test_manual_revenue_is_used_as_is_and_reporting_is_not_called(self):
        fake = FakeReporting(NORMAL)
        revenue = se.gather_revenue(DAY1, manual_revenue=Decimal("166.84"), reporting_module=fake,
                                    out=lambda *args: None)
        self.assertEqual((revenue.net, revenue.source), (Decimal("166.84"), "manual --revenue"))
        self.assertEqual(fake.calls, 0)

    def test_without_manual_revenue_the_net_comes_from_transaction_search(self):
        revenue, lines = gather(NORMAL)
        self.assertEqual(revenue.net, Decimal("166.84"))
        self.assertEqual(revenue.source, "PayPal Transaction Search (4 transactions)")
        text = "\n".join(lines)
        self.assertIn("clean: 4", text)
        self.assertIn("under review (counted): 0", text)
        self.assertIn("excluded (removed): 0", text)
        self.assertEqual(se.calculate_split(revenue.net, PARAMS).drc_due, Decimal("8.34"))

    def test_one_transaction_uses_the_singular(self):
        revenue, _ = gather(NORMAL[:1])
        self.assertEqual(revenue.source, "PayPal Transaction Search (1 transaction)")

    def test_excluded_transactions_are_removed_from_verified_net(self):
        micro = [live_row(f"M{i}", "0.80", "0.36", "FAKE1") for i in range(5)]     # self-dealing pattern
        revenue, lines = gather(NORMAL + micro)
        self.assertEqual(revenue.net, Decimal("166.84"))             # the 5 micro orders are gone
        self.assertEqual(revenue.source, "PayPal Transaction Search (9 transactions)")
        text = "\n".join(lines)
        self.assertIn("excluded (removed): 5", text)
        self.assertEqual(text.count("EXCLUDED (removed from verified_net)"), 5)
        self.assertIn("SELF_DEALING_SUSPECT", text)

    def test_four_small_orders_from_one_payer_are_not_enough_to_exclude(self):
        revenue, _ = gather([live_row(f"S{i}", "0.80", "0.36", "SAME") for i in range(4)])
        self.assertEqual(revenue.net, Decimal("1.76"))               # 4 x 0.44

    def test_under_review_is_counted_but_printed(self):
        suspicious = live_row("UR1", "8.00", "0.50", "P9", list_price="20.00")    # 60 % below list price
        revenue, lines = gather(NORMAL + [suspicious])
        self.assertEqual(revenue.net, Decimal("166.84") + Decimal("7.50"))        # still counted
        text = "\n".join(lines)
        self.assertIn("under review (counted): 1", text)
        self.assertIn("UNDER REVIEW (counted in verified_net): UR1", text)
        self.assertIn("PRICE_OVERRIDE_DEEP", text)

    def test_no_transactions_is_refused_as_possible_data_delay(self):
        with self.assertRaises(se.NoTransactionsFound) as ctx:
            gather([])
        self.assertIn("data delay", str(ctx.exception))

    def test_a_zero_day_can_be_accepted_explicitly(self):
        revenue, _ = gather([], allow_zero_day=True)
        self.assertEqual((revenue.net, revenue.source), (Decimal("0.00"), "PayPal Transaction Search (0 transactions)"))

    def test_dry_run_only_warns_about_an_empty_day(self):
        revenue, lines = gather([], dry_run=True)
        self.assertEqual(revenue.net, Decimal("0.00"))
        self.assertTrue(any(line.startswith("WARNING") and "data delay" in line for line in lines))


class ConfirmationPrompt(unittest.TestCase):
    def test_mask_email(self):
        self.assertEqual(se.mask_email("sb-q2scf53542513@business.example.com"), "sb-q2scf…@business.example.com")
        self.assertEqual(se.mask_email("anna@example.test"), "anna@example.test")
        self.assertEqual(se.mask_email("not-an-email"), "not-an-email")

    def test_prompt_is_english_and_masks_the_escrow_address(self):
        prompts = []
        se.run_settlement(ledger.connect(":memory:"), PARAMS, DAY1, Decimal("166.84"), MERCHANT,
                          "sb-q2scf53542513@business.example.com", FakePayouts(),
                          confirm=lambda prompt: prompts.append(prompt) or True,
                          sleep=lambda seconds: None, out=lambda *args: None)
        self.assertEqual(prompts, ["Pay EUR 8.34 to escrow account sb-q2scf…@business.example.com? "
                                   "Type YES to confirm: "])

    def test_source_is_printed_with_the_calculation(self):
        lines = []
        se.run_settlement(ledger.connect(":memory:"), PARAMS, DAY1, Decimal("166.84"), MERCHANT, ESCROW,
                          FakePayouts(), confirm=lambda prompt: True, sleep=lambda seconds: None,
                          out=lines.append, source="manual --revenue")
        self.assertIn("  source: manual --revenue", lines)

    def test_all_engine_output_is_ascii_english(self):
        lines = []
        se.run_settlement(ledger.connect(":memory:"), PARAMS, DAY1, Decimal("166.84"), MERCHANT, ESCROW,
                          FakePayouts(), confirm=lambda prompt: True, sleep=lambda seconds: None, out=lines.append)
        for line in lines:
            self.assertTrue(line.isascii(), line)


class CommandLine(unittest.TestCase):
    def setUp(self):
        if not se.PARAMS_PATH.exists():
            self.skipTest("data/contract_params.json not found")

    def main(self, argv, **kwargs):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = se.main(argv, **kwargs)
        return code, buffer.getvalue()

    def test_dry_run_with_manual_revenue(self):
        code, text = self.main(["--date", "2026-11-06", "--revenue", "166.84", "--dry-run", "--db", ":memory:"])
        self.assertEqual(code, 0)
        self.assertIn("source: manual --revenue", text)
        self.assertIn("EUR 8.34", text)
        self.assertIn("DRY RUN", text)

    def test_dry_run_reads_transaction_search_when_no_revenue_is_given(self):
        fake = FakeReporting(NORMAL)
        code, text = self.main(["--date", "2026-11-06", "--dry-run", "--db", ":memory:"], reporting_module=fake)
        self.assertEqual(code, 0)
        self.assertEqual(fake.calls, 1)
        self.assertIn("source: PayPal Transaction Search (4 transactions)", text)
        self.assertIn("Anomaly screening", text)
        self.assertIn("EUR 8.34", text)

    def test_a_manual_revenue_never_calls_transaction_search(self):
        fake = FakeReporting(NORMAL)
        self.main(["--date", "2026-11-06", "--revenue", "1000", "--dry-run", "--db", ":memory:"],
                  reporting_module=fake)
        self.assertEqual(fake.calls, 0)


if __name__ == "__main__":
    unittest.main()
