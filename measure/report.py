"""Summarise a batch (measure_out/batch) into the approved measurements. No model calls.

Every rate is given with its denominator and a 95% Wilson interval; raw values are kept.
"""
from __future__ import annotations

import json
import math
import re
import pathlib
import statistics as st

from app.parse import parse_draft
from measure import cost, faults, provenance
from measure.baseline import evaluate, published_values, sentences_from_commentary, sentences_from_draft
from measure.controls import run_controls
from measure.independent import check_commentary
from measure.reviewer_layer import outcome


def wilson(k: int, n: int, z: float = 1.96) -> list[float] | None:
    if n == 0:
        return None
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return [round(max(0.0, c - h), 4), round(min(1.0, c + h), 4)]


def rate(k: int, n: int) -> dict:
    return {"k": k, "n": n, "share": round(k / n, 4) if n else None, "interval_95": wilson(k, n)}


def spread(values: list[float]) -> dict | None:
    if not values:
        return None
    out = {"n": len(values), "median": st.median(values), "min": min(values), "max": max(values),
           "mean": round(st.mean(values), 3)}
    if len(values) > 1:
        out["sd"] = round(st.stdev(values), 3)
        out["cv"] = round(st.stdev(values) / st.mean(values), 4) if st.mean(values) else None
    return out


def _load(folder: pathlib.Path) -> list[dict]:
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(folder.glob("*.json"))
            if not p.name.endswith(".error.json")]


def _steps(timeline: list[dict]) -> dict:
    """Seconds spent in each agent step (a step ends at its t_ms; the previous step's end is its start)."""
    out, prev = {}, 0
    for step in timeline:
        out[step["agent"]] = out.get(step["agent"], 0) + (step["t_ms"] - prev) / 1000
        prev = step["t_ms"]
    return out


# A polite sentence-final form (ます/です) followed by more of the same sentence, e.g. 「上昇しています一方、」.
# Continuations that are grammatical after ます are excluded (が, ので, し、, けれど).
POLITE_MID = re.compile(r"(ます|です)(?!。|」|）|$|が|ので|し、|けれど)")


def ja_polite_mid_sentence(commentary: list[dict]) -> list[dict]:
    """Where the Japanese text continues after a sentence-final polite form, and what produced it:
    a fact's rendered phrase (its fact_id) or the writer's own words ("writer")."""
    found = []
    for sec in commentary:
        for s in sec["sentences"]:
            segs = s["ja"]
            text = "".join(x["text"] for x in segs)
            for m in POLITE_MID.finditer(text):
                pos, acc, source = m.start(), 0, "writer"
                for x in segs:
                    if acc <= pos < acc + len(x["text"]):
                        source = x.get("fact_id") or "writer"
                        break
                    acc += len(x["text"])
                found.append({"ref": s["ref"], "source": source, "context": text[max(0, pos - 12):pos + 10]})
    return found


