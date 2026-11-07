# Daily split engine (CLAUDE.md §3 - changes need Celina's confirmation).
#
# verified_net of one Trading Day (the PayPal-channel net of customer payments, after the
# anomaly rules have removed excluded transactions)
#   -> drc_due       progressive tiers from data/contract_params.json
#                    (5% up to the Baseline Threshold, 3% on the portion above; NOT one flat rate)
#   -> payout_amount = drc_due, paid in full to the escrow account via PayPal Payouts
#   -> escrow split  servicing_fee = drc_due x servicing_fee_rate; investor_net = drc_due - servicing_fee
#   -> merchant_payout_fee: charged by PayPal to the merchant ON TOP of drc_due. It is recorded from
#      PayPal's actual fee, never counts toward the cap and is never taken from the escrow pool.
#      Merchant's total outlay = drc_due + merchant_payout_fee.
#   -> cap: on the day cumulative drc_due would pass the cap, drc_due is cut to reach it exactly and
#      the contract is marked terminated.
#   -> idempotency: sender_batch_id = drc-{merchant_id}-{YYYYMMDD}; a day that already has a SUCCESS
#      row is refused locally, before PayPal is called.
#
# Revenue input:
#   * default            PayPal Transaction Search (paypal/reporting.py) for that Berlin Trading Day,
#                        then ai/anomaly.py rules: EXCLUDED transactions are removed, UNDER_REVIEW
#                        ones stay in verified_net but are printed.
#   * --revenue X        manual verified_net (kept for tests and demos); no transaction-level check.
#
# Money: Decimal, rounded half-up to 2 places. The total drc_due is rounded once, after summing the
# tiers (703.26 x 5% + 296.74 x 3% = 44.0652 -> 44.07).
#
# Usage (from the project root):
#   python -m engine.split_engine --date 2026-11-06 --dry-run
#   python -m engine.split_engine --date 2026-11-06            (asks for YES, then pays)
#   python -m engine.split_engine --date 2026-11-06 --revenue 166.84 --dry-run
import argparse
import calendar
import json
import sys
import time
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import config
from engine import ledger

PARAMS_PATH = Path(__file__).resolve().parent.parent / "data" / "contract_params.json"
CENT = Decimal("0.01")
RATE_Q = Decimal("0.000001")
PENDING_STATUSES = ("PENDING", "PROCESSING")


# ---------------------------------------------------------------- errors
class SettlementError(Exception):
    """Base class: the settlement was refused (nothing was paid)."""


class AlreadySettled(SettlementError):
    pass


class ContractTerminated(SettlementError):
    pass


class OutsideContractTerm(SettlementError):
    pass


class OutOfOrder(SettlementError):
    pass


class UnresolvedAttempt(SettlementError):
    pass


class PayoutNotConfirmed(SettlementError):
    pass


class NoTransactionsFound(SettlementError):
    pass


# --------------------------------------------------------------- helpers
def D(x):
    return Decimal(str(x))


def q2(x):
    return Decimal(x).quantize(CENT, rounding=ROUND_HALF_UP)


def add_months(d, months):
    index = d.month - 1 + months
    year, month = d.year + index // 12, index % 12 + 1
    return date(year, month, min(d.day, calendar.monthrange(year, month)[1]))


def mask_email(email):
    """sb-q2scf53542513@business.example.com -> sb-q2scf…@business.example.com"""
    local, at, domain = email.partition("@")
    if not at:
        return email
    return (local[:8] + "…" if len(local) > 8 else local) + "@" + domain


def load_params(path=PARAMS_PATH):
    path = Path(path)
    if not path.exists():
        raise SystemExit(f"{path} not found. Run: python -m ai.contract_parser")
    return json.loads(path.read_text(encoding="utf-8"))["params"]


def check_in_term(day, params):
    """Contributions run from the Funding Date for max_term_months."""
    funding = date.fromisoformat(params["funding_date"])
    end = add_months(funding, int(params["max_term_months"]))
    if day < funding:
        raise OutsideContractTerm(f"{day} is before the Funding Date {funding}.")
    if day >= end:
        raise OutsideContractTerm(f"{day} is after the maximum term (ends {end}).")


