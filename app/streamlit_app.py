# DRC Copilot - demo web app (read-only).
#
# Reads only data/demo/ (created by scripts/export_demo_snapshot.py) and data/contract_params.json.
# It never calls PayPal or Claude and has no payout buttons.
#
# Run (from the project root):  python -m streamlit run app/streamlit_app.py
import html
import json
import sys
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from ai import anomaly as screening  # noqa: E402  (the same screening rules as the live engine)
from engine import split_engine as se  # noqa: E402  (the same split maths as the live engine)

DEMO = ROOT / "data" / "demo"
CONTRACT_PARAMS = ROOT / "data" / "contract_params.json"
SNAPSHOT_HINT = "python -m scripts.export_demo_snapshot"

BLUE_DARK, BLUE, PALE = "#003087", "#0070E0", "#EAF2FC"
GRAY, RED, ORANGE, GREEN = "#6B7C93", "#D62828", "#F77F00", "#2A9D8F"
SCENARIO_COLORS = {"Baseline": BLUE_DARK, "-20%": BLUE, "-40%": RED}

st.set_page_config(page_title="DRC Copilot", layout="wide")
st.markdown(f"""
<style>
h1, h2, h3, h4 {{ color: {BLUE_DARK}; }}
.step-card {{ background: {PALE}; border-left: 6px solid {BLUE}; border-radius: 10px;
              padding: 16px 18px; min-height: 175px; }}
.step-card .num {{ color: {BLUE}; font-weight: 700; font-size: 0.85rem; letter-spacing: 0.08em; }}
.step-card h4 {{ margin: 4px 0 8px 0; }}
.step-card p {{ margin: 0; font-size: 0.95rem; color: #33415c; }}
.arrow {{ text-align: center; font-size: 2rem; color: {BLUE}; padding-top: 60px; }}
.lead {{ font-size: 1.08rem; color: #33415c; max-width: 64rem; margin: 0 0 6px 0; }}
.why-card {{ background: white; border: 1px solid #d9e2ef; border-top: 4px solid {BLUE}; border-radius: 10px;
             padding: 12px 16px; min-height: 215px; }}
.why-card h4 {{ margin: 0 0 6px 0; font-size: 1.05rem; }}
.bonus-line {{ font-size: 1.25rem; color: #33415c; }}
.bonus-big {{ font-size: 2.6rem; font-weight: 800; color: {GREEN}; }}
.bonus-side {{ font-size: 1.15rem; color: #6B7C93; padding-top: 18px; }}
.real-card {{ background: {PALE}; border-left: 6px solid {BLUE}; border-radius: 10px; padding: 14px 18px;
              margin: 10px 0 14px 0; }}
.real-card h4 {{ margin: 0 0 8px 0; }}
.real-card table {{ border-collapse: collapse; width: 100%; font-size: 0.92rem; }}
.real-card th {{ text-align: left; color: {BLUE_DARK}; padding: 4px 8px; border-bottom: 1px solid #c9d8ee; }}
.real-card td {{ padding: 4px 8px; border-bottom: 1px solid #dde8f6; }}
.real-card p {{ margin: 8px 0 0 0; color: #33415c; }}
.why-card ul {{ margin: 0 0 6px 0; padding-left: 18px; font-size: 0.92rem; color: {BLUE_DARK}; }}
.why-card .big {{ font-size: 1.35rem; font-weight: 700; color: {BLUE_DARK}; }}
.why-card .small {{ font-size: 0.82rem; color: #6B7C93; margin-bottom: 6px; }}
.why-card p {{ margin: 0; font-size: 0.88rem; color: #33415c; }}
.contract-text {{ white-space: pre-wrap; font-family: Georgia, serif; font-size: 0.92rem; line-height: 1.45; }}
table.params {{ border-collapse: collapse; width: 100%; font-size: 0.88rem; }}
table.params th {{ background: {BLUE_DARK}; color: white; text-align: left; padding: 6px 8px; position: sticky; top: 0; }}
table.params td {{ border-bottom: 1px solid #d9e2ef; padding: 6px 8px; vertical-align: top; }}
table.params td.quote {{ color: #33415c; font-style: italic; }}
</style>
""", unsafe_allow_html=True)


# ------------------------------------------------------------------ helpers
def D(x):
    return Decimal(str(x))


def eur(x, decimals=2):
    return f"EUR {D(x):,.{decimals}f}"


def pct(x, digits=1):
    """Percentage with a typographic minus for negative values."""
    return "n/a" if x is None else f"{x * 100:.{digits}f}%".replace("-", "−")


def minus(text):
    """Typographic minus for scenario names such as -20%."""
    return text.replace("-", "−")


@st.cache_data
def _read_json(path_text, modified):
    return json.loads(Path(path_text).read_text(encoding="utf-8"))


def load_json(name):
    """Cached per file version; a missing file is not cached, so creating it later just works."""
    path = DEMO / name
    return _read_json(str(path), path.stat().st_mtime) if path.exists() else None


def need(name):
    data = load_json(name)
    if data is None:
        st.error(f"data/demo/{name} is missing. Create the demo snapshot with: {SNAPSHOT_HINT}")
        st.stop()
    return data


