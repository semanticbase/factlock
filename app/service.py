"""Run the FactLock pipeline and shape the result for the web app."""
from __future__ import annotations

import time
import uuid

from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from app.agents import build_pipeline

APP_NAME = "factlock"


def usage_record(event) -> dict | None:
    """Token usage reported with one model response, as returned by the model (None if not reported).

    Every field is kept as reported (None stays None: "not reported" is not the same as zero), and the
    full usage object is saved under "raw" so that later analysis never depends on this selection."""
    usage = getattr(event, "usage_metadata", None)
    if usage is None or getattr(event, "partial", False):
        return None

    def count(name: str) -> int | None:
        value = getattr(usage, name, None)
        return int(value) if value is not None else None

    traffic = getattr(usage, "traffic_type", None)
    return {
        "agent": event.author,
        "model": getattr(event, "model_version", None),
        "prompt_tokens": count("prompt_token_count"),
        "cached_tokens": count("cached_content_token_count"),
        "output_tokens": count("candidates_token_count"),
        "thinking_tokens": count("thoughts_token_count"),
        "tool_prompt_tokens": count("tool_use_prompt_token_count"),
        "total_tokens": count("total_token_count"),
        "traffic_type": getattr(traffic, "value", traffic),
        "endpoint": (getattr(event, "custom_metadata", None) or {}).get("endpoint"),
        "raw": usage.model_dump(mode="json", exclude_none=True),
    }


async def run_pipeline(*, writer_model, reviewer_model, data_dir: str, fund_id: str, month: str,
                       max_iterations: int = 3) -> dict:
    started = time.perf_counter()
    agent = build_pipeline(writer_model, reviewer_model, data_dir, max_iterations)
    sessions = InMemorySessionService()
    runner = Runner(app_name=APP_NAME, agent=agent, session_service=sessions)
    session = await sessions.create_session(
        app_name=APP_NAME, user_id="reporting-team", session_id=str(uuid.uuid4()),
        state={"fund_id": fund_id, "month": month})
    timeline, usage = [], []
    message = types.Content(role="user", parts=[types.Part(text=f"Draft the {month} commentary for {fund_id}.")])
    async for event in runner.run_async(user_id="reporting-team", session_id=session.id, new_message=message):
        t_ms = round((time.perf_counter() - started) * 1000)
        timeline.append({"agent": event.author, "t_ms": t_ms})
        record = usage_record(event)
        if record is not None:
            usage.append({**record, "t_ms": t_ms})
    final = await sessions.get_session(app_name=APP_NAME, user_id="reporting-team", session_id=session.id)
    state = final.state
    return {
        "status": state.get("status"),
        "iterations": state.get("iteration"),
        "commentary": state.get("rendered") or [],
        "report": state.get("report"),
        "history": state.get("history") or [],
        "fact_sheet": state.get("fact_sheet"),
        "timeline": _compress(timeline),
        "usage": usage,
        "elapsed_ms": round((time.perf_counter() - started) * 1000),
    }


def _compress(timeline: list[dict]) -> list[dict]:
    """Collapse consecutive events from the same agent into one step."""
    steps: list[dict] = []
    for item in timeline:
        if steps and steps[-1]["agent"] == item["agent"]:
            steps[-1]["t_ms"] = item["t_ms"]
        else:
            steps.append(dict(item))
    return steps
