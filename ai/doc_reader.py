# Merchant document reader (AI reads documents, with proof).
#
# Reads a merchant document PDF (Z-Bericht, Tagesabschluss, Umsatzsteuer-Voranmeldung, ...) with
# pdfplumber and asks Claude (tool use) for a fixed set of fields. Every field comes with a
# source_quote copied from the document; a field that is not in the document is null with a reason.
#
# It reads NOTHING but the PDF it is given (only *.pdf files are accepted). The evaluation key in
# data/merchant_docs/ is used by the tests alone, never by this module.
#
# Trust model, as in ai/contract_parser.py (they share the quote check):
#   * the quote of every non-null field must literally appear in the PDF text (whitespace removed);
#   * amounts are returned AS PRINTED (German format, e.g. 1.234,56) and converted by code, never by
#     the model: parse_german_number("1.234,56") -> Decimal("1234.56"). The printed number must also
#     appear inside the quote, so a value cannot come from nowhere.
#
# Usage (from the project root):
#   python -m ai.doc_reader data/merchant_docs/z_bericht_B_2026-10-22.pdf
#   python -m ai.doc_reader --all        (every PDF in data/merchant_docs/ -> extracted.json)
import argparse
import json
import re
import sys
import time
from datetime import date
from decimal import Decimal
from pathlib import Path

import config
from ai.contract_parser import build_meta, call_claude_tool, check_quote, normalize, read_pdf_text

ROOT = Path(__file__).resolve().parent.parent
DOCS_DIR = ROOT / "data" / "merchant_docs"
EXTRACTED_PATH = DOCS_DIR / "extracted.json"
TOOL_NAME = "record_document_fields"
DOC_TYPES = ("z_report", "vat_return", "other")

# (name, kind, description). kind: doc_type | date | amount | integer | currency
FIELDS = [
    ("doc_type", "doc_type",
     "'z_report' for a daily closing report of a cash register (Z-Bericht, Tagesabschluss, Kassenabschluss), "
     "'vat_return' for a Umsatzsteuer-Voranmeldung, 'other' for anything else. The source_quote is the "
     "title or heading of the document."),
    ("period_start", "date", "First day the document covers, ISO format YYYY-MM-DD. A daily report covers "
                             "one day; a monthly VAT return starts on the first day of the month."),
    ("period_end", "date", "Last day the document covers, ISO format YYYY-MM-DD. A daily report: the same day; "
                           "a monthly VAT return: the last day of the month."),
    ("gross_total", "amount", "Total sales including VAT (Umsatz brutto, Gesamtumsatz inkl. USt), as printed."),
    ("cash", "amount", "Cash payments (Bar, Barzahlungen), as printed."),
    ("card", "amount", "Card payments (Karte, Kartenzahlungen), as printed."),
    ("paypal", "amount", "PayPal / online payments (PayPal, Online (PayPal)), as printed."),
    ("voids_count", "integer", "Number of voided transactions (Storno, Stornierte Vorgaenge)."),
    ("voids_amount", "amount", "Total value of the voided transactions (Stornosumme, Betrag of the Storno row), as printed."),
    ("vat_19_net", "amount", "NET amount (without VAT) of sales taxed at 19 %: the Netto of the 19 % row, or "
                             "Kennzahl 81 (Bemessungsgrundlage). If only the VAT amount is shown, return null."),
    ("vat_7_net", "amount", "NET amount (without VAT) of sales taxed at 7 %: the Netto of the 7 % row, or "
                            "Kennzahl 86 (Bemessungsgrundlage). If only the VAT amount is shown, return null."),
    ("currency", "currency", "ISO currency code of the amounts: 'EUR' if they are in EUR or the euro sign."),
]
FIELD_NAMES = [name for name, _, _ in FIELDS]
KINDS = {name: kind for name, kind, _ in FIELDS}

SYSTEM_PROMPT = """You extract fields from a German merchant document (cash-register report or VAT return).

Rules:
1. Use only the document text you are given. Never calculate, estimate or infer a number the document does not print. If a field is not printed (for example only the VAT amount is shown, not the net amount), return null.
2. For every field return value, source_quote and not_found_reason.
3. Amounts: return the number EXACTLY as printed in the document, in German format (for example 1.234,56 or 997,13), without currency symbol. Do NOT convert it; the code converts it. voids_count is a plain integer.
4. source_quote is the line (or sentence) copied character for character from the document text; it must contain the printed number. Whitespace differences do not matter. Never paraphrase, never add ellipses.
5. If the document does not contain a field, set value to null, source_quote to null and explain in not_found_reason.
6. When a field is found, not_found_reason must be null.
7. Answer only by calling the tool record_document_fields exactly once. Do not write any other text."""


# ------------------------------------------------------- German numbers
_GERMAN_NUMBER = re.compile(r"[+-]?(\d{1,3}(\.\d{3})+|\d+)(,\d+)?")