# ----------------------------------------------------------- revenue input
@dataclass(frozen=True)
class RevenueInput:
    net: Decimal      # verified_net used as the split base
    source: str       # e.g. "PayPal Transaction Search (4 transactions)" or "manual --revenue"


def gather_revenue(day, manual_revenue=None, reporting_module=None, allow_zero_day=False,
                   dry_run=False, out=print):
    """verified_net for one Trading Day.

    manual_revenue given -> used as is. Otherwise PayPal Transaction Search is read through
    reporting_module.fetch_trading_day_detailed / daily_summary (default: paypal.reporting), the
    ai/anomaly.py rules remove EXCLUDED transactions and UNDER_REVIEW ones are printed but counted."""
    if manual_revenue is not None:
        return RevenueInput(q2(D(manual_revenue)), "manual --revenue")

    if reporting_module is None:
        from paypal import reporting as reporting_module
    from ai import anomaly

    rows, excluded_by_reporting = reporting_module.fetch_trading_day_detailed(day)
    summary = reporting_module.daily_summary(day, transactions=rows, excluded=excluded_by_reporting, out=out)
    count = summary["count"]
    if count == 0 and not allow_zero_day:
        message = (f"No customer payments found for {day}. This may be data delay (up to 3 hours). "
                   f"Run again later, or pass --allow-zero-day if the shop really had no sales that day.")
        if not dry_run:
            raise NoTransactionsFound(message)
        out(f"WARNING: {message}")

    verdicts = {v["transaction_id"]: v for v in anomaly.classify_live_batch(rows)}
    excluded = [r for r in rows if verdicts[r["transaction_id"]]["tier"] == "EXCLUDED"]
    under_review = [r for r in rows if verdicts[r["transaction_id"]]["tier"] == "UNDER_REVIEW"]
    excluded_net = sum((r["net"] for r in excluded), Decimal("0.00"))
    verified_net = q2(summary["net"] - excluded_net)

    out("")
    out("Anomaly screening (ai/anomaly.py rules)")
    out(f"  clean: {count - len(excluded) - len(under_review)}   "
        f"under review (counted): {len(under_review)}   excluded (removed): {len(excluded)}")
    for tier_label, group in (("UNDER REVIEW (counted in verified_net)", under_review),
                              ("EXCLUDED (removed from verified_net)", excluded)):
        for r in group:
            reasons = ", ".join(verdicts[r["transaction_id"]]["reasons"])
            out(f"  {tier_label}: {r['transaction_id']}  EUR {r['gross']}  {reasons}")
    out(f"  PayPal net EUR {summary['net']} - excluded EUR {excluded_net} = verified_net EUR {verified_net}")

    noun = "transaction" if count == 1 else "transactions"
    return RevenueInput(verified_net, f"PayPal Transaction Search ({count} {noun})")


# ----------------------------------------------------------- calculation
@dataclass(frozen=True)
class Split:
    verified_revenue: Decimal
    tier_parts: tuple          # ((portion, rate, amount), ...) amount unrounded
    drc_due: Decimal
    rate_applied: Decimal      # blended rate actually applied that day
    payout_amount: Decimal
    servicing_fee: Decimal
    investor_net: Decimal
    cumulative_before: Decimal
    cumulative_after: Decimal
    truncated: bool            # drc_due was cut so that the cap is hit exactly
    terminates: bool           # cumulative_after == cap