def load_contract():
    if not CONTRACT_PARAMS.exists():
        return None
    return _read_json(str(CONTRACT_PARAMS), CONTRACT_PARAMS.stat().st_mtime)


def need_contract():
    contract = load_contract()
    if contract is None:
        st.error("data/contract_params.json is missing. Create it with: python -m ai.contract_parser")
        st.stop()
    return contract


@st.cache_data
def _read_daily(path_text, modified):
    return pd.read_csv(path_text, dtype=str)


def load_daily():
    path = DEMO / "verified_daily.csv"
    return _read_daily(str(path), path.stat().st_mtime) if path.exists() else None


def need_daily():
    daily = load_daily()
    if daily is None:
        st.error(f"data/demo/verified_daily.csv is missing. Create the demo snapshot with: {SNAPSHOT_HINT}")
        st.stop()
    return daily


def style(fig, title, height=520):
    fig.update_layout(template="plotly_white", height=height, font=dict(size=14),
                      title=dict(text=title, font=dict(size=20, color=BLUE_DARK)),
                      margin=dict(l=60, r=30, t=70, b=40),
                      legend=dict(orientation="h", yanchor="top", y=-0.15, x=0))
    return fig


def tier_parameters(params):
    tiers = params["tiers"]
    return D(tiers[0]["up_to"]), D(tiers[0]["rate"]), D(tiers[1]["rate"])


# ---------------------------------------------------------------- Overview
def page_overview():
    params = need_contract()["params"]
    settlements = need("settlements.json")
    projection = need("projection.json")

    st.title("DRC Copilot")
    st.subheader("Turn PayPal merchants' verified daily revenue into investable revenue-share assets.")
    st.markdown('<p class="lead">Independent shops need EUR 10–50k for equipment or stock. Banks want collateral and '
                'fixed monthly repayments; revenue-share fits their cash flow, but investors can’t trust '
                'self-reported sales. DRC Copilot makes the revenue verifiable.</p>', unsafe_allow_html=True)
    st.caption("Demo merchant: Kaffee und Tee GmbH, Hannover. Real PayPal sandbox payments on top of a simulated "
               "90-day history. No real money moves.")

    steps = [
        ("STEP 1", "AI verifies", "Rules and an Isolation Forest screen every transaction. Fake orders are removed "
                                  "before any money is split."),
        ("STEP 2", "AI reads contract", "Claude extracts the contract terms. Each value carries a verbatim "
                                        "quote that is checked against the PDF."),
        ("STEP 3", "PayPal splits daily", "Progressive tiers on verified net revenue; the contribution is paid "
                                          "to escrow with PayPal Payouts."),
        ("STEP 4", "Investor gets paid", "The servicing fee is deducted in escrow, the rest goes to the investor "
                                         "until the cap is reached."),
    ]
    columns = st.columns([6, 1, 6, 1, 6, 1, 6])
    for index, (number, title, text) in enumerate(steps):
        with columns[index * 2]:
            st.markdown(f'<div class="step-card"><div class="num">{number}</div><h4>{title}</h4><p>{text}</p></div>',
                        unsafe_allow_html=True)
        if index < 3:
            with columns[index * 2 + 1]:
                st.markdown('<div class="arrow">→</div>', unsafe_allow_html=True)

    settled = [s for s in settlements if s["status"] == "SUCCESS"]
    total = sum((D(s["drc_due"]) for s in settled), Decimal("0.00"))
    baseline = next(s for s in projection["scenarios"] if s["name"] == "Baseline")

    # ---- Why AI: three small cards; every number is read from the project's result files
    st.write("")
    st.markdown("### Why AI")
    by_name = {d["detector"]: d for d in (load_json("anomaly_metrics.json") or {}).get("detectors", [])}
    items = [f"<li>{label}: <b>P {pct(by_name[key]['precision'], 0)} / R {pct(by_name[key]['recall'], 0)}</b></li>"
             for label, key in (("Self-dealing orders", "SELF_DEALING_SUSPECT"),
                                ("Void-spike days", "VOID_SPIKE_DAY"),
                                ("Missing card revenue", "CARD_REVENUE_MISSING")) if key in by_name]
    detector_html = (f"<ul>{''.join(items)}</ul>" if items else
                     f"<p class='small'>Detector results not loaded. Run: {SNAPSHOT_HINT}</p>")

    meta = need_contract().get("meta")
    if meta and meta.get("elapsed_seconds") is not None:
        cost = meta.get("estimated_cost_usd")
        headline = f"{float(meta['elapsed_seconds']):.0f} s" + (f" &middot; ~${float(cost):.3f}" if cost else "")
        parse_html = (f"<div class='big'>{headline}</div><div class='small'>per contract "
                      f"({int(meta['input_tokens']):,} in / {int(meta['output_tokens']):,} out tokens, estimated "
                      f"from list prices)</div>")
    else:
        parse_html = ("<div class='small'>Parse time and cost are not recorded yet. "
                      "Run: python -m ai.contract_parser</div>")

    why_cards = [
        ("Screens every transaction",
         detector_html + "<div class='small'>Measured on the 90-day simulated history with 4 injected fraud patterns "
                         "(answer key in data/). Real-world accuracy will be lower. The rules catch the injected "
                         "patterns; Isolation Forest flags statistical outliers for human review.</div>",
         "Self-dealing, voids and missing card revenue are flagged before any money is split."),
        ("Reads documents, with proof", parse_html,
         "Contracts today; merchants’ POS reports and VAT returns next, in any format. Every extracted value "
         "carries a verbatim quote that code checks against the source."),
        ("Makes small deals economical", "",
         "Manual due diligence doesn’t pay off for a EUR 20,000 deal. Automated verification and daily "
         "servicing make micro revenue-share financing viable."),
    ]
    for column, (title, body, note) in zip(st.columns(3), why_cards):
        column.markdown(f'<div class="why-card"><h4>{title}</h4>{body}<p>{note}</p></div>', unsafe_allow_html=True)
    st.caption("Deterministic code moves the money. AI handles what rules can’t. Humans approve payouts.")

    # ---- five headline numbers (plain grey captions, no delta arrows)
    st.write("")
    base_metrics = baseline["metrics"]
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Investment", eur(params["investment_amount"], 0))
    c2.metric("Cap", eur(params["cap_amount"], 0))
    c3.metric("Settled so far", eur(total))
    c3.caption(f"{len(settled)} settlement day(s)")
    c4.metric("Projected net IRR", pct(base_metrics["net_irr"]))
    c4.caption("base case, simulated revenue")
    c5.metric("Merchant cost (est.)", f"{pct(base_metrics['merchant_apr'])} / yr")
    c5.caption("incl. PayPal payout fees")
    st.caption("Projected net IRR = investor return after the 5% servicing fee. Merchant cost = what the merchant pays "
               "per year, payout fees included. Both are XIRR on actual dates (days / 365) on simulated revenue: "
               "projections, not forecasts.")

    # ---- real vs simulated
    real, simulated = st.columns(2)
    with real:
        with st.container(border=True):
            st.markdown("#### What is real")
            st.markdown("- PayPal sandbox customer payments\n"
                        "- PayPal Payouts to escrow (see the ledger on the Investor view)\n"
                        "- PayPal Transaction Search\n"
                        "- Claude contract parsing with verified quotes\n"
                        "- Idempotent settlement")
    with simulated:
        with st.container(border=True):
            st.markdown("#### What is simulated")
            st.markdown("- 90-day sales history\n- Card and cash channels\n- Future revenue in the projection")


