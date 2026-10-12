"""Render fact placeholders into text.

The writer never types numbers. It writes placeholders such as
{{fund.ret_1m|verb}}, and this module turns them into "rose 2.17%" or
"2.17%の上昇" from the fact's value. Direction words produced by the |verb
format therefore always agree with the data.
"""
from __future__ import annotations

import re

from app.models import FORMATS_BY_UNIT, Fact, Issue

PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z0-9_.\-]+)\s*(?:\|\s*([A-Za-z_]+)\s*)?\}\}")


def _num(value: float, decimals: int) -> str:
    return f"{abs(value):.{decimals}f}"


def _is_zero(value: float, decimals: int) -> bool:
    return float(f"{abs(value):.{decimals}f}") == 0.0


def format_fact(fact: Fact, fmt: str, lang: str) -> str:
    """Format one fact. Raises ValueError for a format the unit does not allow."""
    if fmt not in FORMATS_BY_UNIT[fact.unit]:
        raise ValueError(f"format '{fmt}' is not allowed for unit '{fact.unit}'")
    if fact.unit == "text":
        return fact.text_en if lang == "en" else fact.text_ja
    v, d = fact.value, fact.decimals
    n = _num(v, d)
    sign = "+" if v > 0 and not _is_zero(v, d) else ("-" if v < 0 and not _is_zero(v, d) else "")
    up = v > 0 and not _is_zero(v, d)
    down = v < 0 and not _is_zero(v, d)

    if fact.unit in ("pct", "pct_pa", "weight"):
        if fmt == "value":
            return f"{'-' if v < 0 and not _is_zero(v, d) else ''}{n}%"
        if fmt == "signed":
            return f"{sign}{n}%"
        if fmt == "abs":
            return f"{n}%"
        if lang == "en":
            return f"rose {n}%" if up else (f"fell {n}%" if down else "was unchanged")
        return f"{n}%の上昇" if up else (f"{n}%の{fact.down_word_ja}" if down else "横ばい")

    if fact.unit == "contrib":
        unit_en, unit_ja = " pp", "%"
        if fmt in ("value", "signed"):
            return f"{sign}{n}{unit_en}" if lang == "en" else f"{sign}{n}{unit_ja}"
        if fmt == "abs":
            return f"{n}{unit_en}" if lang == "en" else f"{n}{unit_ja}"
        if lang == "en":
            return (f"added {n} percentage points" if up else
                    (f"subtracted {n} percentage points" if down else "had no effect"))
        return f"{n}%のプラス寄与" if up else (f"{n}%のマイナス寄与" if down else "寄与なし")

    if fact.unit == "pp":
        if fmt in ("value", "signed"):
            return f"{sign}{n} pp" if lang == "en" else f"{sign}{n}%ポイント"
        if fmt == "abs":
            return f"{n} pp" if lang == "en" else f"{n}%ポイント"
        if lang == "en":
            return (f"rose {n} percentage points" if up else
                    (f"fell {n} percentage points" if down else "was unchanged"))
        return f"{n}%ポイントの上昇" if up else (f"{n}%ポイントの{fact.down_word_ja}" if down else "横ばい")

    if fact.unit == "pt":
        if fmt in ("value", "signed"):
            return f"{sign}{n} pt" if lang == "en" else f"{sign}{n}ポイント"
        if fmt == "abs":
            return f"{n} pt" if lang == "en" else f"{n}ポイント"
        if lang == "en":
            return (f"rose {n} points" if up else (f"fell {n} points" if down else "was unchanged"))
        return f"{n}ポイントの上昇" if up else (f"{n}ポイントの{fact.down_word_ja}" if down else "横ばい")

    if fact.unit == "index":
        return f"{v:.{d}f}"
    raise ValueError(f"unsupported unit {fact.unit}")


def render_text(text: str, facts: dict[str, Fact], lang: str, ref: str | None = None
                ) -> tuple[list[dict], list[Issue]]:
    """Split text into plain and fact segments. Unknown facts or formats become issues."""
    segments: list[dict] = []
    issues: list[Issue] = []
    pos = 0
    for match in PLACEHOLDER_RE.finditer(text):
        if match.start() > pos:
            segments.append({"type": "text", "text": text[pos:match.start()]})
        fact_id, fmt = match.group(1), (match.group(2) or "").lower()
        fact = facts.get(fact_id)
        if fact is None:
            issues.append(Issue(code="unknown_fact", severity="error", ref=ref, lang=lang,
                                message=f"Placeholder refers to a fact that does not exist: {fact_id}"))
            segments.append({"type": "error", "text": match.group(0)})
        else:
            fmt = fmt or FORMATS_BY_UNIT[fact.unit][0]
            try:
                segments.append({"type": "fact", "fact_id": fact_id, "format": fmt,
                                 "text": format_fact(fact, fmt, lang)})
            except ValueError as exc:
                issues.append(Issue(code="bad_format", severity="error", ref=ref, lang=lang,
                                    message=f"{fact_id}: {exc}"))
                segments.append({"type": "error", "text": match.group(0)})
        pos = match.end()
    if pos < len(text):
        segments.append({"type": "text", "text": text[pos:]})
    return segments, issues


def segments_to_text(segments: list[dict]) -> str:
    return "".join(s["text"] for s in segments)


def authored_text(text: str) -> str:
    """The part of a sentence written by the model (placeholders removed)."""
    return PLACEHOLDER_RE.sub(" ", text)


def fact_ids_in(text: str) -> list[str]:
    return [m.group(1) for m in PLACEHOLDER_RE.finditer(text)]