def parse_german_number(text):
    """'1.234,56' -> Decimal('1234.56'). Dots are thousands separators, the comma is the decimal
    mark. A currency sign (EUR / euro) and spaces are ignored. English formats such as '1,234.56' or
    '1234.56' are rejected with ValueError instead of being guessed."""
    cleaned = str(text).replace("−", "-").replace(" ", " ")
    cleaned = re.sub(r"(?i)\s*(EUR|€)\s*", "", cleaned).replace(" ", "")
    if not _GERMAN_NUMBER.fullmatch(cleaned):
        raise ValueError(f"not a German-format number: {text!r}")
    return Decimal(cleaned.replace(".", "").replace(",", "."))


# --------------------------------------------------------------- schema
def _value_schema(kind):
    return {"type": ["integer", "null"]} if kind == "integer" else {"type": ["string", "null"]}


def build_tool():
    properties = {}
    for name, kind, description in FIELDS:
        properties[name] = {
            "type": "object",
            "description": description,
            "properties": {
                "value": _value_schema(kind),
                "source_quote": {"type": ["string", "null"],
                                 "description": "The line copied verbatim from the document, or null."},
                "not_found_reason": {"type": ["string", "null"],
                                     "description": "Why the field is null; null when the field was found."},
            },
            "required": ["value", "source_quote", "not_found_reason"],
        }
    return {"name": TOOL_NAME,
            "description": "Record the fields extracted from the merchant document, each with a verbatim source quote.",
            "input_schema": {"type": "object", "properties": properties, "required": FIELD_NAMES}}


# ----------------------------------------------------------- validation
def split_fields(raw):
    """raw tool input -> (raw values, evidence, missing field names)."""
    values, evidence, missing = {}, {}, []
    for name in FIELD_NAMES:
        item = raw.get(name)
        if not isinstance(item, dict):
            missing.append(name)
            item = {}
        values[name] = item.get("value")
        evidence[name] = {"source_quote": item.get("source_quote"), "not_found_reason": item.get("not_found_reason")}
    return values, evidence, missing


def type_problem(kind, value):
    """None if the raw value fits the field kind, else a short description of the problem."""
    if kind == "doc_type":
        return None if value in DOC_TYPES else f"doc_type must be one of {DOC_TYPES}, got {value!r}"
    if kind == "date":
        try:
            date.fromisoformat(value)
        except (TypeError, ValueError):
            return f"{value!r} is not an ISO date (YYYY-MM-DD)"
        return None
    if kind == "amount":
        try:
            amount = parse_german_number(value)
        except ValueError as exc:
            return str(exc)
        return None if amount >= 0 else f"negative amount {value!r}"
    if kind == "integer":
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return f"{value!r} is not a non-negative integer"
        return None
    if kind == "currency":
        return None if value == "EUR" else f"only EUR is supported, got {value!r}"
    return f"unknown field kind {kind}"


def check_field(name, raw_values, evidence, normalized_pdf):
    """(ok, detail) for one field: null needs a reason; otherwise the type, the quote and the number
    inside the quote are checked."""
    value, ev = raw_values.get(name), evidence.get(name) or {}
    if value is None:
        reason = (ev.get("not_found_reason") or "").strip()
        return (True, f"null: {reason}") if reason else (False, "null without a not_found_reason")
    problem = type_problem(KINDS[name], value)
    if problem:
        return False, problem
    ok, detail = check_quote(name, raw_values, evidence, normalized_pdf)        # same check as the contract parser
    if not ok:
        return False, detail
    if KINDS[name] in ("amount", "integer") and normalize(str(value)) not in normalize(ev["source_quote"]):
        return False, f"the number {value!r} does not appear in its source_quote"
    return True, "ok"


def validate_extraction(raw_values, evidence, pdf_text, missing=()):
    """[(check, ok, detail)]. All checks must pass; an info:* line never fails."""
    normalized_pdf = normalize(pdf_text)
    results = [("structure", not missing, "all fields present" if not missing
                else f"missing or malformed fields: {', '.join(missing)}")]
    for name in FIELD_NAMES:
        results.append((f"field[{name}]", *check_field(name, raw_values, evidence, normalized_pdf)))

    start, end = raw_values.get("period_start"), raw_values.get("period_end")
    if type_problem("date", start) is None and type_problem("date", end) is None:
        ok = date.fromisoformat(start) <= date.fromisoformat(end)
        results.append(("period_order", ok, "period_start <= period_end" if ok else f"{start} is after {end}"))

    parts = [raw_values.get(n) for n in ("gross_total", "cash", "card", "paypal")]
    if all(type_problem("amount", p) is None for p in parts):
        gross, cash, card, paypal = (parse_german_number(p) for p in parts)
        adds_up = cash + card + paypal == gross
        results.append(("info:internal_sum", True,
                        "cash + card + paypal = gross_total" if adds_up else
                        f"the document does not add up: {cash} + {card} + {paypal} = {cash + card + paypal}, "
                        f"gross_total is {gross}"))
    return results