# ----------------------------------------------------------- Merchant view
def page_merchant():
    params = need_contract()["params"]
    settlements = [s for s in need("settlements.json") if s["status"] == "SUCCESS"]
    daily = need_daily()
    projection = need("projection.json")
    threshold, rate1, rate2 = tier_parameters(params)

    st.title("Merchant view - Kaffee und Tee GmbH")
    if not settlements:
        st.info("No settlement has been recorded yet.")
    else:
        last = max(settlements, key=lambda s: s["date"])
        revenue = D(last["verified_revenue"])
        part1, part2 = min(revenue, threshold), max(revenue - threshold, Decimal(0))
        st.subheader(f"Latest settlement - {last['date']}")
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Verified revenue today", eur(revenue))
        c2.metric("Contribution to investor", eur(last["drc_due"]))
        c2.caption(f"{D(last['rate_applied']) * 100:.2f}% blended rate")
        c3.metric("PayPal payout fee (paid by you)", eur(last["merchant_payout_fee"]))
        c3.caption("charged by PayPal, on top of the contribution")
        c4.metric("Total outlay today", eur(D(last["drc_due"]) + D(last["merchant_payout_fee"])))
        c4.caption("contribution + payout fee")

        st.markdown("**How today's contribution is calculated**")
        st.table(pd.DataFrame([
            {"Tier": f"Tier 1: up to {eur(threshold)}", "Revenue in tier": eur(part1), "Rate": f"{rate1 * 100:g}%",
             "Contribution": f"{part1 * rate1:.4f}"},
            {"Tier": f"Tier 2: above {eur(threshold)}", "Revenue in tier": eur(part2), "Rate": f"{rate2 * 100:g}%",
             "Contribution": f"{part2 * rate2:.4f}"},
            {"Tier": "Total, rounded to the cent", "Revenue in tier": eur(revenue), "Rate": "",
             "Contribution": eur(last["drc_due"])},
        ]).set_index("Tier"))

        paid, cap = D(last["cumulative_due"]), D(params["cap_amount"])
        st.progress(min(float(paid / cap), 1.0),
                    text=f"Paid so far: {eur(paid)} of the {eur(cap, 0)} cap ({paid / cap * 100:.2f}%)")

        baseline_run = next((s for s in projection["scenarios"] if s["name"] == "Baseline"), None)
        payoff_day = baseline_run["metrics"]["cap_reached_on"] if baseline_run else None
        st.caption(f"Estimated payoff: {date.fromisoformat(payoff_day):%b %Y} at the current pace" if payoff_day
                   else "Estimated payoff: not within the contract term at the current pace")

    # ---- 90 simulated days, split with the live engine's maths: tier 1, tier 2 and the growth bonus
    revenues = [D(v) for v in daily["verified_net"]]
    splits = [se.calculate_split(v, params, Decimal("0")) for v in revenues]
    tier1 = [s.tier_parts[0][2] for s in splits]                         # portion up to the baseline x rate 1
    tier2 = [s.tier_parts[1][2] for s in splits]                         # portion above the baseline x rate 2
    bonus = [s.tier_parts[1][0] * (rate1 - rate2) for s in splits]       # what the lower rate saves
    n_above = sum(1 for s in splits if s.tier_parts[1][0] > 0)
    bonus_total = se.q2(sum(bonus, Decimal("0")))
    per_strong_day = se.q2(bonus_total / n_above) if n_above else Decimal("0.00")

    st.write("")
    st.subheader("Growth bonus")
    left, right = st.columns([3, 2])
    left.markdown(f'<div class="bonus-line">Growth bonus earned: <span class="bonus-big">{eur(bonus_total)}</span> '
                  f'over {n_above} days above baseline ({len(revenues)} simulated days)</div>', unsafe_allow_html=True)
    right.markdown(f'<div class="bonus-side">≈ {eur(per_strong_day)} kept per strong day</div>',
                   unsafe_allow_html=True)
    if settlements:
        today = D(max(settlements, key=lambda s: s["date"])["verified_revenue"])
        if today > threshold:
            st.caption(f"Today: {eur(today - threshold)} above baseline, so you kept "
                       f"{eur((today - threshold) * (rate1 - rate2))} more today.")
        else:
            st.caption(f"Today: {eur(threshold - today)} below baseline")

    dates = list(daily["date"])
    fig = go.Figure()
    for values, name, color in (
            (tier1, f"Tier 1 contribution ({rate1 * 100:g}%)", BLUE),
            (tier2, f"Tier 2 contribution ({rate2 * 100:g}%)", BLUE_DARK),
            (bonus, f"Growth bonus (revenue above baseline x {(rate1 - rate2) * 100:g}%)", GREEN)):
        fig.add_trace(go.Bar(x=dates, y=[float(v) for v in values], name=name, marker_color=color,
                             hovertemplate="%{x}<br>EUR %{y:.2f}<extra>" + name + "</extra>"))
    fig.update_layout(barmode="stack")
    fig.update_yaxes(title_text="EUR / day", rangemode="tozero")
    style(fig, "Your daily contribution, and what the growth bonus saved you", height=520)
    st.plotly_chart(fig, width="stretch")
    st.caption(f"The baseline is {eur(threshold)} of verified revenue per day (90% of your average before funding): "
               f"revenue above it is shared at {rate2 * 100:g}% instead of {rate1 * 100:g}%, and the green part of "
               f"each bar is what you did not pay.")


