"""Run one agent (the reviewer, or a comparison writer) exactly as the pipeline runs it.

The agent comes from the same factory as in production (app.agents.make_writer / make_reviewer),
runs under the same ADK Runner with the same user message, and its token usage is recorded with
the same function (app.service.usage_record).
"""
from __future__ import annotations

import time
import uuid

from google.adk.agents import BaseAgent, SequentialAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event, EventActions
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from app.service import APP_NAME, usage_record


class _SetState(BaseAgent):
    """Puts the given values into the session state before the agent under test runs."""

    values: dict = {}

    async def _run_async_impl(self, ctx: InvocationContext):
        yield Event(author=self.name, invocation_id=ctx.invocation_id,
                    actions=EventActions(state_delta=dict(self.values)))


async def run_agent(agent, state: dict, fund_id: str, month: str) -> dict:
    started = time.perf_counter()
    pipeline = SequentialAgent(name="measure", sub_agents=[_SetState(name="setup", values=state), agent])
    sessions = InMemorySessionService()
    runner = Runner(app_name=APP_NAME, agent=pipeline, session_service=sessions)
    session = await sessions.create_session(app_name=APP_NAME, user_id="reporting-team",
                                            session_id=str(uuid.uuid4()), state={"fund_id": fund_id, "month": month})
    message = types.Content(role="user", parts=[types.Part(text=f"Draft the {month} commentary for {fund_id}.")])
    usage = []
    async for event in runner.run_async(user_id="reporting-team", session_id=session.id, new_message=message):
        record = usage_record(event)
        if record is not None:
            usage.append({**record, "t_ms": round((time.perf_counter() - started) * 1000)})
    final = await sessions.get_session(app_name=APP_NAME, user_id="reporting-team", session_id=session.id)
    return {"state": dict(final.state), "usage": usage, "elapsed_ms": round((time.perf_counter() - started) * 1000)}