def calculate_split(revenue, params, cumulative_before=Decimal("0.00")):
    """Pure calculation, no I/O. Raises ContractTerminated if the cap is already reached."""
    revenue = q2(D(revenue))
    if revenue < 0:
        raise ValueError("verified_revenue cannot be negative")
    cap = q2(D(params["cap_amount"]))
    cumulative_before = q2(D(cumulative_before))
    remaining = cap - cumulative_before
    if remaining <= 0:
        raise ContractTerminated(f"cumulative contributions {cumulative_before} already reached the cap {cap}.")

    tiers = params["tiers"]
    if not tiers or tiers[-1]["up_to"] is not None:
        raise ValueError("the last tier must be open-ended (up_to = null)")

    parts, raw_due, lower = [], Decimal(0), Decimal(0)
    for tier in tiers:
        upper = None if tier["up_to"] is None else D(tier["up_to"])
        top = revenue if upper is None else min(revenue, upper)
        portion = max(top - lower, Decimal(0))
        amount = portion * D(tier["rate"])
        parts.append((portion, D(tier["rate"]), amount))
        raw_due += amount
        if upper is None:
            break
        lower = upper

    uncapped = q2(raw_due)
    truncated = uncapped > remaining
    drc_due = remaining if truncated else uncapped
    effective = remaining if truncated else raw_due
    rate_applied = (effective / revenue).quantize(RATE_Q, rounding=ROUND_HALF_UP) if revenue > 0 \
        else Decimal(0).quantize(RATE_Q)
    servicing_fee = q2(drc_due * D(params["servicing_fee_rate"]))
    return Split(
        verified_revenue=revenue,
        tier_parts=tuple(parts),
        drc_due=drc_due,
        rate_applied=rate_applied,
        payout_amount=drc_due,                      # fees are paid by the merchant on top
        servicing_fee=servicing_fee,
        investor_net=drc_due - servicing_fee,
        cumulative_before=cumulative_before,
        cumulative_after=cumulative_before + drc_due,
        truncated=truncated,
        terminates=drc_due >= remaining,
    )


def merchant_total_outlay(drc_due, merchant_payout_fee):
    """What the merchant really pays for the day: the contribution plus PayPal's payout fee."""
    return q2(D(drc_due) + D(merchant_payout_fee))


# -------------------------------------------------------------- planning
def plan_settlement(conn, merchant_id, day, revenue, params):
    """Run every pre-payment check. Returns (sender_batch_id, existing_row_or_None, split_or_None)."""
    sender_batch_id = ledger.make_sender_batch_id(merchant_id, day)
    existing = ledger.get_by_batch_id(conn, sender_batch_id)
    if existing is not None:
        return sender_batch_id, existing, None
    check_in_term(day, params)
    if ledger.get_termination(conn, merchant_id):
        raise ContractTerminated("the contract is already terminated (cap reached).")
    latest = ledger.latest_success_date(conn, merchant_id)
    if latest and day.isoformat() <= latest:
        raise OutOfOrder(f"{day} is not after the latest settled day {latest}; settle days in order.")
    split = calculate_split(revenue, params, ledger.cumulative_due(conn, merchant_id))
    return sender_batch_id, None, split


def describe_split(split, params, out=print, source=None):
    if source:
        out(f"  source: {source}")
    out(f"  verified_net (split base)         : EUR {split.verified_revenue}")
    for i, (portion, rate, amount) in enumerate(split.tier_parts, start=1):
        out(f"  tier {i}: EUR {portion} x {rate * 100:g}% = {amount}")
    out(f"  drc_due                           : EUR {split.drc_due}   (blended rate {split.rate_applied})")
    if split.truncated:
        out("  ! cap reached: drc_due was cut so that cumulative contributions equal the cap exactly")
    out(f"  payout_amount (to escrow)         : EUR {split.payout_amount}")
    out(f"  escrow: servicing_fee {split.servicing_fee}  |  investor_net {split.investor_net}")
    out(f"  cumulative_due after              : EUR {split.cumulative_after} of cap EUR {q2(D(params['cap_amount']))}")
    estimate = q2(split.payout_amount * config.PAYOUT_FEE_ESTIMATE_RATE)
    out(f"  merchant payout fee               : ~EUR {estimate} (ESTIMATE; actual fee comes from PayPal, "
        f"paid by the merchant on top)")
    if split.terminates:
        out("  ! this settlement terminates the contract")