# ------------------------------------------------------------ Investor view
def projection_figure(projection):
    contract = projection["contract"]
    cap, breakeven = float(contract["cap_amount"]), float(contract["investor_breakeven_contributions"])
    investment = float(contract["investment_amount"])
    servicing = float(contract["servicing_fee_rate"]) * 100
    start, end = contract["funding_date"], contract["end_date"]
    end_day = date.fromisoformat(end)
    axis_end = (end_day.replace(day=1) + timedelta(days=32)).replace(day=1)    # first day of the month after the term

    fig = go.Figure()
    for scenario in projection["scenarios"]:
        color = SCENARIO_COLORS.get(scenario["name"], GRAY)
        xs = [start] + [r["date"] for r in scenario["series"]]
        ys = [0.0] + [float(r["cumulative_drc_due"]) for r in scenario["series"]]
        fig.add_trace(go.Scatter(x=xs, y=ys, mode="lines", line=dict(color=color, width=3),
                                 name=f"{minus(scenario['name'])} (avg {eur(scenario['avg_daily_revenue'], 0)}/day)"))
    fig.add_trace(go.Scatter(x=[start, end], y=[cap, cap], mode="lines", line=dict(color=GREEN, dash="dash", width=2),
                             name=f"Cap: {eur(cap, 0)}"))
    fig.add_trace(go.Scatter(x=[start, end], y=[breakeven, breakeven], mode="lines",
                             line=dict(color=GRAY, dash="dot", width=2),
                             name=(f"Investor breaks even: {eur(breakeven, 0)} contributions = {eur(investment, 0)} "
                                   f"net after {servicing:g}% servicing fee")))
    for dash, text in (("dash", "Vertical dashed line: principal recovered (cum. investor_net = principal)"),
                       ("dot", "Vertical dotted line: cap reached / contract ends (cum. drc_due = cap)")):
        fig.add_trace(go.Scatter(x=[None], y=[None], mode="lines", line=dict(color=GRAY, dash=dash, width=2), name=text))

    plot_height, y_max = 380, cap * 1.15              # plot area in pixels (680 - margins) and in EUR
    px_per_eur = plot_height / y_max
    marks, not_reached = [], []
    for scenario in projection["scenarios"]:
        color = SCENARIO_COLORS.get(scenario["name"], GRAY)
        for key, dash in (("principal_recovered_on", "dash"), ("cap_reached_on", "dot")):
            day = scenario["metrics"][key]
            if day:
                fig.add_shape(type="line", x0=day, x1=day, y0=0, y1=1, yref="paper",
                              line=dict(color=color, width=2, dash=dash))
                marks.append((day, color))
        if not scenario["metrics"]["repaid"]:
            last = scenario["series"][-1]
            not_reached.append((float(last["cumulative_drc_due"]), scenario["name"], color,
                                last["cumulative_drc_due"]))

    # dates sit at the foot of the vertical lines, just above the x axis; neighbours are staggered in height
    for index, (day, color) in enumerate(sorted(marks)):
        near_edge = (axis_end - date.fromisoformat(day)).days < 75
        fig.add_annotation(x=day, y=0, yref="paper", yanchor="bottom", yshift=6 + 24 * (index % 3),
                           xanchor="right" if near_edge else "left", xshift=-4 if near_edge else 4,
                           showarrow=False, text=f"<b>{date.fromisoformat(day):%d %b %Y}</b>",
                           font=dict(color=color, size=14))

    # "cap not reached" notes: to the right of each line's end, pushed apart so they never overlap
    previous_px = None
    for y_end, name, color, collected in sorted(not_reached):
        end_px = y_end * px_per_eur
        text_px = end_px if previous_px is None else max(end_px, previous_px + 58)
        previous_px = text_px
        fig.add_annotation(x=end, y=y_end, ax=26, ay=-(text_px - end_px), axref="pixel", ayref="pixel",
                           xanchor="left", yanchor="middle", showarrow=True, arrowhead=0, arrowwidth=1.5,
                           arrowcolor=color, align="left",
                           text=f"<b>{minus(name)}: cap not reached</b><br>{eur(collected, 0)} at maturity",
                           font=dict(color=color, size=14))

    fig.update_xaxes(range=[start, axis_end.isoformat()], tickformat="%b %Y", dtick="M3")
    fig.update_yaxes(range=[0, y_max], tickprefix="EUR ", tickformat="~s", title_text="Cumulative contributions")
    style(fig, "Cumulative contributions to the investor: base case vs. −20% / −40% revenue stress", height=680)
    fig.update_layout(legend=dict(orientation="v", yanchor="top", y=-0.1, x=0), margin=dict(l=60, r=230, t=70, b=230))
    return fig


