# Tests for paypal/reporting.py. No network: the API is replaced by the mock responses in
# tests/fixtures/ (invented data, two pages), injected through the `request` argument.
#
# Run (from the project root):
#   python -m unittest discover -s tests -p "test_reporting.py" -v
import copy
import json
import sys
import unittest
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from paypal import client, reporting  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
PAGES = [json.loads((FIXTURES / name).read_text(encoding="utf-8"))
         for name in ("transactions_page1.json", "transactions_page2.json")]
DAY = date(2026, 11, 6)
NEXT_DAY = date(2026, 11, 7)


class FakeRequest:
    """Stands in for paypal.client.request: serves the fixture page asked for in params['page']."""

    def __init__(self, pages=None, error=None):
        self.pages, self.error, self.calls = pages if pages is not None else PAGES, error, []

    def __call__(self, method, path, json=None, params=None, headers=None):
        self.calls.append((method, path, dict(params)))
        if self.error:
            raise self.error
        return copy.deepcopy(self.pages[params["page"] - 1])


def ids(rows):
    return [r["transaction_id"] for r in rows]


class TradingDayWindow(unittest.TestCase):
    def test_berlin_day_in_winter_time_is_utc_minus_one_hour(self):
        start, end = reporting.trading_day_window(DAY)
        self.assertEqual(start.strftime("%Y-%m-%d %H:%M"), "2026-11-05 23:00")
        self.assertEqual(end.strftime("%Y-%m-%d %H:%M"), "2026-11-06 23:00")

    def test_daylight_saving_days_are_23_and_25_hours_long(self):
        start, end = reporting.trading_day_window(date(2026, 3, 29))     # clocks go forward
        self.assertEqual((end - start).total_seconds() / 3600, 23)
        start, end = reporting.trading_day_window(date(2026, 10, 25))    # clocks go back
        self.assertEqual((end - start).total_seconds() / 3600, 25)

    def test_api_receives_the_utc_window(self):
        fake = FakeRequest()
        reporting.fetch_trading_day(DAY, request=fake)
        method, path, params = fake.calls[0]
        self.assertEqual((method, path), ("GET", "/v1/reporting/transactions"))
        self.assertEqual(params["start_date"], "2026-11-05T23:00:00+0000")
        self.assertEqual(params["end_date"], "2026-11-06T23:00:00+0000")
        self.assertEqual((params["fields"], params["page_size"]), ("all", 500))


class TimezoneBoundary(unittest.TestCase):
    def test_2359_and_0001_berlin_belong_to_different_trading_days(self):
        day1, day2 = ids(reporting.fetch_trading_day(DAY, request=FakeRequest())), \
            ids(reporting.fetch_trading_day(NEXT_DAY, request=FakeRequest()))
        self.assertIn("TESTTXN0001", day1)        # Berlin 2026-11-06 23:59:30
        self.assertNotIn("TESTTXN0001", day2)
        self.assertIn("TESTTXN0002", day2)        # Berlin 2026-11-07 00:01:00
        self.assertNotIn("TESTTXN0002", day1)

    def test_midnight_belongs_to_the_day_that_starts_there(self):
        day1, day2 = ids(reporting.fetch_trading_day(DAY, request=FakeRequest())), \
            ids(reporting.fetch_trading_day(NEXT_DAY, request=FakeRequest()))
        self.assertIn("TESTTXN0003", day1)        # Berlin 2026-11-06 00:00:00 (start is inclusive)
        self.assertIn("TESTTXN0004", day2)        # Berlin 2026-11-07 00:00:00 (end of the day before is exclusive)
        self.assertNotIn("TESTTXN0004", day1)

    def test_time_berlin_is_a_berlin_local_time(self):
        rows = {r["transaction_id"]: r for r in reporting.fetch_trading_day(DAY, request=FakeRequest())}
        self.assertEqual(rows["TESTTXN0005"]["time_berlin"].strftime("%Y-%m-%d %H:%M:%S"), "2026-11-06 08:15:00")
        self.assertEqual(rows["TESTTXN0001"]["time_berlin"].strftime("%H:%M:%S"), "23:59:30")

    def test_rows_are_sorted_by_time(self):
        rows = reporting.fetch_trading_day(DAY, request=FakeRequest())
        self.assertEqual(ids(rows), ["TESTTXN0003", "TESTTXN0005", "TESTTXN0006", "TESTTXN0001"])


