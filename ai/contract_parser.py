# LLM-based DRC contract parser (CLAUDE.md §4 step 3).
#
# Reads data/sample_contract.pdf with pdfplumber, asks Claude (tool use) to extract
# the contract parameters, and for every field requires a source_quote copied from
# the contract. Nothing is trusted: the result is only saved when every validation
# passes, and each non-null quote must literally appear in the PDF text.
#
# Outputs:
#   data/contract_params.json              params + evidence (committed)
#   data/contract_params_validation.txt    one line per validation check
#   data/contract_params_rejected.json     raw result when validation fails (git-ignored)
#
# Run:  python -m ai.contract_parser
#
# Claude Sonnet 5.5 does not allow forced tool_choice, so the model is told to answer
# only through the tool (tool_choice "auto"); if it does not, we retry once and then fail.
# Only LLM_PROVIDER=anthropic is supported.
import json
import re
import sys
import time
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import config

ROOT = Path(__file__).resolve().parent.parent
PDF_PATH = ROOT / "data" / "sample_contract.pdf"
PARAMS_PATH = ROOT / "data" / "contract_params.json"
REJECTED_PATH = ROOT / "data" / "contract_params_rejected.json"
REPORT_PATH = ROOT / "data" / "contract_params_validation.txt"
TOOL_NAME = "record_contract_params"

# (name, kind, description). kind: number | integer | string | boolean | tiers
FIELDS = [
    ("investment_amount", "number", "Amount the investor pays the merchant, in EUR."),
    ("funding_date", "string", "Date the investment is funded, ISO format YYYY-MM-DD."),
    ("day_basis", "string",
     "'trading_day' if contributions are calculated per Trading Day, 'calendar_day' if per calendar day."),
    ("timezone", "string", "IANA time zone used to determine days and cut-offs, e.g. 'Europe/Berlin'."),
    ("revenue_base", "string",
     "'total_receipts_all_channels' if the revenue base covers all payment channels (PayPal, card, "
     "cash, bank transfer); 'paypal_only' if only PayPal receipts count."),
    ("tiers", "tiers",
     "Revenue share tiers in ascending order. Each tier has up_to = upper bound of daily revenue in EUR "
     "(null for the last tier) and rate = share as a fraction (5% -> 0.05)."),
    ("baseline_threshold_amount", "number", "Baseline Threshold in EUR per trading day, as fixed in the contract."),
    ("baseline_threshold_method", "string",
     "How the Baseline Threshold is calculated, in one sentence using the contract's own wording. "
     "The source_quote must be the sentence that defines the Baseline Threshold."),
    ("cap_multiple", "number", "Repayment cap as a multiple of the investment amount (for example 1.15)."),
    ("cap_amount", "number", "Repayment cap in EUR."),
    ("max_term_months", "integer", "Maximum term of the agreement in months."),
    ("settlement_frequency", "string", "How often contributions are settled to the escrow account: 'daily', 'weekly', ..."),
    ("collection_method", "string", "Primary source from which contributions are collected, e.g. 'PayPal balance'."),
    ("fallback_collection", "string", "Fallback if the primary source is insufficient, e.g. 'SEPA direct debit'."),
    ("payout_fee_borne_by", "string", "Who bears PayPal payout fees: 'merchant', 'investor' or 'shared'."),
    ("servicing_fee_rate", "number", "Servicing fee as a fraction of each daily contribution (5% -> 0.05)."),
    ("health_trigger_drop_pct", "number",
     "Percent by which the short-window average must fall below the long-window average to trigger "
     "the health covenant (20% -> 20)."),
    ("health_window_short_days", "integer", "Length of the short averaging window, in trading days."),
    ("health_window_long_days", "integer", "Length of the long averaging window, in trading days."),
    ("reduced_rate", "number", "Reduced revenue share rate under the health covenant, as a fraction (3% -> 0.03)."),
    ("reduced_rate_max_days", "integer", "Maximum number of days the reduced rate may apply."),
    ("requires_investor_approval", "boolean", "True if the rate reduction requires the investor's approval."),
    ("card_gap_breach_days", "integer",
     "Number of trading days of unexplained card-revenue gap that count as a reporting breach."),
    ("channel_share_drop_trigger_pp", "number",
     "Fall of the PayPal share of total receipts, in percentage points, that lets the Servicer request "
     "a written explanation and a review."),
    ("monthly_report_days", "integer", "Days after month-end by which the monthly receipts report is due."),
    ("vat_copy_days", "integer", "Days after filing by which a copy of each VAT return is due."),
    ("annual_statement_months", "integer", "Months after financial year-end by which annual statements are due."),
]
FIELD_NAMES = [name for name, _, _ in FIELDS]
KINDS = {name: kind for name, kind, _ in FIELDS}

