# SQLite-backed local ledger: records each day's split calculation and
# payout result, enforces idempotency (sender_batch_id uniqueness) before
# calling paypal/payouts.py, and backs reconciliation against
# paypal/reporting.py.
