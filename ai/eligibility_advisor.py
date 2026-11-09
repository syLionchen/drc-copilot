# Eligibility advisor: Claude explains, in plain English for a small-business owner, what to do next to meet the
# conditions of engine/eligibility.py (the checklist itself is deterministic code; Claude never decides eligibility).
#
# Input: the nine check results of one merchant profile (status, numbers used, requirement, how to fix).
# Output (tool use with a JSON schema): a short summary and steps. Each step names ONE condition (check_id),
# what to prepare (materials) and roughly how many days it takes (time_estimate_days).
#
# Code checks the answer before it is shown:
#   * every step must refer to an existing condition that is not met yet (an unknown check_id makes the whole
#     answer INVALID);
#   * every number and date quoted in the text must appear in the input (same check as engine/health_agent.py,
#     +-0.01); a number that is not in the input makes the answer INVALID;
#   * no internal field names (words with an underscore) in the text;
#   * a time estimate must not be shorter than the earliest possible time the input gives (account age etc.).
#
# One answer per profile is stored in data/eligibility/<profile id>.json and exported to data/demo/ by
# scripts/export_demo_snapshot.py. The web page only displays these files; it never calls Claude.
# This module only reads the profiles and writes into data/eligibility/.
#
# Usage (from the project root):
#   python -m ai.eligibility_advisor                    (both profiles, two Claude calls)
#   python -m ai.eligibility_advisor --profile sample-bakery
#   python -m ai.eligibility_advisor --no-ai            (checklists only, no API call, advice stays empty)
import argparse
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
from engine import eligibility as elig  # noqa: E402
from engine import health_agent as ha  # noqa: E402
from engine import split_engine as se  # noqa: E402

OUT_DIR = ROOT / "data" / "eligibility"
TOOL_NAME = "submit_eligibility_advice"
STATUS_WORDS = {"met": "met", "not_met": "not met", "action_needed": "action needed", "needs_consent": "needs consent"}

SYSTEM_PROMPT = f"""You help the owner of a small shop understand what they still need to do to qualify for
revenue-share financing. The checklist was computed by code from their data; you only explain the next steps.

Rules:
- Answer ONLY by calling the tool {TOOL_NAME}.
- Write one step for every condition that is not met (status not met, action needed or needs consent), and
  no step for conditions that are already met. In each step, put the id of that condition, copied exactly, into
  check_id. Never invent an id.
- what_to_do: plain English, addressed to the owner ("you"). materials: what to prepare or upload, as a list.
  time_estimate_days: a whole number of days; it must not be below "Earliest this can be met" when the input
  gives one.
- Use ONLY numbers and dates from the input, copied exactly. Never calculate new numbers, round or convert.
  Code checks every number against the input; one unknown number makes the whole answer invalid.
- Do not write field names or any word with an underscore in the text (only in check_id); a document may be
  named by its file name exactly as given. Do not promise
  approval: the investor makes the final decision.
- summary: two sentences at most: where the merchant stands and the single most important next step."""


def build_tool():
    return {
        "name": TOOL_NAME,
        "description": "Submit the next steps for the merchant, one step per condition that is not met.",
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "steps": {"type": "array", "items": {
                    "type": "object",
                    "properties": {
                        "check_id": {"type": "string", "description": "id of the condition, copied exactly from the input"},
                        "what_to_do": {"type": "string"},
                        "materials": {"type": "array", "items": {"type": "string"}},
                        "time_estimate_days": {"type": "integer", "minimum": 0}},
                    "required": ["check_id", "what_to_do", "materials", "time_estimate_days"]}},
            },
            "required": ["summary", "steps"],
        },
    }


def build_inputs(profile, checks):
    met, total = elig.progress(checks)
    keep = ("id", "number", "name", "status", "requirement", "numbers", "detail", "how_to_fix", "earliest_possible_days")
    return {
        "merchant": {"name": profile["name"], "sample": bool(profile.get("sample")), "as_of": profile["as_of"]},
        "conditions_met": met, "conditions_total": total,
        "checks": [{k: c[k] for k in keep if k in c} for c in checks],
    }


