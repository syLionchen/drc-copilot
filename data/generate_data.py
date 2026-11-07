# Generates 90 days (2026-08-08 .. 2026-11-05) of synthetic transactions for
# Kaffee und Tee GmbH, with four injected anomaly families, for ai/anomaly.py
# to train/test against.
#
# Outputs (CSVs are git-ignored, the PNG is committed):
#   data/transactions.csv         one row per transaction (Europe/Berlin local time);
#                                 list_price = price before any discount / manual override
#   data/injected_anomalies.csv   ground truth: what was injected, where
#   data/daily_summary.csv        daily revenue (raw, i.e. BEFORE AI cleaning)
#   docs/screenshots/revenue_90d_raw.png         (no annotations)
#   docs/screenshots/revenue_90d_answer_key.png  (anomalies marked)
#
# Run:  python -m data.generate_data     (fixed seed -> fully reproducible)
#
# Money is handled as integer cents internally and written as 2-decimal EUR.
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd

SEED = 2026
START = date(2026, 8, 8)
END = date(2026, 11, 5)  # inclusive -> 90 days

DATA_DIR = Path(__file__).resolve().parent
ROOT = DATA_DIR.parent
PLOT_RAW_PATH = ROOT / "docs" / "screenshots" / "revenue_90d_raw.png"
PLOT_KEY_PATH = ROOT / "docs" / "screenshots" / "revenue_90d_answer_key.png"
PLOT_DPI = 200

# ---------------------------------------------------------------- normal model
# Expected transactions per opening hour (shop open 07:00-21:00).
WEEKDAY_PROFILE = {7: 5, 8: 16, 9: 16, 10: 7, 11: 6, 12: 15, 13: 15,
                   14: 6, 15: 5, 16: 6, 17: 8, 18: 8, 19: 7, 20: 5}
WEEKEND_PROFILE = {7: 3, 8: 6, 9: 9, 10: 11, 11: 11, 12: 11, 13: 10,
                   14: 8, 15: 6, 16: 6, 17: 6, 18: 6, 19: 5, 20: 3}  # ~19% lower
EVENING_START_HOUR = 17

CHANNELS = ["PAYPAL", "POS_CARD", "POS_CASH_REPORTED"]
CHANNEL_P = [0.35, 0.45, 0.20]

PP_IDS = ["CONSUMER-A"] + [f"PP-{i:04d}" for i in range(1, 120)]
PP_P = np.array([0.05] + [0.95 / 119] * 119)
CARD_IDS = [f"CARD-{i:04d}" for i in range(1, 401)]
BUSINESS_ID = "JOHN-PETER-GMBH"
FAKE_IDS = ["PP-9001", "PP-9002", "PP-9003"]

VOID_P = 0.02          # baseline void rate
REFUND_P = 0.015       # baseline refund rate (not for cash)
LEGIT_OVERRIDE_P = 0.01  # baseline: staff/loyalty discounts, NOT anomalies

# ------------------------------------------------------------------- anomalies
SELF_DEALING_DAYS = [date(2026, 9, 22), date(2026, 9, 23)]
VOID_WEEK_START, VOID_WEEK_END = date(2026, 10, 5), date(2026, 10, 11)
VOID_SPIKE_P = {"PAYPAL": 0.05, "POS_CARD": 0.30, "POS_CASH_REPORTED": 0.30}
OVERRIDE_DAYS = [date(2026, 8, 20), date(2026, 9, 3), date(2026, 9, 29),
                 date(2026, 10, 15), date(2026, 10, 20)]
OVERRIDES_PER_DAY = 8
LAST14_START = END - timedelta(days=13)  # 2026-10-23
EVENING_FACTOR = 0.70                     # -30 % evening arrivals
CARD_MISSING_DAYS = [date(2026, 10, 27), date(2026, 10, 30), date(2026, 11, 3)]
CARD_KEEP_P = 0.03                        # ~3 % of card txns still show up

T_SELF = "SELF_DEALING_SMALL_ORDERS"
T_VOID = "VOID_SPIKE"
T_PRICE = "PRICE_OVERRIDE"
T_EVENING = "EVENING_TRAFFIC_DROP"
T_CARD = "CARD_REVENUE_MISSING"


def money(cents):
    """Integer cents -> '12.34' (exact, no floats)."""
    return f"{Decimal(int(cents)) / 100:.2f}"