def convert_values(raw_values):
    """Raw printed values -> normalised values: amounts as '1234.56' strings (exact), counts as int."""
    values = {}
    for name, kind in KINDS.items():
        raw = raw_values.get(name)
        if raw is None or type_problem(kind, raw) is not None:
            values[name] = None
        elif kind == "amount":
            values[name] = f"{parse_german_number(raw):.2f}"
        else:
            values[name] = raw
    return values


def build_result(file_name, pdf_text, raw, usage, elapsed_seconds):
    """Validate one extraction and package it. Pure: no I/O, no API."""
    raw_values, evidence, missing = split_fields(raw)
    checks = validate_extraction(raw_values, evidence, pdf_text, missing)
    failed = [{"check": n, "detail": d} for n, ok, d in checks if not ok]
    return {
        "file": file_name,
        "values": convert_values(raw_values),
        "raw": raw_values,
        "evidence": evidence,
        "validation": {"passed": not failed, "failed": failed,
                       "info": [{"check": n, "detail": d} for n, ok, d in checks if n.startswith("info:")]},
        "meta": build_meta(config.CLAUDE_MODEL, elapsed_seconds, usage),
    }


# -------------------------------------------------------- Claude + files
def process_document(pdf_path):
    if Path(pdf_path).suffix.lower() != ".pdf":
        raise SystemExit(f"doc_reader only reads PDF documents, not {Path(pdf_path).name}.")
    started = time.monotonic()
    text = read_pdf_text(pdf_path)           # the only file that is read
    prompt = f"<document>\n{text}\n</document>\n\nExtract the fields by calling the tool {TOOL_NAME}."
    raw, usage = call_claude_tool(SYSTEM_PROMPT, build_tool(), prompt)
    return build_result(Path(pdf_path).name, text, raw, usage, time.monotonic() - started)


def format_table(result):
    lines = [f"{result['file']}", f"{'field':<14}{'value':<14}source_quote / reason"]
    for name in FIELD_NAMES:
        value, ev = result["values"][name], result["evidence"][name]
        shown = "null" if value is None else str(value)
        quote = ev["source_quote"] if ev["source_quote"] else f"(null: {ev['not_found_reason']})"
        lines.append(f"{name:<14}{shown:<14}{quote}")
    validation, meta = result["validation"], result["meta"]
    lines.append(f"validation: {'PASSED' if validation['passed'] else 'FAILED'}")
    for item in validation["failed"]:
        lines.append(f"  [FAIL] {item['check']}: {item['detail']}")
    for item in validation["info"]:
        lines.append(f"  [info] {item['check']}: {item['detail']}")
    cost = meta["estimated_cost_usd"]
    lines.append(f"run: {meta['elapsed_seconds']} s, {meta['input_tokens']:,} in / {meta['output_tokens']:,} out tokens"
                 + (f", estimated ${cost}" if cost else ""))
    return "\n".join(lines)


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")      # the documents contain the euro sign
    parser = argparse.ArgumentParser(description="Extract fields, with source quotes, from merchant documents.")
    parser.add_argument("pdf", nargs="?", help="one merchant document (PDF)")
    parser.add_argument("--all", action="store_true", help=f"process every PDF in {DOCS_DIR.relative_to(ROOT)}")
    parser.add_argument("--out", default=str(EXTRACTED_PATH), help="where --all saves the results")
    args = parser.parse_args(argv)
    if bool(args.pdf) == bool(args.all):
        parser.error("give either one PDF or --all")

    if args.pdf:
        result = process_document(args.pdf)
        print(format_table(result))
        return 0 if result["validation"]["passed"] else 1

    pdfs = sorted(DOCS_DIR.glob("*.pdf"))
    if not pdfs:
        raise SystemExit(f"No PDFs in {DOCS_DIR}. Run: python -m data.make_merchant_docs")
    documents, failed = {}, []
    for pdf in pdfs:
        result = process_document(pdf)
        documents[pdf.name] = result
        print(format_table(result) + "\n")
        if not result["validation"]["passed"]:
            failed.append(pdf.name)
    totals = {
        "elapsed_seconds": round(sum(d["meta"]["elapsed_seconds"] for d in documents.values()), 1),
        "input_tokens": sum(d["meta"]["input_tokens"] for d in documents.values()),
        "output_tokens": sum(d["meta"]["output_tokens"] for d in documents.values()),
        "estimated_cost_usd": (str(sum(Decimal(d["meta"]["estimated_cost_usd"]) for d in documents.values()))
                               if all(d["meta"]["estimated_cost_usd"] for d in documents.values()) else None),
    }
    out = Path(args.out)
    out.write_text(json.dumps({"model": config.CLAUDE_MODEL, "totals": totals, "documents": documents},
                              indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Saved {out} ({len(documents)} documents, {totals['elapsed_seconds']} s, "
          f"estimated ${totals['estimated_cost_usd']})")
    if failed:
        print(f"Validation FAILED for: {', '.join(failed)}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
