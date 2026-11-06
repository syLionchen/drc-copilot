# Transaction Search wrapper: GET /v1/reporting/transactions to pull the
# merchant's real sandbox transactions (used to reconcile and as the live
# segment of the revenue data). Requires "Transaction search" to be enabled
# on the REST app. Sandbox data can lag by up to a few hours.
import argparse
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from paypal import client

MAX_WINDOW = timedelta(days=31)  # API limit per request
PAGE_SIZE = 100


def _fmt(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+0000")


def _money(block):
    return Decimal(block["value"]) if block and "value" in block else Decimal("0.00")


def _normalize(detail):
    info = detail.get("transaction_info", {})
    payer = detail.get("payer_info", {})
    amount = info.get("transaction_amount", {})
    return {
        "txn_id": info.get("transaction_id"),
        "timestamp": info.get("transaction_initiation_date"),
        "event_code": info.get("transaction_event_code"),
        "status": info.get("transaction_status"),
        "amount": _money(amount),
        "currency": amount.get("currency_code", "EUR"),
        "fee": _money(info.get("fee_amount")),
        "payer_email": payer.get("email_address"),
        "payer_name": (payer.get("payer_name") or {}).get("alternate_full_name"),
        "subject": info.get("transaction_subject") or info.get("transaction_note"),
    }


def get_transactions(start, end, only_incoming=False):
    """Return normalized transactions between two datetimes, oldest first.

    Splits ranges longer than 31 days, follows pagination, de-duplicates by
    txn_id. only_incoming=True keeps amount > 0 (customer payments).
    """
    seen, rows = set(), []
    window_start = start
    while window_start < end:
        window_end = min(window_start + MAX_WINDOW, end)
        page = 1
        while True:
            data = client.request(
                "GET", "/v1/reporting/transactions",
                params={
                    "start_date": _fmt(window_start),
                    "end_date": _fmt(window_end),
                    "fields": "transaction_info,payer_info",
                    "page_size": PAGE_SIZE,
                    "page": page,
                },
            )
            for detail in data.get("transaction_details", []):
                row = _normalize(detail)
                if row["txn_id"] not in seen:
                    seen.add(row["txn_id"])
                    rows.append(row)
            if page >= data.get("total_pages", 1):
                break
            page += 1
        window_start = window_end

    if only_incoming:
        rows = [r for r in rows if r["amount"] > 0]
    return sorted(rows, key=lambda r: r["timestamp"] or "")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="List recent merchant transactions.")
    parser.add_argument("--days", type=int, default=3, help="look back this many days (default 3)")
    parser.add_argument("--incoming", action="store_true", help="only customer payments (amount > 0)")
    args = parser.parse_args()

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=args.days)
    txns = get_transactions(start, end, only_incoming=args.incoming)

    print(f"{len(txns)} transactions in the last {args.days} day(s):")
    total = Decimal("0.00")
    for t in txns:
        total += t["amount"]
        print(f"{t['timestamp']}  {t['event_code']}  {t['status']}  "
              f"{t['amount']:>9} {t['currency']}  fee {t['fee']}  {t['payer_email'] or '-'}")
    print(f"Net sum of listed amounts: {total} EUR")
