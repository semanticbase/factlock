"""M4 — can every figure the reader sees be traced to a source, a date and a calculation?

Two levels:
* data: each cited fact has an as-of date and a source; a derived fact names its calculation and
  its inputs, and following the inputs ends at a publisher (or at the fund's own definition);
* reader: the page's own display code (web/format.js, run with Node.js) produces the margin note
  for each locked figure, and the note states that source, calculation and, when it differs from
  the month end, the date.
Names, labels and the report month are not figures; they are counted separately.
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import shutil
import subprocess

LEAF_SOURCES = {"fund_definition": "the fund's own definition", "report": "the report settings"}


def _iso(value: str) -> bool:
    try:
        dt.date.fromisoformat(str(value)[:10])
        return True
    except ValueError:
        return False


def trace(fact_id: str, facts: dict, sources: dict, seen: frozenset = frozenset()) -> tuple[list[str], set[str]]:
    """Problems found on the way, and the publishers (or leaf sources) the fact rests on."""
    fact = facts.get(fact_id)
    if fact is None:
        return [f"input {fact_id} does not exist"], set()
    if fact_id in seen:
        return [f"circular inputs at {fact_id}"], set()
    problems: list[str] = []
    leaves: set[str] = set()
    if not _iso(fact.get("as_of", "")):
        problems.append(f"{fact_id}: no valid as-of date")
    src = fact.get("source")
    if src == "derived":
        if not fact.get("derivation"):
            problems.append(f"{fact_id}: derived without a stated calculation")
        if not fact.get("inputs"):
            problems.append(f"{fact_id}: derived without inputs")
        for i in fact.get("inputs") or []:
            p, l = trace(i, facts, sources, seen | {fact_id})
            problems += p
            leaves |= l
    elif src in sources:
        s = sources[src]
        if not str(s.get("url", "")).startswith("https://"):
            problems.append(f"{fact_id}: source {src} has no https URL")
        if not s.get("publisher"):
            problems.append(f"{fact_id}: source {src} has no publisher")
        if not s.get("retrieved_at"):
            problems.append(f"{fact_id}: source {src} has no retrieval time")
        leaves.add(s.get("publisher", src))
    elif src in LEAF_SOURCES:
        leaves.add(src)
    else:
        problems.append(f"{fact_id}: unknown source {src}")
    return problems, leaves


def _render_notes(root: pathlib.Path, sheet: dict, items: list[dict], used: list[str]) -> list[dict] | None:
    node = shutil.which("node")
    if node is None:
        return None
    proc = subprocess.run([node, str(root / "measure" / "notes.js"), str(root / "web" / "format.js")],
                          input=json.dumps({"sheet": sheet, "items": items, "used": used}),
                          capture_output=True, text=True, timeout=60, check=True)
    return json.loads(proc.stdout)


def note_problems(fact: dict, note: dict, lang: str, sheet: dict, sources: dict) -> list[str]:
    """What a margin note must say for a figure: a value, the calculation or the publisher, and the date
    when it is not the month end."""
    problems = []
    body = note["body"]
    if not note["value"]:
        problems.append("note shows no value")
    src = fact.get("source")
    if src == "derived":
        want = (fact.get("derivation") or "")[1:] if lang == "en" else "算出"
        if not want or want not in body:
            problems.append("note does not state the calculation")
    elif src in sources:
        if not sources[src].get("publisher") or sources[src]["publisher"] not in body:
            problems.append("note does not name the publisher")
    elif src == "fund_definition":
        if ("fund definition" if lang == "en" else "ファンド定義") not in body.lower():
            problems.append("note does not say the figure comes from the fund definition")
    if fact.get("as_of") != sheet["period_end"] and not fact.get("discloses_as_of"):
        y, m, d = (int(x) for x in fact["as_of"][:10].split("-"))
        date_en = f"{dt.date(y, m, d):%B} {d}, {y}"
        if (date_en if lang == "en" else f"{y}年{m}月{d}日") not in body:
            problems.append("note does not state the as-of date")
    return problems


def check_provenance(result: dict, root: pathlib.Path) -> dict:
    sheet = result["fact_sheet"]
    facts, sources = sheet["facts"], {s["key"]: s for s in sheet["sources"]}
    tokens = [(s["ref"], lang, seg["fact_id"])
              for sec in result["commentary"] for s in sec["sentences"] for lang in ("en", "ja")
              for seg in s[lang] if seg.get("type") == "fact"]
    used = sorted({fid for _, _, fid in tokens})
    items = [{"id": fid, "lang": lang} for fid in used for lang in ("en", "ja")]
    notes = _render_notes(root, sheet, items, used)
    note_by = {(n["id"], n["lang"]): n for n in notes} if notes is not None else {}

    rows = []
    for ref, lang, fid in tokens:
        fact = facts.get(fid, {})
        data_problems, leaves = trace(fid, facts, sources)
        is_number = fact.get("unit") != "text"
        note = note_by.get((fid, lang))
        locked = note["locked"] if note is not None else is_number
        figure = is_number or locked
        reader_problems = note_problems(fact, note, lang, sheet, sources) if figure and note is not None else []
        rows.append({"ref": ref, "lang": lang, "fact_id": fid, "figure": figure, "locked": locked,
                     "leaves": sorted(leaves), "data_problems": data_problems,
                     "reader_problems": reader_problems if figure else [],
                     "note": note["body"] if note is not None and figure else None})
    figures = [r for r in rows if r["figure"]]
    return {
        "figure_tokens": len(figures),
        "figure_tokens_traced": sum(not r["data_problems"] for r in figures),
        "figure_tokens_note_complete": (sum(not r["reader_problems"] for r in figures) if notes is not None else None),
        "note_rendering": "web/format.js via Node.js" if notes is not None else "not run (Node.js not found)",
        "name_tokens": len(rows) - len(figures),
        "unique_figures": len({r["fact_id"] for r in figures}),
        "publishers_reached": sorted({l for r in figures for l in r["leaves"]}),
        "failures": [r for r in rows if r["data_problems"] or r["reader_problems"]],
        "rows": rows,
    }