SYSTEM_PROMPT = """You extract structured parameters from a revenue-sharing contract.

Rules:
1. Use only the contract text you are given. Never invent, assume or calculate a value that the contract does not state or directly support. Normalised values (day_basis, revenue_base, settlement_frequency, payout_fee_borne_by, ISO dates) are allowed only when the contract text clearly supports them.
2. For every field return value, source_quote and not_found_reason.
3. source_quote is ONE sentence copied character for character from the contract text: contiguous, no paraphrasing, no ellipses, no changed numbers or quotation marks (line breaks and extra spaces may differ). Choose the sentence that states the value. For baseline_threshold_method it must be the sentence that defines "Baseline Threshold".
4. If the contract does not contain a field, set value to null, source_quote to null and explain in not_found_reason. Do not guess.
5. When a field is found, not_found_reason must be null.
6. Follow the unit conventions in each field description (rates as fractions, percentages as stated).
7. Answer only by calling the tool record_contract_params exactly once. Do not write any other text."""


# ------------------------------------------------------------------ tool schema
def _value_schema(kind):
    if kind == "tiers":
        return {
            "type": ["array", "null"],
            "items": {
                "type": "object",
                "properties": {"up_to": {"type": ["number", "null"]}, "rate": {"type": "number"}},
                "required": ["up_to", "rate"],
            },
        }
    return {"type": [kind, "null"]}


def build_tool():
    properties = {}
    for name, kind, description in FIELDS:
        properties[name] = {
            "type": "object",
            "description": description,
            "properties": {
                "value": _value_schema(kind),
                "source_quote": {"type": ["string", "null"],
                                 "description": "One sentence copied verbatim from the contract, or null."},
                "not_found_reason": {"type": ["string", "null"],
                                     "description": "Why the field is null; null when the field was found."},
            },
            "required": ["value", "source_quote", "not_found_reason"],
        }
    return {
        "name": TOOL_NAME,
        "description": "Record the extracted contract parameters, each with a verbatim source quote.",
        "input_schema": {"type": "object", "properties": properties, "required": FIELD_NAMES},
    }


# ------------------------------------------------------------------ PDF + Claude
def read_pdf_text(path=PDF_PATH):
    import pdfplumber

    with pdfplumber.open(path) as pdf:
        return "\n".join((page.extract_text() or "") for page in pdf.pages)


def extract_with_claude(contract_text):
    """Call Claude once (plus one retry if it skips the tool). Returns (tool input dict, usage dict);
    usage counts API calls, tokens and API seconds over all attempts."""
    prompt = (f"<contract>\n{contract_text}\n</contract>\n\n"
              f"Extract the parameters by calling the tool {TOOL_NAME}.")
    return call_claude_tool(SYSTEM_PROMPT, build_tool(), prompt)