def make_row(ts, channel, ctype, cid, cents, status="COMPLETED",
             overridden=False, anom_type="", anom_detail="", list_cents=None):
    # list_cents = list (shelf) price before any discount; defaults to the amount charged.
    return {
        "timestamp": ts, "channel": channel, "customer_type": ctype,
        "customer_id": cid, "amount_cents": int(cents), "status": status,
        "list_cents": int(cents if list_cents is None else list_cents),
        "price_overridden": overridden,
        "_anom_type": anom_type, "_anom_detail": anom_detail,
    }


def rand_ts(rng, d, hour):
    return datetime.combine(d, time(hour, int(rng.integers(0, 60)), int(rng.integers(0, 60))))


def normal_rows(d, rng, evening_factor):
    profile = WEEKEND_PROFILE if d.weekday() >= 5 else WEEKDAY_PROFILE
    day_effect = rng.lognormal(0.0, 0.07)
    rows = []
    for hour, base_rate in profile.items():
        rate = base_rate * day_effect
        if hour >= EVENING_START_HOUR:
            rate *= evening_factor
        for _ in range(rng.poisson(rate)):
            channel = str(rng.choice(CHANNELS, p=CHANNEL_P))
            if channel == "PAYPAL":
                cid = str(rng.choice(PP_IDS, p=PP_P))
            elif channel == "POS_CARD":
                cid = str(rng.choice(CARD_IDS))
            else:
                cid = "CASH-ANON"
            cents = int(round(float(np.clip(rng.lognormal(np.log(6.5), 0.38), 3, 15)) * 100))
            overridden = False
            list_cents = cents  # price before the (optional) 10 % discount
            if rng.random() < LEGIT_OVERRIDE_P:
                overridden = True
                cents = max(100, int(round(cents * 0.9)))
            status = "COMPLETED"
            r = rng.random()
            if r < VOID_P:
                status = "VOIDED"
            elif r < VOID_P + REFUND_P and channel != "POS_CASH_REPORTED":
                status = "REFUNDED"
            rows.append(make_row(rand_ts(rng, d, hour), channel, "INDIVIDUAL",
                                 cid, cents, status, overridden, list_cents=list_cents))
    # John and Peter GmbH: real team-lunch / meeting orders, every Tue & Thu
    if d.weekday() in (1, 3):
        ts = rand_ts(rng, d, int(rng.integers(11, 13)))
        rows.append(make_row(ts, "PAYPAL", "BUSINESS", BUSINESS_ID,
                             int(rng.integers(8000, 20001))))
    return rows


def self_dealing_rows(d, rng):
    rows = []
    for _ in range(int(rng.integers(180, 261))):
        hour = int(rng.integers(21, 24)) if rng.random() < 0.6 else int(rng.integers(9, 21))
        rows.append(make_row(
            rand_ts(rng, d, hour), "PAYPAL", "INDIVIDUAL", str(rng.choice(FAKE_IDS)),
            int(rng.integers(50, 101)), anom_type=T_SELF,
            anom_detail="micro order EUR 0.50-1.00 from a 3-account ring"))
    return rows


def build_data():
    rng = np.random.default_rng(SEED)
    all_days = [START + timedelta(days=i) for i in range((END - START).days + 1)]
    assert len(all_days) == 90

    rows, day_anoms = [], []
    for d in all_days:
        evening_factor = EVENING_FACTOR if d >= LAST14_START else 1.0
        day = normal_rows(d, rng, evening_factor)

        if d >= LAST14_START:
            day_anoms.append((T_EVENING, d.isoformat(), "",
                              f"evening (>= {EVENING_START_HOUR}:00) arrival rate x{EVENING_FACTOR:.2f}"))

        if d in CARD_MISSING_DAYS:
            before = sum(1 for r in day if r["channel"] == "POS_CARD")
            day = [r for r in day if r["channel"] != "POS_CARD" or rng.random() < CARD_KEEP_P]
            after = sum(1 for r in day if r["channel"] == "POS_CARD")
            day_anoms.append((T_CARD, d.isoformat(), "",
                              f"card txns {after} of ~{before}; suspected diversion to another account"))

        if VOID_WEEK_START <= d <= VOID_WEEK_END:
            for r in day:
                if (r["status"] == "COMPLETED" and r["customer_type"] == "INDIVIDUAL"
                        and rng.random() < VOID_SPIKE_P[r["channel"]]):
                    r["status"] = "VOIDED"
                    r["_anom_type"] = T_VOID
                    r["_anom_detail"] = "injected void during void-spike week"

        if d in OVERRIDE_DAYS:
            eligible = [i for i, r in enumerate(day)
                        if r["channel"] in ("POS_CARD", "POS_CASH_REPORTED")
                        and r["status"] == "COMPLETED" and r["customer_type"] == "INDIVIDUAL"
                        and not r["price_overridden"]]
            for i in rng.choice(eligible, size=OVERRIDES_PER_DAY, replace=False):
                r = day[int(i)]
                orig = r["amount_cents"]
                r["list_cents"] = orig  # the original price before the manual override
                r["amount_cents"] = max(100, int(round(orig * rng.uniform(0.3, 0.6))))
                r["price_overridden"] = True
                r["_anom_type"] = T_PRICE
                r["_anom_detail"] = f"original={money(orig)};overridden={money(r['amount_cents'])}"

        if d in SELF_DEALING_DAYS:
            day += self_dealing_rows(d, rng)

        rows += day

    df = pd.DataFrame(rows)
    df = df.sort_values("timestamp", kind="mergesort").reset_index(drop=True)
    df["date"] = df["timestamp"].dt.date
    seq = df.groupby("date").cumcount() + 1
    df["txn_id"] = "TXN-" + df["timestamp"].dt.strftime("%Y%m%d") + "-" + seq.astype(str).str.zfill(4)
    return df, day_anoms, all_days