def summary_table(projection):
    contract = projection["contract"]
    investment, cap = eur(contract["investment_amount"], 0), eur(contract["cap_amount"], 0)
    table = {}
    for scenario in projection["scenarios"]:
        m = scenario["metrics"]
        principal = (f"{m['principal_recovered_on']} ({m['days_to_principal']} d)" if m["principal_recovered_on"]
                     else f"not reached ({eur(m['total_investor_net'])})")
        reached = (f"{m['cap_reached_on']} ({m['days_to_cap']} d)" if m["cap_reached_on"]
                   else f"not reached ({eur(m['total_drc_due'])})")
        table[minus(scenario["name"])] = {
            "Average daily verified_net": eur(scenario["avg_daily_revenue"]),
            f"Principal recovered (cum. investor_net >= {investment})": principal,
            f"Cap reached / contract ends (cum. drc_due = {cap})": reached,
            "Investor net recovery multiple": f"{m['net_multiple']}x",
            "Net IRR (investor_net)": pct(m["net_irr"]),
            "Gross IRR (drc_due)": pct(m["gross_irr"]),
            "Merchant effective APR (incl. fees)": (
                f"{pct(m['merchant_apr'])} (repaid less than funded)"
                if m["merchant_apr"] is not None and m["merchant_apr"] < 0 else pct(m["merchant_apr"])),
            "Collected drc_due": eur(m["total_drc_due"]),
            "Collected investor_net": eur(m["total_investor_net"]),
            "Shortfall vs cap at maturity": eur(m["shortfall_vs_cap"]),
        }
    return pd.DataFrame(table)


def stress_text(projection):
    contract = projection["contract"]
    scenario = next((s for s in projection["scenarios"] if s["name"] == "-40%"), None)
    if scenario is None:
        return None
    m, investment = scenario["metrics"], D(contract["investment_amount"])
    net, collected, cap = D(m["total_investor_net"]), D(m["total_drc_due"]), D(contract["cap_amount"])
    loss = investment - net
    lead = (f"In the −40% case revenue averages {eur(scenario['avg_daily_revenue'], 0)} per day. By maturity "
            f"({contract['end_date']}) the contract has collected {eur(collected)} of the {eur(cap, 0)} cap and the "
            f"investor has received {eur(net)} net, {m['net_multiple']}x the principal")
    if m["principal_recovered_on"] is None:
        outcome = (f". That is a loss of {eur(loss)} ({loss / investment * 100:.0f}% of the {eur(investment, 0)} invested; "
                   f"net IRR {pct(m['net_irr'])}).")
    elif not m["repaid"]:
        outcome = (f". The principal is recovered, but the cap is not reached: the investor earns less than the "
                   f"{eur(cap, 0)} / 1.15x the contract promises (net IRR {pct(m['net_irr'])}).")
    else:
        outcome = f" (net IRR {pct(m['net_irr'])})."
    reason = (" The risk comes from the fixed 24-month term: repayments are a share of revenue, so they shrink with "
              "revenue, the contract has no top-up for a shortfall at maturity, and the 5% servicing fee comes off "
              "every payment. A health-covenant rate reduction, which needs the investor's approval, would slow "
              "repayment further. The structure is built for merchants whose revenue is verifiable, so diligence "
              "and the AI verification step matter more than the headline multiple.")
    return lead + outcome + reason


