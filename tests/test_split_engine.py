# Unit tests for engine/split_engine.py and engine/ledger.py.
# verified_revenue = verified_net of one Trading Day. No test touches PayPal: payouts are faked,
# the ledger is an in-memory SQLite database.
#
# Run (from the project root):
#   python -m unittest discover -s tests -p "test_split_engine.py" -v
import sys
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine import ledger  # noqa: E402
from engine import split_engine as se  # noqa: E402

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


if __name__ == "__main__":
    unittest.main()
