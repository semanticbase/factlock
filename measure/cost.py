"""M7 — tokens used by one draft, and its cost at Google's list price.

The pipeline records the usage that the model reports with each response (app/service.py). This
module adds it up per agent, checks that every model call reported its usage, and converts it
to US dollars at the published list price.

Billing rules applied (from the price page below):
* input tokens = prompt tokens + tool-use prompt tokens; the cached part of the prompt is billed at
  the cached-input rate (cached tokens are a subset of the prompt tokens);
* output tokens = response tokens + reasoning ("thinking") tokens, billed at the same output rate;
* only requests that return HTTP 200 are billed (failed attempts that were retried cost nothing).
"""
from __future__ import annotations

import re

PRICE_SOURCE = "https://cloud.google.com/gemini-enterprise-agent-platform/generative-ai/pricing"
PRICE_CHECKED_ON = "2026-09-24"
# USD per 1M tokens, Standard (on-demand) tier: (input, cached input, output incl. reasoning).
PRICES: dict[str, dict[str, dict[str, tuple[float, float, float]]]] = {
    "gemini-3.8-flash": {
        "introductory, to 2026-12-31": {"global": (0.75, 0.075, 3.75), "non-global": (0.825, 0.0825, 4.125)},
        "standard, from 2027-01-01": {"global": (1.50, 0.15, 7.50), "non-global": (1.65, 0.165, 8.25)},
    },
}
PRICE_NOTE = ("The introductory price is given as 50% credits back on net spend, so the billing console may "
              "first show the standard price and then a credit.")
FIELDS = ("prompt_tokens", "cached_tokens", "output_tokens", "thinking_tokens", "tool_prompt_tokens", "total_tokens")


def price_key(model: str | None) -> str | None:
    """The price-table entry for a model name, allowing only a numeric version suffix (e.g. -001)."""
    if not model:
        return None
    name = model.split("/")[-1]
    for key in PRICES:
        if re.fullmatch(rf"{re.escape(key)}(-\d+)?", name):
            return key
    return None


def _n(value) -> int:
    return int(value) if value is not None else 0


def call_cost(call: dict, rates: tuple[float, float, float]) -> float:
    rate_in, rate_cached, rate_out = rates
    prompt, cached = _n(call.get("prompt_tokens")), _n(call.get("cached_tokens"))
    billed_in = prompt - cached + _n(call.get("tool_prompt_tokens"))
    billed_out = _n(call.get("output_tokens")) + _n(call.get("thinking_tokens"))
    return (billed_in * rate_in + cached * rate_cached + billed_out * rate_out) / 1_000_000


def summarize(result: dict, model: str | None = None, location: str = "global") -> dict:
    calls = result.get("usage") or []
    history = result.get("history") or []
    expected = {"writer": int(result.get("iterations") or 0),
                "reviewer": sum(1 for h in history if h.get("reviewer") == "ran")}
    observed = {a: sum(1 for c in calls if c["agent"] == a) for a in expected}
    missing = [i for i, c in enumerate(calls) if c.get("prompt_tokens") is None or c.get("output_tokens") is None]
    total_mismatch = [
        i for i, c in enumerate(calls)
        if c.get("total_tokens") is not None and c["total_tokens"] != sum(
            _n(c.get(f)) for f in ("prompt_tokens", "output_tokens", "thinking_tokens", "tool_prompt_tokens"))]
    by_agent = {}
    for agent in sorted({c["agent"] for c in calls}):
        rows = [c for c in calls if c["agent"] == agent]
        by_agent[agent] = {"calls": len(rows), **{f: sum(_n(c.get(f)) for c in rows) for f in FIELDS}}
    totals = {f: sum(_n(c.get(f)) for c in calls) for f in FIELDS}
    region = "global" if location == "global" else "non-global"
    costs, unpriced = {}, []
    for i, c in enumerate(calls):
        key = price_key(c.get("model")) or price_key(model)
        if key is None:
            unpriced.append(i)
            continue
        where = c.get("endpoint") or location           # a fallback call is priced where it was answered
        for tier, table in PRICES[key].items():
            costs[tier] = costs.get(tier, 0.0) + call_cost(c, table["global" if where == "global" else "non-global"])
    complete = bool(calls) and observed == expected and not missing and not unpriced
    return {
        "model_calls": len(calls),
        "expected_calls": expected,
        "observed_calls": observed,
        "calls_missing_counts": missing,
        "calls_total_mismatch": total_mismatch,
        "calls_without_price": unpriced,
        "complete": complete,
        "by_agent": by_agent,
        "totals": totals,
        "region": region,
        "endpoints": sorted({c.get("endpoint") or location for c in calls}),
        "cost_usd": {tier: round(v, 6) for tier, v in costs.items()} if not unpriced else None,
        "price_source": PRICE_SOURCE,
        "price_checked_on": PRICE_CHECKED_ON,
        "price_note": PRICE_NOTE,
    }


def summary_lines(s: dict) -> list[str]:
    if s["model_calls"] == 0:
        return ["Token usage: none reported (the offline demo does not call a model)."]
    exp, obs = s["expected_calls"], s["observed_calls"]
    lines = [f"Model calls with usage: writer {obs['writer']}, reviewer {obs['reviewer']} "
             f"(expected writer {exp['writer']}, reviewer {exp['reviewer']}) — "
             f"{'complete' if s['complete'] else 'INCOMPLETE, the cost below is a lower bound'}"]
    t = s["totals"]
    lines.append(f"Tokens: input {t['prompt_tokens'] + t['tool_prompt_tokens']:,} (cached {t['cached_tokens']:,}), "
                 f"output {t['output_tokens']:,}, thinking {t['thinking_tokens']:,}")
    if s["cost_usd"] is None:
        lines.append("Cost: not computed (no list price recorded for this model).")
    else:
        parts = [f"${v:.4f} ({tier})" for tier, v in s["cost_usd"].items()]
        lines.append(f"Cost at list price, {s['region']} endpoint: " + "; ".join(parts))
    if s["calls_total_mismatch"]:
        lines.append(f"Note: reported total differs from the sum of parts in {len(s['calls_total_mismatch'])} call(s).")
    return lines