def call_claude_tool(system_prompt, tool, prompt):
    """Shared by the contract parser and ai/doc_reader.py. Claude Sonnet 5.5 does not allow forced tool
    use, so the model is told to answer only through `tool`; one retry, then an error. Returns
    (tool input dict, usage dict)."""
    tool_name = tool["name"]
    provider = (config.LLM_PROVIDER or "").strip().lower()
    if provider != "anthropic":
        raise SystemExit(
            f"LLM_PROVIDER is '{config.LLM_PROVIDER}', but this step is only implemented for "
            "'anthropic'. Set LLM_PROVIDER=anthropic in .env (and ANTHROPIC_API_KEY), then run again."
        )
    if not config.ANTHROPIC_API_KEY:
        raise SystemExit("ANTHROPIC_API_KEY is not set in .env.")

    import anthropic

    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    reminder = f"\n\nYou must answer by calling the tool {tool_name}; do not reply with text."

    usage = {"api_calls": 0, "input_tokens": 0, "output_tokens": 0, "api_seconds": 0.0}
    for attempt in (1, 2):
        started = time.monotonic()
        try:
            response = client.messages.create(
                model=config.CLAUDE_MODEL,
                max_tokens=16000,
                system=system_prompt,
                tools=[tool],
                messages=[{"role": "user", "content": prompt + (reminder if attempt == 2 else "")}],
            )
        except anthropic.AuthenticationError:
            raise SystemExit("Anthropic rejected the API key (authentication error). Check ANTHROPIC_API_KEY in .env.")
        except anthropic.RateLimitError:
            raise SystemExit("Anthropic rate limit reached. Wait a moment and run again.")
        except anthropic.APIConnectionError as exc:
            raise SystemExit(f"Could not reach the Anthropic API: {exc}")
        except anthropic.APIStatusError as exc:
            raise SystemExit(f"Anthropic API error {exc.status_code}: {exc.message}")

        usage["api_calls"] += 1
        usage["api_seconds"] += time.monotonic() - started
        usage["input_tokens"] += response.usage.input_tokens
        usage["output_tokens"] += response.usage.output_tokens
        if response.stop_reason == "refusal":
            raise SystemExit("The model refused the request (stop_reason=refusal).")
        if response.stop_reason == "max_tokens":
            raise SystemExit("The answer was cut off (max_tokens). Increase max_tokens in contract_parser.py.")
        for block in response.content:
            if block.type == "tool_use" and block.name == tool_name:
                return block.input, usage
        print(f"Attempt {attempt}: the model did not call the tool; "
              f"{'retrying once' if attempt == 1 else 'giving up'}.")
    raise SystemExit(f"The model did not call the tool {tool_name} after 2 attempts.")


def split_result(raw):
    """raw tool input -> (params, evidence, missing_field_names)."""
    params, evidence, missing = {}, {}, []
    for name in FIELD_NAMES:
        item = raw.get(name)
        if not isinstance(item, dict):
            missing.append(name)
            item = {}
        params[name] = item.get("value")
        evidence[name] = {"source_quote": item.get("source_quote"),
                          "not_found_reason": item.get("not_found_reason")}
    return params, evidence, missing


# ------------------------------------------------------------------- validation
_PUNCT = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"',
                        "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-"})


def normalize(text):
    """Remove all whitespace (and unify typographic quotes/dashes) for substring matching."""
    return re.sub(r"\s+", "", text.translate(_PUNCT))


def D(x):
    return Decimal(str(x))


def _is_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _is_integer(v):
    return _is_number(v) and float(v).is_integer()


def check_structure(missing):
    if missing:
        return False, f"missing or malformed fields: {', '.join(missing)}"
    return True, f"all {len(FIELD_NAMES)} fields present"


def check_types(params):
    bad = []
    for name in FIELD_NAMES:
        v, kind = params.get(name), KINDS[name]
        if v is None:
            continue
        if kind == "number":
            ok = _is_number(v)
        elif kind == "integer":
            ok = _is_integer(v)
        elif kind == "string":
            ok = isinstance(v, str) and v.strip() != ""
        elif kind == "boolean":
            ok = isinstance(v, bool)
        else:  # tiers
            ok = (isinstance(v, list) and len(v) > 0 and all(
                isinstance(t, dict) and _is_number(t.get("rate"))
                and (t.get("up_to") is None or _is_number(t.get("up_to"))) for t in v))
        if not ok:
            bad.append(f"{name} ({kind}) = {v!r}")
    return (not bad), ("; ".join(bad) if bad else "all non-null values have the expected type")


def check_null_reasons(params, evidence):
    nulls = [n for n in FIELD_NAMES if params.get(n) is None]
    bad = [n for n in nulls if not (evidence.get(n, {}).get("not_found_reason") or "").strip()]
    if bad:
        return False, f"null fields without a reason: {', '.join(bad)}"
    return True, f"{len(nulls)} null field(s), each with a reason" if nulls else "no null fields"


