import asyncio
import json
import pathlib
import warnings

import pytest

from app.facts import build_fact_sheet, load_snapshot
from app.llm import ScriptedLlm, build_models, demo_draft
from app.service import run_pipeline

warnings.filterwarnings("ignore", category=DeprecationWarning)
DATA = pathlib.Path(__file__).resolve().parent.parent / "data" / "snapshots"
NO_ISSUES = json.dumps({"issues": []})


@pytest.fixture(scope="module")
def clean_json():
    sheet = build_fact_sheet(load_snapshot(DATA, "DGBF", "2026-08"))
    return json.dumps(demo_draft(set(sheet.facts)), ensure_ascii=False)


@pytest.fixture(scope="module")
def typed_number_json():
    sheet = build_fact_sheet(load_snapshot(DATA, "DGBF", "2026-08"))
    data = demo_draft(set(sheet.facts))
    data["sections"][1]["sentences"][0]["en"] = "The fund returned 2.2% in {{report.month|name}} ({{fund.ret_1m|signed}}, {{fund.name|name}})."
    return json.dumps(data, ensure_ascii=False)


def run(writer, reviewer, max_iterations=3):
    return asyncio.run(run_pipeline(writer_model=writer, reviewer_model=reviewer, data_dir=str(DATA),
                                    fund_id="DGBF", month="2026-08", max_iterations=max_iterations))


def test_clean_draft_passes_first_time(clean_json):
    writer = ScriptedLlm(model="w", replies=[clean_json])
    reviewer = ScriptedLlm(model="r", replies=[NO_ISSUES])
    result = run(writer, reviewer)
    assert (result["status"], result["iterations"], writer.calls, reviewer.calls) == ("passed", 1, 1, 1)
    assert [s["agent"] for s in result["timeline"]] == [
        "fact_builder", "writer", "checker", "reviewer", "gate", "finalizer"]


def test_invalid_json_is_sent_back_and_reviewer_is_skipped(clean_json):
    writer = ScriptedLlm(model="w", replies=["Here is the draft: {not json", clean_json])
    reviewer = ScriptedLlm(model="r", replies=[NO_ISSUES])
    result = run(writer, reviewer)
    assert (result["status"], result["iterations"], reviewer.calls) == ("passed", 2, 1)
    first = result["history"][0]
    assert [e["code"] for e in first["errors"]] == ["invalid_output"]
    assert first["reviewer"].startswith("skipped")
    assert "FEEDBACK ON YOUR PREVIOUS DRAFT" in writer.seen[1]


def test_code_fenced_json_is_accepted(clean_json):
    writer = ScriptedLlm(model="w", replies=[f"```json\n{clean_json}\n```"])
    result = run(writer, ScriptedLlm(model="r", replies=[NO_ISSUES]))
    assert (result["status"], result["iterations"]) == ("passed", 1)


def test_reviewer_finding_triggers_a_revision(clean_json):
    finding = json.dumps({"issues": [{"ref": "market.1", "type": "unsupported_cause",
                                      "explanation": "Mentions a cause that is not in the facts."}]})
    writer = ScriptedLlm(model="w", replies=[clean_json, clean_json])
    reviewer = ScriptedLlm(model="r", replies=[finding, NO_ISSUES])
    result = run(writer, reviewer)
    assert (result["status"], result["iterations"], reviewer.calls) == ("passed", 2, 2)
    assert result["history"][0]["errors"][0]["origin"] == "reviewer"
    assert "Mentions a cause that is not in the facts." in writer.seen[1]


def test_unknown_reviewer_ref_is_kept_without_ref(clean_json):
    finding = json.dumps({"issues": [{"ref": "nowhere.9", "type": "magnitude", "explanation": "Too strong."}]})
    result = run(ScriptedLlm(model="w", replies=[clean_json] * 2),
                 ScriptedLlm(model="r", replies=[finding, NO_ISSUES]))
    assert result["history"][0]["errors"][0]["ref"] is None


def test_stops_at_the_iteration_limit(typed_number_json):
    writer = ScriptedLlm(model="w", replies=[typed_number_json])
    reviewer = ScriptedLlm(model="r", replies=[NO_ISSUES])
    result = run(writer, reviewer, max_iterations=3)
    assert (result["status"], result["iterations"], writer.calls, reviewer.calls) == ("needs_review", 3, 3, 0)
    assert all("typed_number" in [e["code"] for e in h["errors"]] for h in result["history"])


def test_unreadable_review_is_a_warning(clean_json):
    result = run(ScriptedLlm(model="w", replies=[clean_json]), ScriptedLlm(model="r", replies=["n/a"]))
    assert result["status"] == "passed"
    assert [w["code"] for w in result["report"]["warnings"]] == ["review_unavailable"]


def test_offline_demo_fix_once():
    writer, reviewer = build_models("demo", model="x", reviewer_model=None, project=None,
                                    location="global", demo_scenario="fix_once")
    result = run(writer, reviewer)
    assert (result["status"], result["iterations"], writer.calls, reviewer.calls) == ("passed", 2, 2, 1)
    assert {"typed_number", "direction_conflict", "lead_claim", "guarantee"} <= {
        e["code"] for e in result["history"][0]["errors"]}


def test_writer_instruction_carries_facts_and_keeps_placeholders(clean_json):
    writer = ScriptedLlm(model="w", replies=[clean_json])
    run(writer, ScriptedLlm(model="r", replies=[NO_ISSUES]))
    instruction = writer.seen[0]
    assert "<FACTS_JSON>" in instruction and '"id": "fund.ret_1m"' in instruction
    assert "{{fund.ret_1m|verb}}" in instruction