# ------------------------------------------------------------- settlement
def run_settlement(conn, params, day, revenue, merchant_id, escrow_email, payouts, confirm,
                   sleep=time.sleep, poll_seconds=5, timeout_seconds=300, out=print, source=None):
    """Compute, confirm, pay and record one trading day. `revenue` is verified_net; `source` is only
    printed. `payouts` must offer create_payout(receiver_email, amount, sender_batch_id, note) and
    get_payout_status(batch_id); `confirm(prompt) -> bool` is the human confirmation."""
    sender_batch_id, existing, split = plan_settlement(conn, merchant_id, day, revenue, params)

    if existing is not None:
        status = existing["status"]
        if status in ledger.TERMINAL_FOR_DAY:
            raise AlreadySettled(f"{day} is already settled (status {status}); refusing to pay twice.")
        if status in PENDING_STATUSES and existing["payout_batch_id"]:
            out(f"Resuming {sender_batch_id}: payout {existing['payout_batch_id']} is {status} - "
                f"checking PayPal, nothing new is sent.")
            return _finish(conn, existing, params, payouts, sleep, poll_seconds, timeout_seconds, out)
        raise UnresolvedAttempt(
            f"{sender_batch_id} exists with status {status} and cannot be resumed automatically. "
            f"PayPal rejects a repeated sender_batch_id, so check the payout in PayPal first.")

    out(f"Settlement for Trading Day {day}  (merchant {merchant_id})")
    describe_split(split, params, out, source=source)
    row = {
        "date": day.isoformat(), "merchant_id": merchant_id,
        "verified_revenue": split.verified_revenue, "rate_applied": split.rate_applied,
        "drc_due": split.drc_due, "payout_amount": split.payout_amount, "merchant_payout_fee": None,
        "servicing_fee": split.servicing_fee, "investor_net": split.investor_net,
        "cumulative_due": split.cumulative_after, "sender_batch_id": sender_batch_id,
        "payout_batch_id": None, "status": "PENDING", "created_at": ledger.now_iso(),
    }

    if split.drc_due == 0:
        row["status"] = "NO_PAYOUT"
        ledger.insert_settlement(conn, row)
        out("Nothing is due for this day (drc_due = 0.00): recorded as NO_PAYOUT, no PayPal call.")
        return ledger.get_by_batch_id(conn, sender_batch_id)

    if not confirm(f"Pay EUR {split.payout_amount} to escrow account {mask_email(escrow_email)}? "
                   f"Type YES to confirm: "):
        raise PayoutNotConfirmed("not confirmed - nothing was paid and nothing was recorded.")

    ledger.insert_settlement(conn, row)   # sender_batch_id is UNIQUE: a second run cannot pass this point
    try:
        created = payouts.create_payout(escrow_email, split.payout_amount, sender_batch_id,
                                        f"DRC daily contribution {day}")
    except Exception:
        ledger.update_settlement(conn, sender_batch_id, status="ERROR")
        raise
    ledger.update_settlement(conn, sender_batch_id, payout_batch_id=created["batch_header"]["payout_batch_id"])
    return _finish(conn, ledger.get_by_batch_id(conn, sender_batch_id), params, payouts,
                   sleep, poll_seconds, timeout_seconds, out)


def _finish(conn, row, params, payouts, sleep, poll_seconds, timeout_seconds, out):
    """Poll PayPal until the payout is final, then book PayPal's actual fee."""
    sender_batch_id = row["sender_batch_id"]
    deadline = time.monotonic() + timeout_seconds
    while True:
        header = payouts.get_payout_status(row["payout_batch_id"])["batch_header"]
        status = header["batch_status"]
        ledger.update_settlement(conn, sender_batch_id, status=status)
        out(f"  payout status: {status}")
        if status not in PENDING_STATUSES:
            break
        if time.monotonic() >= deadline:
            out(f"Still {status} after {timeout_seconds}s. Run the same command later to resume; "
                f"nothing is paid twice.")
            return ledger.get_by_batch_id(conn, sender_batch_id)
        sleep(poll_seconds)

    if status != "SUCCESS":
        out(f"Payout ended with status {status}. It is NOT counted toward the cap.")
        return ledger.get_by_batch_id(conn, sender_batch_id)

    fee = (header.get("fees") or {}).get("value")
    if fee is None:
        out("WARNING: PayPal returned no fee for this payout; merchant_payout_fee is left empty.")
    else:
        ledger.update_settlement(conn, sender_batch_id, merchant_payout_fee=q2(D(fee)))
    row = ledger.get_by_batch_id(conn, sender_batch_id)

    cap = q2(D(params["cap_amount"]))
    if D(row["cumulative_due"]) >= cap:
        ledger.record_termination(conn, row["merchant_id"], row["date"], row["cumulative_due"], cap)
        out(f"CONTRACT TERMINATED: cumulative contributions reached the cap EUR {cap}.")

    out(f"Settled {row['date']}: EUR {row['drc_due']} transferred to the escrow account.")
    if fee is not None:
        out(f"  merchant_payout_fee (PayPal actual): EUR {row['merchant_payout_fee']}  (not counted toward the cap)")
        out(f"  merchant total outlay today = drc_due + merchant_payout_fee = "
            f"EUR {merchant_total_outlay(row['drc_due'], row['merchant_payout_fee'])}")
    out(f"  escrow split: servicing_fee {row['servicing_fee']}, investor_net {row['investor_net']}")
    out(f"  cumulative_due: {row['cumulative_due']} of cap {cap}")
    return row