def check_quote(name, params, evidence, normalized_pdf):
    value = params.get(name)
    if value is None:
        return True, "value is null (skipped)"
    quote = (evidence.get(name, {}) or {}).get("source_quote")
    if not isinstance(quote, str) or not quote.strip():
        return False, "non-null value without a source_quote"
    if normalize(quote) not in normalized_pdf:
        return False, f"quote not found in the PDF text: {quote[:90]!r}"
    return True, "quote found in the PDF text"


def check_rates(params):
    rates = []
    for i, tier in enumerate(params.get("tiers") or []):
        rates.append((f"tiers[{i}].rate", tier.get("rate") if isinstance(tier, dict) else None))
    for name in ("servicing_fee_rate", "reduced_rate"):
        if params.get(name) is not None:
            rates.append((name, params[name]))
    if not params.get("tiers"):
        return False, "tiers is missing, so no rate could be checked"
    bad = [f"{n}={v!r}" for n, v in rates if not _is_number(v) or not (0 <= D(v) <= 1)]
    return (not bad), ("; ".join(bad) if bad else "all rates are between 0 and 1")


def check_cap(params):
    inv, mult, cap = (params.get(k) for k in ("investment_amount", "cap_multiple", "cap_amount"))
    if not all(_is_number(v) for v in (inv, mult, cap)):
        return False, "investment_amount, cap_multiple or cap_amount is missing or not a number"
    expected = D(inv) * D(mult)
    if expected != D(cap):
        return False, f"cap_amount {cap} != investment_amount {inv} x cap_multiple {mult} = {expected}"
    return True, f"cap_amount {cap} = {inv} x {mult}"


def check_tiers_ascending(params):
    tiers = params.get("tiers")
    if not isinstance(tiers, list) or not tiers:
        return False, "tiers is missing or empty"
    previous = None
    for i, tier in enumerate(tiers):
        up_to = tier.get("up_to") if isinstance(tier, dict) else None
        last = i == len(tiers) - 1
        if last:
            if up_to is not None:
                return False, f"the last tier must have up_to = null, got {up_to}"
        else:
            if not _is_number(up_to) or D(up_to) <= 0:
                return False, f"tiers[{i}].up_to must be a positive number, got {up_to!r}"
            if previous is not None and D(up_to) <= previous:
                return False, f"tiers[{i}].up_to {up_to} is not greater than the previous {previous}"
            previous = D(up_to)
    return True, f"{len(tiers)} tier(s), up_to strictly increasing, last tier open-ended"


def check_servicing(params):
    rate = params.get("servicing_fee_rate")
    if not _is_number(rate):
        return False, "servicing_fee_rate is missing or not a number"
    if not (D(rate) < D("0.2")):
        return False, f"servicing_fee_rate {rate} is not below 0.2"
    return True, f"servicing_fee_rate {rate} < 0.2"


def check_first_tier_baseline(params):
    tiers = params.get("tiers")
    baseline = params.get("baseline_threshold_amount")
    if not isinstance(tiers, list) or not tiers or not isinstance(tiers[0], dict):
        return False, "tiers is missing"
    up_to = tiers[0].get("up_to")
    if not _is_number(up_to) or not _is_number(baseline):
        return False, f"tiers[0].up_to={up_to!r} or baseline_threshold_amount={baseline!r} is missing"
    if D(up_to) != D(baseline):
        return False, f"tiers[0].up_to {up_to} != baseline_threshold_amount {baseline}"
    return True, f"tiers[0].up_to {up_to} = baseline_threshold_amount {baseline}"


def check_funding_date(params):
    value = params.get("funding_date")
    try:
        date.fromisoformat(value)
    except (TypeError, ValueError):
        return False, f"funding_date {value!r} is not an ISO date (YYYY-MM-DD)"
    return True, f"funding_date {value} is a valid ISO date"


def validate(params, evidence, pdf_text, missing=()):
    """Run every check. Returns [(name, ok, detail), ...]."""
    normalized_pdf = normalize(pdf_text)
    results = [
        ("structure", *check_structure(list(missing))),
        ("types", *check_types(params)),
        ("null_fields_have_reason", *check_null_reasons(params, evidence)),
        ("rates_between_0_and_1", *check_rates(params)),
        ("cap_amount_equals_investment_x_multiple", *check_cap(params)),
        ("tiers_ascending", *check_tiers_ascending(params)),
        ("servicing_fee_below_0.2", *check_servicing(params)),
        ("first_tier_up_to_equals_baseline", *check_first_tier_baseline(params)),
        ("funding_date_is_iso", *check_funding_date(params)),
    ]
    for name in FIELD_NAMES:
        results.append((f"source_quote[{name}]", *check_quote(name, params, evidence, normalized_pdf)))
    return results


