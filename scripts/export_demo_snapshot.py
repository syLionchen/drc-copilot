# Exports the data the web app needs into data/demo/ (this folder IS committed).
#
#   verified_daily.csv     copy of data/verified_daily.csv (simulated data, safe to publish)
#   settlements.json       the ledger's settlements table, whitelisted columns only
#                          (no sender_batch_id, no payout_batch_id, no e-mail addresses)
#   paypal_receipts.json   customer payments of the demo days from PayPal Transaction Search;
#                          transaction ids are cut to their last 4 characters, payer names are kept
#                          (sandbox test accounts)
#   projection.json        the three irr_tracker scenarios: daily cumulative curve + summary metrics
#   anomaly_daily.json     per day raw / verified / excluded / at_risk amounts and the day flags
#   anomaly_flagged.json   every excluded / under-review transaction with its reasons (simulated history)
#   anomaly_metrics.json   precision / recall per detector against the injected anomalies
#   sample_contract.pdf    copy of data/sample_contract.pdf
#   extracted.json, reconciliation.json, merchant_doc_texts.json
#                          the merchant documents: what the AI read, how it matched our data, and the
#                          PDF text (only when ai.doc_reader --all and engine.reconcile have been run)
#   health_recommendation.json
#                          copy of data/health/recommendation.json: the merchant-health trigger metrics, Claude's
#                          recommendation (recommendation only) and the chart series (only after engine.health_agent)
#
# Safety check: everything is built in memory and scanned first. If any output contains "@", "sk-",
# "client_secret" or "access_token", nothing is written and the script stops with an error.
#
# Usage (from the project root):
#   python -m scripts.export_demo_snapshot
#   python -m scripts.export_demo_snapshot --skip-paypal      (do not call Transaction Search)
import argparse
import csv
import json
import sqlite3
import sys
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
from engine import irr_tracker as irr  # noqa: E402
from engine import ledger  # noqa: E402
from engine import split_engine as se  # noqa: E402

DEMO_DIR = ROOT / "data" / "demo"
DEFAULT_DAYS = (date(2026, 11, 6), date(2026, 11, 7))
SETTLEMENT_COLUMNS = ("date", "verified_revenue", "rate_applied", "drc_due", "payout_amount",
                      "merchant_payout_fee", "servicing_fee", "investor_net", "cumulative_due", "status")
# (text, case_sensitive)
FORBIDDEN = (("@", True), ("sk-", True), ("client_secret", False), ("access_token", False))


class UnsafeExport(Exception):
    pass


def money(x):
    return f"{Decimal(x):.2f}"


def mask_id(transaction_id):
    """Keep only the last 4 characters of a PayPal transaction id."""
    if transaction_id and len(transaction_id) > 4:
        return "…" + transaction_id[-4:]
    return transaction_id


def check_clean(files):
    """files: {name: text}. Raises UnsafeExport naming every forbidden string that appears."""
    problems = []
    for name, text in files.items():
        for needle, case_sensitive in FORBIDDEN:
            haystack, target = (text, needle) if case_sensitive else (text.lower(), needle.lower())
            index = haystack.find(target)
            if index >= 0:   # report the position only: never echo the offending text (it may be a secret)
                line = text.count("\n", 0, index) + 1
                problems.append(f"{name}: contains {needle!r} (line {line}, character {index})")
    if problems:
        raise UnsafeExport("Refusing to export, forbidden strings found:\n  " + "\n  ".join(problems))


def dump(obj):
    return json.dumps(obj, indent=2, ensure_ascii=False, default=_json_default) + "\n"


def _json_default(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, date):
        return value.isoformat()
    raise TypeError(f"cannot serialise {type(value).__name__}")


