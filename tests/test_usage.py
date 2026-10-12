"""M7: token usage is recorded for every model call and converted to cost at list price.

The fake model below returns usage in the same object the Gemini client returns
(google.genai GenerateContentResponseUsageMetadata), and the pipeline runs through ADK
exactly as in production, so these tests cover the real path from the model response to
the saved record. The token numbers are synthetic; after the first observed Gemini run
they are to be compared with the real record (the raw usage is saved for that).
"""
import asyncio
import json
import pathlib
import warnings
from typing import AsyncGenerator

import pytest
from google.adk.models import BaseLlm, LlmRequest, LlmResponse
from google.genai import types

from app.facts import build_fact_sheet, load_snapshot
from app.llm import build_models, demo_draft
from app.service import run_pipeline
from measure import cost
from scripts import run_once

warnings.filterwarnings("ignore", category=DeprecationWarning)
DATA = pathlib.Path(__file__).resolve().parent.parent / "data" / "snapshots"
NO_ISSUES = json.dumps({"issues": []})


class UsageLlm(BaseLlm):
    """Replays replies and reports usage with each one (None = the model reports no usage)."""

    replies: list[str] = []
    usages: list = []
    version: str = "gemini-3.8-flash"
    calls: int = 0

    async def generate_content_async(self, llm_request: LlmRequest, stream: bool = False
                                     ) -> AsyncGenerator[LlmResponse, None]:
        i = min(self.calls, len(self.replies) - 1)
        self.calls += 1
        usage = self.usages[min(i, len(self.usages) - 1)]
        yield LlmResponse(content=types.Content(role="model", parts=[types.Part(text=self.replies[i])]),
                          usage_metadata=usage, model_version=self.version)


def usage(prompt, output, thinking=None, cached=None):
    total = prompt + output + (thinking or 0)
    return types.GenerateContentResponseUsageMetadata(
        prompt_token_count=prompt, candidates_token_count=output, thoughts_token_count=thinking,
        cached_content_token_count=cached, total_token_count=total)


@pytest.fixture(scope="module")
def drafts():
    sheet = build_fact_sheet(load_snapshot(DATA, "DGBF", "2026-08"))
    clean = json.dumps(demo_draft(set(sheet.facts)), ensure_ascii=False)
    bad = demo_draft(set(sheet.facts))
    bad["sections"][1]["sentences"][0]["en"] = "The fund returned 2.2% ({{fund.ret_1m|signed}}, {{fund.name|name}}, {{report.month|name}})."
    return clean, json.dumps(bad, ensure_ascii=False)


def run(writer, reviewer):
    return asyncio.run(run_pipeline(writer_model=writer, reviewer_model=reviewer, data_dir=str(DATA),
                                    fund_id="DGBF", month="2026-08"))


def test_usage_is_recorded_for_each_model_call(drafts):
    clean, _ = drafts
    writer = UsageLlm(model="w", replies=[clean], usages=[usage(12000, 1500, thinking=2500, cached=2000)])
    reviewer = UsageLlm(model="r", replies=[NO_ISSUES], usages=[usage(9000, 40, thinking=800)])
    result = run(writer, reviewer)
    assert result["status"] == "passed"
    rows = result["usage"]
    assert [r["agent"] for r in rows] == ["writer", "reviewer"]
    w = rows[0]
    assert (w["prompt_tokens"], w["cached_tokens"], w["output_tokens"], w["thinking_tokens"]) == (12000, 2000, 1500, 2500)
    assert w["model"] == "gemini-3.8-flash" and w["raw"]["prompt_token_count"] == 12000
    assert rows[1]["cached_tokens"] is None          # not reported stays "not reported", not zero
    s = cost.summarize(result)
    assert s["complete"] and s["observed_calls"] == {"writer": 1, "reviewer": 1}
    assert s["calls_total_mismatch"] == []


def test_a_skipped_review_is_not_counted_as_a_call(drafts):
    clean, bad = drafts
    writer = UsageLlm(model="w", replies=[bad, clean], usages=[usage(12000, 1500), usage(13000, 1600)])
    reviewer = UsageLlm(model="r", replies=[NO_ISSUES], usages=[usage(9000, 40)])
    result = run(writer, reviewer)
    assert result["iterations"] == 2 and reviewer.calls == 1
    s = cost.summarize(result)
    assert s["expected_calls"] == {"writer": 2, "reviewer": 1}
    assert s["observed_calls"] == {"writer": 2, "reviewer": 1} and s["complete"]


