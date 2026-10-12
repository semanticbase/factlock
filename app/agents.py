"""FactLock as a multi-agent pipeline on the Agent Development Kit (ADK).

  fact_builder ─┐
                └─ revise_loop (max N rounds) ─────────────────────────┐
                     writer   (Gemini)  drafts EN + JA with placeholders│
                     checker  (rules)   renders numbers, runs checks    │
                     reviewer (Gemini)  finds unsupported claims        │
                     gate     (rules)   passes, or sends feedback back  │
  finalizer ────────────────────────────────────────────────────────────┘

Only the writer and reviewer use a language model. Everything that decides
whether a number is right is deterministic.
"""
from __future__ import annotations

import datetime as dt
from typing import AsyncGenerator, Optional

from google.adk.agents import BaseAgent, LlmAgent, LoopAgent, SequentialAgent
from google.adk.agents.callback_context import CallbackContext
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event, EventActions
from google.genai import types

from app.facts import build_fact_sheet, load_snapshot
from app.models import Draft, FactSheet, Issue, Report
from app.parse import parse_draft, parse_review
from app.prompts import reviewer_instruction, writer_instruction
from app.verify import check_draft, count_by_code


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds")


def _sheet(state) -> FactSheet:
    return FactSheet.model_validate(state["fact_sheet"])


class FactBuilderAgent(BaseAgent):
    """Loads the public-data snapshot and computes every fact (no model)."""

    data_dir: str

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        import pathlib
        state = ctx.session.state
        snapshot = load_snapshot(pathlib.Path(self.data_dir), state["fund_id"], state["month"])
        sheet = build_fact_sheet(snapshot)
        yield Event(author=self.name, invocation_id=ctx.invocation_id, actions=EventActions(state_delta={
            "fact_sheet": sheet.model_dump(), "iteration": 0, "history": [], "feedback": [],
            "draft_raw": "", "status": "drafting", "started_at": _now()}))


def _writer_instruction(ctx) -> str:
    state = ctx.state
    return writer_instruction(_sheet(state), state.get("feedback") or [], state.get("previous_draft"))


def _reviewer_instruction(ctx) -> str:
    state = ctx.state
    return reviewer_instruction(_sheet(state), state.get("rendered") or [])


def _skip_reviewer_on_rule_errors(callback_context: CallbackContext) -> Optional[types.Content]:
    """No need to spend a model call when the rules already rejected the draft."""
    if callback_context.state.get("rule_errors"):
        return types.Content(role="model", parts=[types.Part(text='{"skipped": true}')])
    return None


class RuleCheckAgent(BaseAgent):
    """Parses the draft, renders every placeholder, and runs the rule checks (no model)."""

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        state = ctx.session.state
        sheet = _sheet(state)
        draft, issues = parse_draft(state.get("draft_raw"))
        rendered: list[dict] = []
        if draft is not None:
            rendered, rule_issues = check_draft(draft, sheet)
            issues += rule_issues
        errors = [i for i in issues if i.severity == "error"]
        yield Event(author=self.name, invocation_id=ctx.invocation_id, actions=EventActions(state_delta={
            "draft": draft.model_dump() if draft else None,
            "rendered": rendered,
            "rule_issues": [i.model_dump() for i in issues],
            "rule_errors": len(errors)}))


class GateAgent(BaseAgent):
    """Combines rule and reviewer findings. Passes the draft or sends feedback."""

    max_iterations: int = 3

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        state = ctx.session.state
        iteration = int(state.get("iteration", 0)) + 1
        rule_issues = [Issue.model_validate(i) for i in state.get("rule_issues") or []]
        refs = {s["ref"] for sec in state.get("rendered") or [] for s in sec["sentences"]}
        review_issues, review_skipped = parse_review(state.get("review_raw"), refs)
        issues = rule_issues + review_issues
        errors = [i for i in issues if i.severity == "error"]
        warnings = [i for i in issues if i.severity == "warning"]
        report = Report(passed=not errors, errors=errors, warnings=warnings, rule_counts=count_by_code(issues))
        entry = {
            "iteration": iteration, "at": _now(), "passed": report.passed,
            "reviewer": "skipped (rules already failed)" if review_skipped else "ran",
            "errors": [i.model_dump() for i in errors], "warnings": [i.model_dump() for i in warnings],
            "draft_raw": state.get("draft_raw") or "",
        }
        delta = {
            "iteration": iteration,
            "report": report.model_dump(),
            "history": list(state.get("history") or []) + [entry],
            "feedback": [i.model_dump() for i in errors],
            "previous_draft": state.get("draft_raw") or "",
            "status": "passed" if report.passed else (
                "needs_review" if iteration >= self.max_iterations else "revising"),
        }
        yield Event(author=self.name, invocation_id=ctx.invocation_id,
                    actions=EventActions(state_delta=delta, escalate=report.passed))


class FinalizerAgent(BaseAgent):
    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        state = ctx.session.state
        status = state.get("status")
        if status not in ("passed", "needs_review"):
            status = "needs_review"
        yield Event(author=self.name, invocation_id=ctx.invocation_id,
                    actions=EventActions(state_delta={"status": status, "finished_at": _now()}))


WRITER_DESCRIPTION = "Drafts the bilingual commentary using fact placeholders only."
REVIEWER_DESCRIPTION = "Finds unsupported causes, comparisons and bilingual differences."


def _json_output() -> types.GenerateContentConfig:
    return types.GenerateContentConfig(temperature=0.2, response_mime_type="application/json")


def make_writer(model, instruction=_writer_instruction, description: str = WRITER_DESCRIPTION) -> LlmAgent:
    """The drafting agent. Measurement code builds its comparison writer here too, so both share one
    configuration (model settings, identity, how the instruction is passed)."""
    return LlmAgent(
        name="writer", model=model, instruction=instruction, include_contents="none",
        output_key="draft_raw", generate_content_config=_json_output(), description=description)


def make_reviewer(model) -> LlmAgent:
    return LlmAgent(
        name="reviewer", model=model, instruction=_reviewer_instruction, include_contents="none",
        output_key="review_raw", generate_content_config=_json_output(),
        before_agent_callback=_skip_reviewer_on_rule_errors, description=REVIEWER_DESCRIPTION)


def build_pipeline(writer_model, reviewer_model, data_dir: str, max_iterations: int = 3) -> SequentialAgent:
    writer = make_writer(writer_model)
    reviewer = make_reviewer(reviewer_model)
    loop = LoopAgent(name="revise_loop", max_iterations=max_iterations, sub_agents=[
        writer, RuleCheckAgent(name="checker"), reviewer, GateAgent(name="gate", max_iterations=max_iterations)])
    return SequentialAgent(name="factlock", sub_agents=[
        FactBuilderAgent(name="fact_builder", data_dir=data_dir), loop, FinalizerAgent(name="finalizer")])


def draft_from_state(state) -> Optional[Draft]:
    raw = state.get("draft")
    return Draft.model_validate(raw) if raw else None