class Filtering(unittest.TestCase):
    def test_payouts_refunds_and_negative_amounts_are_excluded(self):
        everything = ids(reporting.fetch_trading_day(DAY, request=FakeRequest())) + \
            ids(reporting.fetch_trading_day(NEXT_DAY, request=FakeRequest()))
        self.assertNotIn("TESTPAYOUT01", everything)     # the merchant's own payout, -8.34
        self.assertNotIn("TESTREFUND01", everything)     # refund, -5.00

    def test_only_status_s_is_kept(self):
        self.assertNotIn("TESTPENDING1", ids(reporting.fetch_trading_day(DAY, request=FakeRequest())))

    def test_non_eur_is_an_error(self):
        pages = copy.deepcopy(PAGES)
        pages[0]["transaction_details"][0]["transaction_info"]["transaction_amount"]["currency_code"] = "USD"
        with self.assertRaises(reporting.UnsupportedCurrency):
            reporting.fetch_trading_day(DAY, request=FakeRequest(pages))

    def test_non_eur_outgoing_transactions_do_not_matter(self):
        pages = copy.deepcopy(PAGES)
        pages[0]["transaction_details"][2]["transaction_info"]["transaction_amount"]["currency_code"] = "USD"
        reporting.fetch_trading_day(DAY, request=FakeRequest(pages))      # the payout row is skipped, no error


class ExclusionRules(unittest.TestCase):
    def setUp(self):
        self.rows, self.excluded = reporting.fetch_trading_day_detailed(DAY, request=FakeRequest())
        self.excluded_by_id = {e["transaction_id"]: e for e in self.excluded}

    def test_t1105_positive_amount_without_payer_is_excluded(self):
        self.assertNotIn("TESTHOLD1105", ids(self.rows))            # T1105, +1.02, no payer
        entry = self.excluded_by_id["TESTHOLD1105"]
        self.assertEqual((entry["event_code"], entry["amount"], entry["payer_name"]),
                         ("T1105", Decimal("1.02"), None))
        self.assertIn("T1105", entry["reason"])
        self.assertIn("not a customer payment", entry["reason"])
        self.assertIn("reversal", entry["reason"])

    def test_only_t00_codes_count_as_customer_revenue(self):
        self.assertTrue(self.rows)
        self.assertTrue(all(r["event_code"].startswith("T00") for r in self.rows))

    def test_t00_code_with_a_negative_amount_is_still_excluded(self):
        entry = self.excluded_by_id["TESTMASSPAY1"]                  # T0001 mass payment, -20.00
        self.assertEqual(entry["amount"], Decimal("-20.00"))
        self.assertIn("not positive", entry["reason"])
        self.assertNotIn("not a customer payment", entry["reason"])

    def test_every_excluded_transaction_is_listed_with_id_code_and_reason(self):
        self.assertEqual(set(self.excluded_by_id),
                         {"TESTPAYOUT01", "TESTREFUND01", "TESTMASSPAY1", "TESTPENDING1", "TESTHOLD1105"})
        for entry in self.excluded:
            self.assertTrue(entry["transaction_id"] and entry["event_code"] and entry["reason"])
        self.assertIn("withdrawal", self.excluded_by_id["TESTPAYOUT01"]["reason"])     # T0400
        self.assertIn("status P", self.excluded_by_id["TESTPENDING1"]["reason"])

    def test_excluded_transactions_of_other_days_are_not_listed(self):
        _, excluded_next_day = reporting.fetch_trading_day_detailed(NEXT_DAY, request=FakeRequest())
        self.assertEqual(excluded_next_day, [])

    def test_excluded_section_is_printed_below_the_table(self):
        lines = []
        reporting.daily_summary(DAY, out=lines.append, request=FakeRequest())
        total = next(i for i, line in enumerate(lines) if line.startswith("TOTAL"))
        title = lines.index("Excluded (not customer revenue)")
        self.assertGreater(title, total)
        below = "\n".join(lines[title:])
        self.assertIn("TESTHOLD1105", below)
        self.assertIn("T1105", below)
        self.assertNotIn("TESTHOLD1105", "\n".join(lines[:title]))

    def test_excluded_amounts_do_not_change_the_totals(self):
        summary = reporting.daily_summary(DAY, out=lambda line: None, request=FakeRequest())
        self.assertEqual((summary["count"], summary["gross"]), (4, Decimal("122.70")))


class Pagination(unittest.TestCase):
    def test_all_pages_are_fetched(self):
        fake = FakeRequest()
        rows = reporting.fetch_trading_day(DAY, request=fake)
        self.assertEqual([call[2]["page"] for call in fake.calls], [1, 2])
        self.assertIn("TESTTXN0001", ids(rows))      # on page 1
        self.assertIn("TESTTXN0005", ids(rows))      # on page 2

    def test_a_single_page_is_fetched_once(self):
        page = copy.deepcopy(PAGES[0])
        page["total_pages"] = 1
        fake = FakeRequest([page])
        reporting.fetch_trading_day(DAY, request=fake)
        self.assertEqual(len(fake.calls), 1)


