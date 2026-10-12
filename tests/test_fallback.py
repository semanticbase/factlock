"""P7: a quota refusal (429) at the first endpoint sends the request to the same model elsewhere.

Every branch is exercised here before it runs in production: the switch itself, the endpoint tag on
each response (so the printed record and the cost name the endpoint actually used), the errors that
must not switch, and the plain message a reader sees when every endpoint is busy. The 429 error is the
class ADK itself raises (google.adk.models.google_llm._ResourceExhaustedError).
"""
import asyncio
import json
import pathlib
import warnings
from typing import AsyncGenerator

import pytest
from fastapi.testclient import TestClient
from google.adk.models import BaseLlm, LlmRequest, LlmResponse
from google.adk.models.google_llm import _ResourceExhaustedError
from google.genai import types
from google.genai.errors import ClientError

import app.main as main
from app.facts import build_fact_sheet, load_snapshot
from app.llm import FallbackLlm, demo_draft, is_quota_error
from app.service import run_pipeline
from measure import cost
from tests.test_usage import NO_ISSUES, UsageLlm, usage

warnings.filterwarnings("ignore", category=DeprecationWarning)
DATA = pathlib.Path(__file__).resolve().parent.parent / "data" / "snapshots"


def quota_error():
    return _ResourceExhaustedError(ClientError(429, {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED",
                                                               "message": "Resource exhausted."}}))


class Refusing(BaseLlm):
    """Raises the given error, optionally after yielding one response first."""
    error: object = None
    partial_first: bool = False
    calls: int = 0

    async def generate_content_async(self, llm_request: LlmRequest, stream: bool = False
                                     ) -> AsyncGenerator[LlmResponse, None]:
        self.calls += 1
        if self.partial_first:
            yield LlmResponse(content=types.Content(role="model", parts=[types.Part(text="{")]), partial=True)
        raise self.error


@pytest.fixture(scope="module")
def clean_reply():
    sheet = build_fact_sheet(load_snapshot(DATA, "DGBF", "2026-08"))
    return json.dumps(demo_draft(set(sheet.facts)), ensure_ascii=False)


def collect(llm):
    async def go():
        return [r async for r in llm.generate_content_async(LlmRequest(), False)]
    return asyncio.run(go())


def test_the_adk_quota_error_is_recognised():
    assert is_quota_error(quota_error()) and is_quota_error(ClientError(429, {}))
    assert not is_quota_error(ClientError(400, {})) and not is_quota_error(RuntimeError("x"))


def test_a_quota_refusal_goes_to_the_fallback_and_is_tagged_there(clean_reply):
    first = Refusing(model="m", error=quota_error())
    second = UsageLlm(model="m", replies=[clean_reply], usages=[usage(10, 1)])
    got = collect(FallbackLlm(model="m", primary=first, fallback=second, primary_location="global",
                              fallback_location="asia-northeast1"))
    assert first.calls == 1 and second.calls == 1
    assert [r.custom_metadata["endpoint"] for r in got] == ["asia-northeast1"]


def test_an_answer_from_the_first_endpoint_is_tagged_there(clean_reply):
    first = UsageLlm(model="m", replies=[clean_reply], usages=[usage(10, 1)])
    second = UsageLlm(model="m", replies=[clean_reply], usages=[usage(10, 1)])
    got = collect(FallbackLlm(model="m", primary=first, fallback=second, primary_location="global",
                              fallback_location="asia-northeast1"))
    assert second.calls == 0 and got[0].custom_metadata == {"endpoint": "global"}


@pytest.mark.parametrize("error,partial", [(ClientError(400, {}), False), (RuntimeError("network"), False),
                                           (None, True)])
def test_other_errors_and_half_answered_requests_do_not_switch(error, partial, clean_reply):
    first = Refusing(model="m", error=error or quota_error(), partial_first=partial)
    second = UsageLlm(model="m", replies=[clean_reply], usages=[usage(10, 1)])
    with pytest.raises(Exception):
        collect(FallbackLlm(model="m", primary=first, fallback=second, primary_location="global",
                            fallback_location="asia-northeast1"))
    assert second.calls == 0          # never send a request twice when part of an answer already arrived


def test_through_the_real_pipeline_the_record_and_the_cost_name_the_endpoint_used(clean_reply):
    writer = FallbackLlm(model="gemini-3.8-flash", primary=Refusing(model="gemini-3.8-flash", error=quota_error()),
                         fallback=UsageLlm(model="w", replies=[clean_reply], usages=[usage(8620, 2078, thinking=6717)]),
                         primary_location="global", fallback_location="asia-northeast1")
    reviewer = UsageLlm(model="r", replies=[NO_ISSUES], usages=[usage(11676, 5, thinking=2023)])
    result = asyncio.run(run_pipeline(writer_model=writer, reviewer_model=reviewer, data_dir=str(DATA),
                                      fund_id="DGBF", month="2026-08"))
    assert result["status"] == "passed"
    assert [u["endpoint"] for u in result["usage"]] == ["asia-northeast1", None]
    s = cost.summarize(result)
    assert s["endpoints"] == ["asia-northeast1", "global"]
    # writer at the non-global price, reviewer at the global price
    want = (8620 * 0.825 + (2078 + 6717) * 4.125 + 11676 * 0.75 + (5 + 2023) * 3.75) / 1e6
    assert s["cost_usd"]["introductory, to 2026-12-31"] == pytest.approx(want, abs=1e-6)


