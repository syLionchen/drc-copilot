# Orders API wrapper: lets Consumer A/B place real sandbox orders that pay the
# merchant (Kaffee und Tee GmbH). POST /v2/checkout/orders -> buyer approves in
# the browser -> POST /v2/checkout/orders/{id}/capture.
import argparse
import uuid
from decimal import ROUND_HALF_UP, Decimal

import config
from paypal import client

CURRENCY = "EUR"
CENT = Decimal("0.01")
APPROVE_RELS = ("payer-action", "approve")  # link rel differs by payload style


def _to_money(amount):
    """Decimal with 2 places. Rejects floats to avoid binary rounding."""
    if isinstance(amount, float):
        raise TypeError("amount must be Decimal, str or int - not float")
    value = Decimal(str(amount)).quantize(CENT, rounding=ROUND_HALF_UP)
    if value <= 0:
        raise ValueError(f"order amount must be > 0, got {value}")
    return value


def create_order(amount, description, use_payee=True, payee_email=None, classic=False):
    """Create a CAPTURE order in EUR payable to the merchant.

    Returns {"order_id", "status", "approve_url"}. The buyer must open
    approve_url and pay before capture_order() is called.
    """
    if len(description) > 127:
        raise ValueError("description must be <= 127 characters (PayPal limit)")
    value = _to_money(amount)

    unit = {
        "amount": {"currency_code": CURRENCY, "value": str(value)},
        "description": description,
    }
    # payee_email (explicit) wins; otherwise MERCHANT_EMAIL unless use_payee=False.
    payee = payee_email or (config.MERCHANT_EMAIL if use_payee else "")
    if payee:
        unit["payee"] = {"email_address": payee}

    payload = {
        "intent": "CAPTURE",
        "purchase_units": [unit],
        "payment_source": {
            "paypal": {
                "experience_context": {
                    "user_action": "PAY_NOW",
                    "shipping_preference": "NO_SHIPPING",
                    "return_url": "https://example.com/paypal/return",
                    "cancel_url": "https://example.com/paypal/cancel",
                }
            }
        },
    }
    if classic:
        # Older payload style: application_context instead of payment_source.
        payload.pop("payment_source")
        payload["application_context"] = {
            "user_action": "PAY_NOW",
            "shipping_preference": "NO_SHIPPING",
            "return_url": "https://example.com/paypal/return",
            "cancel_url": "https://example.com/paypal/cancel",
        }
    # PayPal-Request-Id makes a retried POST return the same order instead of a new one.
    result = client.request(
        "POST", "/v2/checkout/orders", json=payload,
        headers={"PayPal-Request-Id": str(uuid.uuid4())},
    )
    approve_url = next(
        (link["href"] for link in result.get("links", []) if link.get("rel") in APPROVE_RELS),
        None,
    )
    return {"order_id": result["id"], "status": result["status"], "approve_url": approve_url}


def capture_order(order_id):
    """Capture an approved order. Returns {"order_id", "status", "capture_id", "amount", "currency"}."""
    result = client.request(
        "POST", f"/v2/checkout/orders/{order_id}/capture", json={},
        headers={"PayPal-Request-Id": f"capture-{order_id}"},
    )
    capture = result["purchase_units"][0]["payments"]["captures"][0]
    return {
        "order_id": result["id"],
        "status": result["status"],
        "capture_id": capture["id"],
        "amount": capture["amount"]["value"],
        "currency": capture["amount"]["currency_code"],
    }


# PayPal sandbox TEST card only (fake data). Never put a real card here.
SANDBOX_TEST_CARD = {
    "number": "4111111111111111",
    "expiry": "2030-12",
    "security_code": "123",
    "name": "Anna Schmidt",
    "billing_address": {
        "address_line_1": "Musterstrasse 1",
        "admin_area_2": "Berlin",
        "postal_code": "10115",
        "country_code": "DE",
    },
}