class AmountsAndNet(unittest.TestCase):
    def setUp(self):
        self.rows = {r["transaction_id"]: r for r in reporting.fetch_trading_day(DAY, request=FakeRequest())}

    def test_net_is_gross_minus_a_positive_fee(self):
        row = self.rows["TESTTXN0001"]                 # fee is -0.59 in the API response
        self.assertEqual((row["gross"], row["fee"], row["net"]), (Decimal("12.50"), Decimal("0.59"), Decimal("11.91")))
        self.assertEqual(self.rows["TESTTXN0003"]["net"], Decimal("97.75"))

    def test_a_missing_fee_counts_as_zero(self):
        row = self.rows["TESTTXN0006"]
        self.assertEqual((row["fee"], row["net"]), (Decimal("0.00"), Decimal("6.00")))

    def test_amounts_are_decimals_and_currency_is_eur(self):
        for row in self.rows.values():
            self.assertTrue(all(isinstance(row[k], Decimal) for k in ("gross", "fee", "net")))
            self.assertEqual(row["currency"], "EUR")
            self.assertGreaterEqual(row["fee"], 0)

    def test_payer_name_falls_back_to_given_name_and_surname(self):
        self.assertEqual(self.rows["TESTTXN0001"]["payer_name"], "Test Buyer One")
        self.assertEqual(self.rows["TESTTXN0006"]["payer_name"], "Max Test")

    def test_payer_id_is_the_account_id_or_the_email(self):
        self.assertEqual(self.rows["TESTTXN0001"]["payer_id"], "TESTACCT0001")
        self.assertEqual(self.rows["TESTTXN0005"]["payer_id"], "buyer5@example.test")
        self.assertIsNone(self.rows["TESTTXN0006"]["payer_id"])

    def test_event_code_is_returned(self):
        self.assertEqual(self.rows["TESTTXN0001"]["event_code"], "T0006")


class DailySummary(unittest.TestCase):
    def test_totals(self):
        lines = []
        summary = reporting.daily_summary(DAY, out=lines.append, request=FakeRequest())
        self.assertEqual(summary["count"], 4)
        self.assertEqual(summary["gross"], Decimal("122.70"))      # 100.00 + 4.20 + 6.00 + 12.50
        self.assertEqual(summary["fees"], Decimal("3.27"))         # 2.25 + 0.43 + 0.00 + 0.59
        self.assertEqual(summary["net"], Decimal("119.43"))
        self.assertEqual(summary["net"], summary["gross"] - summary["fees"])
        self.assertTrue(any("TOTAL" in line for line in lines))

    def test_next_day_totals(self):
        summary = reporting.daily_summary(NEXT_DAY, out=lambda line: None, request=FakeRequest())
        self.assertEqual((summary["count"], summary["gross"], summary["fees"], summary["net"]),
                         (2, Decimal("11.40"), Decimal("0.92"), Decimal("10.48")))

    def test_empty_result_mentions_data_delay(self):
        empty = [{"transaction_details": [], "total_pages": 0, "page": 1}]
        lines = []
        summary = reporting.daily_summary(DAY, out=lines.append, request=FakeRequest(empty))
        self.assertEqual(summary["count"], 0)
        self.assertEqual(summary["net"], Decimal("0.00"))
        self.assertTrue(any("data delay" in line and "3 hours" in line for line in lines))


class Errors(unittest.TestCase):
    def test_403_asks_to_enable_transaction_search(self):
        error = client.PayPalError("forbidden", status_code=403, body={"name": "NOT_AUTHORIZED"})
        with self.assertRaises(reporting.TransactionSearchNotEnabled) as ctx:
            reporting.fetch_trading_day(DAY, request=FakeRequest(error=error))
        self.assertIn("Transaction search", str(ctx.exception))

    def test_not_authorized_body_is_recognised_even_without_403(self):
        error = client.PayPalError("denied", status_code=401, body={"name": "NOT_AUTHORIZED"})
        with self.assertRaises(reporting.TransactionSearchNotEnabled):
            reporting.fetch_trading_day(DAY, request=FakeRequest(error=error))

    def test_other_api_errors_are_not_swallowed(self):
        error = client.PayPalError("server error", status_code=500, body={"name": "INTERNAL_SERVICE_ERROR"})
        with self.assertRaises(client.PayPalError):
            reporting.fetch_trading_day(DAY, request=FakeRequest(error=error))


if __name__ == "__main__":
    unittest.main()