# ------------------------------------------------------------- builders
def build_settlements(db_path, merchant_id):
    """Whitelisted columns of the settlements table, oldest first. Empty if there is no ledger yet."""
    db_path = Path(db_path)
    if not db_path.exists():
        return []
    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(f"SELECT {', '.join(SETTLEMENT_COLUMNS)} FROM settlements "
                            f"WHERE merchant_id = ? ORDER BY date, id", (merchant_id,)).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def build_receipts(days, reporting_module=None):
    """Customer payments (and the excluded rows) per Trading Day. No payer ids or e-mail addresses."""
    if reporting_module is None:
        from paypal import reporting as reporting_module
    zero = Decimal("0.00")
    result = []
    for day in days:
        rows, excluded = reporting_module.fetch_trading_day_detailed(day)
        result.append({
            "date": day.isoformat(),
            "summary": {"count": len(rows),
                        "gross": money(sum((r["gross"] for r in rows), zero)),
                        "fees": money(sum((r["fee"] for r in rows), zero)),
                        "net": money(sum((r["net"] for r in rows), zero))},
            "transactions": [{
                "transaction_id": mask_id(r["transaction_id"]),
                "time_berlin": r["time_berlin"].isoformat(timespec="seconds"),
                "payer_name": r["payer_name"],
                "gross": money(r["gross"]), "fee": money(r["fee"]), "net": money(r["net"]),
                "currency": r["currency"], "event_code": r["event_code"],
            } for r in rows],
            "excluded": [{
                "transaction_id": mask_id(e["transaction_id"]),
                "event_code": e["event_code"], "amount": money(e["amount"]), "reason": e["reason"],
            } for e in excluded],
        })
    return result


def _plain(value):
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value


def build_projection(params, revenues, actuals):
    investment = se.q2(se.D(params["investment_amount"]))
    servicing = se.D(params["servicing_fee_rate"])
    funding = date.fromisoformat(params["funding_date"])
    scenarios = []
    for name, factor in irr.SCENARIOS:
        p = irr.project(params, revenues, factor, actuals, name=name)
        net_total, series = Decimal("0.00"), []
        for r in p.rows:
            net_total += r["investor_net"]
            series.append({"date": r["date"].isoformat(), "actual": r["actual"],
                           "drc_due": str(r["drc_due"]), "investor_net": str(r["investor_net"]),
                           "merchant_payout_fee": str(r["fee"]),
                           "cumulative_drc_due": str(r["cumulative"]),
                           "cumulative_investor_net": str(net_total)})
        scenarios.append({
            "name": name, "revenue_factor": str(factor),
            "avg_daily_revenue": str(se.q2(p.avg_daily_revenue)),
            "metrics": {key: _plain(value) for key, value in irr.metrics(p).items()},
            "series": series,
        })
    return {
        "contract": {
            "investment_amount": str(investment), "cap_amount": str(se.q2(se.D(params["cap_amount"]))),
            "funding_date": funding.isoformat(),
            "end_date": se.add_months(funding, int(params["max_term_months"])).isoformat(),
            "max_term_months": int(params["max_term_months"]),
            "servicing_fee_rate": str(servicing),
            "investor_breakeven_contributions": str(se.q2(investment / (1 - servicing))),
        },
        "assumptions": {
            "revenue": "the 90 verified_net days of verified_daily.csv, repeated (simulated data)",
            "merchant_payout_fee": f"estimated at {config.PAYOUT_FEE_ESTIMATE_RATE * 100:g}% of drc_due",
            "irr": "XIRR, actual days / 365; IRR is the investor's view, payout fees are the merchant's cost",
            "principal_recovered": "cumulative investor_net >= investment_amount",
            "cap_reached": "cumulative drc_due = cap_amount (contract ends)",
        },
        "scenarios": scenarios,
    }


def build_anomaly_daily(csv_text):
    """Per-day raw / verified / excluded / at_risk amounts and flags, from ai/anomaly.py's output."""
    rows = []
    for r in csv.DictReader(csv_text.splitlines()):
        rows.append({
            "date": r["date"],
            "raw": r["raw_gross"], "excluded": r["excluded_amount"], "at_risk": r["at_risk_amount"],
            "verified_gross": r["verified_gross"], "verified_net": r["verified_net"],
            "flagged_txn_count": int(r["flagged_txn_count"]),
            "flags": [f for f in r["day_flags"].split(";") if f],
        })
    return rows


# ----------------------------------------------------------------- main
def build_flagged(csv_text):
    """Excluded and under-review transactions (simulated history) with their reasons, from
    data/flagged_transactions.csv. Customer ids are synthetic (PP-xxxx, CARD-xxxx, CASH-ANON)."""
    rows = []
    for r in csv.DictReader(csv_text.splitlines()):
        rows.append({
            "txn_id": r["txn_id"], "timestamp": r["timestamp"], "channel": r["channel"],
            "customer_id": r["customer_id"], "amount_eur": r["amount_eur"],
            "list_price": r["list_price"], "discount_pct": r["discount_pct"],
            "tier": r["tier"], "reasons": [x for x in r["reasons"].split(";") if x],
        })
    return rows


