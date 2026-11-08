# Reconciliation of merchant documents with our own data.
#
# Reads data/merchant_docs/extracted.json (made by ai/doc_reader.py) and cross-checks every figure the
# merchant reported against data we hold ourselves:
#
#   Z-Bericht / Tagesabschluss   paypal      vs  PayPal data of that day
#                                              (simulated days: daily_summary.csv paypal_eur;
#                                               real days: PayPal Transaction Search)
#                                card        vs  verified_daily.csv card_revenue_eur
#                                cash        vs  daily_summary.csv pos_cash_reported_eur
#                                gross_total vs  verified_daily.csv raw_gross
#   Umsatzsteuer-Voranmeldung    Kz81 net x 1.19 + Kz86 net x 1.07  vs  verified gross revenue of the month
#
# Tolerance +-3 %. Outside it a check is MISMATCH with the difference, the percentage and the likely
# explanation (over-reported / under-reported).
#
# A MISMATCH NEVER changes the split. It only produces a "review required" record: whether a wrong
# report is a breach is decided by a person (see the Financial and Tax Reporting, Representations and
# Consequences of Breach clauses of the contract). Findings with one root cause are ONE record:
# if card is under-reported and gross_total is short by the same amount, that is one finding.
#
# Usage (from the project root):  python -m engine.reconcile
import argparse
import json
import re
import sys
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS_DIR = ROOT / "data" / "merchant_docs"
EXTRACTED_PATH = DOCS_DIR / "extracted.json"
REPORT_PATH = DOCS_DIR / "reconciliation.json"
VERIFIED_DAILY = ROOT / "data" / "verified_daily.csv"
DAILY_SUMMARY = ROOT / "data" / "daily_summary.csv"
CONTRACT_PDF = ROOT / "data" / "sample_contract.pdf"

TOLERANCE_PERCENT = Decimal("3")
GROSS_EXPLAINED_EUR = Decimal("0.05")      # gross gap and component gaps may differ by this much (rounding)
CENT = Decimal("0.01")
COMPONENTS = ("paypal", "card", "cash")
CLAUSES = ("Financial and Tax Reporting", "Representations", "Consequences of Breach")


def q2(x):
    return Decimal(x).quantize(CENT, rounding=ROUND_HALF_UP)


# -------------------------------------------------------- our own data
class Reference:
    """The data we compare against. `paypal_fetch(day) -> Decimal` is called only for days that are not
    in the simulated history (real PayPal Transaction Search); without it such days have no reference."""

    def __init__(self, verified_rows, summary_rows, paypal_fetch=None):
        self.verified = {r["date"]: r for r in verified_rows}
        self.summary = {r["date"]: r for r in summary_rows}
        self.paypal_fetch = paypal_fetch

    def raw_gross(self, day):
        row = self.verified.get(day)
        return (Decimal(row["raw_gross"]), "verified_daily.csv raw_gross") if row else None

    def card(self, day):
        row = self.verified.get(day)
        return (Decimal(row["card_revenue_eur"]), "verified_daily.csv card_revenue_eur") if row else None

    def cash(self, day):
        row = self.summary.get(day)
        return (Decimal(row["pos_cash_reported_eur"]), "daily_summary.csv pos_cash_reported_eur") if row else None

    def paypal(self, day):
        row = self.summary.get(day)
        if row:
            return Decimal(row["paypal_eur"]), "daily_summary.csv paypal_eur"
        if self.paypal_fetch:
            return Decimal(self.paypal_fetch(day)), "PayPal Transaction Search"
        return None

    def verified_gross(self, start, end):
        """Verified gross revenue from start to end (inclusive); None unless every day is known."""
        days, day = [], start
        while day <= end:
            days.append(day.isoformat())
            day += timedelta(days=1)
        rows = [self.verified.get(d) for d in days]
        if not days or any(r is None for r in rows):
            return None
        total = sum((Decimal(r["verified_gross"]) for r in rows), Decimal("0.00"))
        return total, f"verified_daily.csv verified_gross, {days[0]}..{days[-1]} ({len(days)} days)"


def load_reference(paypal_fetch=None):
    import csv

    for path, hint in ((VERIFIED_DAILY, "python -m ai.anomaly"), (DAILY_SUMMARY, "python -m data.generate_data")):
        if not path.exists():
            raise SystemExit(f"{path} not found. Run: {hint}")
    rows = {}
    for key, path in (("verified", VERIFIED_DAILY), ("summary", DAILY_SUMMARY)):
        with path.open(newline="", encoding="utf-8") as handle:
            rows[key] = list(csv.DictReader(handle))
    return Reference(rows["verified"], rows["summary"], paypal_fetch)


def transaction_search_fetch(day):
    """Gross customer payments of a Berlin Trading Day from PayPal Transaction Search (real days)."""
    from paypal import reporting

    return sum((r["gross"] for r in reporting.fetch_trading_day(day)), Decimal("0.00"))