def month_year(day):
    return f"{date.fromisoformat(day):%b %Y}" if day else "not reached"


def basis_sentence(assumptions):
    """The projection assumptions as one sentence, each with its own subject."""
    lead = {"revenue": "future revenue is ", "merchant_payout_fee": "merchant payout fee ",
            "irr": "returns use ", "principal_recovered": "principal recovered means ",
            "cap_reached": "cap reached means "}
    return "Projection basis: " + "; ".join(lead.get(key, f"{key.replace('_', ' ')}: ") + text
                                           for key, text in assumptions.items()) + "."


def page_investor():
    projection = need("projection.json")
    settlements = need("settlements.json")
    st.title("Investor view")

    scenarios = {s["name"]: s for s in projection["scenarios"]}
    base, worst = scenarios.get("Baseline"), scenarios.get("-40%")
    investment = eur(projection["contract"]["investment_amount"], 0)
    c1, c2, c3, c4 = st.columns(4)
    if base:
        bm = base["metrics"]
        c1.metric("Projected net IRR", pct(bm["net_irr"]))
        c1.caption("base case")
        c2.metric("Principal back", month_year(bm["principal_recovered_on"]))
        c2.caption(f"investor net reaches {investment}")
        c3.metric("Contract ends", month_year(bm["cap_reached_on"]))
        c3.caption("cap reached" if bm["cap_reached_on"] else "cap not reached within the term")
    if worst:
        wm = worst["metrics"]
        c4.metric(f"Worst case ({minus('-40%')})", f"{wm['net_multiple']}x")
        c4.caption("net recovery multiple" + ("" if wm["principal_recovered_on"] else ", principal not recovered"))

    st.plotly_chart(projection_figure(projection), width="stretch")

    st.subheader("Scenario summary")
    st.table(summary_table(projection))
    baseline = next((s for s in projection["scenarios"] if s["name"] == "Baseline"), None)
    if baseline and baseline["metrics"]["repaid"]:
        m = baseline["metrics"]
        st.caption(f"Reading the table: the investor's return is the IRR, not the multiple. Capital comes back "
                   f"gradually, every day, so the base case returns {m['net_multiple']}x net over {m['days_to_cap']} days "
                   f"- a net IRR of {pct(m['net_irr'])} (gross {pct(m['gross_irr'])}).")

    st.subheader("Downside: the −40% revenue case")
    text = stress_text(projection)
    if text:
        st.warning(text)
    st.caption(basis_sentence(projection["assumptions"]))

    st.subheader("Actual settlement ledger")
    if settlements:
        ledger_table = pd.DataFrame(settlements)
        ledger_table["rate_applied"] = [f"{float(r) * 100:.2f}%" for r in ledger_table["rate_applied"]]
        st.dataframe(ledger_table, width="stretch", hide_index=True,
                     column_config={"status": st.column_config.TextColumn("status", width="medium"),
                                    "date": st.column_config.TextColumn("date", width="small")})
    else:
        st.info("No settlements recorded yet.")


# --------------------------------------------------------- AI verification
REASON_TEXT = {
    "SELF_DEALING_SUSPECT": "Same customer placed 5+ orders that day, each under EUR 1.50 (self-dealing pattern)",
    "PRICE_OVERRIDE_DEEP": "Charged 30% or more below the list price",
    "IF_OUTLIER": "Statistical outlier (Isolation Forest): kept in revenue, flagged for review",
}


def reasons_text(reasons):
    return "; ".join(REASON_TEXT.get(r, r) for r in reasons)


DETECTOR_LABELS = {
    "SELF_DEALING_SUSPECT": "Self-dealing orders",
    "PRICE_OVERRIDE_DEEP": "Deep price overrides",
    "CARD_REVENUE_MISSING": "Card revenue missing (days)",
    "EVENING_SHARE_DROP": "Evening-share drop (days)",
    "SELF_DEALING_DAY": "Self-dealing days",
    "VOID_SPIKE_DAY": "Void-spike days",
}


