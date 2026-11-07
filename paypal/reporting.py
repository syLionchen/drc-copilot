# Transaction Search wrapper: GET /v1/reporting/transactions (CLAUDE.md §5 reconciliation).
#
# fetch_trading_day(date) returns the customer payments received by the merchant on one Trading
# Day, where the day is Europe/Berlin 00:00-24:00 (converted to UTC for the API).
#
# A transaction counts as customer revenue only if ALL of these hold:
#   * transaction_event_code starts with "T00" (customer payment family)
#   * transaction_amount > 0           (payouts, refunds and fees are money going out)
#   * transaction_status == "S"        (completed)
# Everything else inside the day is NOT dropped silently: fetch_trading_day_detailed() returns it
# as "excluded" with a reason, and daily_summary() prints it below the table.
# Only EUR is accepted for customer revenue.
#
# Requires "Transaction search" to be enabled on the REST app. Data can lag by up to 3 hours.
#
# Usage (from the project root):  python -m paypal.reporting --date 2026-11-06
import argparse
import json
import sys
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

from paypal import client

BERLIN = ZoneInfo("Europe/Berlin")
UTC = timezone.utc
CURRENCY = "EUR"
PAGE_SIZE = 500
PATH = "/v1/reporting/transactions"
CENT = Decimal("0.01")
CUSTOMER_PAYMENT_PREFIX = "T00"
# Short hints for the excluded-section reasons (prefix of the event code -> meaning).
CODE_HINTS = {"T11": "reversal / hold release", "T04": "withdrawal", "T03": "top-up / funding"}

NOT_ENABLED_MESSAGE = ("Transaction search is not enabled for this app. Turn it on in the app settings "
                       "(developer.paypal.com > Apps & Credentials > your app > Transaction search), "
                       "save, and try again.")
EMPTY_MESSAGE = "No customer payments found. This may be data delay (up to 3 hours); try again later."
EXCLUDED_TITLE = "Excluded (not customer revenue)"


class ReportingError(Exception):
    pass


class TransactionSearchNotEnabled(ReportingError):
    pass


class UnsupportedCurrency(ReportingError):
    pass


def D(x):
    return Decimal(str(x))


def q2(x):
    return Decimal(x).quantize(CENT, rounding=ROUND_HALF_UP)


def trading_day_window(day):
    """[start, end) of a Berlin calendar day, in UTC. DST days are 23 or 25 hours long."""
    start = datetime.combine(day, time(0, 0), tzinfo=BERLIN)
    end = datetime.combine(day + timedelta(days=1), time(0, 0), tzinfo=BERLIN)
    return start.astimezone(UTC), end.astimezone(UTC)


def _api_date(dt):
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S+0000")


def _parse_time(text):
    try:
        parsed = datetime.strptime(text, "%Y-%m-%dT%H:%M:%S%z")   # PayPal: 2026-11-06T22:59:30+0000
    except ValueError:
        parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _search(start_utc, end_utc, request):
    """All transaction_details between two UTC datetimes, following pagination."""
    details, page, total_pages = [], 1, 1
    while page <= total_pages:
        params = {"start_date": _api_date(start_utc), "end_date": _api_date(end_utc),
                  "fields": "all", "page_size": PAGE_SIZE, "page": page}
        try:
            data = request("GET", PATH, params=params)
        except client.PayPalError as exc:
            if exc.status_code == 403 or "NOT_AUTHORIZED" in json.dumps(exc.body, default=str):
                raise TransactionSearchNotEnabled(NOT_ENABLED_MESSAGE) from exc
            raise
        details.extend(data.get("transaction_details") or [])
        total_pages = int(data.get("total_pages") or 1)
        page += 1
    return details


def _payer_name(detail):
    name = (detail.get("payer_info") or {}).get("payer_name") or {}
    full = name.get("alternate_full_name") or " ".join(
        part for part in (name.get("given_name"), name.get("surname")) if part)
    return full or None


def _payer_id(detail):
    info = detail.get("payer_info") or {}
    return info.get("account_id") or info.get("email_address") or None


def _exclusion_reasons(code, value, status):
    reasons = []
    if not code.startswith(CUSTOMER_PAYMENT_PREFIX):
        hint = next((text for prefix, text in CODE_HINTS.items() if code.startswith(prefix)), None)
        label = f"event code {code or 'missing'}" + (f" ({hint})" if hint else "")
        reasons.append(f"{label} is not a customer payment ({CUSTOMER_PAYMENT_PREFIX}xx)")
    if value <= 0:
        reasons.append("amount is not positive (money out: payout, refund or fee)")
    if status != "S":
        reasons.append(f"status {status} is not completed (S)")
    return reasons


