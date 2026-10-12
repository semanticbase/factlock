"""M5 — fault injection against the rule checks.

Each case in fault_catalog.json changes one thing in a draft that passes every check, and the
draft is then parsed and checked exactly as in production (app.parse, app.verify). The catalog
was written from what goes wrong in fund commentary, not from the rules, and was fixed (md5
recorded) before the first run.

Outcome of a case:
  blocked : at least one error — the draft goes back to the writer, the reader never sees it
  flagged : no error, but a warning — a person is asked to look before approval
  passed  : nothing raised by the rules — only the reviewer agent or a person can catch it
"blocked" is split by reason: by the rule written for that kind of mistake ("target"), or only
by an unrelated rule ("other"). A block for the wrong reason sends the wrong feedback to the
writer, which may fix that and keep the mistake.
"""
from __future__ import annotations

import copy
import hashlib
import json
import pathlib

from app.facts import build_fact_sheet, load_snapshot
from app.llm import demo_draft
from app.models import Draft
from app.parse import parse_draft
from app.verify import check_draft

CATALOG = pathlib.Path(__file__).resolve().parent / "fault_catalog.json"

# The rule in app/verify.py or app/parse.py written for each kind of mistake. Empty: no rule exists.
TARGET_CODES: dict[str, set[str]] = {
    "A1": {"typed_number"}, "A2": {"typed_number"}, "A3": {"typed_number"}, "A4": set(), "A5": set(),
    "A6": set(), "A7": {"unknown_fact", "bad_format"}, "A8": {"undisclosed_date"},
    "B1": {"direction_conflict"}, "B2": {"direction_conflict", "direction_ambiguous"},
    "B3": {"dramatic_wording"}, "B4": set(), "B5": set(),
    "C1": {"causal_claim"}, "C2": {"causal_claim"}, "C3": {"causal_claim"}, "C4": {"unhedged_forecast"},
    "C5": {"guarantee"}, "C6": {"recommendation"}, "C7": {"lead_claim"},
    "D1": {"bilingual_mismatch"}, "D2": set(), "D3": {"empty_sentence", "bilingual_mismatch"},
    "E1": {"missing_key_fact"}, "E2": {"market_coverage"}, "E3": {"missing_section"},
    "E4": {"unhedged_forecast"}, "F1": {"invalid_output"},
}


def catalog_md5() -> str:
    return hashlib.md5(CATALOG.read_bytes()).hexdigest()


def load_catalog() -> dict:
    return json.loads(CATALOG.read_text(encoding="utf-8"))


def _locate(draft: dict, at: str) -> tuple[dict, int]:
    key, index = at.rsplit(".", 1)
    section = next(s for s in draft["sections"] if s["key"] == key)
    return section, int(index)


def build_raw(case: dict, base: dict) -> str:
    """The writer's output for this case, as a string (exactly what the checker would receive)."""
    if "raw" in case:
        return case["raw"]
    draft = copy.deepcopy(base)
    for edit in case.get("edits", []):
        section, i = _locate(draft, edit["at"])
        for lang in ("en", "ja"):
            if lang in edit:
                section["sentences"][i][lang] = edit[lang]
    if "drop" in case:
        section, i = _locate(draft, case["drop"])
        del section["sentences"][i]
    if "drop_section" in case:
        draft["sections"] = [s for s in draft["sections"] if s["key"] != case["drop_section"]]
    if "schema_drop" in case:
        where, lang = case["schema_drop"].rsplit(".", 1)
        section, i = _locate(draft, where)
        del section["sentences"][i][lang]
    text = json.dumps(draft, ensure_ascii=False)
    if "truncate" in case:
        text = text[: int(len(text) * case["truncate"])]
    if case.get("wrap") == "fence":
        text = "```json\n" + text + "\n```"
    elif case.get("wrap") == "preamble":
        text = "Here is the draft:\n" + text
    return text


def run_case(case: dict, base: dict, sheet) -> dict:
    draft, issues = parse_draft(build_raw(case, base))
    if draft is not None:
        _, rule_issues = check_draft(draft, sheet)
        issues = issues + rule_issues
    errors = sorted({i.code for i in issues if i.severity == "error"})
    warnings = sorted({i.code for i in issues if i.severity == "warning"})
    status = "blocked" if errors else ("flagged" if warnings else "passed")
    targets = TARGET_CODES.get(case["family"], set())
    raised = errors if errors else warnings
    reason = None
    if status != "passed":
        reason = "target" if targets & set(raised) else "other"
    return {"id": case["id"], "family": case["family"], "severity": case["severity"], "status": status,
            "reason": reason, "errors": errors, "warnings": warnings,
            "issues": [{"code": i.code, "severity": i.severity, "ref": i.ref, "lang": i.lang} for i in issues],
            "rule_exists": bool(targets) if case["family"] != "L" else None}


def _tally(rows: list[dict]) -> dict:
    return {
        "cases": len(rows),
        "blocked_target": sum(r["status"] == "blocked" and r["reason"] == "target" for r in rows),
        "blocked_other": sum(r["status"] == "blocked" and r["reason"] == "other" for r in rows),
        "flagged": sum(r["status"] == "flagged" for r in rows),
        "passed": sum(r["status"] == "passed" for r in rows),
    }


def run(data_dir: pathlib.Path, fund_id: str = "DGBF", month: str = "2026-08") -> dict:
    sheet = build_fact_sheet(load_snapshot(data_dir, fund_id, month))
    base = demo_draft(set(sheet.facts))
    base_draft = Draft.model_validate(base)
    _, base_issues = check_draft(base_draft, sheet)
    catalog = load_catalog()
    faults = [run_case(c, base, sheet) for c in catalog["faults"]]
    legit = [run_case(c, base, sheet) for c in catalog["legit"]]
    families = {}
    for fam in catalog["families"]:
        rows = [r for r in faults if r["family"] == fam]
        if rows:
            families[fam] = {"name_en": catalog["families"][fam]["en"], "name_ja": catalog["families"][fam]["ja"],
                             "rule_exists": bool(TARGET_CODES.get(fam)), **_tally(rows)}
    return {
        "catalog_md5": catalog_md5(),
        "catalog_version": catalog["version"],
        "base_draft_issues": [i.code for i in base_issues],
        "faults": {
            "all": _tally(faults),
            "fatal": _tally([r for r in faults if r["severity"] == "fatal"]),
            "caution": _tally([r for r in faults if r["severity"] == "caution"]),
            "by_family": families,
            "cases": faults,
        },
        "legit": {
            "cases": len(legit),
            "false_rejections": sum(r["status"] == "blocked" for r in legit),
            "flagged_for_review": sum(r["status"] == "flagged" for r in legit),
            "passed": sum(r["status"] == "passed" for r in legit),
            "rows": legit,
        },
    }
