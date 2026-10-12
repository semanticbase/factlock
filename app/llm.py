"""Model back-ends for the agents.

* gemini : Gemini on Google Cloud (Gemini Enterprise Agent Platform), used in production.
* demo   : an offline, scripted writer and reviewer. No AI model is called.
           Used for local previews and as a fallback; the UI labels it clearly.
* Tests use ScriptedLlm to replay exact model responses.
"""
from __future__ import annotations

import json
import logging
import re
from typing import AsyncGenerator

from google.adk.models import BaseLlm, LlmRequest, LlmResponse
from google.genai import types


def _system_text(request: LlmRequest) -> str:
    instruction = request.config.system_instruction if request.config else None
    if instruction is None:
        return ""
    if isinstance(instruction, str):
        return instruction
    parts = getattr(instruction, "parts", None) or []
    return "".join(getattr(p, "text", "") or "" for p in parts)


def _reply(text: str) -> LlmResponse:
    return LlmResponse(content=types.Content(role="model", parts=[types.Part(text=text)]))


class ScriptedLlm(BaseLlm):
    """Replays a fixed list of responses and records every instruction it receives."""

    replies: list[str] = []
    calls: int = 0
    seen: list[str] = []

    async def generate_content_async(self, llm_request: LlmRequest, stream: bool = False
                                     ) -> AsyncGenerator[LlmResponse, None]:
        self.seen.append(_system_text(llm_request))
        text = self.replies[min(self.calls, len(self.replies) - 1)]
        self.calls += 1
        yield _reply(text)


def _fact_ids(system_text: str) -> set[str]:
    match = re.search(r"<FACTS_JSON>\s*(.*?)\s*</FACTS_JSON>", system_text, re.S)
    if not match:
        return set()
    return {row["id"] for row in json.loads(match.group(1))}