def report_unchanged(path, new_df):
    """Before overwriting a CSV, compare it with the new content (shared columns only)
    and print whether it is identical. Lets us prove that adding a column did not
    change any existing data."""
    if not path.exists():
        return
    old = pd.read_csv(path, dtype=str, keep_default_na=False)
    cols = [c for c in old.columns if c in new_df.columns]
    new = new_df[cols].astype(str)
    same = old[cols].to_numpy().tolist() == new.to_numpy().tolist()
    verdict = "identical to the previous file on shared columns" if same else "CHANGED vs the previous file"
    print(f"check {path.name:<22}: {verdict} ({len(old):,} -> {len(new_df):,} rows)")


def write_outputs(df, day_anoms, all_days):
    # transactions.csv
    out = pd.DataFrame({
        "txn_id": df["txn_id"],
        "timestamp": df["timestamp"].dt.strftime("%Y-%m-%d %H:%M:%S"),
        "channel": df["channel"],
        "customer_type": df["customer_type"],
        "customer_id": df["customer_id"],
        "amount_eur": df["amount_cents"].map(money),
        "list_price": df["list_cents"].map(money),
        "status": df["status"],
        "price_overridden": df["price_overridden"],
    })
    report_unchanged(DATA_DIR / "transactions.csv", out)
    out.to_csv(DATA_DIR / "transactions.csv", index=False)

    # injected_anomalies.csv (ground truth)
    tagged = df[df["_anom_type"] != ""]
    txn_anoms = pd.DataFrame({
        "anomaly_type": tagged["_anom_type"],
        "date": tagged["date"].astype(str),
        "txn_id": tagged["txn_id"],
        "detail": tagged["_anom_detail"],
    })
    day_df = pd.DataFrame(day_anoms, columns=["anomaly_type", "date", "txn_id", "detail"])
    anoms = pd.concat([txn_anoms, day_df]).sort_values(
        ["anomaly_type", "date", "txn_id"]).reset_index(drop=True)
    report_unchanged(DATA_DIR / "injected_anomalies.csv", anoms)
    anoms.to_csv(DATA_DIR / "injected_anomalies.csv", index=False)

    # daily_summary.csv (raw revenue = COMPLETED only, before AI cleaning)
    df = df.assign(completed=df["amount_cents"].where(df["status"] == "COMPLETED", 0))
    by_day = df.groupby("date")
    s = pd.DataFrame({
        "txn_count": by_day.size(),
        "revenue": by_day["completed"].sum(),
        "voided": df[df["status"] == "VOIDED"].groupby("date")["amount_cents"].sum(),
        "refunded": df[df["status"] == "REFUNDED"].groupby("date")["amount_cents"].sum(),
        "void_count": (df["status"] == "VOIDED").groupby(df["date"]).sum(),
    }).reindex(all_days, fill_value=0).fillna(0)
    chan = df.pivot_table(index="date", columns="channel", values="completed",
                          aggfunc="sum", fill_value=0).reindex(index=all_days, columns=CHANNELS, fill_value=0)

    summary = pd.DataFrame({
        "date": [d.isoformat() for d in all_days],
        "weekday": [d.strftime("%a") for d in all_days],
        "txn_count": s["txn_count"].astype(int).values,
        "revenue_eur": [money(c) for c in s["revenue"]],
        "paypal_eur": [money(c) for c in chan["PAYPAL"]],
        "pos_card_eur": [money(c) for c in chan["POS_CARD"]],
        "pos_cash_reported_eur": [money(c) for c in chan["POS_CASH_REPORTED"]],
        "voided_eur": [money(c) for c in s["voided"]],
        "refunded_eur": [money(c) for c in s["refunded"]],
        "void_rate": (s["void_count"] / s["txn_count"]).round(4).values,
    })
    summary.to_csv(DATA_DIR / "daily_summary.csv", index=False)
    return out, anoms, s