def estimate_cost_usd(model, input_tokens, output_tokens):
    """Estimated cost of the tokens at list price (config.CLAUDE_PRICING_USD_PER_MTOK), in USD,
    rounded to 0.0001. None if the model has no price on file. An estimate, not a billed amount."""
    price = config.CLAUDE_PRICING_USD_PER_MTOK.get(model)
    if price is None:
        return None
    cost = (Decimal(input_tokens) * price["input"] + Decimal(output_tokens) * price["output"]) / Decimal(1_000_000)
    return cost.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def build_meta(model, elapsed_seconds, usage):
    """Run statistics stored in contract_params.json under 'meta' (shown on the web app)."""
    cost = estimate_cost_usd(model, usage["input_tokens"], usage["output_tokens"])
    price = config.CLAUDE_PRICING_USD_PER_MTOK.get(model)
    return {
        "elapsed_seconds": round(elapsed_seconds, 1),          # PDF read + Claude call(s) + validation
        "api_seconds": round(usage["api_seconds"], 1),
        "api_calls": usage["api_calls"],
        "input_tokens": usage["input_tokens"],
        "output_tokens": usage["output_tokens"],
        "estimated_cost_usd": str(cost) if cost is not None else None,
        "pricing_usd_per_mtok": {k: str(v) for k, v in price.items()} if price else None,
        "note": "cost is an estimate from list prices, not a billed amount",
    }


def write_report(results, model):
    failed = [r for r in results if not r[1]]
    lines = [
        "Contract parameter validation",
        f"PDF   : {PDF_PATH.relative_to(ROOT).as_posix()}",
        f"Model : {model}",
        f"Result: {'PASSED' if not failed else 'FAILED'} "
        f"({len(results) - len(failed)} of {len(results)} checks passed)",
        "",
    ]
    lines += [f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}" for name, ok, detail in results]
    REPORT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return failed


# ------------------------------------------------------------------------- main
def main():
    started = time.monotonic()
    if not PDF_PATH.exists():
        raise SystemExit(f"{PDF_PATH} not found. Run: python -m data.make_contract")
    pdf_text = read_pdf_text(PDF_PATH)
    print(f"Read {PDF_PATH.name}: {len(pdf_text):,} characters. Asking {config.CLAUDE_MODEL} ...")

    raw, usage = extract_with_claude(pdf_text)
    params, evidence, missing = split_result(raw)
    results = validate(params, evidence, pdf_text, missing)
    failed = write_report(results, config.CLAUDE_MODEL)
    meta = build_meta(config.CLAUDE_MODEL, time.monotonic() - started, usage)

    payload = {
        "source_pdf": PDF_PATH.relative_to(ROOT).as_posix(),
        "model": config.CLAUDE_MODEL,
        "params": params,
        "evidence": evidence,
        "meta": meta,
    }
    if failed:
        REJECTED_PATH.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"\nValidation FAILED ({len(failed)} of {len(results)} checks):")
        for name, _, detail in failed:
            print(f"  [FAIL] {name}: {detail}")
        print(f"\nReport : {REPORT_PATH}\nRaw result (not trusted): {REJECTED_PATH}")
        sys.exit(1)

    PARAMS_PATH.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Validation PASSED ({len(results)} checks).")
    print(f"Saved  : {PARAMS_PATH}\nReport : {REPORT_PATH}")
    cost = meta["estimated_cost_usd"]
    print(f"Run    : {meta['elapsed_seconds']} s, {meta['api_calls']} API call(s), "
          f"{meta['input_tokens']:,} in / {meta['output_tokens']:,} out tokens"
          + (f", estimated cost ${cost}" if cost else ""))
    print(f"tiers  : {params['tiers']}")
    print(f"cap    : {params['cap_multiple']}x = EUR {params['cap_amount']}, "
          f"term {params['max_term_months']} months, funding {params['funding_date']}")


if __name__ == "__main__":
    main()
