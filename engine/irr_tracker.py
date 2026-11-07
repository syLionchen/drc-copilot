# IRR tracker and scenario projection (CLAUDE.md §3 rule 6).
#
# Three annualised rates, all XIRR on actual dates (days / 365). IRR is always the INVESTOR's view,
# the payout fee is the merchant's cost and never part of the investor IRR:
#   gross IRR            -investment on the funding date, + drc_due on every settlement day
#   net IRR              -investment on the funding date, + investor_net (after the servicing fee)
#   merchant effective   +investment on the funding date, -(drc_due + merchant_payout_fee) on every day
#   APR                  (what the merchant really pays, fees included)
#
# The real ledger only has a few days, so IRR on actual data is not meaningful. The projection
# continues after the last actual settlement: the 90 verified_net values of data/verified_daily.csv
# are cycled as future daily revenue (day 1 of the projection = row 1), every day is split with
# engine.split_engine.calculate_split (progressive tiers, cap cut, servicing fee), and the merchant's
# payout fee is estimated as 2% of drc_due. It runs until the cap is reached or the maximum term ends.
# Stress scenarios scale all revenue by 0.8 and 0.6.
#
# "Principal recovered" = the investor's cumulative investor_net reaches the investment amount.
# "Cap reached" = cumulative drc_due reaches the repayment cap (the contract ends).
#
# Usage (from the project root):  python -m engine.irr_tracker [--db data/drc.db]
import argparse
import csv
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import config
from engine import ledger
from engine import split_engine as se

ROOT = Path(__file__).resolve().parent.parent
VERIFIED_DAILY_PATH = ROOT / "data" / "verified_daily.csv"
PLOT_PATH = ROOT / "docs" / "screenshots" / "irr_projection.png"
MIN_DAYS_FOR_IRR = 30
SCENARIOS = (("Baseline", Decimal("1.0")), ("-20%", Decimal("0.8")), ("-40%", Decimal("0.6")))


# ------------------------------------------------------------------ XIRR
def xnpv(rate, flows):
    """Net present value of dated flows [(date, amount)], discounted to the earliest date (days / 365)."""
    t0 = flows[0][0]
    return sum(float(amount) / (1.0 + rate) ** ((d - t0).days / 365.0) for d, amount in flows)


def xirr(flows, low=-0.99, high=100.0, tolerance=1e-12, max_iterations=300):
    """Annualised rate that makes xnpv zero (bisection). None if it is undefined: no sign change in
    the flows, all flows on one day, or no root between -99% and +10,000%."""
    flows = sorted(flows, key=lambda flow: flow[0])
    if not any(a < 0 for _, a in flows) or not any(a > 0 for _, a in flows):
        return None
    if flows[0][0] == flows[-1][0]:
        return None
    f_low, f_high = xnpv(low, flows), xnpv(high, flows)
    if f_low * f_high > 0:
        return None
    for _ in range(max_iterations):
        mid = (low + high) / 2
        f_mid = xnpv(mid, flows)
        if abs(f_mid) < tolerance or (high - low) < tolerance:
            return mid
        if f_low * f_mid < 0:
            high = mid
        else:
            low, f_low = mid, f_mid
    return (low + high) / 2


# --------------------------------------------------------------- inputs
def load_actuals(conn, merchant_id, fee_rate=config.PAYOUT_FEE_ESTIMATE_RATE):
    """Confirmed (SUCCESS) settlements from the ledger. A missing payout fee is estimated."""
    rows = []
    for r in ledger.list_settlements(conn, merchant_id):
        if r["status"] != ledger.COUNTED_STATUS:
            continue
        drc_due = Decimal(r["drc_due"])
        fee = Decimal(r["merchant_payout_fee"]) if r["merchant_payout_fee"] is not None \
            else se.q2(drc_due * fee_rate)
        rows.append({"date": date.fromisoformat(r["date"]), "drc_due": drc_due,
                     "investor_net": Decimal(r["investor_net"]), "fee": fee, "actual": True})
    return rows