def plot_revenue(s, all_days, annotated, path):
    """annotated=False: 'What the investor sees' (line + 7-day MA only).
    annotated=True: same plus every injected anomaly marked (answer key)."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt
    from matplotlib.patches import Patch

    x = pd.to_datetime(all_days)
    y = (s["revenue"].astype(int) / 100).values  # plotting only
    ma7 = pd.Series(y).rolling(7).mean().values  # trailing 7-day average
    half = pd.Timedelta(hours=12)

    fig, ax = plt.subplots(figsize=(12.5, 5.8), dpi=PLOT_DPI)
    ax.plot(x, y, color="#1f3b5c", lw=1.4, alpha=0.85, zorder=3)
    ax.plot(x, ma7, color="#e76f51", lw=2.4, zorder=3)

    handles = [
        plt.Line2D([], [], color="#1f3b5c", lw=1.4),
        plt.Line2D([], [], color="#e76f51", lw=2.4),
    ]
    labels = ["Daily revenue (COMPLETED, raw)", "7-day moving average"]

    if annotated:
        ax.axvspan(pd.Timestamp(SELF_DEALING_DAYS[0]) - half, pd.Timestamp(SELF_DEALING_DAYS[-1]) + half,
                   color="#d62728", alpha=0.25, zorder=1)
        ax.axvspan(pd.Timestamp(VOID_WEEK_START) - half, pd.Timestamp(VOID_WEEK_END) + half,
                   color="#ff9f1c", alpha=0.25, zorder=1)
        ax.axvspan(pd.Timestamp(LAST14_START) - half, pd.Timestamp(END) + half,
                   color="#2a9d8f", alpha=0.18, zorder=1)

        idx = {d: i for i, d in enumerate(all_days)}
        ov = [idx[d] for d in OVERRIDE_DAYS]
        ax.scatter(x[ov], y[ov], marker="o", s=60, color="#7b2cbf", edgecolor="white", zorder=4)
        cm = [idx[d] for d in CARD_MISSING_DAYS]
        ax.scatter(x[cm], y[cm], marker="v", s=90, color="#111111", edgecolor="white", zorder=4)

        handles += [
            Patch(color="#d62728", alpha=0.35),
            Patch(color="#ff9f1c", alpha=0.35),
            plt.Line2D([], [], marker="o", color="none", markerfacecolor="#7b2cbf", markersize=8),
            Patch(color="#2a9d8f", alpha=0.3),
            plt.Line2D([], [], marker="v", color="none", markerfacecolor="#111111", markersize=9),
        ]
        labels += [
            "Self-dealing micro orders (2 days)",
            "Void spike week",
            "Manual price-override days",
            "Last 14 days: evening traffic -30%",
            "Card revenue ~0 (suspected diversion)",
        ]
        title = "Kaffee und Tee GmbH - daily revenue with injected anomalies (answer key)"
    else:
        title = "What the investor sees - Kaffee und Tee GmbH daily revenue, 2026-08-08 to 2026-11-05"

    ax.legend(handles, labels, loc="upper left", ncol=2, fontsize=8.5, frameon=False)
    ax.set_title(title, fontsize=11, loc="left")
    ax.set_ylabel("EUR per day")
    ax.set_ylim(0, float(y.max()) * 1.25)
    ax.set_xlim(pd.Timestamp(START), pd.Timestamp(END))
    ax.xaxis.set_major_locator(mdates.WeekdayLocator(byweekday=mdates.MO, interval=2))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d %b"))
    ax.grid(axis="y", alpha=0.25)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=PLOT_DPI)
    plt.close(fig)


def main():
    df, day_anoms, all_days = build_data()
    out, anoms, s = write_outputs(df, day_anoms, all_days)
    plot_revenue(s, all_days, annotated=False, path=PLOT_RAW_PATH)
    plot_revenue(s, all_days, annotated=True, path=PLOT_KEY_PATH)

    print(f"transactions.csv        : {len(out):,} rows")
    print(f"injected_anomalies.csv  : {len(anoms):,} rows")
    print(anoms.groupby("anomaly_type").size().to_string())
    print(f"daily_summary.csv       : {len(all_days)} days, total raw revenue EUR {money(s['revenue'].sum())}")
    print(f"chart (raw)             : {PLOT_RAW_PATH}")
    print(f"chart (answer key)      : {PLOT_KEY_PATH}")


if __name__ == "__main__":
    main()