# ------------------------------------------------------------------ CLI
def _decimal_arg(text):
    try:
        value = Decimal(text)
    except Exception:
        raise argparse.ArgumentTypeError(f"not a number: {text!r}")
    if value < 0:
        raise argparse.ArgumentTypeError("must not be negative")
    return value


def main(argv=None, reporting_module=None):
    parser = argparse.ArgumentParser(description="Compute and pay one Trading Day's DRC contribution.")
    parser.add_argument("--date", required=True, type=date.fromisoformat, help="Trading Day, YYYY-MM-DD (Berlin)")
    parser.add_argument("--revenue", type=_decimal_arg, default=None,
                        help="manual verified_net in EUR; without it, revenue is read from PayPal Transaction Search")
    parser.add_argument("--allow-zero-day", action="store_true",
                        help="accept a day with no customer payments in Transaction Search (otherwise it is "
                             "treated as possible data delay and refused)")
    parser.add_argument("--dry-run", action="store_true", help="only print the calculation, pay nothing")
    parser.add_argument("--merchant-id", default=config.MERCHANT_ID)
    parser.add_argument("--db", default=str(ledger.DB_PATH))
    parser.add_argument("--params", default=str(PARAMS_PATH))
    args = parser.parse_args(argv)

    live_revenue = args.revenue is None
    external_errors = ()
    if live_revenue or not args.dry_run:
        from paypal.client import PayPalError
        from paypal.reporting import ReportingError
        external_errors = (PayPalError, ReportingError)

    params = load_params(args.params)
    conn = ledger.connect(args.db)
    try:
        # A day that already has a ledger row is handled before PayPal Transaction Search is queried.
        batch_id = ledger.make_sender_batch_id(args.merchant_id, args.date)
        existing = ledger.get_by_batch_id(conn, batch_id)
        if existing is not None:
            if args.dry_run:
                print(f"A settlement row for {args.date} already exists (status {existing['status']}).")
                return 1
            if existing["status"] in ledger.TERMINAL_FOR_DAY:
                raise AlreadySettled(f"{args.date} is already settled (status {existing['status']}); "
                                     f"refusing to pay twice.")
            revenue = RevenueInput(Decimal("0.00"), "existing ledger row")   # unused: the row decides
        else:
            revenue = gather_revenue(args.date, manual_revenue=args.revenue, reporting_module=reporting_module,
                                     allow_zero_day=args.allow_zero_day, dry_run=args.dry_run)

        if args.dry_run:
            _, _, split = plan_settlement(conn, args.merchant_id, args.date, revenue.net, params)
            print("")
            print(f"DRY RUN - Trading Day {args.date}  (merchant {args.merchant_id}); nothing is paid or recorded")
            describe_split(split, params, source=revenue.source)
            return 0

        if not config.ESCROW_EMAIL:
            raise SystemExit("ESCROW_EMAIL is not set in .env.")
        from paypal import payouts as paypal_payouts  # imported late so tests never need the network

        print("")
        run_settlement(conn, params, args.date, revenue.net, args.merchant_id, config.ESCROW_EMAIL,
                       paypal_payouts, confirm=lambda prompt: input(prompt).strip() == "YES",
                       source=revenue.source)
        return 0
    except SettlementError as exc:
        print(f"REFUSED: {exc}")
        return 1
    except external_errors as exc:
        print(f"ERROR: {exc}")
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