def _prf(predicted, truth):
    tp = len(predicted & truth)
    precision = tp / len(predicted) if predicted else None
    recall = tp / len(truth) if truth else None
    return {"true": len(truth), "tp": tp, "fp": len(predicted) - tp, "fn": len(truth) - tp,
            "precision": precision, "recall": recall}


def build_anomaly_metrics(flagged_text, injected_text, daily_text, transactions_text=None):
    """Precision / recall of the detectors against the injected anomalies (ground truth)."""
    injected = list(csv.DictReader(injected_text.splitlines()))
    truth_txn, truth_day = {}, {}
    for r in injected:
        if r["txn_id"]:
            truth_txn.setdefault(r["anomaly_type"], set()).add(r["txn_id"])
        truth_day.setdefault(r["anomaly_type"], set()).add(r["date"])

    flagged = list(csv.DictReader(flagged_text.splitlines()))
    by_reason = {}
    for r in flagged:
        for reason in r["reasons"].split(";"):
            by_reason.setdefault(reason, set()).add(r["txn_id"])

    day_flags = {}
    for r in csv.DictReader(daily_text.splitlines()):
        for flag in r["day_flags"].split(";"):
            if flag:
                day_flags.setdefault(flag, set()).add(r["date"])

    detectors = []
    for detector, injected_type in (("SELF_DEALING_SUSPECT", "SELF_DEALING_SMALL_ORDERS"),
                                    ("PRICE_OVERRIDE_DEEP", "PRICE_OVERRIDE")):
        detectors.append({"level": "transaction", "detector": detector, "injected_type": injected_type,
                          **_prf(by_reason.get(detector, set()), truth_txn.get(injected_type, set()))})
    for detector, injected_type in (("CARD_REVENUE_MISSING", "CARD_REVENUE_MISSING"),
                                    ("EVENING_SHARE_DROP", "EVENING_TRAFFIC_DROP"),
                                    ("SELF_DEALING_DAY", "SELF_DEALING_SMALL_ORDERS"),
                                    ("VOID_SPIKE_DAY", "VOID_SPIKE")):
        detectors.append({"level": "day", "detector": detector, "injected_type": injected_type,
                          **_prf(day_flags.get(detector, set()), truth_day.get(injected_type, set()))})

    result = {"detectors": detectors}
    if transactions_text:
        business = {r["txn_id"] for r in csv.DictReader(transactions_text.splitlines())
                    if r["customer_type"] == "BUSINESS"}
        flagged_business = {r["txn_id"] for r in flagged if r["customer_type"] == "BUSINESS"}
        result["business_orders"] = {"total": len(business), "false_positives": len(flagged_business)}
    return result


def build_merchant_docs(merchant_dir):
    """extracted.json and reconciliation.json (as they are) plus the text of every merchant PDF.
    Returns {} if the documents have not been read and reconciled yet."""
    merchant_dir = Path(merchant_dir)
    extracted, reconciliation = merchant_dir / "extracted.json", merchant_dir / "reconciliation.json"
    if not (extracted.exists() and reconciliation.exists()):
        return {}
    from ai.contract_parser import read_pdf_text

    names = sorted(json.loads(extracted.read_text(encoding="utf-8"))["documents"])
    texts = {}
    for name in names:
        pdf = merchant_dir / name
        if not pdf.exists():
            raise SystemExit(f"{pdf} not found (it is listed in extracted.json). Run: python -m data.make_merchant_docs")
        texts[name] = read_pdf_text(pdf)
    return {
        "extracted.json": extracted.read_text(encoding="utf-8"),
        "reconciliation.json": reconciliation.read_text(encoding="utf-8"),
        "merchant_doc_texts.json": dump(texts),
    }


def build_health(health_dir):
    """data/health/recommendation.json as health_recommendation.json (a recommendation only; the file holds the
    metrics, the Claude recommendation and the chart series). Returns {} if engine.health_agent has not run yet."""
    path = Path(health_dir) / "recommendation.json"
    if not path.exists():
        return {}
    return {"health_recommendation.json": path.read_text(encoding="utf-8")}


