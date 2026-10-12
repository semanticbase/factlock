"""M5, second layer — the reviewer agent on the fault catalog.

Each case is built, parsed and rule-checked exactly as in measure/faults.py, then the production
reviewer (app.agents.make_reviewer) reads the rendered draft. In production the reviewer is
skipped when the rules already rejected a draft; here it reads every case, so its own detection
can be measured. The system outcome combines both layers as production would on a first draft.
"""
from __future__ import annotations

from app.agents import make_reviewer
from app.parse import parse_draft, parse_review
from app.verify import check_draft
from measure.agentrun import run_agent
from measure.faults import build_raw


async def review_case(case: dict, base: dict, sheet, reviewer_model, fund_id: str, month: str) -> dict:
    draft, issues = parse_draft(build_raw(case, base))
    record = {"id": case["id"], "family": case["family"], "severity": case["severity"]}
    if draft is None:
        record.update(rules={"errors": sorted({i.code for i in issues if i.severity == "error"}), "warnings": []},
                      reviewer=None, usage=[], note="not a draft; the reviewer never sees it")
        return record
    rendered, rule_issues = check_draft(draft, sheet)
    issues = issues + rule_issues
    state = {"fact_sheet": sheet.model_dump(), "rendered": rendered, "rule_errors": 0}
    run = await run_agent(make_reviewer(reviewer_model), state, fund_id, month)
    raw = run["state"].get("review_raw")
    refs = {s["ref"] for sec in rendered for s in sec["sentences"]}
    review, _ = parse_review(raw, refs)
    record.update(
        rules={"errors": sorted({i.code for i in issues if i.severity == "error"}),
               "warnings": sorted({i.code for i in issues if i.severity == "warning"})},
        reviewer={"raw": raw, "issues": [i.model_dump() for i in review]},
        usage=run["usage"], elapsed_ms=run["elapsed_ms"])
    return record


def outcome(record: dict) -> dict:
    rules_err, rules_warn = record["rules"]["errors"], record["rules"]["warnings"]
    rev = (record.get("reviewer") or {}).get("issues") or []
    rev_err = [i for i in rev if i["severity"] == "error"]
    rev_warn = [i for i in rev if i["severity"] == "warning"]
    reviewer_alone = "blocked" if rev_err else ("flagged" if rev_warn else "passed")
    if record.get("reviewer") is None:
        reviewer_alone = None
    if rules_err:
        system = "blocked by rules"
    elif rev_err:
        system = "blocked by reviewer"
    elif rules_warn or rev_warn:
        system = "flagged"
    else:
        system = "passed"
    return {"reviewer_alone": reviewer_alone, "system": system}
