# DRC Copilot - demo web app (read-only).
#
# Reads only data/demo/ (created by scripts/export_demo_snapshot.py) and data/contract_params.json.
# It never calls PayPal or Claude and has no payout buttons.
#
# Run (from the project root):  python -m streamlit run app/streamlit_app.py
import html
import json
import re
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
from engine import eligibility as elig  # noqa: E402  (the same eligibility rules as the checklist files)
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
.doc-card {{ background: white; border: 1px solid #d9e2ef; border-radius: 10px; padding: 14px 16px; min-height: 190px; }}
.doc-card.selected {{ box-shadow: 0 0 0 3px {BLUE}; }}
.doc-card h4 {{ margin: 0 0 6px 0; }}
.doc-card p {{ margin: 2px 0; font-size: 0.9rem; color: #33415c; }}
.doc-card .verdict {{ margin-top: 10px; font-weight: 700; }}
.doc-text {{ white-space: pre-wrap; font-family: Consolas, 'Courier New', monospace; font-size: 0.84rem; line-height: 1.4; }}
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


WORD_FIXES = {"pct": "%", "vat": "VAT", "pp": "pp", "pdf": "PDF", "id": "ID", "eur": "EUR"}


def human_name(name):
    """An internal name as plain words: 'health_trigger_drop_pct' -> 'Health trigger drop %'."""
    text = " ".join(WORD_FIXES.get(word, word) for word in str(name).lower().split("_"))
    return text[:1].upper() + text[1:]


def plain_value(value):
    """An internal code shown as a value ('trading_day', 'z_report') as plain words."""
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+", value):
        return value.replace("_", " ").lower()
    return value


# internal terms that can appear in text coming from the data files -> words for people
TERM_WORDS = (("verified_daily.csv", "the simulated history"), ("verified_net", "daily revenue"),
              ("drc_due", "contributions"), ("investor_net", "investor net"),
              ("investment_amount", "amount invested"), ("cap_amount", "cap"), (">=", "≥"))


def readable(text):
    for internal, words in TERM_WORDS:
        text = text.replace(internal, words)
    return text


def tier_parameters(params):
    tiers = params["tiers"]
    return D(tiers[0]["up_to"]), D(tiers[0]["rate"]), D(tiers[1]["rate"])


# ---------------------------------------------------------------- Overview
def reader_stats(extracted):
    """Average reading time and estimated cost per merchant document (from ai/doc_reader.py's run records)."""
    documents = (extracted or {}).get("documents") or {}
    metas = [d["meta"] for d in documents.values() if d.get("meta")]
    if not metas:
        return None
    costs = [D(m["estimated_cost_usd"]) for m in metas if m.get("estimated_cost_usd")]
    return {"documents": len(metas),
            "avg_seconds": sum(float(m["elapsed_seconds"]) for m in metas) / len(metas),
            "avg_cost": (float(sum(costs) / len(costs)) if len(costs) == len(metas) else None)}


def caught_sentence(reconciliation):
    """'In the demo set it caught ...' from the first review record of reconciliation.json (None if there is none)."""
    if not reconciliation:
        return None
    for review in reconciliation.get("review_required", []):
        checks = reconciliation["documents"][review["document"]]["checks"]
        lead = next((c for c in checks if c["check"] in review["affected_checks"] and c["status"] == "MISMATCH"), None)
        if lead and lead["percent"] is not None:
            label = CHECK_LABELS.get(lead["check"], (human_name(lead["check"]),))[0].lower()
            size = abs(Decimal(lead["percent"])).normalize()
            return (f"In the demo set it caught a POS report that {lead['direction']} {label} by "
                    f"{size:f}%.")
    return None


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
        ("STEP 2", "AI reads documents", "Claude extracts contract terms, POS reports and VAT returns. Each value "
                                         "carries a verbatim quote that code checks against the source."),
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

    reading = reader_stats(load_json("extracted.json"))
    if reading:
        headline = f"{reading['avg_seconds']:.0f} s" + (f" &middot; ~${reading['avg_cost']:.3f}"
                                                         if reading["avg_cost"] is not None else "")
        parse_html = (f"<div class='big'>{headline}</div><div class='small'>per document (average of "
                      f"{reading['documents']}, cost estimated from list prices)</div>")
    else:
        parse_html = ("<div class='small'>Reading time and cost are not recorded yet. "
                      "Run: python -m ai.doc_reader --all</div>")

    caught = caught_sentence(load_json("reconciliation.json"))
    caught_html = f" {html.escape(caught)}" if caught else ""

    why_cards = [
        ("Screens every transaction",
         detector_html + "<div class='small'>Measured on the 90-day simulated history with 4 injected fraud patterns "
                         "(answer key in data/). Real-world accuracy will be lower. The rules catch the injected "
                         "patterns; Isolation Forest flags statistical outliers for human review.</div>",
         "Self-dealing, voids and missing card revenue are flagged before any money is split."),
        ("Reads documents, with proof", parse_html,
         "Contracts, POS reports and VAT returns in any format. Every extracted value carries a verbatim "
         "quote that code checks against the source." + caught_html),
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
                        "- Claude reading of POS reports and VAT returns, cross-checked by code\n"
                        "- Idempotent settlement\n"
                        "- Eligibility rules and checks run as code on the same data as the rest of the demo; "
                        "Claude’s next-step advice is generated by the Claude API and validated by code")
    with simulated:
        with st.container(border=True):
            st.markdown("#### What is simulated")
            st.markdown("- 90-day sales history\n- Card and cash channels\n"
                        "- Merchant documents (sample POS reports and a VAT return)\n"
                        "- Future revenue in the projection\n"
                        "- Merchant applications (consent ticks are not stored; Sample Bakery is a sample profile)")


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
    for dash, text in (("dash", "Vertical dashed line: principal recovered (cumulative investor net = amount invested)"),
                       ("dot", "Vertical dotted line: cap reached / contract ends (cumulative contributions = cap)")):
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
            "Average daily revenue": eur(scenario["avg_daily_revenue"]),
            f"Principal recovered (investor net ≥ {investment})": principal,
            f"Cap reached (contributions = {cap})": reached,
            "Investor net recovery multiple": f"{m['net_multiple']}x",
            "Net IRR (investor)": pct(m["net_irr"]),
            "Gross IRR (contributions)": pct(m["gross_irr"]),
            "Merchant effective APR (incl. fees)": (
                f"{pct(m['merchant_apr'])} (repaid less than funded)"
                if m["merchant_apr"] is not None and m["merchant_apr"] < 0 else pct(m["merchant_apr"])),
            "Collected contributions": eur(m["total_drc_due"]),
            "Collected by investor": eur(m["total_investor_net"]),
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
    return "Projection basis: " + "; ".join(lead.get(key, f"{key.replace('_', ' ')}: ") + readable(text)
                                           for key, text in assumptions.items()) + "."


# settlements.json column -> header shown in the ledger table (payout_amount equals DRC due, so it is not shown)
LEDGER_COLUMNS = {"date": "Date", "verified_revenue": "Verified revenue", "rate_applied": "Rate",
                  "drc_due": "DRC due", "merchant_payout_fee": "Payout fee", "servicing_fee": "Servicing fee",
                  "investor_net": "Investor net", "cumulative_due": "Cumulative", "status": "Status"}


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
        ledger_table = ledger_table[list(LEDGER_COLUMNS)].rename(columns=LEDGER_COLUMNS)
        st.dataframe(ledger_table, width="stretch", hide_index=True,
                     column_config={"Status": st.column_config.TextColumn("Status", width="medium"),
                                    "Date": st.column_config.TextColumn("Date", width="small")})
    else:
        st.info("No settlements recorded yet.")

    merchant_health()


# ----------------------------------------------------------- Merchant health
RECOMMENDATION_LABELS = {"approve_reduced_rate": ("Approve reduced rate", GREEN),
                         "decline": ("Decline", RED),
                         "request_more_info": ("Request more information", ORANGE)}


def float_or_none(value):
    return None if value is None else float(value)


def health_figure(record):
    terms, metrics = record["contract_terms"], record["metrics"]
    short, long_ = terms["health_window_short_days"], terms["health_window_long_days"]
    chart = record["chart"]
    dates = [row["date"] for row in chart]
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=dates, y=[float(r["verified_net"]) for r in chart], mode="lines",
                             name="Daily verified net", line=dict(color="#C5D3E8", width=1.5)))
    fig.add_trace(go.Scatter(x=dates, y=[float_or_none(r["ma_long"]) for r in chart], mode="lines",
                             name=f"{long_}-day mean", line=dict(color=BLUE_DARK, width=3)))
    fig.add_trace(go.Scatter(x=dates, y=[float_or_none(r["ma_short"]) for r in chart], mode="lines",
                             name=f"{short}-day mean", line=dict(color=BLUE, width=3)))
    fig.add_trace(go.Scatter(x=dates, y=[float_or_none(r["trigger_level"]) for r in chart], mode="lines",
                             name=f"Trigger line ({metrics['trigger_decline_pct']}% below the {long_}-day mean)",
                             line=dict(color=RED, width=2, dash="dash")))
    by_date = {r["date"]: r for r in chart}
    fired = [d for d in record.get("triggered_days", []) if d in by_date and by_date[d]["ma_short"]]
    if fired:
        fig.add_trace(go.Scatter(x=fired, y=[float(by_date[d]["ma_short"]) for d in fired], mode="markers",
                                 name="Trigger fired", marker=dict(color=RED, size=8)))
    fig.update_yaxes(title_text="EUR per day", rangemode="tozero")
    fig.update_xaxes(range=[dates[0], dates[-1]])          # end on the last day of data, no empty space on the right
    return style(fig, f"{short}-day vs {long_}-day mean of verified net revenue"
                 + (" (simulated scenario)" if record["simulated_scenario"] else ""), height=420)


def bullet_list(items):
    return "<ul>" + "".join(f"<li>{html.escape(str(item))}</li>" for item in items) + "</ul>"


def merchant_health():
    st.divider()
    st.subheader("Merchant health")
    st.markdown("**Recommendation only. The payout rule changes only after the investor approves.**")
    record = load_json("health_recommendation.json")
    if record is None:
        st.info("No health check yet. Run: python -m engine.health_agent --scenario "
                f"data/health_scenarios/dip_scenario.csv, then {SNAPSHOT_HINT}")
        return
    terms, metrics, rec = record["contract_terms"], record["metrics"], record["recommendation"]
    short, long_ = metrics["short_window_days"], metrics["long_window_days"]

    base, dip = record.get("base_history"), record.get("scenario_dip")
    if record["simulated_scenario"] and base and dip:
        triggers = ("no day triggers" if base["triggered_days"] == 0 else
                    f"{base['triggered_days']} day(s) trigger")
        st.caption(f"On the {base['days']}-day base history {triggers} (largest fall on {base['largest_fall_date']}: "
                   f"{base['largest_fall_pct']}%). To show the agent, this scenario applies a −{dip['dip_pct']}% "
                   f"dip to the last {dip['dip_days']} days"
                   + (", with a sample merchant note." if record.get("merchant_note_used") else "."))
    st.plotly_chart(health_figure(record), width="stretch")
    st.caption(f"As of {metrics['as_of']}: the {short}-day mean is {eur(metrics['ma_short'])}, the {long_}-day mean "
               f"{eur(metrics['ma_long'])}, a fall of {metrics['decline_pct']}%. The contract triggers at more than "
               f"{metrics['trigger_decline_pct']}%.")

    with st.container(border=True):
        tags = ""
        if record["simulated_scenario"]:
            tags += (f"<span style='background:{ORANGE};color:white;border-radius:6px;padding:2px 10px;"
                     f"font-weight:700'>Simulated scenario</span> ")
        valid = record["status"] == "VALID"
        tags += (f"<span style='background:{GREEN if valid else RED};color:white;border-radius:6px;padding:2px 10px;"
                 f"font-weight:700'>{record['status']}</span>")
        st.markdown(f"#### Claude's recommendation &nbsp; {tags}", unsafe_allow_html=True)
        if not valid:
            st.error("This recommendation failed the checks and must not be relied on:\n\n"
                     + "\n".join(f"- {problem}" for problem in record["problems"]))
        label, color = RECOMMENDATION_LABELS.get(rec.get("recommendation"), (str(rec.get("recommendation")), GRAY))
        c1, c2, c3, c4 = st.columns(4)
        c1.markdown(f"<div style='font-size:0.85rem;color:{GRAY}'>Recommendation</div>"
                    f"<div style='font-size:1.4rem;font-weight:700;color:{color}'>{html.escape(label)}</div>",
                    unsafe_allow_html=True)
        c2.metric("Suggested duration", f"{rec.get('suggested_duration_days')} days")
        c2.caption(f"contract limit {terms['reduced_rate_max_days']} days")
        c3.metric("Reduced rate", pct(float(terms["reduced_rate"]), 0))
        c3.caption("set by the contract")
        c4.metric("Confidence", str(rec.get("confidence", "")).capitalize())

        left, right = st.columns(2)
        with left:
            st.markdown("**Conditions**")
            st.markdown(bullet_list(rec.get("conditions", [])), unsafe_allow_html=True)
            st.markdown("**Risks**")
            st.markdown(bullet_list(rec.get("risks", [])), unsafe_allow_html=True)
        with right:
            st.markdown("**Reasons and evidence**")
            for item in rec.get("reasons", []):
                st.markdown(f"<p style='margin:0 0 2px 0'><b>{html.escape(str(item.get('reason', '')))}</b></p>"
                            f"<p style='margin:0 0 10px 0;color:#33415c'>Evidence: "
                            f"{html.escape(str(item.get('evidence', '')))}</p>", unsafe_allow_html=True)

        meta = record.get("meta") or {}
        cost = meta.get("estimated_cost_usd")
        st.caption(f"Written by Claude in {meta.get('elapsed_seconds')} s"
                   + (f", ~${float(cost):.3f} (estimated from list prices)" if cost else "")
                   + ". Every number in the reasons was checked against the input data by code. "
                   + ("Based on a simulated revenue dip and a sample merchant note, not real data."
                      if record["simulated_scenario"] else ""))


# --------------------------------------------------------- AI verification
REASON_TEXT = {
    "SELF_DEALING_SUSPECT": "Same customer placed 5+ orders that day, each under EUR 1.50 (self-dealing pattern)",
    "PRICE_OVERRIDE_DEEP": "Charged 30% or more below the list price",
    "IF_OUTLIER": "Statistical outlier (Isolation Forest): kept in revenue, flagged for review",
}


def reasons_text(reasons):
    return "; ".join(REASON_TEXT.get(r, human_name(r)) for r in reasons)


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
        column.markdown(f"**{label}**")
        precision_col, recall_col = column.columns(2)
        precision_col.metric("Precision", pct(d["precision"], 0))
        recall_col.metric("Recall", pct(d["recall"], 0))
        column.caption(f"TP {d['tp']}, FP {d['fp']}, FN {d['fn']} (of {d['true']} injected)")
    business = metrics.get("business_orders")
    if business:
        st.success(f"John and Peter GmbH regular team orders: {business['false_positives']} false positives "
                   f"out of {business['total']} orders.")

    receipts_card()

    with st.expander("All detectors"):
        st.dataframe(pd.DataFrame([{
            "Detector": DETECTOR_LABELS.get(d["detector"], human_name(d["detector"])), "Level": d["level"],
            "Injected pattern": human_name(d["injected_type"]), "Injected": d["true"],
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
                             text=[", ".join(human_name(x) for x in f) for f in anomaly["flags"]], hovertemplate="%{x}: EUR %{y:.2f}<br>%{text}"))
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


# ------------------------------------------------------------- Apply for DRC
# source key -> (label, required, purpose, who can see it, how long it is kept)
CONSENT_SOURCES = {
    "paypal": ("PayPal transaction history", True,
               "to verify your revenue and run the eligibility check",
               "our verification engine; the investor sees verified totals, not individual customers",
               "while you apply and during the contract, then deleted unless the law requires it to be kept"),
    "pos": ("POS daily reports", True,
            "to cross-check your cash and card takings with PayPal data",
            "our verification engine and the AI that reads the PDFs; the investor sees the result of the check",
            "while you apply and during the contract, then deleted unless the law requires it to be kept"),
    "vat": ("VAT returns", True,
            "to confirm your total revenue for a period",
            "our verification engine and the AI that reads the PDFs; the investor sees the result of the check",
            "while you apply and during the contract, then deleted unless the law requires it to be kept"),
    "bank": ("Bank statements", False,
             "optional extra evidence, for example if much of your revenue is cash",
             "only the verification engine; never shown to the investor",
             "only while we check them, then deleted"),
}
STATUS_STYLE = {"met": ("✓", GREEN, "Met"), "not_met": ("✗", RED, "Not met"),
                "action_needed": ("⚠", ORANGE, "Action needed"), "needs_consent": ("⚠", GRAY, "Needs consent")}
TERMS_SHOWN = ("tiers", "baseline_threshold_amount", "cap_multiple", "cap_amount", "max_term_months",
               "servicing_fee_rate", "settlement_frequency", "collection_method", "reduced_rate",
               "reduced_rate_max_days", "requires_investor_approval")


def load_eligibility():
    """{profile id: record} from data/demo/eligibility_*.json, the demo merchant first."""
    records = {}
    for path in sorted(DEMO.glob("eligibility_*.json")):
        record = _read_json(str(path), path.stat().st_mtime)
        records[record["profile"]["id"]] = record
    return dict(sorted(records.items(), key=lambda item: item[0] != elig.DEMO_ID))


def term_text(name, value):
    if name.endswith("_rate") and isinstance(value, (int, float)):
        return f"{value * 100:g}%"
    return format_value(name, value)


def days_text(n):
    return elig.days_text(n) if isinstance(n, int) and not isinstance(n, bool) else f"{n} days"


def source_names(keys):
    return ", ".join(CONSENT_SOURCES[k][0] for k in keys if k in CONSENT_SOURCES)


def page_apply():
    st.title("Apply for DRC")
    st.markdown("**Financing based on verified data, not collateral. You choose what to share; nothing is stored "
                "in this demo.**")
    records = load_eligibility()
    if not records:
        st.info("No eligibility results yet. Run: python -m ai.eligibility_advisor, then " + SNAPSHOT_HINT)
        return

    # ---- Step 1
    st.subheader("Step 1 · Data consent")
    st.caption("Ticks exist only in your browser session. You can withdraw any consent at any time.")
    consents = {}
    for key, (label, required, purpose, who, how_long) in CONSENT_SOURCES.items():
        consents[key] = st.checkbox(f"{label} ({'required' if required else 'optional'})", value=required,
                                    key=f"consent_{key}")
        st.caption(f"Purpose: {purpose}. Who can see it: {who}. How long it is kept: {how_long}. "
                   f"You can withdraw it at any time.")

    # ---- Step 2
    st.subheader("Step 2 · Readiness check")
    labels = {pid: r["profile"]["name"] + (" (SAMPLE)" if r["profile"]["sample"] else "") for pid, r in records.items()}
    chosen = st.selectbox("Merchant profile", list(records), format_func=lambda pid: labels[pid])
    record = records[chosen]
    st.caption(record["profile"]["description"])
    required_sources = record["rules"]["rules"]["consent"]["required_sources"]
    checks = elig.apply_consent(record["checks"], consents, required_sources)
    met, total = elig.progress(checks)
    st.progress(met / total, text=f"{met} of {total} conditions met")

    rows = []
    for c in checks:
        icon, color, word = STATUS_STYLE[c["status"]]
        if c["status"] == "needs_consent":
            detail = f"Needs your consent to share: {source_names(c['missing_consent'])}."
            todo = "Tick the box in Step 1."
        else:
            detail = c["detail"]
            todo = c["how_to_fix"] if c["status"] != "met" else ""
        rows.append(f"<tr><td style='color:{color};font-size:1.2rem;font-weight:700'>{icon}</td>"
                    f"<td><b>{c['number']}. {html.escape(c['name'])}</b><br><span style='color:{GRAY}'>"
                    f"{html.escape(c['requirement'])}</span></td>"
                    f"<td style='color:{color};font-weight:700'>{word}</td>"
                    f"<td>{html.escape(detail)}</td><td class='quote'>{html.escape(todo)}</td></tr>")
    st.markdown("<table class='params'><tr><th></th><th>Condition</th><th>Status</th><th>What we found</th>"
                "<th>What to do</th></tr>" + "".join(rows) + "</table>", unsafe_allow_html=True)
    st.caption("All thresholds are illustrative. The account-age and annual-sales thresholds are modelled on the "
               "published conditions of PayPal Businesskredit.")

    # ---- Step 3
    st.subheader("Step 3 · What to do next")
    advice = record.get("advice")
    if advice is None:
        st.info("No advice was generated for this profile. Run: python -m ai.eligibility_advisor")
    else:
        valid = advice["status"] == "VALID"
        tag = (f"<span style='background:{GREEN if valid else RED};color:white;border-radius:6px;padding:2px 10px;"
               f"font-weight:700'>{advice['status']}</span>")
        st.markdown(f"Written by Claude &nbsp; {tag}", unsafe_allow_html=True)
        if not valid:
            st.error("This advice failed the checks and must not be relied on:\n\n"
                     + "\n".join(f"- {p}" for p in advice["problems"]))
        if advice.get("summary"):
            st.markdown(f"**{advice['summary']}**")
        by_id = {c["id"]: c for c in record["checks"]}
        for step in advice.get("steps") or []:
            check = by_id.get(step.get("check_id"))
            title = f"Condition {check['number']}: {check['name']}" if check else "Unknown condition"
            materials = bullet_list(step.get("materials", []))
            st.markdown(f"<div class='doc-card' style='min-height:0;margin-bottom:10px'><h4>{html.escape(title)}</h4>"
                        f"<p>{html.escape(str(step.get('what_to_do', '')))}</p>"
                        f"<p><b>Prepare:</b></p>{materials}"
                        f"<p><b>Time:</b> about {html.escape(days_text(step.get('time_estimate_days')))}</p></div>",
                        unsafe_allow_html=True)
        meta = advice.get("meta") or {}
        cost = meta.get("estimated_cost_usd")
        st.caption("Pre-generated by Claude; this page does not call Claude. Every number and condition id in it was "
                   f"checked by code. Written in {meta.get('elapsed_seconds')} s"
                   + (f", ~${float(cost):.3f} (estimated from list prices)" if cost else "")
                   + (". The advice assumes that all required data is shared."
                      if any(c["status"] == "needs_consent" for c in checks) else "."))

    # ---- Step 4
    st.subheader("Step 4 · Indicative amount and main terms")
    if met == total:
        ind = elig.indicative_amount(record["annual_verified_revenue_eur"], record["rules"])
        st.metric("Indicative amount", eur(ind["amount"], 0))
        st.caption(f"{ind['percent']:g}% of annualised verified revenue ({eur(ind['annual_verified_revenue'], 0)}), "
                   f"capped at {eur(ind['cap'], 0)}.")
        st.markdown(f"**{ind['disclaimer']}**")
        params = need_contract()["params"]
        terms = [f"<tr><td><b>{html.escape(human_name(n))}</b></td><td>{html.escape(term_text(n, params[n]))}</td></tr>"
                 for n in TERMS_SHOWN if params.get(n) is not None]
        st.markdown("Main terms, as in the demo contract:")
        st.markdown("<table class='params'><tr><th>Term</th><th>Value</th></tr>" + "".join(terms) + "</table>",
                    unsafe_allow_html=True)
    else:
        open_items = [f"{c['number']}. {c['name']}" for c in checks if c["status"] != "met"]
        st.warning(f"{total - met} of {total} conditions are still open: " + "; ".join(open_items)
                   + ". The indicative amount appears here once all of them are met.")


# ------------------------------------------------------- Merchant documents
DOC_ORDER = ("z_bericht_A", "z_bericht_B", "ustva")
SOURCE_FORMATS = {"z_bericht_A": "Kassensystem A", "z_bericht_B": "Kassensystem B", "ustva": "UStVA"}
DOC_TYPE_LABELS = {"z_report": "POS daily report (Z-Bericht)", "vat_return": "VAT return (Umsatzsteuer-Voranmeldung)",
                   "other": "Other document"}
CHECK_LABELS = {  # check -> (what the merchant reported, name of our data)
    "paypal": ("PayPal payments", "PayPal data"),
    "card": ("Card payments", "card terminal data"),
    "cash": ("Cash payments", "our cash data"),
    "gross_total": ("Gross total", "our gross revenue data"),
    "vat_vs_verified_revenue": ("Revenue implied by the VAT bases", "verified revenue"),
}


STATUS_LABELS = {"NOT_REPORTED": "Not reported", "NO_REFERENCE": "No data to compare"}


def doc_key(file_name):
    return next((k for k in DOC_ORDER if file_name.startswith(k)), None)


def fmt_percent(value):
    if value is None:
        return "n/a"
    x = Decimal(str(value)).quantize(Decimal("0.01"))
    return f"{abs(x) if x == 0 else x}%".replace("-", "−")


def source_label(source, values):
    """Reader-friendly name of the reference data. The October figures come from the simulated ledger."""
    if not source or ".csv" not in source:      # verified_daily.csv and daily_summary.csv are both simulated history
        return source or ""
    start, end = date.fromisoformat(values["period_start"]), date.fromisoformat(values["period_end"])
    month = start.strftime("%b %Y")
    if values.get("doc_type") == "vat_return":
        return f"Simulated ledger, {start.day}–{end.day} {month}"
    return f"Simulated ledger ({month})"


def plural_checks(n):
    return f"{n} check" if n == 1 else f"{n} checks"


def sections_text(contract_clause):
    numbers = sorted({int(n) for n in re.findall(r"Section (\d+)", contract_clause or "")})
    if not numbers:
        return "the contract"
    if len(numbers) > 1 and numbers == list(range(numbers[0], numbers[-1] + 1)):
        return f"Sections {numbers[0]}–{numbers[-1]}"
    return ("Section " if len(numbers) == 1 else "Sections ") + ", ".join(str(n) for n in numbers)


def review_message(review, checks):
    """Plain-English explanation of one review record, built from the reconciliation numbers."""
    by_name = {c["check"]: c for c in checks}
    lead = by_name[review["affected_checks"][0]]
    label, data_label = CHECK_LABELS.get(lead["check"], (human_name(lead["check"]), "our data"))
    text = (f"{label} reported EUR {review['reported']}, {data_label} shows EUR {review['reference']} "
            f"({fmt_percent(lead['percent'])}, {lead['direction']}). Flagged for review (contract "
            f"{sections_text(review['contract'])}: a person decides whether it is a breach); "
            f"no automatic change to the split.")
    same = [by_name[n] for n in review["affected_checks"][1:] if n in by_name]
    if same:
        text += (" Same root cause: " + ", ".join(
            f"{CHECK_LABELS.get(c['check'], (human_name(c['check']),))[0].lower()} is short by the same amount "
            f"({fmt_percent(c['percent'])})" for c in same) + ", counted as one finding.")
    return text


def select_document(name):
    st.session_state["merchant_doc"] = name


def page_merchant_docs():
    extracted = need("extracted.json")["documents"]
    reconciliation = need("reconciliation.json")
    texts = need("merchant_doc_texts.json")
    names = sorted(extracted, key=lambda n: (DOC_ORDER.index(doc_key(n)) if doc_key(n) else len(DOC_ORDER), n))

    st.title("Merchant documents")
    st.markdown("**Merchants submit POS reports and VAT returns in whatever format their till or tax adviser "
                "produces. AI reads them; code checks every number against PayPal and the verified ledger.**")
    tolerance = reconciliation.get("tolerance_percent", "3")

    def verdict(name):
        summary = reconciliation["documents"].get(name, {}).get("summary", {})
        total = sum(summary.get(k, 0) for k in ("ok", "mismatch", "not_reported", "no_reference"))
        if summary.get("mismatch"):
            return "MISMATCH", RED, f"MISMATCH: {summary['mismatch']} of {plural_checks(total)} outside ±{tolerance}%"
        return "OK", GREEN, f"OK: {summary.get('ok', 0)} of {plural_checks(total)} within ±{tolerance}%"

    if st.session_state.get("merchant_doc") not in extracted:
        st.session_state["merchant_doc"] = next((n for n in names if verdict(n)[0] == "MISMATCH"), names[0])
    selected = st.session_state["merchant_doc"]

    for column, name in zip(st.columns(len(names)), names):
        meta = extracted[name]["meta"]
        status, color, text = verdict(name)
        cost = meta.get("estimated_cost_usd")
        reading = f"{float(meta['elapsed_seconds']):.1f} s" + (f" &middot; ~${float(cost):.3f}" if cost else "")
        column.markdown(
            f'<div class="doc-card{" selected" if name == selected else ""}" style="border-top: 5px solid {color}">'
            f'<h4>{DOC_TYPE_LABELS.get(extracted[name]["values"]["doc_type"], "Document")}</h4>'
            f'<p>Source format: <b>{SOURCE_FORMATS.get(doc_key(name), "Other")}</b></p>'
            f'<p>Read by AI in {reading}</p><p style="color:#6B7C93">{html.escape(name)}</p>'
            f'<div class="verdict" style="color:{color}">{text}</div></div>', unsafe_allow_html=True)
        column.button("Showing below" if name == selected else "Open details", key=f"open_{name}",
                      on_click=select_document, args=(name,), disabled=name == selected)

    result, checks = extracted[selected], reconciliation["documents"].get(selected, {}).get("checks", [])
    st.divider()
    st.subheader(f"{SOURCE_FORMATS.get(doc_key(selected), 'Document')} - {selected}")

    reviews = [r for r in reconciliation.get("review_required", []) if r["document"] == selected]
    if reviews:
        for review in reviews:
            st.error(review_message(review, checks))
        info = next((i["detail"] for i in result["validation"].get("info", []) if "adds up" in i["detail"]
                     or "=" in i["detail"]), None)
        if info and "does not add up" not in info:
            st.caption("The document is internally consistent (cash + card + PayPal = total), so the gap only shows "
                       "when the figures are compared with external data.")
    else:
        st.success(f"All checks are within ±{tolerance}% of our data. Nothing to review.")

    left, right = st.columns([5, 7])
    with left:
        st.markdown("#### Document text (as read from the PDF)")
        with st.container(height=640, border=True):
            st.markdown(f'<div class="doc-text">{html.escape(texts.get(selected, ""))}</div>', unsafe_allow_html=True)
    with right:
        st.markdown("#### Extracted fields, with source quotes")
        rows = []
        for field, value in result["values"].items():
            evidence = result["evidence"][field]
            printed = result["raw"].get(field)
            shown = "—" if value is None else html.escape(str(plain_value(value)))
            if value is not None and printed is not None and str(printed) != str(value):
                shown += f" <span style='color:#6B7C93'>(printed {html.escape(str(printed))})</span>"
            quote = evidence["source_quote"] or f"(not in the document: {evidence['not_found_reason']})"
            rows.append(f"<tr><td><b>{html.escape(human_name(field))}</b></td><td>{shown}</td>"
                        f"<td class='quote'>{html.escape(quote)}</td></tr>")
        with st.container(height=340, border=True):
            st.markdown("<table class='params'><tr><th>Field</th><th>Value</th><th>Source quote</th></tr>"
                        + "".join(rows) + "</table>", unsafe_allow_html=True)

        st.markdown("#### Comparison with our data")
        comparison = []
        for c in checks:
            color = {"OK": GREEN, "MISMATCH": RED}.get(c["status"], GRAY)
            label = CHECK_LABELS.get(c["check"], (human_name(c["check"]),))[0]
            status = STATUS_LABELS.get(c["status"], c["status"]) + (f" ({c['direction']})" if c["direction"] else "")
            comparison.append(
                f"<tr><td>{html.escape(label)}</td><td>{c['reported'] or '—'}</td><td>{c['reference'] or '—'}</td>"
                f"<td>{c['difference'] or '—'}</td><td>{fmt_percent(c['percent'])}</td>"
                f"<td style='color:{color};font-weight:700'>{status}</td>"
                f"<td class='quote'>{html.escape(source_label(c['reference_source'], result['values']))}</td></tr>")
        st.markdown("<table class='params'><tr><th>Check</th><th>Reported (EUR)</th><th>Our data (EUR)</th>"
                    "<th>Diff</th><th>%</th><th>Result</th><th>Our data source</th></tr>"
                    + "".join(comparison) + "</table>", unsafe_allow_html=True)
        st.caption("October figures are simulated history; real PayPal sandbox data starts 6 Nov 2026.")


# ------------------------------------------------------------------ Contract
@st.cache_data
def contract_text(path_text, modified):
    import pdfplumber
    with pdfplumber.open(path_text) as pdf:
        return "\n\n".join((page.extract_text() or "") for page in pdf.pages)


def money_text(value):
    """20000 -> 'EUR 20,000'; 703.26 -> 'EUR 703.26'. Display only; the stored value is not changed."""
    amount = D(value)
    return eur(amount, 0 if amount == amount.to_integral_value() else 2)


def format_value(name, value):
    """Contract value as text for people: amounts as EUR, multiples as 1.15x, terms with their unit."""
    if name == "tiers" and isinstance(value, list):
        return "; ".join(f"up to {money_text(t['up_to'])}: {t['rate'] * 100:g}%" if t["up_to"] is not None
                         else f"above: {t['rate'] * 100:g}%" for t in value)
    if isinstance(value, bool):
        return "yes" if value else "no"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        if name.endswith("_amount"):
            return money_text(value)
        if name.endswith("_multiple"):
            return f"{value:g}x"
        for suffix, unit in (("_months", "month"), ("_days", "day")):
            if name.endswith(suffix):
                return f"{value:g} {unit}" + ("" if value == 1 else "s")
    return str(plain_value(value))


def extraction_note(contract):
    """'Extracted by <model> with tool use in 23 s, ~$0.051.' with whatever the run record holds."""
    meta = contract.get("meta") or {}
    parts = []
    if meta.get("elapsed_seconds") is not None:
        parts.append(f"in {float(meta['elapsed_seconds']):.0f} s")
    if meta.get("estimated_cost_usd"):
        parts.append(f"~${float(meta['estimated_cost_usd']):.3f} (estimated from list prices)")
    return (f"Extracted by {contract.get('model', 'Claude')} with tool use"
            + (" " + ", ".join(parts) if parts else "") + f". {len(contract['params'])} fields, each with a source quote.")


REVENUE_BASE_NOTE = ("Card and cash are part of Total Receipts, so the merchant’s POS reports and VAT returns "
                     "are checked on the Merchant documents page.")


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
        st.caption(extraction_note(contract))
        rows = []
        for name, value in contract["params"].items():
            evidence = contract["evidence"].get(name, {})
            quote = evidence.get("source_quote") or f"(not found: {evidence.get('not_found_reason')})"
            rows.append(f"<tr><td><b>{html.escape(human_name(name))}</b></td><td>{html.escape(format_value(name, value))}</td>"
                        f"<td class='quote'>{html.escape(quote)}</td></tr>")
            if name == "revenue_base":
                rows.append(f"<tr><td colspan='3' style='color:{GRAY};font-size:0.82rem'>{html.escape(REVENUE_BASE_NOTE)}"
                            f"</td></tr>")
        with st.container(height=640, border=True):
            st.markdown("<table class='params'><tr><th>Field</th><th>Value</th><th>Source quote</th></tr>"
                        + "".join(rows) + "</table>", unsafe_allow_html=True)


# ---------------------------------------------------------------- navigation
st.sidebar.markdown(f"<h2 style='color:{BLUE_DARK};margin-bottom:0'>DRC Copilot</h2>", unsafe_allow_html=True)
st.sidebar.caption("Read-only demo. Simulated history plus PayPal sandbox payments.")

navigation = st.navigation([
    st.Page(page_overview, title="Overview", url_path="overview", default=True),
    st.Page(page_merchant, title="Merchant view", url_path="merchant"),
    st.Page(page_apply, title="Apply for DRC", url_path="apply"),
    st.Page(page_investor, title="Investor view", url_path="investor"),
    st.Page(page_ai, title="AI verification", url_path="ai-verification"),
    st.Page(page_merchant_docs, title="Merchant documents", url_path="merchant-documents"),
    st.Page(page_contract, title="Contract", url_path="contract"),
])
navigation.run()