def receipts_card():
    """Real sandbox payments from the snapshot, run through the same screening rules as the live engine."""
    receipts = load_json("paypal_receipts.json")
    if not receipts:
        st.caption(f"Real PayPal payments are not loaded. Create the snapshot with: {SNAPSHOT_HINT}")
        return
    verdict_label = {"": ("clean", GREEN), "UNDER_REVIEW": ("under review", ORANGE), "EXCLUDED": ("excluded", RED)}
    rows, sentences = [], []
    for day in receipts:
        transactions = day["transactions"]
        if not transactions:
            continue
        batch = [{"transaction_id": t["transaction_id"], "gross": D(t["gross"]), "payer_name": t["payer_name"]}
                 for t in transactions]
        passed, passed_net = 0, Decimal("0.00")
        for transaction, verdict in zip(transactions, screening.classify_live_batch(batch)):
            label, color = verdict_label[verdict["tier"]]
            rows.append(f"<tr><td>{day['date']}</td><td>{html.escape(transaction['payer_name'] or '-')}</td>"
                        f"<td>{transaction['gross']}</td><td>{html.escape(transaction['event_code'] or '')}</td>"
                        f"<td style='color:{color};font-weight:600'>{label}</td></tr>")
            if verdict["tier"] != "EXCLUDED":
                passed += 1
                passed_net += D(transaction["net"])
        sentences.append(f"<p>{passed} of {len(transactions)} real payments on {day['date']} passed screening; "
                         f"{eur(passed_net)} went into the split.</p>")
    if not rows:
        st.caption("No real PayPal payments in the snapshot yet.")
        return
    st.markdown('<div class="real-card"><h4>Real PayPal sandbox payments, same screening</h4><table>'
                '<tr><th>Date</th><th>Payer</th><th>Amount (EUR)</th><th>Event code</th><th>Screening result</th></tr>'
                + "".join(rows) + "</table>" + "".join(sentences) + "</div>", unsafe_allow_html=True)


def page_ai():
    anomaly = pd.DataFrame(need("anomaly_daily.json"))
    flagged = pd.DataFrame(need("anomaly_flagged.json"))
    metrics = need("anomaly_metrics.json")
    for column in ("raw", "excluded", "at_risk", "verified_gross", "verified_net"):
        anomaly[column] = anomaly[column].astype(float)

    st.title("AI verification")
    st.caption("Detectors are scored against anomalies that were injected into the simulated 90-day history. "
               "P = precision, R = recall.")
    by_name = {d["detector"]: d for d in metrics["detectors"]}
    items = (("Self-dealing orders", "SELF_DEALING_SUSPECT"), ("Deep price overrides", "PRICE_OVERRIDE_DEEP"),
             ("Card revenue missing (days)", "CARD_REVENUE_MISSING"), ("Evening-share drop (days)", "EVENING_SHARE_DROP"))
    for column, (label, key) in zip(st.columns(4), items):
        d = by_name[key]
        column.metric(label, f"P {pct(d['precision'], 0)} / R {pct(d['recall'], 0)}")
        column.caption(f"TP {d['tp']}, FP {d['fp']}, FN {d['fn']} (of {d['true']} injected)")
    business = metrics.get("business_orders")
    if business:
        st.success(f"John and Peter GmbH regular team orders: {business['false_positives']} false positives "
                   f"out of {business['total']} orders.")

    receipts_card()

    with st.expander("All detectors"):
        st.dataframe(pd.DataFrame([{
            "Detector": DETECTOR_LABELS.get(d["detector"], d["detector"]), "Level": d["level"],
            "Injected pattern": d["injected_type"], "Injected": d["true"],
            "TP": d["tp"], "FP": d["fp"], "FN": d["fn"],
            "Precision": pct(d["precision"], 0), "Recall": pct(d["recall"], 0)} for d in metrics["detectors"]]),
            width="stretch", hide_index=True)

    totals = st.columns(4)
    totals[0].metric("Raw revenue (90 days)", eur(anomaly["raw"].sum()))
    totals[1].metric("Excluded", eur(anomaly["excluded"].sum()))
    totals[1].caption("removed from verified revenue")
    totals[2].metric("Under review", eur(anomaly["at_risk"].sum()))
    totals[2].caption("kept in revenue, flagged")
    totals[3].metric("Verified revenue", eur(anomaly["verified_gross"].sum()))

    fig = go.Figure()
    fig.add_trace(go.Bar(x=anomaly["date"], y=anomaly["excluded"], name="Excluded (removed)", marker_color=RED))
    fig.add_trace(go.Bar(x=anomaly["date"], y=anomaly["at_risk"], name="Under review (kept)", marker_color=ORANGE))
    fig.add_trace(go.Scatter(x=anomaly["date"], y=anomaly["raw"], mode="lines", name="Raw revenue",
                             line=dict(color=GRAY, width=2, dash="dot")))
    fig.add_trace(go.Scatter(x=anomaly["date"], y=anomaly["verified_gross"], mode="lines", name="Verified revenue",
                             line=dict(color=BLUE_DARK, width=3),
                             text=[", ".join(f) for f in anomaly["flags"]], hovertemplate="%{x}: EUR %{y:.2f}<br>%{text}"))
    fig.update_layout(barmode="stack")
    fig.update_yaxes(tickprefix="EUR ", title_text="Revenue per day")
    style(fig, "Raw vs. verified revenue (90 simulated days)", height=500)
    st.plotly_chart(fig, width="stretch")

    excluded_tab, review_tab = st.tabs([f"Excluded transactions ({int((flagged['tier'] == 'EXCLUDED').sum())})",
                                        f"Under review ({int((flagged['tier'] == 'UNDER_REVIEW').sum())})"])
    reason_column = st.column_config.TextColumn("Reason", width=760)       # wide enough for the full sentence
    with excluded_tab:
        rows = flagged[flagged["tier"] == "EXCLUDED"]
        st.dataframe(pd.DataFrame({
            "Transaction": rows["txn_id"], "Time": rows["timestamp"], "Channel": rows["channel"],
            "Customer": rows["customer_id"], "Amount (EUR)": rows["amount_eur"],
            "Reason": [reasons_text(r) for r in rows["reasons"]]}), width="stretch", hide_index=True,
            column_config={"Reason": reason_column})
    with review_tab:
        rows = flagged[flagged["tier"] == "UNDER_REVIEW"].copy()
        # clear evidence first (charged 30% or more below the list price), then Isolation Forest only
        rows["_rank"] = [0 if "PRICE_OVERRIDE_DEEP" in r else 1 for r in rows["reasons"]]
        rows = rows.sort_values(["_rank", "timestamp"], kind="stable")
        with_basis = int((rows["_rank"] == 0).sum())
        st.caption(f"These transactions stay in verified revenue but are shown as at-risk. First the {with_basis} "
                   f"with a clear basis (charged 30% or more below the list price), then the "
                   f"{len(rows) - with_basis} flagged only by Isolation Forest (statistical outliers).")
        st.dataframe(pd.DataFrame({
            "Transaction": rows["txn_id"], "Time": rows["timestamp"], "Channel": rows["channel"],
            "Amount (EUR)": rows["amount_eur"], "List price (EUR)": rows["list_price"],
            "Reason": [reasons_text(r) for r in rows["reasons"]]}), width="stretch", hide_index=True,
            column_config={"Reason": reason_column})


