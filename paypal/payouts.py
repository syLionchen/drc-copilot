# Payouts API wrapper: POST /v1/payments/payouts to send the daily DRC
# payout, GET /v1/payments/payouts/{batch_id} to poll status. Enforces
# idempotent sender_batch_id = drc-{merchant_id}-{YYYYMMDD} (see CLAUDE.md §3.7).
import time
from decimal import ROUND_HALF_UP, Decimal

import config
from paypal import client

CURRENCY = "EUR"
CENT = Decimal("0.01")


def _to_money(amount):
    """Convert to Decimal with 2 places. Rejects floats to avoid binary rounding."""
    if isinstance(amount, float):
        raise TypeError("amount must be Decimal, str or int - not float")
    value = Decimal(str(amount)).quantize(CENT, rounding=ROUND_HALF_UP)
    if value <= 0:
        raise ValueError(f"payout amount must be > 0, got {value}")
    return value


def create_payout(receiver_email, amount, sender_batch_id, note):
    """Send a single EUR payout. Returns PayPal's response (has payout_batch_id).

    PayPal rejects a repeated sender_batch_id, which is our idempotency guard.
    """
    value = _to_money(amount)
    payload = {
        "sender_batch_header": {
            "sender_batch_id": sender_batch_id,
            "email_subject": "DRC settlement payout",
            "email_message": note,
        },
        "items": [
            {
                "recipient_type": "EMAIL",
                "receiver": receiver_email,
                "amount": {"value": str(value), "currency": CURRENCY},
                "note": note,
                "sender_item_id": sender_batch_id,
            }
        ],
    }
    return client.request("POST", "/v1/payments/payouts", json=payload)


def get_payout_status(batch_id):
    """Fetch a payout batch. Status is at result['batch_header']['batch_status']
    (PENDING / PROCESSING / SUCCESS / DENIED / CANCELED ...)."""
    return client.request("GET", f"/v1/payments/payouts/{batch_id}")


if __name__ == "__main__":
    if not config.ESCROW_EMAIL:
        raise SystemExit("ESCROW_EMAIL is not set in .env")

    batch_id = f"drc-test-{int(time.time())}"
    print(f"Sending 1.00 EUR to ESCROW_EMAIL (sender_batch_id={batch_id}) ...")
    created = create_payout(config.ESCROW_EMAIL, Decimal("1.00"), batch_id, "DRC Copilot test payout")
    payout_batch_id = created["batch_header"]["payout_batch_id"]
    print(f"Created: payout_batch_id={payout_batch_id}, "
          f"status={created['batch_header']['batch_status']}")

    deadline = time.time() + 180
    while True:
        time.sleep(5)
        result = get_payout_status(payout_batch_id)
        header = result["batch_header"]
        status = header["batch_status"]
        print(f"  status = {status}")
        if status not in ("PENDING", "PROCESSING") or time.time() > deadline:
            break

    amount = header.get("amount", {})
    fees = header.get("fees", {})
    print("--- Result ---")
    print(f"payout_batch_id : {payout_batch_id}")
    print(f"sender_batch_id : {batch_id}")
    print(f"final status    : {status}")
    print(f"amount          : {amount.get('value')} {amount.get('currency')}")
    print(f"fee             : {fees.get('value')} {fees.get('currency')}")
    for item in result.get("items", []):
        print(f"item status     : {item.get('transaction_status')} -> {item.get('payout_item', {}).get('receiver')}")
    if status in ("PENDING", "PROCESSING"):
        print(f"Still {status} after 180 s - check again later with get_payout_status().")