def demo_draft(ids: set[str], flawed: bool = False) -> dict:
    """A draft built only from placeholders, as the writer is asked to do."""
    s = lambda en, ja: {"en": en, "ja": ja}  # noqa: E731
    market = []
    if {"etf.SPY.ret_1m", "etf.SPDW.ret_1m", "etf.SPEM.ret_1m"} <= ids:
        market.append(s(
            "In {{report.month|name}}, {{etf.SPY.label|name}} {{etf.SPY.ret_1m|verb}}, "
            "{{etf.SPDW.label|name}} {{etf.SPDW.ret_1m|verb}} and {{etf.SPEM.label|name}} {{etf.SPEM.ret_1m|verb}}.",
            "{{report.month|name}}は、{{etf.SPY.label|name}}が{{etf.SPY.ret_1m|verb}}、"
            "{{etf.SPDW.label|name}}が{{etf.SPDW.ret_1m|verb}}、{{etf.SPEM.label|name}}が{{etf.SPEM.ret_1m|verb}}となりました。"))
    if {"etf.SPAB.ret_1m", "rate.USA.chg_1m"} <= ids:
        market.append(s(
            "{{etf.SPAB.label|name}} {{etf.SPAB.ret_1m|verb}} on a total-return basis, while "
            "{{rate.USA.label|name}} {{rate.USA.chg_1m|verb}} to {{rate.USA.level|value}}.",
            "{{etf.SPAB.label|name}}はトータルリターンで{{etf.SPAB.ret_1m|verb}}となり、"
            "{{rate.USA.label|name}}は{{rate.USA.level|value}}と前月から{{rate.USA.chg_1m|verb}}となりました。"))
    if "cli.G7.level" in ids:
        market.append(s(
            "The OECD leading indicator for {{cli.G7.label|name}} {{cli.G7.chg_1m|verb}} to {{cli.G7.level|value}}, "
            "{{cli.G7.trend|name}}, and {{cli.G7.streak|name}}.",
            "OECDの景気先行指数は、{{cli.G7.label|name}}で{{cli.G7.level|value}}と前月から{{cli.G7.chg_1m|verb}}となり、"
            "{{cli.G7.trend|name}}にあり、{{cli.G7.streak|name}}となっています。"))
    if "cli.A5M.level" in ids:
        market.append(s(
            "For {{cli.A5M.label|name}}, the indicator stood at {{cli.A5M.level|value}}, {{cli.A5M.phase|name}}.",
            "{{cli.A5M.label|name}}の景気先行指数は{{cli.A5M.level|value}}で、{{cli.A5M.phase|name}}にあります。"))

    performance = [
        s("{{fund.name|name}} {{fund.ret_1m|verb}} in {{report.month|name}} (hypothetical portfolio, before fees).",
          "{{fund.name|name}}の{{report.month|name}}の騰落率は{{fund.ret_1m|signed}}となりました（仮想ポートフォリオ、費用控除前）。"),
        s("The largest contributor was {{fund.top_contributor|name}} at {{fund.top_contributor_value|signed}}, "
          "and {{fund.detractors|name}}.",
          "最大の寄与は{{fund.top_contributor|name}}の{{fund.top_contributor_value|signed}}で、{{fund.detractors|name}}。"),
        s("{{fund.smallest_contributor|name}} made the smallest contribution, at {{fund.smallest_contributor_value|signed}}.",
          "寄与が最も小さかったのは{{fund.smallest_contributor|name}}で、{{fund.smallest_contributor_value|signed}}でした。"),
    ]
    portfolio = [s(
        "The fund held its target allocation of {{etf.SPY.weight|value}} in {{etf.SPY.label|name}}, "
        "{{etf.SPDW.weight|value}} in {{etf.SPDW.label|name}}, {{etf.SPEM.weight|value}} in {{etf.SPEM.label|name}} "
        "and {{etf.SPAB.weight|value}} in {{etf.SPAB.label|name}}, rebalanced at each month end.",
        "目標配分は、{{etf.SPY.label|name}}{{etf.SPY.weight|value}}、{{etf.SPDW.label|name}}{{etf.SPDW.weight|value}}、"
        "{{etf.SPEM.label|name}}{{etf.SPEM.weight|value}}、{{etf.SPAB.label|name}}{{etf.SPAB.weight|value}}で、"
        "毎月末にリバランスしています。")]
    if "lookthrough.3.name" in ids:
        portfolio.append(s(
            "As of {{holdings.as_of|name}}, the largest look-through holdings were {{lookthrough.1.name|name}} "
            "({{lookthrough.1.weight|value}}), {{lookthrough.2.name|name}} ({{lookthrough.2.weight|value}}) "
            "and {{lookthrough.3.name|name}} ({{lookthrough.3.weight|value}}).",
            "{{holdings.as_of|name}}時点のルックスルーでの組入上位は、{{lookthrough.1.name|name}}"
            "（{{lookthrough.1.weight|value}}）、{{lookthrough.2.name|name}}（{{lookthrough.2.weight|value}}）、"
            "{{lookthrough.3.name|name}}（{{lookthrough.3.weight|value}}）でした。"))
    outlook = [
        s("We will keep monitoring leading indicators and long-term interest rates, and will keep rebalancing "
          "to the target allocation at each month end.",
          "引き続き景気先行指数と長期金利の動向を注視し、毎月末に目標配分へのリバランスを行う方針です。"),
        s("Returns may vary with changes in market conditions.",
          "市場環境の変化により、騰落率は変動する可能性があります。"),
    ]
    if flawed:
        performance[0] = s(
            "{{fund.name|name}} fell in {{report.month|name}}, returning {{fund.ret_1m|signed}} (about 2.2%).",
            performance[0]["ja"])
        outlook[1] = s(
            "Leading indicators lead stock prices by several months, so further gains are likely.",
            "景気先行指数は株価に数か月先行するため、今後も必ず上昇が期待できます。")
    return {"sections": [
        {"key": "market", "sentences": market},
        {"key": "performance", "sentences": performance},
        {"key": "portfolio", "sentences": portfolio},
        {"key": "outlook", "sentences": outlook},
    ]}


log = logging.getLogger("factlock.llm")


