# Two-layer anomaly detection on data/transactions.csv (see CLAUDE.md §4 step 2).
#
# Layer 1 (transaction level): IsolationForest score + rules. Every COMPLETED
#   transaction ends up in one of three tiers:
#     EXCLUDED      SELF_DEALING_SUSPECT (rule-confirmed)      -> removed from verified revenue
#     UNDER_REVIEW  PRICE_OVERRIDE_DEEP (charged >= 30 % below list_price),
#                   or flagged only by the IsolationForest      -> stays in verified revenue,
#                                                                 summed separately as at-risk
#     (clean)       everything else (incl. price_overridden with a smaller discount,
#                   which counts as a normal discount)
#   VOIDED transactions are not revenue; they are only used for the daily
#   VOID_SPIKE_DAY check.
# Layer 2 (daily level): moving averages, evening share, card share ->
#   CARD_REVENUE_MISSING, EVENING_SHARE_DROP, SELF_DEALING_DAY, VOID_SPIKE_DAY,
#   plus REVENUE_DECLINE and PRICE_OVERRIDE_CLUSTER_DAY, which are health signals
#   (not scored against ground truth).
#
# verified_net = verified_gross - estimated PayPal/card fees (config.FEE_MODEL).
# verified_net is the base for the daily split (CLAUDE.md §3).
#
# Outputs:
#   data/flagged_transactions.csv   every suspicious transaction + reasons + tier
#   data/verified_daily.csv         raw_gross, excluded_amount, at_risk_amount,
#                                   verified_gross, est_fees, verified_net, day flags
#   docs/screenshots/anomaly_detected.png
# and prints precision / recall per injected anomaly type
# (ground truth: data/injected_anomalies.csv).
#
# Run:  python -m ai.anomaly
#
# Money is kept as integer cents; floats are used only for ML features,
# ratios and plotting.
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import IsolationForest

import config

ROOT = Path(__file__).resolve().parent.parent
TX_PATH = ROOT / "data" / "transactions.csv"
INJECTED_PATH = ROOT / "data" / "injected_anomalies.csv"
FLAGGED_PATH = ROOT / "data" / "flagged_transactions.csv"
DAILY_PATH = ROOT / "data" / "verified_daily.csv"
PLOT_PATH = ROOT / "docs" / "screenshots" / "anomaly_detected.png"

RANDOM_STATE = 42

# ---- layer 1 settings
IF_CONTAMINATION = 0.02          # share of transactions IsolationForest flags
SELF_DEALING_MIN_COUNT = 5       # same customer, same day, >= this many orders ...
SELF_DEALING_MAX_CENTS = 150     # ... each below EUR 1.50
ANON_PREFIXES = ("CASH-",)       # anonymous customers: no per-customer rule possible
BUSINESS_WEEKDAYS = (1, 3)       # Tue, Thu
BUSINESS_MIN_CENTS, BUSINESS_MAX_CENTS = 8000, 20000  # EUR 80-200
DEEP_DISCOUNT_PCT = 30           # discount of >= 30 % off list_price = deep override

# ---- layer 2 settings
EVENING_HOUR = 18                # "evening" = 18:00 and later
CARD_FRACTION = 0.20             # card revenue < 20 % of the same-weekday baseline
CARD_BASELINE_WEEKS = 4          # average of the previous 4 same weekdays ...
CARD_BASELINE_MIN = 3            # ... needs at least 3 usable weeks
DECLINE_RATIO = 0.85             # 7-day MA more than 15 % below 30-day MA
EVENING_DROP_RATIO = 0.85        # 7-day evening share < 85 % of the earlier 30-day share
SELF_DEALING_DAY_MIN = 10        # >= 10 self-dealing flags on a day
PRICE_CLUSTER_DAY_MIN = 5        # >= 5 deep price overrides on a day
VOID_RATE_MULT, VOID_RATE_MIN = 3.0, 0.05  # void rate >= 3x median and >= 5 %


def money(cents):
    """Integer cents -> '12.34' (exact, no floats)."""
    return f"{Decimal(int(cents)) / 100:.2f}"


