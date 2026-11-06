# Core daily waterfall split logic (CLAUDE.md §3 — changes need Celina's
# confirmation): verified_revenue -> drc_due (tiered rate) -> payout_amount
# net of PayPal's 2% payout fee -> escrow ledger split (servicing fee /
# settlement cost / investor net) -> cap enforcement -> contract termination.
# All amounts use Decimal, 2dp, EUR.