def create_card_order(amount, description):
    """Pay the merchant with the sandbox test card via payment_source.card.

    No browser step: PayPal authorizes and captures the card in the create call.
    Returns {"status", "order_id", "capture_id", "amount", "currency", "payer_action_url"}.
    capture_id is None unless the card was captured. If PayPal asks for 3-D Secure
    the status is PAYER_ACTION_REQUIRED and payer_action_url holds the link.
    Needs "Advanced Credit and Debit Card Payments" enabled on the REST app.
    """
    if "sandbox" not in config.PAYPAL_BASE_URL:
        raise RuntimeError("create_card_order sends raw card data and is sandbox-only")
    if len(description) > 127:
        raise ValueError("description must be <= 127 characters (PayPal limit)")
    value = _to_money(amount)

    payload = {
        "intent": "CAPTURE",
        "purchase_units": [{
            "amount": {"currency_code": CURRENCY, "value": str(value)},
            "description": description,
        }],
        "payment_source": {"card": dict(SANDBOX_TEST_CARD)},
    }
    result = client.request(
        "POST", "/v2/checkout/orders", json=payload,
        headers={"PayPal-Request-Id": str(uuid.uuid4())},
    )

    captures = (result.get("purchase_units") or [{}])[0].get("payments", {}).get("captures", [])
    if not captures and result.get("status") == "APPROVED":
        # Approved but not yet captured: capture explicitly.
        cap = capture_order(result["id"])
        return {"status": cap["status"], "order_id": cap["order_id"],
                "capture_id": cap["capture_id"], "amount": cap["amount"],
                "currency": cap["currency"], "payer_action_url": None}

    capture = captures[0] if captures else None
    payer_action_url = next(
        (link["href"] for link in result.get("links", []) if link.get("rel") == "payer-action"),
        None,
    )
    return {
        "status": result["status"],
        "order_id": result["id"],
        "capture_id": capture["id"] if capture else None,
        "amount": capture["amount"]["value"] if capture else str(value),
        "currency": capture["amount"]["currency_code"] if capture else CURRENCY,
        "payer_action_url": payer_action_url,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Create and capture one sandbox order.")
    parser.add_argument("--mode", choices=("paypal", "card"), default="paypal",
                        help="paypal: buyer approves in browser (default); "
                             "card: pay with the sandbox test card, no browser")
    parser.add_argument("--amount", default="12.50", help="EUR amount (default 12.50)")
    parser.add_argument("--description", default="2x Cappuccino + Croissant")
    parser.add_argument("--no-payee", action="store_true",
                        help="do not send MERCHANT_EMAIL as payee (pays the app's own account)")
    parser.add_argument("--classic", action="store_true",
                        help="use the older application_context payload (no payment_source)")
    parser.add_argument("--payee-email", help="pay this email instead of MERCHANT_EMAIL")
    parser.add_argument("--payee-escrow", action="store_true",
                        help="pay ESCROW_EMAIL from .env (shortcut for --payee-email)")
    args = parser.parse_args()

    if args.mode == "card":
        if args.payee_email or args.payee_escrow or args.no_payee or args.classic:
            parser.error("--payee-*, --no-payee and --classic only apply to --mode paypal")
        try:
            res = create_card_order(Decimal(args.amount), args.description)
        except client.PayPalError as exc:
            raise SystemExit(f"Card order failed: {exc}")
        print("--- Card order result ---")
        print(f"order status : {res['status']}")
        print(f"order id     : {res['order_id']}")
        print(f"capture id   : {res['capture_id']}")
        print(f"amount       : {res['amount']} {res['currency']}")
        if res["payer_action_url"]:
            print(f"3-D Secure / payer action needed, open: {res['payer_action_url']}")
        raise SystemExit(0)

    payee_email = args.payee_email
    if args.payee_escrow:
        if payee_email:
            parser.error("use either --payee-email or --payee-escrow, not both")
        if not config.ESCROW_EMAIL:
            parser.error("ESCROW_EMAIL is not set in .env")
        payee_email = config.ESCROW_EMAIL
    if payee_email and args.no_payee:
        parser.error("--no-payee cannot be combined with a payee email")

    if payee_email:
        print(f"Payee override: {payee_email}")
    order = create_order(Decimal(args.amount), args.description,
                         use_payee=not args.no_payee, payee_email=payee_email,
                         classic=args.classic)
    print(f"Order created: id={order['order_id']} status={order['status']}")
    print("\nOpen this link in a browser and pay as a sandbox BUYER (Consumer A or B):")
    print(order["approve_url"])
    input("\nAfter you have approved the payment, press Enter to capture ...")

    try:
        cap = capture_order(order["order_id"])
    except client.PayPalError as exc:
        raise SystemExit(f"Capture failed: {exc}")
    print("--- Capture result ---")
    print(f"order status : {cap['status']}")
    print(f"capture id   : {cap['capture_id']}")
    print(f"amount       : {cap['amount']} {cap['currency']}")