def fee_cents(channel, cents):
    """Estimated fee for one transaction, in cents, rounded half-up (config.FEE_MODEL)."""
    model = config.FEE_MODEL[channel]
    fee = Decimal(int(cents)) * model["percent"] + model["fixed"] * 100
    return int(fee.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


# ------------------------------------------------------------------ layer 1
def load_transactions():
    tx = pd.read_csv(TX_PATH, dtype={"amount_eur": str})
    tx["timestamp"] = pd.to_datetime(tx["timestamp"])
    tx["date"] = tx["timestamp"].dt.normalize()
    tx["hour"] = tx["timestamp"].dt.hour
    tx["weekday"] = tx["timestamp"].dt.day_of_week
    tx["amount_cents"] = tx["amount_eur"].map(lambda s: int(Decimal(s) * 100))
    tx["list_cents"] = tx["list_price"].map(lambda s: int(Decimal(s) * 100))
    tx["discount_pct"] = 1 - tx["amount_cents"] / tx["list_cents"]
    tx["price_overridden"] = tx["price_overridden"].astype(bool)
    return tx


def detect_transactions(tx):
    tx = tx.copy()
    tx["amount"] = tx["amount_cents"] / 100.0
    tx["is_weekend"] = (tx["weekday"] >= 5).astype(int)
    tx["identified"] = ~tx["customer_id"].str.startswith(ANON_PREFIXES)
    completed = tx["status"] == "COMPLETED"

    # features -----------------------------------------------------------
    same_day = tx.groupby(["date", "customer_id"])["txn_id"].transform("count")
    tx["cust_day_count"] = np.where(tx["identified"], same_day, 1)
    slot_median = tx.groupby(["is_weekend", "hour"])["amount"].transform("median")
    tx["dev_from_median"] = tx["amount"] - slot_median
    tx["rel_dev"] = tx["amount"] / slot_median

    features = pd.DataFrame({
        "amount": tx["amount"],
        "hour": tx["hour"],
        "weekday": tx["weekday"],
        "is_business": (tx["customer_type"] == "BUSINESS").astype(int),
        "is_voided": (tx["status"] == "VOIDED").astype(int),
        "price_overridden": tx["price_overridden"].astype(int),
        "cust_day_count": tx["cust_day_count"],
        "dev_from_median": tx["dev_from_median"],
        "rel_dev": tx["rel_dev"],
    })
    for channel in ("PAYPAL", "POS_CARD", "POS_CASH_REPORTED"):
        features[f"ch_{channel}"] = (tx["channel"] == channel).astype(int)

    forest = IsolationForest(n_estimators=200, contamination=IF_CONTAMINATION,
                             random_state=RANDOM_STATE)
    forest.fit(features)
    tx["anomaly_score"] = -forest.score_samples(features)  # higher = more unusual
    tx["if_raw"] = forest.predict(features) == -1

    # John and Peter's regular team orders are normal business -------------
    tx["regular_business"] = (
        (tx["customer_type"] == "BUSINESS")
        & tx["weekday"].isin(BUSINESS_WEEKDAYS)
        & tx["amount_cents"].between(BUSINESS_MIN_CENTS, BUSINESS_MAX_CENTS)
        & completed
    )

    # rules: only COMPLETED transactions can carry revenue risk -------------
    tx["r_void"] = tx["status"] == "VOIDED"      # used for the daily void-spike check only
    tx["r_self"] = (
        completed
        & tx["identified"]
        & (tx["cust_day_count"] >= SELF_DEALING_MIN_COUNT)
        & (tx["amount_cents"] < SELF_DEALING_MAX_CENTS)
        & ~tx["regular_business"]
    )
    # deep discount = charged at least 30 % below list price (integer math, no float edge cases);
    # a smaller discount on an overridden sale is treated as a normal discount
    deep = tx["amount_cents"] * 100 <= tx["list_cents"] * (100 - DEEP_DISCOUNT_PCT)
    tx["r_price"] = completed & tx["price_overridden"] & deep
    tx["r_if"] = completed & tx["if_raw"] & ~tx["regular_business"]

    masks = {
        "PRICE_OVERRIDE_DEEP": tx["r_price"],
        "SELF_DEALING_SUSPECT": tx["r_self"],
        "IF_OUTLIER": tx["r_if"],
    }
    names = list(masks)
    hits = np.column_stack([masks[n].to_numpy() for n in names])
    tx["reasons"] = [";".join(n for n, hit in zip(names, row) if hit) for row in hits]

    tx["tier"] = np.select(
        [tx["r_self"], tx["r_price"] | tx["r_if"]],
        ["EXCLUDED", "UNDER_REVIEW"],
        default="",
    )
    tx["flagged"] = tx["tier"] != ""
    tx["fee_cents"] = [fee_cents(c, a) for c, a in zip(tx["channel"], tx["amount_cents"])]
    return tx


# ------------------------------------------------------------------ layer 2
def card_missing_flags(card):
    """Card revenue < 20 % of the mean of the previous 4 same-weekday values.

    Days already flagged are skipped when building the baseline, so one bad
    day does not lower the yardstick for the next same weekday.
    """
    values = card.to_numpy(dtype=float)
    n = len(values)
    flagged = np.zeros(n, dtype=bool)
    baseline = np.full(n, np.nan)
    for i in range(n):
        picks = []
        j = i - 7
        while j >= 0 and len(picks) < CARD_BASELINE_WEEKS:
            if not flagged[j]:
                picks.append(values[j])
            j -= 7
        if len(picks) >= CARD_BASELINE_MIN:
            baseline[i] = np.mean(picks)
            flagged[i] = values[i] < CARD_FRACTION * baseline[i]
    return pd.Series(flagged, index=card.index), pd.Series(baseline, index=card.index)


def build_daily(tx):
    days = pd.date_range(tx["date"].min(), tx["date"].max(), freq="D")
    done = tx[tx["status"] == "COMPLETED"]
    verified_tx = done[done["tier"] != "EXCLUDED"]

    def per_day(frame, col="amount_cents"):
        return frame.groupby("date")[col].sum().reindex(days, fill_value=0)

    raw_gross = per_day(done)
    excluded = per_day(done[done["tier"] == "EXCLUDED"])
    at_risk = per_day(done[done["tier"] == "UNDER_REVIEW"])
    verified_gross = raw_gross - excluded
    est_fees = per_day(verified_tx, "fee_cents")
    verified_net = verified_gross - est_fees

    card = per_day(verified_tx[verified_tx["channel"] == "POS_CARD"])
    evening = per_day(verified_tx[verified_tx["hour"] >= EVENING_HOUR])

    gross_f = verified_gross.astype(float)
    ev_f = evening.astype(float)
    net_f = verified_net.astype(float)
    ma7 = net_f.rolling(7, min_periods=7).mean()
    ma30 = net_f.rolling(30, min_periods=14).mean()

    card_flag, card_baseline = card_missing_flags(card)
    evening_share = ev_f / gross_f.replace(0, np.nan)
    card_share = card.astype(float) / gross_f.replace(0, np.nan)
    recent_share = ev_f.rolling(7, min_periods=7).sum() / gross_f.rolling(7, min_periods=7).sum()
    base_share = (ev_f.shift(7).rolling(30, min_periods=14).sum()
                  / gross_f.shift(7).rolling(30, min_periods=14).sum())

    n_self = tx[tx["r_self"]].groupby("date").size().reindex(days, fill_value=0)
    n_price = tx[tx["r_price"]].groupby("date").size().reindex(days, fill_value=0)
    voided_n = tx[tx["r_void"]].groupby("date").size().reindex(days, fill_value=0)
    total_n = tx.groupby("date").size().reindex(days, fill_value=0)
    void_rate = voided_n / total_n
    void_threshold = max(VOID_RATE_MIN, VOID_RATE_MULT * float(void_rate.median()))

    flags = pd.DataFrame({
        "SELF_DEALING_DAY": n_self >= SELF_DEALING_DAY_MIN,
        "VOID_SPIKE_DAY": void_rate >= void_threshold,
        "CARD_REVENUE_MISSING": card_flag,
        "EVENING_SHARE_DROP": recent_share < EVENING_DROP_RATIO * base_share,
        "REVENUE_DECLINE": ma7 < DECLINE_RATIO * ma30,   # health signal, not scored
        "PRICE_OVERRIDE_CLUSTER_DAY": n_price >= PRICE_CLUSTER_DAY_MIN,  # health signal, not scored
    }, index=days)
    day_flags = [";".join(n for n, hit in zip(flags.columns, row) if hit)
                 for row in flags.to_numpy()]

    daily = pd.DataFrame({
        "date": days.strftime("%Y-%m-%d"),
        "raw_gross": [money(c) for c in raw_gross],
        "excluded_amount": [money(c) for c in excluded],
        "at_risk_amount": [money(c) for c in at_risk],
        "verified_gross": [money(c) for c in verified_gross],
        "est_fees": [money(c) for c in est_fees],
        "verified_net": [money(c) for c in verified_net],
        "flagged_txn_count": tx[tx["flagged"]].groupby("date").size().reindex(days, fill_value=0).values,
        "void_rate": void_rate.round(4).values,
        "card_revenue_eur": [money(c) for c in card],
        "card_baseline_eur": (card_baseline / 100).round(2).values,
        "card_share": card_share.round(4).values,
        "evening_share": evening_share.round(4).values,
        "evening_share_7d": recent_share.round(4).values,
        "verified_net_ma7": (ma7 / 100).round(2).values,
        "verified_net_ma30": (ma30 / 100).round(2).values,
        "day_flags": day_flags,
    })
    series = {"days": days, "raw_gross": raw_gross, "excluded": excluded, "at_risk": at_risk,
              "verified_gross": verified_gross, "est_fees": est_fees, "verified_net": verified_net}
    return daily, flags, series


# ---------------------------------------------------------------- evaluation
def prf(pred, truth):
    tp = len(pred & truth)
    precision = tp / len(pred) if pred else float("nan")
    recall = tp / len(truth) if truth else float("nan")
    return tp, len(pred) - tp, len(truth) - tp, precision, recall


def pct(x):
    return "  n/a" if x != x else f"{x * 100:5.1f}%"


def evaluate(tx, flags):
    inj = pd.read_csv(INJECTED_PATH)
    inj_txn = inj[inj["txn_id"].notna()]
    truth_txn = {t: set(g["txn_id"]) for t, g in inj_txn.groupby("anomaly_type")}
    truth_day = {t: set(pd.to_datetime(g["date"])) for t, g in inj.groupby("anomaly_type")}
    header = (f"{'injected type':<28}{'detector':<22}{'true':>6}{'TP':>6}{'FP':>6}{'FN':>6}"
              f"{'precision':>11}{'recall':>9}")

    print("\n=== Layer 1: transaction level (match by txn_id) ===")
    print(header)
    txn_eval = [
        ("SELF_DEALING_SMALL_ORDERS", "SELF_DEALING_SUSPECT", "r_self"),
        ("PRICE_OVERRIDE", "PRICE_OVERRIDE_DEEP", "r_price"),
    ]
    for inj_type, detector, col in txn_eval:
        pred = set(tx.loc[tx[col], "txn_id"])
        truth = truth_txn.get(inj_type, set())
        tp, fp, fn, p, r = prf(pred, truth)
        print(f"{inj_type:<28}{detector:<22}{len(truth):>6}{tp:>6}{fp:>6}{fn:>6}{pct(p):>11}{pct(r):>9}")
    print("(VOID_SPIKE is scored at day level only: voided sales are not revenue.)")
    ov = tx[tx["price_overridden"] & (tx["status"] == "COMPLETED")]
    print(f"price_overridden COMPLETED txns: {len(ov)}; deep (>= {DEEP_DISCOUNT_PCT} % off list): "
          f"{int(ov['r_price'].sum())}; treated as normal discount: {int((~ov['r_price']).sum())}")

    scored_truth = truth_txn.get("SELF_DEALING_SMALL_ORDERS", set()) | truth_txn.get("PRICE_OVERRIDE", set())
    print("\n--- Tiers (COMPLETED transactions) ---")
    for tier, label in (("EXCLUDED", "excluded from verified"), ("UNDER_REVIEW", "under review (kept, at risk)")):
        part = tx[tx["tier"] == tier]
        injected = len(set(part["txn_id"]) & scored_truth)
        print(f"  {label:<30}: {len(part):>5} txns, EUR {money(part['amount_cents'].sum()):>9}, "
              f"injected among them: {injected}")
    review = tx[tx["tier"] == "UNDER_REVIEW"]
    print(f"    of which PRICE_OVERRIDE_DEEP: {int(review['r_price'].sum())}, "
          f"IsolationForest only: {int((review['r_if'] & ~review['r_price']).sum())}")

    if_set = set(tx.loc[tx["r_if"], "txn_id"])
    if_only = set(tx.loc[tx["r_if"] & ~tx["r_price"] & ~tx["r_self"], "txn_id"])
    print(f"\nIsolationForest (contamination={IF_CONTAMINATION}), COMPLETED txns only: flagged {len(if_set)}, "
          f"{len(if_set & scored_truth)} of them are injected self-dealing / price-override orders")
    print(f"  flagged by IsolationForest only (no rule): {len(if_only)}, "
          f"of which injected: {len(if_only & scored_truth)}")
    for inj_type in ("SELF_DEALING_SMALL_ORDERS", "PRICE_OVERRIDE"):
        truth = truth_txn.get(inj_type, set())
        rec = len(if_set & truth) / len(truth) if truth else float("nan")
        print(f"  IF recall on {inj_type:<27}{pct(rec).strip()}")

    biz = tx["customer_type"] == "BUSINESS"
    print("\n=== John and Peter GmbH (BUSINESS) orders ===")
    print(f"  business orders total              : {int(biz.sum())}")
    print(f"  regular Tue/Thu EUR 80-200 orders  : {int(tx['regular_business'].sum())}")
    print(f"  IsolationForest raw false alarms   : {int((tx['if_raw'] & biz).sum())}")
    print(f"  final false positives (flagged)    : {int((tx['flagged'] & biz).sum())}")

    print("\n=== Layer 2: daily level (match by date) ===")
    print(header)
    day_eval = [
        ("CARD_REVENUE_MISSING", "CARD_REVENUE_MISSING"),
        ("EVENING_TRAFFIC_DROP", "EVENING_SHARE_DROP"),
        ("SELF_DEALING_SMALL_ORDERS", "SELF_DEALING_DAY"),
        ("VOID_SPIKE", "VOID_SPIKE_DAY"),
    ]
    for inj_type, detector in day_eval:
        pred = set(flags.index[flags[detector].to_numpy()])
        truth = truth_day.get(inj_type, set())
        tp, fp, fn, p, r = prf(pred, truth)
        print(f"{inj_type:<28}{detector:<22}{len(truth):>6}{tp:>6}{fp:>6}{fn:>6}{pct(p):>11}{pct(r):>9}")
    print(f"\nHealth signals (not scored): REVENUE_DECLINE on {int(flags['REVENUE_DECLINE'].sum())} day(s), "
          f"PRICE_OVERRIDE_CLUSTER_DAY on {int(flags['PRICE_OVERRIDE_CLUSTER_DAY'].sum())} day(s)")


# ---------------------------------------------------------------------- plot
def plot_detection(series, flags):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt

    days = series["days"]
    raw = series["raw_gross"].to_numpy() / 100
    ver = series["verified_gross"].to_numpy() / 100
    half = pd.Timedelta(hours=12)

    fig, ax = plt.subplots(figsize=(12.5, 5.8), dpi=200)
    ax.plot(days, raw, color="#9aa5b1", lw=1.6, label="Raw daily revenue (gross)", zorder=2)
    ax.plot(days, ver, color="#1f3b5c", lw=1.8, label="Verified revenue (gross, after exclusions)", zorder=3)

    spans = [
        ("REVENUE_DECLINE", "#7b2cbf", 0.10, "Revenue decline (health signal)"),
        ("EVENING_SHARE_DROP", "#2a9d8f", 0.20, "Evening revenue share dropped"),
        ("VOID_SPIKE_DAY", "#ff9f1c", 0.28, "Void-rate spike"),
        ("SELF_DEALING_DAY", "#d62728", 0.28, "Self-dealing micro orders"),
    ]
    for name, color, alpha, label in spans:
        flagged_days = days[flags[name].to_numpy()]
        for i, d in enumerate(flagged_days):
            ax.axvspan(d - half, d + half, color=color, alpha=alpha, lw=0,
                       label=label if i == 0 else None, zorder=1)

    card_mask = flags["CARD_REVENUE_MISSING"].to_numpy()
    if card_mask.any():
        ax.scatter(days[card_mask], ver[card_mask], marker="v", s=90,
                   color="#111111", edgecolor="white", zorder=4,
                   label="Card revenue missing")

    ax.legend(loc="upper left", ncol=2, fontsize=8.5, frameon=False)
    ax.set_title("Kaffee und Tee GmbH - raw vs AI-verified daily revenue (days flagged by the detector shaded)",
                 fontsize=11, loc="left")
    ax.set_ylabel("EUR per day")
    ax.set_ylim(0, float(max(raw.max(), ver.max())) * 1.25)
    ax.set_xlim(days[0], days[-1])
    ax.xaxis.set_major_locator(mdates.WeekdayLocator(byweekday=mdates.MO, interval=2))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d %b"))
    ax.grid(axis="y", alpha=0.25)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    fig.tight_layout()
    PLOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(PLOT_PATH, dpi=200)
    plt.close(fig)


# ---------------------------------------------------------------------- main
def main():
    tx = detect_transactions(load_transactions())
    daily, flags, series = build_daily(tx)

    flagged = tx[tx["flagged"]].sort_values("timestamp")
    pd.DataFrame({
        "txn_id": flagged["txn_id"],
        "timestamp": flagged["timestamp"].dt.strftime("%Y-%m-%d %H:%M:%S"),
        "channel": flagged["channel"],
        "customer_type": flagged["customer_type"],
        "customer_id": flagged["customer_id"],
        "amount_eur": flagged["amount_eur"],
        "list_price": flagged["list_price"],
        "discount_pct": flagged["discount_pct"].round(3),
        "status": flagged["status"],
        "price_overridden": flagged["price_overridden"],
        "anomaly_score": flagged["anomaly_score"].round(4),
        "reasons": flagged["reasons"],
        "tier": flagged["tier"],
    }).to_csv(FLAGGED_PATH, index=False)
    daily.to_csv(DAILY_PATH, index=False)
    plot_detection(series, flags)

    total = {k: int(series[k].sum()) for k in
             ("raw_gross", "excluded", "at_risk", "verified_gross", "est_fees", "verified_net")}
    print(f"transactions            : {len(tx):,}")
    print(f"flagged transactions    : {len(flagged):,}  -> {FLAGGED_PATH}")
    print("  by reason             : " + ", ".join(
        f"{r}={int(tx['reasons'].str.contains(r).sum())}"
        for r in ("PRICE_OVERRIDE_DEEP", "SELF_DEALING_SUSPECT", "IF_OUTLIER")))
    print(f"raw_gross               : EUR {money(total['raw_gross'])}")
    print(f"excluded_amount         : EUR {money(total['excluded'])}   (SELF_DEALING_SUSPECT)")
    print(f"verified_gross          : EUR {money(total['verified_gross'])}")
    print(f"  of which at_risk      : EUR {money(total['at_risk'])}   (under review, still counted)")
    print(f"est_fees                : EUR {money(total['est_fees'])}   (config.FEE_MODEL)")
    print(f"verified_net            : EUR {money(total['verified_net'])}  -> {DAILY_PATH}")
    print(f"days with a day flag    : {int(flags.any(axis=1).sum())} of {len(flags)}")
    print(f"chart                   : {PLOT_PATH}")

    evaluate(tx, flags)


if __name__ == "__main__":
    main()
