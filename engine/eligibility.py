# Eligibility self-check for a merchant who wants to apply for DRC financing. Deterministic code, no AI.
#
# Nine conditions, thresholds in data/eligibility_rules.json (all ILLUSTRATIVE; the account-age and annual-sales
# thresholds are modelled on the published PayPal Businesskredit conditions):
#   1 PayPal business account at least 90 days old        2 PayPal sales, annualised, at least EUR 12,000
#   3 verified revenue history of at least 90 trading days (needed for the Baseline Threshold)
#   4 stable revenue: the health covenant is not triggered (computed with engine/health_agent.py)
#   5 verifiable share of revenue at least 60% (PayPal revenue + revenue covered by reconciled documents)
#   6 documents complete: a recent POS daily report and the latest VAT return, read by the AI with validation PASSED
#   7 reconciliation: no unresolved MISMATCH between a document and our data
#   8 screening: excluded transactions are less than 5% of revenue
#   9 data consent: every required data source is granted
# Each check returns: id, number, name, status (met / not_met / action_needed / needs_consent), the numbers
# used, how to fix it, and the data sources it depends on. If all nine are met there is an "indicative
# amount" (8% of annualised verified revenue, at most EUR 50,000). Indicative only, not an offer.
#
# The functions here are pure (no file or network access) except load_rules() and the profile loaders.
# Profiles: the demo merchant is built from the project's data files, the sample bakery is
# data/eligibility_scenarios/new_merchant.json.
#
# Usage (from the project root):
#   python -m engine.eligibility                       (prints the checklist of both profiles)
#   python -m engine.eligibility --profile sample-bakery
import argparse
import csv
import json
import sys
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine import health_agent as ha  # noqa: E402
from engine import split_engine as se  # noqa: E402

RULES_PATH = ROOT / "data" / "eligibility_rules.json"
SCENARIO_DIR = ROOT / "data" / "eligibility_scenarios"
NEW_MERCHANT_PATH = SCENARIO_DIR / "new_merchant.json"
MERCHANT_DOCS_DIR = ROOT / "data" / "merchant_docs"
DAILY_SUMMARY_PATH = ROOT / "data" / "daily_summary.csv"
FLAGGED_PATH = ROOT / "data" / "flagged_transactions.csv"

CENT = Decimal("0.01")
STATUSES = ("met", "not_met", "action_needed", "needs_consent")
NOT_AN_OFFER = "Indicative only, not an offer. The investor makes the final decision."
DEMO_ID, BAKERY_ID = "kaffee-und-tee", "sample-bakery"

# id, number, name
CHECKS = (
    ("paypal_account_age", 1, "PayPal business account history"),
    ("paypal_annual_sales", 2, "PayPal annual sales"),
    ("verified_history", 3, "Verified revenue history"),
    ("stable_revenue", 4, "Stable revenue"),
    ("verifiable_share", 5, "Verifiable share of revenue"),
    ("documents_complete", 6, "Documents complete"),
    ("reconciliation_clean", 7, "Document reconciliation"),
    ("screening", 8, "Transaction screening"),
    ("consent", 9, "Data consent"),
)
# data sources each check needs (see apply_consent); the check shows needs_consent while one is not granted
DEPENDS_ON = {
    "paypal_account_age": ["paypal"], "paypal_annual_sales": ["paypal"], "verified_history": ["paypal"],
    "stable_revenue": ["paypal"], "verifiable_share": ["paypal", "pos", "vat"],
    "documents_complete": ["pos", "vat"], "reconciliation_clean": ["pos", "vat"],
    "screening": ["paypal"], "consent": [],
}


def q2(x):
    return Decimal(x).quantize(CENT, rounding=ROUND_HALF_UP)


def D(x):
    return Decimal(str(x))


def days_text(n):
    """1 -> '1 day', 45 -> '45 days'."""
    return f"{n} day" if int(n) == 1 else f"{n} days"


def m(x):
    """Amount for a sentence: 29375.19 -> '29,375.19'."""
    return f"{q2(x):,.2f}"


def load_rules(path=RULES_PATH):
    path = Path(path)
    if not path.exists():
        raise SystemExit(f"{path} not found.")
    return json.loads(path.read_text(encoding="utf-8"))