# ------------------------------------------------------------ checks
def compare(name, reported, reference, source, tolerance=TOLERANCE_PERCENT, label=None):
    """One check: reported vs reference, within +-tolerance percent or MISMATCH."""
    label = label or name
    if reported is None:
        return {"check": name, "status": "NOT_REPORTED", "reported": None, "reference": None if reference is None else str(q2(reference[0])),
                "reference_source": None if reference is None else reference[1], "difference": None, "percent": None,
                "direction": None, "explanation": f"the document does not report {label}"}
    if reference is None:
        return {"check": name, "status": "NO_REFERENCE", "reported": str(q2(reported)), "reference": None,
                "reference_source": source, "difference": None, "percent": None, "direction": None,
                "explanation": f"we hold no data to check {label} against"}
    ref, source = reference
    diff = reported - ref
    within = abs(diff) * 100 <= tolerance * abs(ref)             # exact: no rounding at the boundary
    percent = None if ref == 0 else (diff / ref * 100).quantize(CENT, rounding=ROUND_HALF_UP)
    direction = None if within else ("under-reported" if diff < 0 else "over-reported")
    if within:
        explanation = f"within +-{tolerance}% of our data"
    elif diff < 0:
        explanation = (f"{label} is {abs(diff):.2f} EUR below our data: possible under-reporting of {label} "
                       f"(less revenue share would be due), or a gap in our own data")
    else:
        explanation = (f"{label} is {diff:.2f} EUR above our data: possible over-reporting of {label}, "
                       f"or revenue that our screening excluded")
    return {"check": name, "status": "OK" if within else "MISMATCH", "reported": str(q2(reported)),
            "reference": str(q2(ref)), "reference_source": source, "difference": str(q2(diff)),
            "percent": None if percent is None else str(percent), "direction": direction, "explanation": explanation}


def _amount(values, name):
    value = values.get(name)
    return None if value is None else Decimal(value)


def check_z_report(values, reference, tolerance):
    day = values["period_start"]
    return [
        compare("paypal", _amount(values, "paypal"), reference.paypal(day), "PayPal data", tolerance, "PayPal payments"),
        compare("card", _amount(values, "card"), reference.card(day), "card terminal data", tolerance, "card payments"),
        compare("cash", _amount(values, "cash"), reference.cash(day), "cash data", tolerance, "cash payments"),
        compare("gross_total", _amount(values, "gross_total"), reference.raw_gross(day), "gross revenue", tolerance,
                "gross total"),
    ]


def check_vat_return(values, reference, tolerance):
    net19, net7 = _amount(values, "vat_19_net"), _amount(values, "vat_7_net")
    start, end = date.fromisoformat(values["period_start"]), date.fromisoformat(values["period_end"])
    if net19 is None or net7 is None:
        return [compare("vat_vs_verified_revenue", None, reference.verified_gross(start, end), "verified revenue",
                        tolerance, "the VAT bases")]
    reported = q2(net19 * Decimal("1.19") + net7 * Decimal("1.07"))
    return [compare("vat_vs_verified_revenue", reported, reference.verified_gross(start, end), "verified revenue",
                    tolerance, "gross revenue implied by Kz 81 x 1.19 + Kz 86 x 1.07")]


# ----------------------------------------------------------- reviews
def clause_numbers():
    """Section numbers of the clauses we cite, read from the contract PDF (numbering may change)."""
    numbers = {title: None for title in CLAUSES}
    if not CONTRACT_PDF.exists():
        return numbers
    try:
        from ai.contract_parser import read_pdf_text

        text = read_pdf_text(CONTRACT_PDF)
    except ImportError:
        return numbers
    for title in CLAUSES:
        pattern = r"(\d+)\.\s+" + r"\s+".join(map(re.escape, title.split())) + r"\."
        match = re.search(pattern, text)
        numbers[title] = int(match.group(1)) if match else None
    return numbers


def clause_text(numbers):
    def ref(title):
        return f"{title} (Section {numbers[title]})" if numbers.get(title) else title

    return (f"Possible breach of {ref('Financial and Tax Reporting')} and {ref('Representations')}; whether it is a "
            f"breach is decided by a person ({ref('Consequences of Breach')}).")


