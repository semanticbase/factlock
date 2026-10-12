"""Tolerant parsing of model output.

Models sometimes wrap JSON in code fences or add a sentence around it. We
accept that, but anything that still does not match the schema becomes an
issue that is sent back to the writer, instead of crashing the run.
"""
from __future__ import annotations

import json
import re

from pydantic import ValidationError

from app.models import Draft, Issue

# Reviewer findings of these types send the draft back; "other" is shown to a person as a warning.
REVIEW_TYPES_ERROR = {"unsupported_cause", "unsupported_comparison", "magnitude", "bilingual_meaning",
                      "wrong_figure", "compliance"}


def extract_json(text: str) -> dict:
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I)
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object found")
    return json.loads(cleaned[start:end + 1])


def parse_draft(text: str | None) -> tuple[Draft | None, list[Issue]]:
    if not text or not str(text).strip():
        return None, [Issue(code="invalid_output", severity="error", origin="format",
                            message="The writer returned an empty response.")]
    try:
        data = extract_json(str(text))
        return Draft.model_validate(data), []
    except (ValueError, json.JSONDecodeError) as exc:
        return None, [Issue(code="invalid_output", severity="error", origin="format",
                            message=f"The writer's output is not valid JSON ({exc}). Return JSON only.")]
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(p) for p in first.get("loc", []))
        return None, [Issue(code="invalid_output", severity="error", origin="format",
                            message=f"The writer's JSON does not match the schema at '{where}': {first.get('msg')}.")]


def parse_review(text: str | None, valid_refs: set[str]) -> tuple[list[Issue], bool]:
    """Returns (issues, skipped)."""
    if not text or not str(text).strip():
        return [Issue(code="review_unavailable", severity="warning", origin="reviewer",
                      message="The reviewer returned an empty response; human review recommended.")], False
    try:
        data = extract_json(str(text))
    except (ValueError, json.JSONDecodeError):
        return [Issue(code="review_unavailable", severity="warning", origin="reviewer",
                      message="The reviewer's output could not be read; human review recommended.")], False
    if data.get("skipped"):
        return [], True
    issues = []
    for item in data.get("issues") or []:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("type", "other"))
        ref = str(item.get("ref", "")) or None
        if ref and ref not in valid_refs:
            ref = None
        issues.append(Issue(
            code=f"review_{kind}", severity="error" if kind in REVIEW_TYPES_ERROR else "warning",
            origin="reviewer", ref=ref, lang="both",
            message=str(item.get("explanation", "")).strip() or kind))
    return issues, False