def summarize(batch: pathlib.Path, snapshot: dict, root: pathlib.Path) -> dict:
    m1 = _load(batch / "m1")
    m6 = _load(batch / "m6")
    m6b = _load(batch / "m6b")
    m5 = _load(batch / "m5")
    report: dict = {"runs": {"m1": len(m1), "m6": len(m6), "m6b": len(m6b), "m5": len(m5)},
                    "failed_files": sorted(p.name for p in batch.rglob("*.error.json"))}

    # ---- M1 time, M2 passes and send-backs, M7 cost -------------------------------------------
    results = [r["result"] for r in m1]
    elapsed = [r["elapsed_ms"] / 1000 for r in results]
    model_s, code_s = [], []
    for r in results:
        steps = _steps(r["timeline"])
        m = steps.get("writer", 0) + steps.get("reviewer", 0)
        model_s.append(round(m, 3))
        code_s.append(round(r["elapsed_ms"] / 1000 - m, 3))
    report["m1_time_seconds"] = {"total": spread(elapsed), "model": spread(model_s), "code": spread(code_s)}
    first = sum(1 for r in results if r["status"] == "passed" and r["iterations"] == 1)
    passed = sum(1 for r in results if r["status"] == "passed")
    sendbacks = []
    for i, r in enumerate(results, start=1):
        for h in r["history"]:
            if not h["passed"]:
                sendbacks.append({"run": i, "draft": h["iteration"],
                                  "reasons": [{"code": e["code"], "origin": e.get("origin"), "ref": e.get("ref"),
                                               "message": e["message"]} for e in h["errors"]]})
    codes: dict[str, int] = {}
    for s in sendbacks:
        for e in s["reasons"]:
            codes[e["code"]] = codes.get(e["code"], 0) + 1
    report["m2"] = {"first_draft_passed": rate(first, len(results)), "passed_in_the_end": rate(passed, len(results)),
                    "drafts_per_run": {str(k): sum(1 for r in results if r["iterations"] == k) for k in (1, 2, 3)},
                    "send_back_reasons": codes, "send_backs": sendbacks}
    usage = [cost.summarize(r) for r in results]
    per_draft = [u["cost_usd"] for u in usage if u["cost_usd"]]
    report["m7_cost_usd_per_draft"] = {tier: spread([c[tier] for c in per_draft]) for tier in (per_draft[0] if per_draft else {})}
    report["m7_usage_complete"] = rate(sum(u["complete"] for u in usage), len(usage))

    # ---- M3 and M4 on every Gemini draft -------------------------------------------------------
    m3 = [check_commentary(r["commentary"], snapshot) for r in results]
    controls = [run_controls(r["commentary"], snapshot) for r in results]
    report["m3"] = {
        "numbers": sum(x["numbers"] for x in m3), "numbers_correct": sum(x["numbers_ok"] for x in m3),
        "text_with_figures": sum(x["text_with_figures"] for x in m3),
        "text_with_figures_correct": sum(x["text_with_figures_ok"] for x in m3),
        "digits_between_tokens": sum(x["digits_outside_tokens"] for x in m3),
        "drafts_with_any_problem": rate(sum(1 for x in m3 if x["problems"]), len(m3)),
        "controls_as_expected": rate(sum(c["all_as_expected"] for c in controls), len(controls)),
        "controls_applied": sum(c["applicable"] for c in controls),
    }

    m4 = [provenance.check_provenance(r, root) for r in results]
    report["m4"] = {
        "figures": sum(x["figure_tokens"] for x in m4),
        "traced": sum(x["figure_tokens_traced"] for x in m4),
        "notes_complete": (sum(x["figure_tokens_note_complete"] for x in m4)
                           if m4 and all(x["figure_tokens_note_complete"] is not None for x in m4) else None),
        "note_rendering": m4[0]["note_rendering"] if m4 else None,
    }

    # ---- M8 stability -----------------------------------------------------------------------------
    chars_en = [sum(len("".join(x["text"] for x in s["en"])) for sec in r["commentary"] for s in sec["sentences"])
                for r in results]
    chars_ja = [sum(len("".join(x["text"] for x in s["ja"])) for sec in r["commentary"] for s in sec["sentences"])
                for r in results]
    structure: dict[str, int] = {}
    for r in results:
        key = " / ".join(f"{sec['key']} {len(sec['sentences'])}" for sec in r["commentary"])
        structure[key] = structure.get(key, 0) + 1
    warn_codes: dict[str, int] = {}
    for r in results:
        for w in (r.get("report") or {}).get("warnings") or []:
            warn_codes[w["code"]] = warn_codes.get(w["code"], 0) + 1
    grammar = [ja_polite_mid_sentence(r["commentary"]) for r in results]
    report["m14_auto"] = {
        "ja_polite_form_mid_sentence": sum(len(g) for g in grammar),
        "drafts_affected": rate(sum(1 for g in grammar if g), len(grammar)),
        "by_source": {k: sum(1 for g in grammar for x in g if x["source"] == k)
                      for k in sorted({x["source"] for g in grammar for x in g})},
        "examples": [x for g in grammar for x in g][:10],
    }
    report["m8"] = {"characters_en": spread(chars_en), "characters_ja": spread(chars_ja),
                    "numbers_per_draft": spread([x["numbers"] for x in m3]),
                    "sentence_structures": dict(sorted(structure.items(), key=lambda kv: -kv[1])),
                    "warnings_on_final_drafts": warn_codes}

    # ---- M6 FactLock against ordinary use, with one yardstick ------------------------------------
    sheet = results[0]["fact_sheet"] if results else None
    published = published_values(snapshot)   # the same allowance for every condition
    fl = [evaluate(sentences_from_commentary(r["commentary"]), r["fact_sheet"], published) for r in results]
    def side_eval(records):
        evals, invalid = [], 0
        for rec in records:
            draft, issues = parse_draft(rec["raw"])
            if draft is None:
                invalid += 1
                continue
            evals.append(evaluate(sentences_from_draft(draft), sheet, published))
        return evals, invalid
    base_eval, invalid = side_eval(m6)
    inputs_eval, inputs_invalid = side_eval(m6b)
    kinds = ("unsupported", "unit", "direction", "prohibited", "placeholder", "misattribution", "parity", "caution")

    def side(evals, n_total, extra_invalid=0):
        return {"drafts": n_total, "invalid_output": extra_invalid,
                "drafts_with_certain_error": rate(sum(1 for e in evals if e["certain_errors"]) + extra_invalid, n_total),
                "certain_errors_per_draft": spread([e["certain_errors"] for e in evals]),
                "numbers_per_draft": spread([e["numbers"] for e in evals]),
                "by_kind": {k: sum(e["counts"][k] for e in evals) for k in kinds},
                "for_human_review": [dict(run=i + 1, **{k: e["details"][k] for k in ("misattribution", "parity")})
                                     for i, e in enumerate(evals) if e["details"]["misattribution"] or e["details"]["parity"]]}
    intro = cost.PRICES["gemini-3.8-flash"]["introductory, to 2026-12-31"]["global"]
    report["m6"] = {"factlock": side(fl, len(fl)), "ordinary_use": side(base_eval, len(m6), invalid),
                    "ordinary_use_inputs_only": side(inputs_eval, len(m6b), inputs_invalid),
                    "ordinary_use_cost_usd": spread([sum(cost.call_cost(c, intro) for c in rec.get("usage") or []) for rec in m6]),
                    "ordinary_use_inputs_only_cost_usd": spread([sum(cost.call_cost(c, intro) for c in rec.get("usage") or []) for rec in m6b]),
                    "ordinary_use_inputs_only_seconds": spread([rec["elapsed_ms"] / 1000 for rec in m6b])}

    # ---- M5 both layers ------------------------------------------------------------------------------
    rows = []
    for rec in m5:
        o = outcome(rec)
        rows.append({"id": rec["id"], "family": rec["family"], "severity": rec["severity"],
                     "rules": rec["rules"], "reviewer_codes": [i["code"] for i in (rec.get("reviewer") or {}).get("issues") or []],
                     **o})

    def tally(sel):
        return {k: sum(1 for r in sel if r["system"] == k) for k in ("blocked by rules", "blocked by reviewer", "flagged", "passed")}
    fatal = [r for r in rows if r["severity"] == "fatal"]
    caution = [r for r in rows if r["severity"] == "caution"]
    legit = [r for r in rows if r["severity"] == "accept"]
    families = {}
    for fam in sorted({r["family"] for r in rows if r["family"] != "L"}):
        sel = [r for r in rows if r["family"] == fam]
        families[fam] = {"cases": len(sel), **tally(sel),
                         "reviewer_alone_blocked": sum(1 for r in sel if r["reviewer_alone"] == "blocked")}
    report["m5"] = {
        "catalog_md5": faults.catalog_md5(),
        "fatal": {"cases": len(fatal), **tally(fatal),
                  "stopped_before_reader": rate(sum(1 for r in fatal if r["system"].startswith("blocked")), len(fatal)),
                  "stopped_or_flagged": rate(sum(1 for r in fatal if r["system"] != "passed"), len(fatal))},
        "caution": {"cases": len(caution), **tally(caution)},
        "legit": {"cases": len(legit), **tally(legit),
                  "wrongly_blocked": rate(sum(1 for r in legit if r["system"].startswith("blocked")), len(legit)),
                  "reviewer_wrongly_blocked": rate(sum(1 for r in legit if r["reviewer_alone"] == "blocked"), len(legit))},
        "by_family": families, "cases": rows}
    return report