def load_daily_revenues(path=VERIFIED_DAILY_PATH):
    """The verified_net column of data/verified_daily.csv, in date order."""
    path = Path(path)
    if not path.exists():
        raise SystemExit(f"{path} not found. Run: python -m ai.anomaly")
    with path.open(newline="", encoding="utf-8") as handle:
        values = [Decimal(row["verified_net"]) for row in csv.DictReader(handle)]
    if not values:
        raise SystemExit(f"{path} has no rows.")
    return values


# ----------------------------------------------------------- projection
@dataclass
class Projection:
    name: str
    factor: Decimal
    funding_date: date
    end_date: date               # first day after the maximum term
    investment: Decimal
    cap: Decimal
    rows: list                   # actual rows first, then simulated rows
    cap_reached_on: object       # date or None
    principal_recovered_on: object
    avg_daily_revenue: Decimal


def project(params, revenues, factor, actuals, fee_rate=config.PAYOUT_FEE_ESTIMATE_RATE, name="Baseline"):
    """Actual settlements, then simulated days until the cap is reached or the term ends."""
    investment = se.q2(se.D(params["investment_amount"]))
    cap = se.q2(se.D(params["cap_amount"]))
    funding = date.fromisoformat(params["funding_date"])
    end = se.add_months(funding, int(params["max_term_months"]))

    rows, state = [], {"drc": Decimal("0.00"), "net": Decimal("0.00"), "cap_on": None, "principal_on": None}

    def add(day, drc_due, investor_net, fee, actual):
        state["drc"] += drc_due
        state["net"] += investor_net
        rows.append({"date": day, "drc_due": drc_due, "investor_net": investor_net, "fee": fee,
                     "cumulative": state["drc"], "actual": actual})
        if state["principal_on"] is None and state["net"] >= investment:
            state["principal_on"] = day
        if state["cap_on"] is None and state["drc"] >= cap:
            state["cap_on"] = day

    for a in sorted(actuals, key=lambda r: r["date"]):
        add(a["date"], a["drc_due"], a["investor_net"], a["fee"], True)

    day = max(a["date"] for a in actuals) + timedelta(days=1) if actuals else funding
    k, simulated_revenue = 0, []
    while day < end and state["drc"] < cap:
        revenue = se.q2(revenues[k % len(revenues)] * factor)
        split = se.calculate_split(revenue, params, state["drc"])     # same math as the live engine
        add(day, split.drc_due, split.investor_net, se.q2(split.drc_due * fee_rate), False)
        simulated_revenue.append(revenue)
        day += timedelta(days=1)
        k += 1

    sample = simulated_revenue or [se.q2(v * factor) for v in revenues]
    return Projection(name, factor, funding, end, investment, cap, rows, state["cap_on"],
                      state["principal_on"], sum(sample, Decimal("0.00")) / len(sample))


def flows_of(p, kind):
    """Dated cash flows of a projection for one of the three rates."""
    if kind == "gross":
        return [(p.funding_date, -p.investment)] + [(r["date"], r["drc_due"]) for r in p.rows]
    if kind == "net":
        return [(p.funding_date, -p.investment)] + [(r["date"], r["investor_net"]) for r in p.rows]
    if kind == "merchant":
        return [(p.funding_date, p.investment)] + [(r["date"], -(r["drc_due"] + r["fee"])) for r in p.rows]
    raise ValueError(kind)


def metrics(p):
    total_drc = sum((r["drc_due"] for r in p.rows), Decimal("0.00"))
    total_net = sum((r["investor_net"] for r in p.rows), Decimal("0.00"))
    total_fee = sum((r["fee"] for r in p.rows), Decimal("0.00"))
    return {
        "repaid": p.cap_reached_on is not None,
        "cap_reached_on": p.cap_reached_on,
        "days_to_cap": (p.cap_reached_on - p.funding_date).days if p.cap_reached_on else None,
        "principal_recovered_on": p.principal_recovered_on,
        "days_to_principal": (p.principal_recovered_on - p.funding_date).days if p.principal_recovered_on else None,
        "gross_irr": xirr(flows_of(p, "gross")),
        "net_irr": xirr(flows_of(p, "net")),
        "merchant_apr": xirr(flows_of(p, "merchant")),
        "total_drc_due": total_drc,
        "total_investor_net": total_net,
        "total_merchant_fees": total_fee,
        "net_multiple": (total_net / p.investment).quantize(Decimal("0.001")),
        "shortfall_vs_cap": max(p.cap - total_drc, Decimal("0.00")),
    }