def _check(check_id, status, requirement, numbers, detail, how_to_fix="", earliest_days=None):
    number, name = next((n, label) for cid, n, label in CHECKS if cid == check_id)
    check = {"id": check_id, "number": number, "name": name, "status": status, "base_status": status,
             "requirement": requirement, "numbers": {k: str(v) for k, v in numbers.items()}, "detail": detail,
             "how_to_fix": how_to_fix, "depends_on": DEPENDS_ON[check_id], "missing_consent": []}
    if earliest_days is not None:
        check["earliest_possible_days"] = int(earliest_days)
    return check


# ============================================================================ the nine checks
def _daily(profile):
    d = profile["daily"]
    days = [date.fromisoformat(x) for x in d["dates"]]
    return days, [D(x) for x in d["revenue"]], [D(x) for x in d["paypal_revenue"]], [D(x) for x in d["verified_net"]]


def check_account_age(profile, rules):
    need = int(rules["paypal_account_age"]["min_days"])
    age = (date.fromisoformat(profile["as_of"]) - date.fromisoformat(profile["paypal"]["account_opened"])).days
    ok = age >= need
    return _check("paypal_account_age", "met" if ok else "not_met", f"at least {need} days",
                  {"Account age (days)": age, "Required (days)": need},
                  f"The PayPal business account is {age} days old; {need} days are required.",
                  "" if ok else f"Only time helps: the account needs {days_text(need - age)} more.",
                  None if ok else need - age)


def check_annual_sales(profile, rules):
    need = D(rules["paypal_annual_sales"]["min_eur"])
    sales, span = D(profile["paypal"]["sales_eur"]), int(profile["paypal"]["sales_period_days"])
    annual = q2(sales / span * 365)
    ok = annual >= need
    return _check("paypal_annual_sales", "met" if ok else "not_met", f"at least EUR {need:,.0f} a year",
                  {"PayPal sales in the period (EUR)": q2(sales), "Period (days)": span,
                   "Annualised PayPal sales (EUR)": annual, "Required (EUR)": q2(need)},
                  f"PayPal sales of EUR {m(sales)} in {span} days are EUR {m(annual)} a year; EUR {need:,.0f} are required.",
                  "" if ok else f"Route more of your sales through PayPal: EUR {m(need - annual)} a year are missing.")


def check_verified_history(profile, rules):
    need = int(rules["verified_history"]["min_trading_days"])
    days, revenue, _, _ = _daily(profile)
    trading = sum(1 for r in revenue if r > 0)
    ok = trading >= need
    return _check("verified_history", "met" if ok else "not_met", f"at least {need} trading days",
                  {"Verified trading days": trading, "Required (trading days)": need},
                  f"{trading} trading days of verified revenue are available; {need} are needed to compute the Baseline Threshold.",
                  "" if ok else f"Only time helps: {need - trading} more trading {'day' if need - trading == 1 else 'days'} of PayPal history "
                  f"{'is' if need - trading == 1 else 'are'} needed.",
                  None if ok else need - trading)


def check_stable_revenue(profile, terms):
    days, _, _, net = _daily(profile)
    series = list(zip(days, net))
    metrics = ha.compute_metrics(series, series[-1][0], terms) if len(series) >= terms["health_window_long_days"] else None
    short, long_ = terms["health_window_short_days"], terms["health_window_long_days"]
    limit = terms["health_trigger_drop_pct"]
    requirement = f"the {short}-day average is not more than {ha.plain(limit)}% below the {long_}-day average"
    if metrics is None:
        return _check("stable_revenue", "not_met", requirement, {"Days of data": len(series), "Needed (days)": long_},
                      f"Only {len(series)} days of data; {long_} are needed to compare the two averages.",
                      f"Wait until {long_ - len(series)} more days of data exist.", long_ - len(series))
    shown = ha.display_metrics(metrics, terms)
    ok = not metrics["triggered"]
    return _check("stable_revenue", "met" if ok else "not_met", requirement,
                  {f"{short}-day average (EUR)": shown["ma_short"], f"{long_}-day average (EUR)": shown["ma_long"],
                   f"Fall vs {long_}-day average (%)": shown["decline_pct"], "Limit (%)": ha.plain(limit)},
                  f"The {short}-day average is EUR {shown['ma_short']}, the {long_}-day average EUR {shown['ma_long']}: "
                  f"a fall of {shown['decline_pct']}% against a limit of {ha.plain(limit)}%.",
                  "" if ok else "Revenue must recover first; the health covenant of the contract would trigger today.")