def build_all(db_path, merchant_id, params, verified_daily_path, days, skip_paypal=False, reporting_module=None,
              data_dir=None):
    """Everything as {file name: text}. Nothing is written here."""
    data_dir = Path(data_dir) if data_dir else ROOT / "data"
    verified_daily_path = Path(verified_daily_path)
    if not verified_daily_path.exists():
        raise SystemExit(f"{verified_daily_path} not found. Run: python -m ai.anomaly")
    csv_text = verified_daily_path.read_text(encoding="utf-8")
    revenues = irr.load_daily_revenues(verified_daily_path)
    flagged_path, injected_path, transactions_path = (
        data_dir / "flagged_transactions.csv", data_dir / "injected_anomalies.csv", data_dir / "transactions.csv")
    for needed, hint in ((flagged_path, "python -m ai.anomaly"), (injected_path, "python -m data.generate_data")):
        if not needed.exists():
            raise SystemExit(f"{needed} not found. Run: {hint}")
    flagged_text = flagged_path.read_text(encoding="utf-8")
    injected_text = injected_path.read_text(encoding="utf-8")
    transactions_text = transactions_path.read_text(encoding="utf-8") if transactions_path.exists() else None

    actuals = []
    if Path(db_path).exists():
        conn = ledger.connect(db_path)
        try:
            actuals = irr.load_actuals(conn, merchant_id)
        finally:
            conn.close()

    files = {
        "verified_daily.csv": csv_text,
        "settlements.json": dump(build_settlements(db_path, merchant_id)),
        "projection.json": dump(build_projection(params, revenues, actuals)),
        "anomaly_daily.json": dump(build_anomaly_daily(csv_text)),
        "anomaly_flagged.json": dump(build_flagged(flagged_text)),
        "anomaly_metrics.json": dump(build_anomaly_metrics(flagged_text, injected_text, csv_text, transactions_text)),
    }
    files.update(build_merchant_docs(data_dir / "merchant_docs"))
    files.update(build_health(data_dir / "health"))
    if not skip_paypal:
        files["paypal_receipts.json"] = dump(build_receipts(days, reporting_module))
    return files


def main(argv=None):
    parser = argparse.ArgumentParser(description="Export the demo snapshot for the web app to data/demo/.")
    parser.add_argument("--db", default=str(ledger.DB_PATH))
    parser.add_argument("--params", default=str(se.PARAMS_PATH))
    parser.add_argument("--merchant-id", default=config.MERCHANT_ID)
    parser.add_argument("--verified-daily", default=str(irr.VERIFIED_DAILY_PATH))
    parser.add_argument("--days", nargs="+", type=date.fromisoformat, default=list(DEFAULT_DAYS),
                        help="Trading Days for paypal_receipts.json (default: 2026-11-06 2026-11-07)")
    parser.add_argument("--skip-paypal", action="store_true", help="do not call Transaction Search")
    parser.add_argument("--out", default=str(DEMO_DIR))
    args = parser.parse_args(argv)

    params = se.load_params(args.params)
    external_errors = ()
    if not args.skip_paypal:
        from paypal.client import PayPalError
        from paypal.reporting import ReportingError
        external_errors = (PayPalError, ReportingError)
    try:
        files = build_all(args.db, args.merchant_id, params, args.verified_daily, args.days,
                          skip_paypal=args.skip_paypal)
    except external_errors as exc:
        print(f"ERROR reading Transaction Search: {exc}\nUse --skip-paypal to export the other files.")
        return 1

    try:
        check_clean(files)
    except UnsafeExport as exc:
        print(f"ERROR: {exc}")
        return 1

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (out / name).write_text(text, encoding="utf-8")
        print(f"wrote {out / name}  ({len(text.encode('utf-8')):,} bytes)")
    contract_pdf = ROOT / "data" / "sample_contract.pdf"          # the sample contract (no personal data)
    if contract_pdf.exists():
        (out / contract_pdf.name).write_bytes(contract_pdf.read_bytes())
        print(f"wrote {out / contract_pdf.name}  (copy of the sample contract)")
    if args.skip_paypal:
        print("paypal_receipts.json was skipped (--skip-paypal); an existing file is left untouched.")
    if "extracted.json" not in files:
        print("Merchant documents were not exported: run `python -m ai.doc_reader --all` and "
              "`python -m engine.reconcile` first.")
    if "health_recommendation.json" not in files:
        print("Merchant health was not exported: run `python -m engine.health_agent` first.")
    print("Safety check passed: no '@', 'sk-', 'client_secret' or 'access_token' in any output.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