def actual_summary(params, actuals):
    """IRR on the real ledger. Only meaningful once the data spans MIN_DAYS_FOR_IRR days."""
    funding = date.fromisoformat(params["funding_date"])
    if not actuals:
        return {"count": 0, "span_days": 0, "meaningful": False}
    p = project(params, [Decimal("0.00")], Decimal("1"), actuals)       # no simulation needed: just the rows
    p.rows = [r for r in p.rows if r["actual"]]
    span = (max(a["date"] for a in actuals) - funding).days
    meaningful = span >= MIN_DAYS_FOR_IRR
    return {
        "count": len(p.rows), "span_days": span, "meaningful": meaningful,
        "collected": sum((r["drc_due"] for r in p.rows), Decimal("0.00")),
        "investor_net": sum((r["investor_net"] for r in p.rows), Decimal("0.00")),
        "fees": sum((r["fee"] for r in p.rows), Decimal("0.00")),
        "gross_irr": xirr(flows_of(p, "gross")) if meaningful else None,
        "net_irr": xirr(flows_of(p, "net")) if meaningful else None,
        "merchant_apr": xirr(flows_of(p, "merchant")) if meaningful else None,
    }


# --------------------------------------------------------------- report
def _pct(x):
    return "n/a" if x is None else f"{x * 100:.1f}%"


def _eur(x):
    return f"EUR {x:,.2f}"


def build_summary(projections, actual, params):
    """The summary as a list of text lines."""
    cap, investment = se.q2(se.D(params["cap_amount"])), se.q2(se.D(params["investment_amount"]))
    term = int(params["max_term_months"])
    lines = [
        f"DRC projection - funding {params['funding_date']}, investment {_eur(investment)}, "
        f"cap {_eur(cap)}, max term {term} months",
        "",
        f"Actual ledger (SUCCESS settlements): {actual['count']}",
    ]
    if actual["count"]:
        lines.append(f"  collected {_eur(actual['collected'])} (investor_net {_eur(actual['investor_net'])}, "
                     f"merchant payout fees {_eur(actual['fees'])}), span {actual['span_days']} day(s)")
    if actual["meaningful"]:
        lines.append(f"  IRR on actual data: gross {_pct(actual['gross_irr'])}, net {_pct(actual['net_irr'])}, "
                     f"merchant APR {_pct(actual['merchant_apr'])}")
    else:
        lines.append(f"  IRR on actual data: n/a - the data spans {actual['span_days']} day(s), "
                     f"fewer than {MIN_DAYS_FOR_IRR}; see the projection below")
    lines += ["", "Projection: the 90 verified_net days of data/verified_daily.csv, repeated; "
                  "merchant payout fee estimated at 2% of drc_due", ""]

    ms = [metrics(p) for p in projections]
    label_width, width = 64, 28

    def row(label, cells):
        return f"{label:<{label_width}}" + "".join(f"{c:>{width}}" for c in cells)

    def reached_or_not(on_key, days_key, total_key):
        """Date (days from funding) if reached, else 'not reached' with the amount collected at maturity."""
        return [f"{m[on_key]} ({m[days_key]} d)" if m[on_key] else f"not reached ({_eur(m[total_key])})"
                for m in ms]

    lines.append(row("", [p.name for p in projections]))
    lines.append("-" * (label_width + width * len(projections)))
    lines.append(row("Average daily verified_net", [_eur(p.avg_daily_revenue) for p in projections]))
    lines.append(row(f"Principal recovered (cum. investor_net >= {_eur(investment)})",
                     reached_or_not("principal_recovered_on", "days_to_principal", "total_investor_net")))
    lines.append(row(f"Cap reached / contract ends (cum. drc_due = {_eur(cap)})",
                     reached_or_not("cap_reached_on", "days_to_cap", "total_drc_due")))
    lines.append(row("Investor net recovery multiple (investor_net / principal)", [f"{m['net_multiple']}x" for m in ms]))
    lines.append(row("Net IRR (investor_net)", [_pct(m["net_irr"]) for m in ms]))
    lines.append(row("Gross IRR (drc_due)", [_pct(m["gross_irr"]) for m in ms]))
    lines.append(row("Merchant effective APR (incl. fees)", [_pct(m["merchant_apr"]) for m in ms]))
    lines.append(row("Collected drc_due", [_eur(m["total_drc_due"]) for m in ms]))
    lines.append(row("Collected investor_net", [_eur(m["total_investor_net"]) for m in ms]))
    lines.append(row("Merchant payout fees (est.)", [_eur(m["total_merchant_fees"]) for m in ms]))
    lines.append(row("Shortfall vs cap at maturity", [_eur(m["shortfall_vs_cap"]) if not m["repaid"] else "-" for m in ms]))

    repaid = next(((p, m) for p, m in zip(projections, ms) if m["repaid"]), None)
    if repaid:
        p, m = repaid
        lines += ["", "Reading the table: the investor's return is the IRR, not the multiple. Capital comes back "
                      f"gradually, every day, so {p.name} returns {m['net_multiple']}x net over {m['days_to_cap']} "
                      f"days - a net IRR of {_pct(m['net_irr'])} (gross {_pct(m['gross_irr'])})."]
    for p, m in zip(projections, ms):
        if not m["repaid"]:
            lines.append("")
            lines.append(f"{p.name}: NOT fully repaid within {term} months. At maturity ({p.end_date - timedelta(days=1)}) "
                         f"the contract collected {_eur(m['total_drc_due'])} of the {_eur(cap)} cap; the investor "
                         f"received {_eur(m['total_investor_net'])} net = {m['net_multiple']}x the principal "
                         f"(principal {'recovered' if m['principal_recovered_on'] else 'NOT recovered'}).")
    return lines