def covered_days(profile):
    """Days covered by a document that reconciles with our data (a daily report covers one day, a VAT return its period)."""
    days = set()
    for doc in profile.get("documents", []):
        if doc.get("reconciliation_status") == "OK":
            start, end = date.fromisoformat(doc["period_start"]), date.fromisoformat(doc["period_end"])
            days.update(start + timedelta(days=i) for i in range((end - start).days + 1))
    return days


def check_verifiable_share(profile, rules):
    need = D(rules["verifiable_share"]["min_percent"])
    days, revenue, paypal, _ = _daily(profile)
    total = sum(revenue, Decimal(0))
    covered = covered_days(profile)
    paypal_total = sum(paypal, Decimal(0))
    documents = sum((r - p for d, r, p in zip(days, revenue, paypal) if d in covered), Decimal(0))
    share_paypal = paypal_total / total * 100 if total else Decimal(0)
    share_docs = documents / total * 100 if total else Decimal(0)
    share = share_paypal + share_docs
    ok = q2(share) >= need
    numbers = {"PayPal share of revenue (%)": q2(share_paypal), "Covered by reconciled documents (%)": q2(share_docs),
               "Verifiable share (%)": q2(share), "Required (%)": ha.plain(need)}
    fix = ""
    if not ok:
        gap = q2(need - q2(share))
        numbers["Missing (percentage points)"] = gap
        options = vat_return_options(days, revenue, paypal, covered, total, share, date.fromisoformat(profile["as_of"]))
        fix = (f"The {q2(share)}% is {q2(share_paypal)}% visible in PayPal plus {q2(share_docs)}% covered by reconciled "
               f"documents; {ha.plain(need)}% is required, so {gap} percentage points are missing. ")
        if options:
            for o in options:
                numbers[f"Adds with the UStVA for {o['label']} (%)"] = o["adds"]
                numbers[f"Total with the UStVA for {o['label']} (%)"] = o["total"]
            fix += ("Submit the VAT return (UStVA) for " + " or for ".join(
                f"{o['label']} ({o['first']} to {o['last']}; adds {o['adds']}%, giving {o['total']}%)" for o in options)
                    + ". A daily POS report covers only one day. ")
        else:
            fix += "Submit POS daily reports or VAT returns for periods that are not covered yet; a daily report covers one day. "
        fix += "Resolve any MISMATCH first: a document that does not reconcile does not count."
    return _check("verifiable_share", "met" if ok else "action_needed", f"at least {ha.plain(need)}% of revenue", numbers,
                  f"{q2(share_paypal)}% of revenue is visible in PayPal and {q2(share_docs)}% is covered by reconciled "
                  f"documents: {q2(share)}% verifiable against {ha.plain(need)}% required.", fix)