class DemoWriterLlm(BaseLlm):
    """Offline writer. scenario='clean' passes first time; 'fix_once' makes
    deliberate mistakes in the first draft so the correction loop can be seen."""

    scenario: str = "clean"
    calls: int = 0

    async def generate_content_async(self, llm_request: LlmRequest, stream: bool = False
                                     ) -> AsyncGenerator[LlmResponse, None]:
        system = _system_text(llm_request)
        flawed = self.scenario == "fix_once" and "FEEDBACK ON YOUR PREVIOUS DRAFT" not in system
        self.calls += 1
        yield _reply(json.dumps(demo_draft(_fact_ids(system), flawed=flawed), ensure_ascii=False))


def is_quota_error(exc: BaseException) -> bool:
    """429 RESOURCE_EXHAUSTED from Gemini (ADK raises a ClientError subclass with code 429)."""
    from google.genai.errors import ClientError
    return isinstance(exc, ClientError) and getattr(exc, "code", None) == 429


class FallbackLlm(BaseLlm):
    """The same model at a second endpoint, used only when the first answers 429 RESOURCE_EXHAUSTED.

    Every response is tagged with the endpoint that produced it (custom_metadata["endpoint"]), so the
    record of a draft and its cost always name the endpoint actually used."""

    primary: BaseLlm
    fallback: BaseLlm
    primary_location: str
    fallback_location: str

    async def generate_content_async(self, llm_request: LlmRequest, stream: bool = False
                                     ) -> AsyncGenerator[LlmResponse, None]:
        yielded = False
        try:
            async for response in self.primary.generate_content_async(llm_request, stream):
                yielded = True
                yield _tagged(response, self.primary_location)
            return
        except Exception as exc:  # noqa: BLE001 - only a quota error on an untouched request switches over
            if yielded or not is_quota_error(exc):
                raise
            log.warning("Gemini at %s is exhausted (429); retrying at %s", self.primary_location, self.fallback_location)
        async for response in self.fallback.generate_content_async(llm_request, stream):
            yield _tagged(response, self.fallback_location)


def _tagged(response: LlmResponse, location: str) -> LlmResponse:
    meta = dict(response.custom_metadata or {})
    meta["endpoint"] = location
    response.custom_metadata = meta
    return response


class DemoReviewerLlm(BaseLlm):
    calls: int = 0

    async def generate_content_async(self, llm_request: LlmRequest, stream: bool = False
                                     ) -> AsyncGenerator[LlmResponse, None]:
        self.calls += 1
        yield _reply(json.dumps({"issues": []}))


def build_models(mode: str, *, model: str, reviewer_model: str | None, project: str | None,
                 location: str, demo_scenario: str = "clean", fallback_location: str | None = None):
    """Return (writer_model, reviewer_model) for the requested mode. With fallback_location, a request that
    the first endpoint refuses with 429 goes to the same model at that location."""
    if mode == "demo":
        return DemoWriterLlm(model="demo-writer", scenario=demo_scenario), DemoReviewerLlm(model="demo-reviewer")
    if mode != "gemini":
        raise ValueError(f"unknown LLM mode: {mode}")
    from google import genai
    from google.adk.models.google_llm import Gemini

    # A pre-configured client is used by ADK for every call (Gemini.api_client returns it as is), so the
    # retry settings belong here. Without a fallback: six attempts, waits 2-4-8-16-30 s (60 s in all).
    # With a fallback: three quick attempts at the first endpoint (2 and 4 s), then the fallback endpoint
    # with the long schedule. On 2026-09-24 the global endpoint refused 6 of 12 calls within minutes even
    # after 60 s of retries, so waiting longer at the same endpoint does not help a live demo.
    def client_for(where: str, attempts: int, max_delay: float):
        return genai.Client(enterprise=True, project=project, location=where, http_options=types.HttpOptions(
            retry_options=types.HttpRetryOptions(attempts=attempts, initial_delay=2.0, max_delay=max_delay,
                                                 http_status_codes=[429, 500, 502, 503, 504])))

    names = (model, reviewer_model or model)
    if not fallback_location or fallback_location == location:
        client = client_for(location, 6, 30.0)
        return tuple(Gemini(model=name, client=client) for name in names)
    first, second = client_for(location, 3, 4.0), client_for(fallback_location, 6, 30.0)
    return tuple(FallbackLlm(model=name, primary=Gemini(model=name, client=first), fallback=Gemini(model=name, client=second),
                             primary_location=location, fallback_location=fallback_location) for name in names)