# ------------------------------------------------------------------ Contract
@st.cache_data
def contract_text(path_text, modified):
    import pdfplumber
    with pdfplumber.open(path_text) as pdf:
        return "\n\n".join((page.extract_text() or "") for page in pdf.pages)


def format_value(name, value):
    if name == "tiers" and isinstance(value, list):
        return "; ".join(f"up to {t['up_to']}: {t['rate'] * 100:g}%" if t["up_to"] is not None
                         else f"above: {t['rate'] * 100:g}%" for t in value)
    if isinstance(value, bool):
        return "yes" if value else "no"
    return "null" if value is None else str(value)


def page_contract():
    contract = need_contract()
    st.title("Contract")
    st.markdown("**Every parameter is traceable to a verbatim clause. A deterministic check rejects any value whose "
                "quote is not in the contract.**")
    pdf_path = DEMO / "sample_contract.pdf"
    if not pdf_path.exists():
        pdf_path = ROOT / "data" / "sample_contract.pdf"

    left, right = st.columns([5, 7])
    with left:
        st.subheader("Daily Revenue Contribution Agreement")
        if pdf_path.exists():
            st.download_button("Download sample_contract.pdf", pdf_path.read_bytes(), file_name="sample_contract.pdf",
                               mime="application/pdf")
            with st.container(height=640, border=True):
                text = contract_text(str(pdf_path), pdf_path.stat().st_mtime)
                st.markdown(f'<div class="contract-text">{html.escape(text)}</div>', unsafe_allow_html=True)
        else:
            st.error(f"sample_contract.pdf is missing. Create it with: python -m data.make_contract")
    with right:
        st.subheader("Extracted parameters")
        st.caption(f"Extracted by {contract.get('model', 'Claude')} with tool use. "
                   f"{len(contract['params'])} fields, each with a source quote.")
        rows = []
        for name, value in contract["params"].items():
            evidence = contract["evidence"].get(name, {})
            quote = evidence.get("source_quote") or f"(not found: {evidence.get('not_found_reason')})"
            rows.append(f"<tr><td><b>{html.escape(name)}</b></td><td>{html.escape(format_value(name, value))}</td>"
                        f"<td class='quote'>{html.escape(quote)}</td></tr>")
        with st.container(height=640, border=True):
            st.markdown("<table class='params'><tr><th>Field</th><th>Value</th><th>Source quote</th></tr>"
                        + "".join(rows) + "</table>", unsafe_allow_html=True)


# ---------------------------------------------------------------- navigation
st.sidebar.markdown(f"<h2 style='color:{BLUE_DARK};margin-bottom:0'>DRC Copilot</h2>", unsafe_allow_html=True)
st.sidebar.caption("Read-only demo. Simulated history plus PayPal sandbox payments.")

navigation = st.navigation([
    st.Page(page_overview, title="Overview", url_path="overview", default=True),
    st.Page(page_merchant, title="Merchant view", url_path="merchant"),
    st.Page(page_investor, title="Investor view", url_path="investor"),
    st.Page(page_ai, title="AI verification", url_path="ai-verification"),
    st.Page(page_contract, title="Contract", url_path="contract"),
])
navigation.run()
