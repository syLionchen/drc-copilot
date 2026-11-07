# SQLite-backed local ledger (data/drc.db): records each trading day's settlement and
# payout result, and enforces idempotency (sender_batch_id is UNIQUE) before
# paypal/payouts.py is called. Only SUCCESS rows count toward the cumulative total.
#
# Money is stored as TEXT decimal strings (SQLite has no exact decimal type) and is
# summed with Decimal in Python, never with floating point.
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "drc.db"

COUNTED_STATUS = "SUCCESS"   # only confirmed payouts count toward the cap
TERMINAL_FOR_DAY = ("SUCCESS", "NO_PAYOUT")  # a day with one of these is closed

COLUMNS = (
    "date", "merchant_id", "verified_revenue", "rate_applied", "drc_due", "payout_amount",
    "merchant_payout_fee", "servicing_fee", "investor_net", "cumulative_due",
    "sender_batch_id", "payout_batch_id", "status", "created_at",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS settlements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    merchant_id TEXT NOT NULL,
    verified_revenue TEXT NOT NULL,
    rate_applied TEXT NOT NULL,
    drc_due TEXT NOT NULL,
    payout_amount TEXT NOT NULL,
    merchant_payout_fee TEXT,
    servicing_fee TEXT NOT NULL,
    investor_net TEXT NOT NULL,
    cumulative_due TEXT NOT NULL,
    sender_batch_id TEXT NOT NULL UNIQUE,
    payout_batch_id TEXT,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS contract_terminations (
    merchant_id TEXT PRIMARY KEY,
    terminated_on TEXT NOT NULL,
    cumulative_due TEXT NOT NULL,
    cap_amount TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


class DuplicateSettlement(Exception):
    """A settlement with this sender_batch_id already exists."""


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path=DB_PATH):
    """Open the ledger (creating the file and tables if needed). ':memory:' works for tests."""
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def make_sender_batch_id(merchant_id, day):
    """drc-{merchant_id}-{YYYYMMDD}: one payout per merchant per trading day (CLAUDE.md §3.7)."""
    return f"drc-{merchant_id}-{day:%Y%m%d}"


def get_by_batch_id(conn, sender_batch_id):
    row = conn.execute("SELECT * FROM settlements WHERE sender_batch_id = ?", (sender_batch_id,)).fetchone()
    return dict(row) if row else None


def insert_settlement(conn, row):
    """Insert one settlement row (dict with all COLUMNS except merchant_payout_fee/payout_batch_id)."""
    values = [None if row.get(c) is None else str(row[c]) for c in COLUMNS]
    try:
        with conn:
            conn.execute(
                f"INSERT INTO settlements ({', '.join(COLUMNS)}) VALUES ({', '.join('?' * len(COLUMNS))})",
                values,
            )
    except sqlite3.IntegrityError as exc:
        raise DuplicateSettlement(row.get("sender_batch_id")) from exc


def update_settlement(conn, sender_batch_id, **fields):
    unknown = set(fields) - set(COLUMNS)
    if unknown:
        raise ValueError(f"unknown settlement columns: {sorted(unknown)}")
    if not fields:
        return
    assignments = ", ".join(f"{name} = ?" for name in fields)
    with conn:
        conn.execute(f"UPDATE settlements SET {assignments} WHERE sender_batch_id = ?",
                     [None if v is None else str(v) for v in fields.values()] + [sender_batch_id])


def cumulative_due(conn, merchant_id):
    """Sum of drc_due over confirmed (SUCCESS) settlements. Payout fees are never included."""
    rows = conn.execute("SELECT drc_due FROM settlements WHERE merchant_id = ? AND status = ?",
                        (merchant_id, COUNTED_STATUS)).fetchall()
    return sum((Decimal(r["drc_due"]) for r in rows), Decimal("0.00"))


def latest_success_date(conn, merchant_id):
    row = conn.execute("SELECT MAX(date) AS d FROM settlements WHERE merchant_id = ? AND status = ?",
                       (merchant_id, COUNTED_STATUS)).fetchone()
    return row["d"] if row and row["d"] else None


def list_settlements(conn, merchant_id):
    rows = conn.execute("SELECT * FROM settlements WHERE merchant_id = ? ORDER BY date, id", (merchant_id,))
    return [dict(r) for r in rows]


def record_termination(conn, merchant_id, terminated_on, cumulative, cap_amount):
    """Contract termination record: written once, when cumulative contributions reach the cap."""
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO contract_terminations "
            "(merchant_id, terminated_on, cumulative_due, cap_amount, created_at) VALUES (?, ?, ?, ?, ?)",
            (merchant_id, str(terminated_on), str(cumulative), str(cap_amount), now_iso()),
        )


def get_termination(conn, merchant_id):
    row = conn.execute("SELECT * FROM contract_terminations WHERE merchant_id = ?", (merchant_id,)).fetchone()
    return dict(row) if row else None