def plot_projection(projections, params, path=PLOT_PATH):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt

    from matplotlib.lines import Line2D
    from matplotlib.ticker import FuncFormatter, MultipleLocator

    cap, investment = float(params["cap_amount"]), float(params["investment_amount"])
    servicing = se.D(params["servicing_fee_rate"])
    # drc_due at which the investor's NET (after the servicing fee) equals the principal
    breakeven = float(se.D(params["investment_amount"]) / (1 - servicing))
    funding = date.fromisoformat(params["funding_date"])
    end_date = projections[0].end_date
    colors = ("#1f3b5c", "#e76f51", "#7b2cbf")
    y_max = cap * 1.28

    fig, ax = plt.subplots(figsize=(14, 7), dpi=200)
    vertical_marks, not_reached = [], []
    for p, color in zip(projections, colors):
        xs = [p.funding_date] + [r["date"] for r in p.rows]
        ys = [0.0] + [float(r["cumulative"]) for r in p.rows]
        ax.plot(xs, ys, color=color, lw=2.4, label=f"{p.name} (avg EUR {float(p.avg_daily_revenue):,.0f}/day)")
        # vertical lines: dashed = principal recovered, dotted = cap reached (only if reached)
        for on, style in ((p.principal_recovered_on, "--"), (p.cap_reached_on, ":")):
            if on:
                ax.axvline(on, color=color, ls=style, lw=1.6, alpha=0.9)
                vertical_marks.append((on, color))
        if p.cap_reached_on:
            ax.scatter([p.cap_reached_on], [cap], color=color, s=60, zorder=4)
        else:
            not_reached.append((p, color, ys[-1]))

    # horizontal references (they stop at the plot edge, so the notes on the right never touch them)
    ax.axhline(cap, color="#2a9d8f", ls="--", lw=1.8, label=f"Cap: EUR {cap:,.0f}")
    ax.axhline(breakeven, color="#6c757d", ls=":", lw=1.8,
               label=(f"Investor breaks even: EUR {breakeven:,.0f} contributions = EUR {investment:,.0f} "
                      f"net after {float(servicing) * 100:g}% servicing fee"))

    # dates of the vertical lines: horizontal text, staggered in height so neighbours never overlap
    levels = (0.985, 0.925, 0.865)
    for i, (on, color) in enumerate(sorted(vertical_marks, key=lambda mark: mark[0])):
        near_end = (end_date - on).days < 130
        ax.text(on + timedelta(days=-4 if near_end else 4), y_max * levels[i % len(levels)], f"{on:%d %b %Y}",
                ha="right" if near_end else "left", va="top", fontsize=12, color=color, fontweight="bold")

    # "cap not reached" notes at the right-hand end of each line, outside the plot area
    gap, previous = 0.11, None
    for p, color, y_end in sorted(not_reached, key=lambda item: item[2]):
        y_text = y_end / y_max
        if previous is not None and y_text - previous < gap:
            y_text = previous + gap
        previous = y_text
        ax.scatter([end_date - timedelta(days=1)], [y_end], color=color, s=60, zorder=4)
        ax.annotate(f"{p.name}: cap not reached\nEUR {y_end:,.0f} at maturity",
                    xy=(end_date - timedelta(days=1), y_end), xycoords="data",
                    xytext=(1.015, y_text), textcoords="axes fraction", ha="left", va="center",
                    fontsize=12, color=color, fontweight="bold", annotation_clip=False,
                    arrowprops={"arrowstyle": "-", "color": color, "lw": 1.3, "shrinkA": 0, "shrinkB": 2})

    ax.set_title("Cumulative contributions to the investor: base case vs. −20% / −40% revenue stress",
                 fontsize=16, loc="left", pad=14)
    ax.set_ylabel("Cumulative contributions (drc_due)", fontsize=13)
    ax.set_xlim(funding, end_date)
    ax.set_ylim(0, y_max)
    ax.yaxis.set_major_locator(MultipleLocator(5000))
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: "EUR 0" if v == 0 else f"EUR {v / 1000:g}k"))
    ax.xaxis.set_major_locator(mdates.MonthLocator(interval=3))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %Y"))
    ax.tick_params(axis="both", labelsize=13)
    ax.grid(axis="y", alpha=0.25)

    handles, labels = ax.get_legend_handles_labels()
    handles += [Line2D([], [], color="#444444", ls="--", lw=1.6), Line2D([], [], color="#444444", ls=":", lw=1.6)]
    labels += ["Vertical dashed line: principal recovered (cum. investor_net = principal)",
               "Vertical dotted line: cap reached / contract ends (cum. drc_due = cap)"]
    ax.legend(handles, labels, loc="upper left", bbox_to_anchor=(0.0, -0.1), ncol=1, fontsize=12, frameon=False)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    fig.tight_layout()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=200, bbox_inches="tight")   # tight box keeps the legend and the notes on the right
    plt.close(fig)


# ------------------------------------------------------------------ CLI
def main(argv=None):
    parser = argparse.ArgumentParser(description="IRR on the ledger plus base and stress projections.")
    parser.add_argument("--db", default=str(ledger.DB_PATH))
    parser.add_argument("--params", default=str(se.PARAMS_PATH))
    parser.add_argument("--merchant-id", default=config.MERCHANT_ID)
    parser.add_argument("--verified-daily", default=str(VERIFIED_DAILY_PATH))
    parser.add_argument("--no-plot", action="store_true", help="skip saving the chart")
    args = parser.parse_args(argv)

    params = se.load_params(args.params)
    conn = ledger.connect(args.db)
    try:
        actuals = load_actuals(conn, args.merchant_id)
    finally:
        conn.close()
    revenues = load_daily_revenues(args.verified_daily)

    projections = [project(params, revenues, factor, actuals, name=name) for name, factor in SCENARIOS]
    print("\n".join(build_summary(projections, actual_summary(params, actuals), params)))
    if not args.no_plot:
        plot_projection(projections, params)
        print(f"\nChart: {PLOT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