def summary_lines(r: dict) -> list[str]:
    def pct(x):
        return f"{x['k']}/{x['n']} ({x['share']:.0%}, 95% {x['interval_95'][0]:.0%}-{x['interval_95'][1]:.0%})" if x["n"] else "n/a"
    lines = [f"Runs: FactLock {r['runs']['m1']}, ordinary use {r['runs']['m6']}, inputs only {r['runs']['m6b']}, fault cases {r['runs']['m5']}; "
             f"failed files {len(r['failed_files'])}"]
    t = r["m1_time_seconds"]["total"]
    if t:
        lines.append(f"M1 time per draft: median {t['median']:.1f} s (min {t['min']:.1f}, max {t['max']:.1f}); "
                     f"model median {r['m1_time_seconds']['model']['median']:.1f} s, code median {r['m1_time_seconds']['code']['median']:.2f} s")
        lines.append(f"M2 first draft passed: {pct(r['m2']['first_draft_passed'])}; passed in the end: {pct(r['m2']['passed_in_the_end'])}; "
                     f"send-back reasons {r['m2']['send_back_reasons'] or 'none'}")
        for tier, s in r["m7_cost_usd_per_draft"].items():
            lines.append(f"M7 cost per draft ({tier}): median ${s['median']:.4f}, max ${s['max']:.4f}")
        m3 = r["m3"]
        lines.append(f"M3 numbers correct {m3['numbers_correct']}/{m3['numbers']}; figures in phrases "
                     f"{m3['text_with_figures_correct']}/{m3['text_with_figures']}; drafts with any problem "
                     f"{pct(m3['drafts_with_any_problem'])}; controls as expected {pct(m3['controls_as_expected'])}")
        m4 = r["m4"]
        lines.append(f"M4 figures traced {m4['traced']}/{m4['figures']}; notes complete "
                     f"{m4['notes_complete'] if m4['notes_complete'] is not None else 'n/a'}/{m4['figures']} ({m4['note_rendering']})")
    for name, side in (("FactLock", r["m6"]["factlock"]), ("ordinary use", r["m6"]["ordinary_use"]),
                       ("ordinary use, inputs only", r["m6"]["ordinary_use_inputs_only"])):
        if side["drafts"]:
            lines.append(f"M6 {name}: drafts with a certain error {pct(side['drafts_with_certain_error'])}; "
                         f"by kind {side['by_kind']}; invalid {side['invalid_output']}")
    m5 = r["m5"]
    if m5["cases"]:
        f = m5["fatal"]
        lines.append(f"M5 fatal {f['cases']}: rules {f['blocked by rules']}, reviewer {f['blocked by reviewer']}, "
                     f"flagged {f['flagged']}, passed {f['passed']}; stopped before the reader {pct(f['stopped_before_reader'])}")
        lg = m5["legit"]
        lines.append(f"M5 legitimate {lg['cases']}: wrongly blocked {pct(lg['wrongly_blocked'])} "
                     f"(reviewer alone {pct(lg['reviewer_wrongly_blocked'])})")
    return lines