def _classify(detail):
    """-> ("keep", row) for customer revenue, ("exclude", entry) for everything else."""
    info = detail.get("transaction_info") or {}
    amount = info.get("transaction_amount") or {}
    code = info.get("transaction_event_code") or ""
    value = D(amount.get("value", "0"))
    status = info.get("transaction_status")
    time_berlin = _parse_time(info["transaction_initiation_date"]).astimezone(BERLIN)

    reasons = _exclusion_reasons(code, value, status)
    if reasons:
        return "exclude", {
            "transaction_id": info.get("transaction_id"),
            "time_berlin": time_berlin,
            "event_code": code or None,
            "amount": q2(value),
            "currency": amount.get("currency_code"),
            "status": status,
            "payer_name": _payer_name(detail),
            "reason": "; ".join(reasons),
        }

    if amount.get("currency_code") != CURRENCY:
        raise UnsupportedCurrency(
            f"transaction {info.get('transaction_id')} is in {amount.get('currency_code')}; only {CURRENCY} is supported.")
    fee_block = info.get("fee_amount")
    fee = Decimal("0.00")
    if fee_block:
        if fee_block.get("currency_code") != CURRENCY:
            raise UnsupportedCurrency(
                f"fee of transaction {info.get('transaction_id')} is in {fee_block.get('currency_code')}; "
                f"only {CURRENCY} is supported.")
        fee = abs(q2(D(fee_block["value"])))          # PayPal reports fees as negative numbers
    gross = q2(value)
    return "keep", {
        "transaction_id": info.get("transaction_id"),
        "time_berlin": time_berlin,
        "payer_name": _payer_name(detail),
        "payer_id": _payer_id(detail),
        "gross": gross,
        "fee": fee,
        "net": gross - fee,
        "currency": CURRENCY,
        "event_code": code,
    }


def fetch_trading_day_detailed(day, request=None):
    """(customer_revenue_rows, excluded_entries) of one Berlin Trading Day, oldest first.

    `request` defaults to paypal.client.request (token caching, retries, debug_id printing)."""
    request = request or client.request
    start_utc, end_utc = trading_day_window(day)
    seen, rows, excluded = set(), [], []
    for detail in _search(start_utc, end_utc, request):
        kind, item = _classify(detail)
        if item["transaction_id"] in seen:
            continue
        # the API window is inclusive at both ends; keep [start, end) so midnight belongs to one day only
        if not (start_utc <= item["time_berlin"].astimezone(UTC) < end_utc):
            continue
        seen.add(item["transaction_id"])
        (rows if kind == "keep" else excluded).append(item)
    by_time = lambda item: item["time_berlin"]  # noqa: E731
    return sorted(rows, key=by_time), sorted(excluded, key=by_time)


def fetch_trading_day(day, request=None):
    """Customer revenue of one Berlin Trading Day, oldest first (see fetch_trading_day_detailed)."""
    return fetch_trading_day_detailed(day, request=request)[0]


def daily_summary(day, transactions=None, excluded=None, out=print, request=None):
    """Count / gross / fees / net of one Trading Day, printed as a table followed by the excluded
    transactions. Returns the summary dict."""
    if transactions is None:
        rows, excluded = fetch_trading_day_detailed(day, request=request)
    else:
        rows, excluded = transactions, excluded or []
    summary = {
        "date": day.isoformat(),
        "count": len(rows),
        "gross": sum((r["gross"] for r in rows), Decimal("0.00")),
        "fees": sum((r["fee"] for r in rows), Decimal("0.00")),
        "net": sum((r["net"] for r in rows), Decimal("0.00")),
    }
    out(f"Trading Day {day} (Europe/Berlin 00:00-24:00), customer payments in {CURRENCY}")
    if rows:
        out(f"{'transaction_id':<20}{'time_berlin':<21}{'payer_name':<26}{'gross':>10}{'fee':>8}{'net':>10}  event")
        for r in rows:
            out(f"{r['transaction_id']:<20}{r['time_berlin']:%Y-%m-%d %H:%M:%S}  "
                f"{(r['payer_name'] or '-')[:24]:<26}{r['gross']:>10}{r['fee']:>8}{r['net']:>10}  {r['event_code']}")
        out("-" * 100)
    else:
        out(EMPTY_MESSAGE)
    out(f"{'TOTAL':<20}{'count ' + str(summary['count']):<21}{'':<26}"
        f"{summary['gross']:>10}{summary['fees']:>8}{summary['net']:>10}")

    out("")
    out(EXCLUDED_TITLE)
    if excluded:
        out(f"{'transaction_id':<20}{'event':<8}{'amount':>10}  reason")
        for e in excluded:
            out(f"{e['transaction_id']:<20}{(e['event_code'] or '-'):<8}{e['amount']:>10}  {e['reason']}")
    else:
        out("  (none)")
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description="List the merchant's customer payments for one Trading Day.")
    parser.add_argument("--date", required=True, type=date.fromisoformat, help="Trading Day, YYYY-MM-DD (Berlin)")
    args = parser.parse_args(argv)
    try:
        daily_summary(args.date)
    except ReportingError as exc:
        print(f"ERROR: {exc}")
        return 1
    except client.PayPalError as exc:
        print(f"PayPal error: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