def test_missing_usage_makes_the_record_incomplete(drafts):
    clean, _ = drafts
    writer = UsageLlm(model="w", replies=[clean], usages=[usage(12000, 1500)])
    reviewer = UsageLlm(model="r", replies=[NO_ISSUES], usages=[None])
    s = cost.summarize(run(writer, reviewer))
    assert s["observed_calls"] == {"writer": 1, "reviewer": 0} and not s["complete"]
    assert "INCOMPLETE" in cost.summary_lines(s)[0]


def test_the_offline_demo_reports_no_usage():
    writer, reviewer = build_models("demo", model="x", reviewer_model=None, project=None, location="global")
    result = run(writer, reviewer)
    assert result["usage"] == []
    assert cost.summary_lines(cost.summarize(result)) == [
        "Token usage: none reported (the offline demo does not call a model)."]


def test_cost_arithmetic_by_hand():
    result = {"iterations": 1, "history": [{"reviewer": "ran"}], "usage": [
        {"agent": "writer", "model": "gemini-3.8-flash", "prompt_tokens": 10000, "cached_tokens": 2000,
         "output_tokens": 1000, "thinking_tokens": 3000, "tool_prompt_tokens": None, "total_tokens": 14000},
        {"agent": "reviewer", "model": "gemini-3.8-flash", "prompt_tokens": 8000, "cached_tokens": None,
         "output_tokens": 200, "thinking_tokens": None, "tool_prompt_tokens": None, "total_tokens": 8200},
    ]}
    s = cost.summarize(result)
    # writer: 8,000 × 0.75 + 2,000 × 0.075 + 4,000 × 3.75 = 21,150 → $0.02115
    # reviewer: 8,000 × 0.75 + 200 × 3.75 = 6,750 → $0.00675
    assert s["cost_usd"]["introductory, to 2026-12-31"] == pytest.approx(0.0279)
    assert s["cost_usd"]["standard, from 2027-01-01"] == pytest.approx(0.0558)
    assert cost.summarize(result, location="asia-northeast1")["cost_usd"]["introductory, to 2026-12-31"] == pytest.approx(
        (8000 * 0.825 + 2000 * 0.0825 + 4000 * 4.125 + 8000 * 0.825 + 200 * 4.125) / 1e6)
    result["usage"][0]["total_tokens"] = 99
    assert cost.summarize(result)["calls_total_mismatch"] == [0]


@pytest.mark.parametrize("name,key", [
    ("gemini-3.8-flash", "gemini-3.8-flash"), ("gemini-3.8-flash-001", "gemini-3.8-flash"),
    ("publishers/google/models/gemini-3.8-flash", "gemini-3.8-flash"),
    ("gemini-3.8-flash-cyber", None), ("gemini-3.5-flash", None), (None, None),
])
def test_price_lookup_does_not_guess(name, key):
    assert cost.price_key(name) == key


def test_unknown_model_is_not_priced():
    result = {"iterations": 1, "history": [], "usage": [
        {"agent": "writer", "model": "some-other-model", "prompt_tokens": 10, "output_tokens": 1}]}
    s = cost.summarize(result)
    assert s["cost_usd"] is None and not s["complete"]
    assert "not computed" in cost.summary_lines(s)[2]


def test_run_once_prints_tokens_and_cost(drafts, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("FACTLOCK_MODEL", raising=False)
    monkeypatch.delenv("FACTLOCK_LOCATION", raising=False)
    run_once.RESULT_FILE = tmp_path / "run.json"
    clean, _ = drafts
    writer = UsageLlm(model="w", replies=[clean], usages=[usage(12000, 1500, thinking=2500)])
    reviewer = UsageLlm(model="r", replies=[NO_ISSUES], usages=[usage(9000, 40)])
    assert run_once.main(models=(writer, reviewer)) == 0
    out = capsys.readouterr().out
    assert "Model calls with usage: writer 1, reviewer 1 (expected writer 1, reviewer 1) — complete" in out
    assert "Cost at list price, global endpoint: $0.0" in out
    saved = json.loads(run_once.RESULT_FILE.read_text())
    assert saved["usage"][0]["raw"]["thoughts_token_count"] == 2500