def vat_return_options(days, revenue, paypal, covered, total, share, as_of):
    """Complete calendar months that are not covered yet, with what a VAT return for each would add to the share.
    The month of `as_of` is left out: its VAT return cannot be filed yet."""
    months = {}
    for d, r, p in zip(days, revenue, paypal):
        if d not in covered:
            months.setdefault((d.year, d.month), []).append((d, r - p))
    options = []
    for (year, month), rows in sorted(months.items()):
        month_end = (date(year + month // 12, month % 12 + 1, 1) - timedelta(days=1))
        if month_end >= as_of or not total:
            continue
        adds = q2(sum((x for _, x in rows), Decimal(0)) / total * 100)
        if adds > 0:
            options.append({"label": rows[0][0].strftime("%B %Y"), "first": rows[0][0].isoformat(),
                            "last": rows[-1][0].isoformat(), "adds": adds, "total": q2(share + adds)})
    return options


def check_documents(profile, rules):
    rule = rules["documents_complete"]
    as_of = date.fromisoformat(profile["as_of"])
    pos_max, vat_max = int(rule["recent_pos_report_max_age_days"]), int(rule["vat_return_max_age_days"])
    docs = profile.get("documents", [])

    def recent(doc, max_age):
        age = (as_of - date.fromisoformat(doc["period_end"])).days
        return 0 <= age <= max_age

    pos = [d for d in docs if d["type"] == "z_report" and recent(d, pos_max)]
    vat = [d for d in docs if d["type"] == "vat_return" and recent(d, vat_max)]
    failed = [d for d in pos + vat if not d.get("validation_passed")]
    problems, fixes = [], []
    if not pos:
        problems.append(f"no POS daily report from the last {pos_max} days")
        fixes.append("Upload at least one recent POS daily report (Z-Bericht) as a PDF.")
    if not vat:
        problems.append("no recent VAT return (UStVA)")
        fixes.append("Upload your latest VAT return (UStVA) as a PDF.")
    if failed:
        problems.append(f"{len(failed)} document(s) failed the AI reading checks")
        fixes.append("Upload a clearer copy of: " + ", ".join(d["file"] for d in failed) + ".")
    ok = not problems
    return _check("documents_complete", "met" if ok else "action_needed",
                  "at least 1 recent POS daily report and the latest VAT return, read with validation PASSED",
                  {"Recent POS daily reports": len(pos), "Recent VAT returns": len(vat),
                   "Documents failing validation": len(failed)},
                  "All required documents are present and were read successfully." if ok
                  else "Missing or unreadable: " + "; ".join(problems) + ".",
                  " ".join(fixes))


def check_reconciliation(profile, rules):
    open_items = [d for d in profile.get("documents", [])
                  if d.get("reconciliation_status") == "MISMATCH" and not d.get("review_resolved")]
    if not open_items:
        return _check("reconciliation_clean", "met", "no unresolved MISMATCH",
                      {"Unresolved MISMATCH": 0}, "No document disagrees with our data by more than the tolerance.")
    first = open_items[0]["mismatch"]
    label = first.get("label", first.get("check", "figure"))
    numbers = {"Unresolved MISMATCH": len(open_items), "Document": open_items[0]["file"],
               f"{label.capitalize()} reported (EUR)": first["reported"], "Our data (EUR)": first["reference"],
               "Difference (EUR)": first["difference_eur"], "Difference (%)": first["percent"]}
    detail = "; ".join(
        f"{d['file']}: {d['mismatch'].get('label', 'figure')} reported EUR {d['mismatch']['reported']}, our data shows "
        f"EUR {d['mismatch']['reference']} (difference EUR {d['mismatch']['difference_eur']}, {d['mismatch']['percent']}%)"
        for d in open_items) + "."
    return _check("reconciliation_clean", "action_needed", "no unresolved MISMATCH", numbers, detail,
                  "Explain the difference in writing, or send a corrected report. A person reviews it; "
                  "nothing changes automatically.")


def check_screening(profile, rules):
    limit = D(rules["screening"]["max_excluded_percent"])
    s = profile["screening"]
    raw, excluded = D(s["raw_gross_eur"]), D(s["excluded_eur"])
    share = excluded / raw * 100 if raw else Decimal(0)
    ok = share < limit
    by_count = Decimal(s["excluded_txns"]) / Decimal(s["total_txns"]) * 100 if s["total_txns"] else Decimal(0)
    return _check("screening", "met" if ok else "not_met", f"excluded transactions below {ha.plain(limit)}% of revenue (by amount)",
                  {"Revenue (EUR)": q2(raw), "Excluded (EUR)": q2(excluded), "Excluded transactions": s["excluded_txns"],
                   "Transactions": s["total_txns"], "Excluded share by amount (%)": q2(share),
                   "Excluded share by count (%)": q2(by_count), "Limit (%)": ha.plain(limit)},
                  f"By amount, EUR {m(excluded)} of EUR {m(raw)} were excluded by screening: {q2(share)}% against a limit "
                  f"of {ha.plain(limit)}%. (By count it is {s['excluded_txns']:,} of {s['total_txns']:,} transactions, "
                  f"{q2(by_count)}%; the limit applies to the amount.)",
                  "" if ok else "Too many orders look artificial. Explain them, or stop the pattern, before applying.")


def check_consent(rules, consents):
    required = list(rules["consent"]["required_sources"])
    missing = [s for s in required if not consents.get(s)]
    return _check("consent", "needs_consent" if missing else "met", "all required data sources are shared",
                  {"Required sources shared": f"{len(required) - len(missing)} of {len(required)}"},
                  "Every required data source is shared." if not missing else
                  "Not shared yet: " + ", ".join(missing) + ".",
                  "" if not missing else "Tick the required data sources in Step 1.")


# ============================================================================ evaluation
def default_consents(rules):
    return {s: True for s in rules["rules"]["consent"]["required_sources"]}


def evaluate(profile, rules, params, consents=None):
    """The nine checks for a profile. `params` are the parsed contract parameters (health covenant terms)."""
    rules_by_id = rules["rules"]
    terms = ha.health_terms(params)
    consents = default_consents(rules) if consents is None else consents
    checks = [
        check_account_age(profile, rules_by_id), check_annual_sales(profile, rules_by_id),
        check_verified_history(profile, rules_by_id), check_stable_revenue(profile, terms),
        check_verifiable_share(profile, rules_by_id), check_documents(profile, rules_by_id),
        check_reconciliation(profile, rules_by_id), check_screening(profile, rules_by_id),
        check_consent(rules_by_id, {s: True for s in rules_by_id["consent"]["required_sources"]}),
    ]
    return apply_consent(checks, consents, rules_by_id["consent"]["required_sources"])


def apply_consent(checks, consents, required_sources):
    """Copy of the checks with consent applied: a check whose data source is not shared shows needs_consent;
    condition 9 itself is needs_consent while a required source is missing. Works on checks read from a JSON file."""
    missing = [s for s in required_sources if not consents.get(s)]
    result = []
    for check in checks:
        c = dict(check)
        base = c.get("base_status", c["status"])
        c["base_status"] = base
        if c["id"] == "consent":
            c["status"] = "needs_consent" if missing else "met"
            c["missing_consent"] = list(missing)
            c["numbers"] = {"Required sources shared": f"{len(required_sources) - len(missing)} of {len(required_sources)}"}
            c["detail"] = "Every required data source is shared." if not missing else "Not shared yet: " + ", ".join(missing) + "."
            c["how_to_fix"] = "" if not missing else "Tick the required data sources in Step 1."
        else:
            blocked = [s for s in c.get("depends_on", []) if s in missing]
            c["status"] = "needs_consent" if blocked else base
            c["missing_consent"] = blocked
        result.append(c)
    return result


def progress(checks):
    return sum(1 for c in checks if c["status"] == "met"), len(checks)


def annual_verified_revenue(profile):
    """Verified revenue (revenue minus excluded) annualised from the profile's period."""
    s = profile["screening"]
    days = len(profile["daily"]["dates"])
    return q2((D(s["raw_gross_eur"]) - D(s["excluded_eur"])) / days * 365)


def indicative_amount(annual_verified_eur, rules):
    """8% of annualised verified revenue, capped. Only meaningful when all nine conditions are met."""
    spec = rules["indicative_amount"]
    percent, cap = D(spec["percent_of_annual_verified_revenue"]), D(spec["cap_eur"])
    uncapped = q2(D(annual_verified_eur) * percent / 100)
    return {"amount": min(uncapped, q2(cap)), "uncapped": uncapped, "capped": uncapped > cap, "percent": percent,
            "cap": q2(cap), "annual_verified_revenue": q2(annual_verified_eur), "disclaimer": spec.get("disclaimer", NOT_AN_OFFER)}


# ============================================================================ profiles
def _read_csv(path):
    with Path(path).open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def _document_records(docs_dir):
    """Documents as the eligibility check sees them: type, period, validation, reconciliation (from the AI run)."""
    extracted = json.loads((docs_dir / "extracted.json").read_text(encoding="utf-8"))["documents"]
    reconciliation = json.loads((docs_dir / "reconciliation.json").read_text(encoding="utf-8"))
    labels = {"card": "card payments", "paypal": "PayPal payments", "cash": "cash payments", "gross_total": "gross total"}
    records = []
    for name in sorted(extracted):
        item, values = extracted[name], extracted[name]["values"]
        recon = reconciliation["documents"].get(name, {})
        checks = recon.get("checks", [])
        bad = next((c for c in checks if c["status"] == "MISMATCH"), None)
        record = {"file": name, "type": values["doc_type"], "period_start": values["period_start"],
                  "period_end": values["period_end"], "validation_passed": not item["validation"]["failed"],
                  "reconciliation_status": "MISMATCH" if bad else ("OK" if checks else None), "review_resolved": False}
        if bad:
            review = next((r for r in reconciliation.get("review_required", []) if r["document"] == name), None)
            lead = next((c for c in checks if review and c["check"] in review["affected_checks"] and c["status"] == "MISMATCH"), bad)
            record["mismatch"] = {"check": lead["check"], "label": labels.get(lead["check"], lead["check"]),
                                  "reported": lead["reported"], "reference": lead["reference"],
                                  "difference_eur": lead["difference"], "percent": lead["percent"]}
        records.append(record)
    return records


def profile_demo_merchant(params, root=ROOT):
    """Kaffee und Tee GmbH from the project's own data (simulated 90-day history plus the sample documents)."""
    daily = _read_csv(root / "data" / "verified_daily.csv")
    summary = {r["date"]: r for r in _read_csv(root / "data" / "daily_summary.csv")}
    flagged = _read_csv(root / "data" / "flagged_transactions.csv")
    dates = [r["date"] for r in daily]
    paypal = [D(summary[d]["paypal_eur"]) for d in dates]
    return {
        "id": DEMO_ID, "name": "Kaffee und Tee GmbH", "sample": False,
        "description": "Demo merchant. Simulated 90-day history (2026-08-08 to 2026-11-05), sample POS reports and VAT "
                       "return. The PayPal account is assumed to be open since the first day of the history.",
        "as_of": params["funding_date"],
        "paypal": {"account_opened": dates[0], "sales_eur": str(q2(sum(paypal, Decimal(0)))), "sales_period_days": len(dates)},
        "daily": {"dates": dates, "revenue": [r["raw_gross"] for r in daily],
                  "paypal_revenue": [str(p) for p in paypal], "verified_net": [r["verified_net"] for r in daily]},
        "documents": _document_records(root / "data" / "merchant_docs"),
        "screening": {"raw_gross_eur": str(q2(sum((D(r["raw_gross"]) for r in daily), Decimal(0)))),
                      "excluded_eur": str(q2(sum((D(r["excluded_amount"]) for r in daily), Decimal(0)))),
                      "excluded_txns": sum(1 for f in flagged if f["tier"] == "EXCLUDED"),
                      "total_txns": sum(int(r["txn_count"]) for r in summary.values())},
    }


def load_profile_file(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_profiles(params):
    """Both profiles, in dropdown order."""
    return [profile_demo_merchant(params), load_profile_file(NEW_MERCHANT_PATH)]


def profile_summary(profile):
    return {k: profile[k] for k in ("id", "name", "sample", "description", "as_of")}


def format_checklist(checks):
    icon = {"met": "[ok]", "not_met": "[x] ", "action_needed": "[!] ", "needs_consent": "[?] "}
    met, total = progress(checks)
    lines = [f"{met} of {total} conditions met"]
    for c in checks:
        lines.append(f"  {icon[c['status']]} {c['number']}. {c['name']}: {c['status']} - {c['detail']}")
        if c["status"] != "met" and c["how_to_fix"]:
            lines.append(f"        how to fix: {c['how_to_fix']}")
    return lines


def main(argv=None):
    parser = argparse.ArgumentParser(description="Eligibility checklist for the demo profiles (no AI, no network).")
    parser.add_argument("--profile", choices=[DEMO_ID, BAKERY_ID, "all"], default="all")
    parser.add_argument("--params", default=str(se.PARAMS_PATH))
    args = parser.parse_args(argv)
    params, rules = se.load_params(args.params), load_rules()
    for profile in load_profiles(params):
        if args.profile not in ("all", profile["id"]):
            continue
        checks = evaluate(profile, rules, params)
        print(f"\n{profile['name']}{' (SAMPLE)' if profile['sample'] else ''}, as of {profile['as_of']}")
        print("\n".join(format_checklist(checks)))
        if progress(checks)[0] == progress(checks)[1]:
            ind = indicative_amount(annual_verified_revenue(profile), rules)
            print(f"  Indicative amount: EUR {ind['amount']:,.2f}. {ind['disclaimer']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