def build_reviews(file_name, day, checks, numbers):
    """Turn the MISMATCH checks of one document into review records. Findings with one root cause are
    merged: a gross_total gap that equals the gap of the mismatching channel(s) is not a second finding."""
    mismatches = [c for c in checks if c["status"] == "MISMATCH"]
    gross = next((c for c in mismatches if c["check"] == "gross_total"), None)
    parts = [c for c in mismatches if c["check"] in COMPONENTS]
    others = [c for c in mismatches if c["check"] not in COMPONENTS and c is not gross]
    groups = []                                      # [(root_cause, [checks])]
    gross_explained = bool(gross and parts and abs(Decimal(gross["difference"]) -
                                                   sum(Decimal(c["difference"]) for c in parts)) <= GROSS_EXPLAINED_EUR)
    if gross_explained and len(parts) > 1:
        groups.append(("; ".join(f"{c['check']} {c['direction']}" for c in parts), parts + [gross]))
    else:
        for c in parts:
            groups.append((f"{c['check']} {c['direction']}", [c] + ([gross] if gross_explained else [])))
    if gross and not gross_explained:
        groups.append((f"gross_total {gross['direction']}", [gross]))
    groups += [(f"{c['check']} {c['direction']}", [c]) for c in others]

    reviews = []
    for root_cause, members in groups:
        lead = members[0]
        also = [m["check"] for m in members[1:]]
        explanation = lead["explanation"]
        if also:
            explanation += (f". {', '.join(also)} is short by the same amount, so this is ONE finding with one root "
                            f"cause, not {len(members)} separate ones")
        reviews.append({
            "id": f"REV-{day}-{lead['check']}",
            "document": file_name, "date": day, "status": "review required",
            "root_cause": root_cause,
            "affected_checks": [m["check"] for m in members],
            "difference_eur": lead["difference"], "percent": lead["percent"],
            "reported": lead["reported"], "reference": lead["reference"], "reference_source": lead["reference_source"],
            "explanation": explanation,
            "contract": clause_text(numbers),
            "counts_as": "one review item (one root cause)",
            "automatic_action": "none: the split is unchanged until a person decides",
        })
    return reviews


# ----------------------------------------------------------- the report
def reconcile(extracted_documents, reference, tolerance=TOLERANCE_PERCENT, numbers=None):
    """extracted_documents: the 'documents' dict of extracted.json. Returns the report dict."""
    numbers = numbers if numbers is not None else clause_numbers()
    documents, reviews = {}, []
    for file_name, result in sorted(extracted_documents.items()):
        values = result["values"]
        if values["doc_type"] == "z_report":
            checks, label = check_z_report(values, reference, tolerance), values["period_start"]
        elif values["doc_type"] == "vat_return":
            checks, label = check_vat_return(values, reference, tolerance), f"{values['period_start']}..{values['period_end']}"
        else:
            checks, label = [], values.get("period_start")
        day = values["period_start"]
        found = build_reviews(file_name, day, checks, numbers)
        reviews += found
        count = lambda status: sum(1 for c in checks if c["status"] == status)  # noqa: E731
        documents[file_name] = {
            "doc_type": values["doc_type"], "period": label, "checks": checks,
            "summary": {"ok": count("OK"), "mismatch": count("MISMATCH"), "not_reported": count("NOT_REPORTED"),
                        "no_reference": count("NO_REFERENCE"), "review_items": len(found)},
        }
    return {
        "tolerance_percent": str(tolerance),
        "note": "A MISMATCH never changes the split; it creates a review record for a person to decide.",
        "documents": documents,
        "review_required": reviews,
    }


def format_table(report):
    lines = [f"Reconciliation, tolerance +-{report['tolerance_percent']}%", ""]
    header = f"{'document':<28}{'check':<26}{'reported':>11}{'our data':>11}{'diff EUR':>10}{'%':>8}  status"
    lines += [header, "-" * len(header)]
    for name, doc in report["documents"].items():
        for c in doc["checks"]:
            status = c["status"] + (f" ({c['direction']})" if c["direction"] else "")
            lines.append(f"{name[:27]:<28}{c['check']:<26}{c['reported'] or '-':>11}{c['reference'] or '-':>11}"
                         f"{c['difference'] or '-':>10}{c['percent'] or '-':>8}  {status}")
    lines.append("")
    if not report["review_required"]:
        lines.append("No review required.")
    for r in report["review_required"]:
        lines += [f"REVIEW REQUIRED {r['id']}: {r['document']}",
                  f"  root cause : {r['root_cause']}   (affects: {', '.join(r['affected_checks'])})",
                  f"  difference : {r['difference_eur']} EUR ({r['percent']}%)",
                  f"  explanation: {r['explanation']}",
                  f"  contract   : {r['contract']}",
                  f"  action     : {r['automatic_action']}"]
    return "\n".join(lines)


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Cross-check extracted merchant documents with our own data.")
    parser.add_argument("--extracted", default=str(EXTRACTED_PATH))
    parser.add_argument("--out", default=str(REPORT_PATH))
    parser.add_argument("--tolerance", type=Decimal, default=TOLERANCE_PERCENT, help="percent, default 3")
    parser.add_argument("--no-live", action="store_true", help="never call PayPal Transaction Search")
    args = parser.parse_args(argv)

    extracted = Path(args.extracted)
    if not extracted.exists():
        raise SystemExit(f"{extracted} not found. Run: python -m ai.doc_reader --all")
    documents = json.loads(extracted.read_text(encoding="utf-8"))["documents"]
    failed = [n for n, d in documents.items() if not d["validation"]["passed"]]
    if failed:
        raise SystemExit(f"These extractions failed validation and are not reconciled: {', '.join(failed)}")

    report = reconcile(documents, load_reference(None if args.no_live else transaction_search_fetch), args.tolerance)
    print(format_table(report))
    Path(args.out).write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"\nSaved {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