def test_build_models_wires_the_fallback_with_short_then_long_retries(monkeypatch):
    import google.genai as genai
    made = []

    real = genai.Client

    class FakeClient(real):          # a real Client subclass, so ADK's type check runs as in production
        def __init__(self, **kw):    # no credentials needed: nothing is called
            made.append(kw)
    monkeypatch.setattr(genai, "Client", FakeClient)
    from app.llm import build_models
    w, r = build_models("gemini", model="gemini-3.8-flash", reviewer_model=None, project="p", location="global",
                        fallback_location="asia-northeast1")
    assert isinstance(w, FallbackLlm) and isinstance(r, FallbackLlm)
    assert [(m["location"], m["http_options"].retry_options.attempts, m["http_options"].retry_options.max_delay)
            for m in made] == [("global", 3, 4.0), ("asia-northeast1", 6, 30.0)]
    made.clear()
    w, _ = build_models("gemini", model="gemini-3.8-flash", reviewer_model=None, project="p", location="global")
    assert not isinstance(w, FallbackLlm) and made[0]["http_options"].retry_options.attempts == 6


def _api_with(monkeypatch, error, ledger=None):
    from app.ledger import MemoryLedger
    async def failing(**kw):
        raise error
    monkeypatch.setattr(main, "run_pipeline", failing)
    monkeypatch.setattr(main, "ledger", ledger or MemoryLedger())   # an empty ledger: nothing on record
    main.settings.passphrase = ""
    return TestClient(main.app).post("/api/generate", json={"fund_id": "DGBF", "month": "2026-08", "mode": "demo"})


def test_a_reader_sees_plain_words_when_every_endpoint_is_busy(monkeypatch):
    r = _api_with(monkeypatch, quota_error())
    assert r.status_code == 503
    detail = r.json()["detail"]
    assert detail.startswith("Gemini was too busy to answer (429, quota exhausted on the ") and "try again in a minute" in detail
    assert "No verified draft is on record yet" in detail
    assert "_ResourceExhaustedError" not in detail and "adk-docs" not in detail


def test_a_busy_gemini_serves_the_latest_verified_draft_from_the_ledger(monkeypatch):
    """The submission is evaluated as found, with no retries: when Gemini is busy, the reader gets the
    most recent draft that passed the checks, marked as served from the ledger, instead of an error."""
    from app.ledger import MemoryLedger
    ledger = MemoryLedger()
    run_id = ledger.start_run("DGBF", "2026-08", "visitor-1")
    ledger.finish_run(run_id, {"status": "passed", "iterations": 1, "commentary": [], "history": []})
    r = _api_with(monkeypatch, quota_error(), ledger)
    assert r.status_code == 200
    body = r.json()
    assert body["served_from"] == "ledger" and body["run_id"] == run_id and body["status"] == "passed"
    assert body["served_reason"].startswith("Gemini was too busy to answer (429")
    # The failed attempt is on record too, and a draft that never passed is not served.
    assert [row["status"] for row in ledger.runs.values()].count("failed") == 1
    ledger2 = MemoryLedger()
    ledger2.finish_run(ledger2.start_run("DGBF", "2026-08", None), {"status": "needs_review", "iterations": 3})
    assert _api_with(monkeypatch, quota_error(), ledger2).status_code == 503


def test_other_failures_are_reported_without_technical_text(monkeypatch):
    r = _api_with(monkeypatch, RuntimeError("socket closed at 0x7f"))
    assert r.status_code == 502
    assert r.json()["detail"].startswith("The draft could not be completed (RuntimeError while calling the model")
    assert "socket" not in r.text and "0x7f" not in r.text            # the cause's kind, never its internal text


def test_the_printed_record_names_the_endpoints_that_answered():
    js = (pathlib.Path(__file__).resolve().parent.parent / "web" / "app.js").read_text(encoding="utf-8")
    assert "engine.locations && engine.locations.length ? engine.locations : [state.health.location]" in js
    assert "on Google Cloud (${where})" in js and "（Google Cloud、${where}）" in js
    src = (pathlib.Path(__file__).resolve().parent.parent / "app" / "main.py").read_text(encoding="utf-8")
    assert '"locations": sorted({c["endpoint"] or settings.location for c in result.get("usage") or []})' in src


def test_fallback_location_setting(monkeypatch):
    for value, want in (("asia-northeast1", "asia-northeast1"), ("none", None), ("", None), ("us-central1", "us-central1")):
        monkeypatch.setenv("FACTLOCK_FALLBACK_LOCATION", value)
        assert main.Settings().fallback_location == want
    monkeypatch.delenv("FACTLOCK_FALLBACK_LOCATION")
    assert main.Settings().fallback_location is None          # off until the model exists at a second location