def readable_inputs(inputs):
    m = inputs["merchant"]
    lines = [f"Merchant: {m['name']}" + (" (SAMPLE merchant)" if m["sample"] else ""),
             f"Checked on: {m['as_of']}", f"Conditions met: {inputs['conditions_met']} of {inputs['conditions_total']}"]
    for c in inputs["checks"]:
        lines += ["", f"Condition {c['number']}: {c['name']} (id: {c['id']})",
                  f"  Status: {STATUS_WORDS.get(c['status'], c['status'])}", f"  Requirement: {c['requirement']}"]
        lines += [f"  {label}: {value}" for label, value in c["numbers"].items()]
        lines.append(f"  Detail: {c['detail']}")
        if c.get("how_to_fix"):
            lines.append(f"  How to fix: {c['how_to_fix']}")
        if c.get("earliest_possible_days") is not None:
            lines.append(f"  Earliest this can be met: {c['earliest_possible_days']} days")
    return "\n".join(lines)


def _days(n):
    return elig.days_text(n) if isinstance(n, int) and not isinstance(n, bool) else f"{n} days"


def build_prompt(inputs):
    return (f"<input>\n{readable_inputs(inputs)}\n</input>\n\nExplain the next steps by calling the tool {TOOL_NAME}.")


def _texts(advice):
    """[(where, text)] of every free text in the answer."""
    out = [("summary", advice.get("summary", ""))]
    for i, step in enumerate(advice.get("steps") if isinstance(advice.get("steps"), list) else [], start=1):
        if isinstance(step, dict):
            out.append((f"step {i}", step.get("what_to_do", "")))
            out += [(f"materials of step {i}", x) for x in step.get("materials", []) if isinstance(x, str)]
    return out


def validate_advice(advice, inputs):
    """List of problems; empty means valid."""
    if not isinstance(advice, dict):
        return ["the answer is not a JSON object"]
    problems = []
    by_id = {c["id"]: c for c in inputs["checks"]}
    if not str(advice.get("summary", "")).strip():
        problems.append("the summary is empty")
    steps = advice.get("steps")
    if not isinstance(steps, list):
        return problems + ["steps must be a list"]
    unmet = [c for c in inputs["checks"] if c["status"] != "met"]
    if unmet and not steps:
        problems.append("there are conditions that are not met but no steps")

    for i, step in enumerate(steps, start=1):
        if not isinstance(step, dict):
            problems.append(f"step {i} is not an object")
            continue
        check_id = step.get("check_id")
        check = by_id.get(check_id)
        if check is None:
            problems.append(f"step {i} refers to the condition id {check_id!r}, which does not exist")
        elif check["status"] == "met":
            problems.append(f"step {i} is for condition {check['number']}, which is already met")
        if not str(step.get("what_to_do", "")).strip():
            problems.append(f"step {i} has no what_to_do text")
        materials = step.get("materials")
        if not isinstance(materials, list) or not all(isinstance(x, str) and x.strip() for x in materials):
            problems.append(f"step {i}: materials must be a list of non-empty strings")
        days = step.get("time_estimate_days")
        if isinstance(days, bool) or not isinstance(days, int) or days < 0:
            problems.append(f"step {i}: time_estimate_days must be a whole number of days, got {days!r}")
        elif check is not None and check.get("earliest_possible_days") is not None and days < check["earliest_possible_days"]:
            problems.append(f"step {i}: {days} days is shorter than the earliest possible time of "
                            f"{check['earliest_possible_days']} days")

    allowed, allowed_dates = ha.allowed_numbers(inputs)
    file_names = set(re.findall(r"[\w.\-]+\.pdf", json.dumps(inputs)))        # documents may be named as given
    for where, text in _texts(advice):
        for word in ha._FIELD_NAME.findall(str(text)):
            if any(word in name for name in file_names):
                continue
            problems.append(f"the text uses the internal field name '{word}' ({where}); write it in plain English")
        found, found_dates = ha.numbers_in(text)
        for n in found:
            if not any(abs(abs(n) - abs(a)) <= ha.TOLERANCE for a in allowed):
                problems.append(f"{where} quotes the number {n.normalize():f}, which is not in the input data")
        for d in found_dates:
            if d not in allowed_dates:
                problems.append(f"{where} quotes the date {d}, which is not in the input data")
    return problems


def advise(inputs, call=None):
    """Ask Claude, then validate. Returns (advice, usage, problems). `call(system, tool, prompt)` is injectable."""
    if call is None:
        from ai import contract_parser
        call = contract_parser.call_claude_tool
    advice, usage = call(SYSTEM_PROMPT, build_tool(), build_prompt(inputs))
    return advice, usage, validate_advice(advice, inputs)


def build_record(profile, checks, rules, advice_block):
    met, total = elig.progress(checks)
    annual = elig.annual_verified_revenue(profile)
    indicative = None
    if met == total:
        ind = elig.indicative_amount(annual, rules)
        indicative = {k: (str(v) if not isinstance(v, (bool, str)) else v) for k, v in ind.items()}
    return {"profile": elig.profile_summary(profile), "notice": elig.NOT_AN_OFFER, "rules": rules, "checks": checks,
            "progress": {"met": met, "total": total}, "annual_verified_revenue_eur": str(annual),
            "indicative": indicative, "advice": advice_block}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Eligibility checklist plus Claude's next steps, one file per profile.")
    parser.add_argument("--profile", choices=[elig.DEMO_ID, elig.BAKERY_ID, "all"], default="all")
    parser.add_argument("--no-ai", action="store_true", help="checklists only: no API call")
    parser.add_argument("--params", default=str(se.PARAMS_PATH))
    args = parser.parse_args(argv)

    params, rules = se.load_params(args.params), elig.load_rules()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    exit_code = 0
    for profile in elig.load_profiles(params):
        if args.profile not in ("all", profile["id"]):
            continue
        checks = elig.evaluate(profile, rules, params)
        print(f"\n{profile['name']}{' (SAMPLE)' if profile['sample'] else ''}")
        print("\n".join(elig.format_checklist(checks)))

        advice_block = None
        if not args.no_ai:
            from ai import contract_parser

            inputs = build_inputs(profile, checks)
            started = time.monotonic()
            advice, usage, problems = advise(inputs)
            meta = contract_parser.build_meta(config.CLAUDE_MODEL, time.monotonic() - started, usage)
            advice_block = {"status": "INVALID" if problems else "VALID", "problems": problems,
                            "model": config.CLAUDE_MODEL, "meta": meta,
                            "summary": advice.get("summary") if isinstance(advice, dict) else None,
                            "steps": advice.get("steps") if isinstance(advice, dict) else None}
            print(f"\nClaude: {advice_block['status']}, {meta['elapsed_seconds']} s, ~${meta['estimated_cost_usd']} (estimate)")
            if isinstance(advice, dict):
                print(f"  {advice.get('summary')}")
                for step in advice.get("steps") or []:
                    if isinstance(step, dict):
                        print(f"  - [{step.get('check_id')}] {step.get('what_to_do')} (about {_days(step.get('time_estimate_days'))})")
            for problem in problems:
                print(f"  INVALID: {problem}")
            exit_code = 2 if problems else exit_code

        path = OUT_DIR / f"{profile['id']}.json"
        record = build_record(profile, checks, rules, advice_block)
        path.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        # the disclaimer belongs to an indicative amount, so it is printed only when all conditions are met
        print(f"Saved {ha.relative(path)}." + (f" {elig.NOT_AN_OFFER}" if record["indicative"] else ""))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
