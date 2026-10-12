"""Batch measurements with Gemini, run in Cloud Shell.

  M1, M2, M8  the full FactLock pipeline, n times            -> measure_out/batch/m1/run_NNN.json
  M6          the comparison writer (ordinary use), n times   -> measure_out/batch/m6/run_NNN.json
  M6b         ordinary use with the published inputs only     -> measure_out/batch/m6b/run_NNN.json
  M5          the reviewer agent on every fault-catalog case  -> measure_out/batch/m5/<case>.json

FactLock and comparison runs alternate, so both conditions meet the same service conditions.
Every result is saved the moment it arrives. Run the same command again to continue after an
interruption: finished runs are skipped. The run stops by itself when the cost so far, at the
standard list price (the higher of the two), reaches --cap-usd, or after 3 failures in a row.

    python3 scripts/measure_batch.py --n 40
Then, anywhere (no model calls):
    python3 scripts/measure_report.py
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import pathlib
import sys
import time
import warnings

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
warnings.filterwarnings("ignore", category=DeprecationWarning)

from app.agents import make_writer  # noqa: E402
from app.facts import build_fact_sheet, load_snapshot  # noqa: E402
from app.llm import build_models, demo_draft  # noqa: E402
from app.service import run_pipeline  # noqa: E402
from measure import cost  # noqa: E402
from measure.agentrun import run_agent  # noqa: E402
from measure.baseline import BASELINE_DESCRIPTION, baseline_instruction, inputs_instruction  # noqa: E402
from measure.faults import load_catalog  # noqa: E402
from measure.reviewer_layer import review_case  # noqa: E402

DATA = ROOT / "data" / "snapshots"
FUND = "DGBF"
_fb = os.environ.get("FACTLOCK_FALLBACK_LOCATION", "none").strip()
FALLBACK = None if _fb.lower() in ("", "none") else _fb


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _save(path: pathlib.Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)  # never leave a half-written result behind


def _usage_of(record: dict) -> list[dict]:
    if "result" in record:
        return record["result"].get("usage") or []
    return record.get("usage") or []


def spent(out: pathlib.Path, model: str, location: str) -> dict:
    """Cost so far at both list prices, from every saved result (so a restart keeps counting)."""
    totals = {tier: 0.0 for tier in cost.PRICES[cost.price_key(model)]}
    calls = 0
    for path in out.rglob("*.json"):
        record = json.loads(path.read_text(encoding="utf-8"))
        for call in _usage_of(record):
            key = cost.price_key(call.get("model")) or cost.price_key(model)
            calls += 1
            where = call.get("endpoint") or location
            for tier, table in cost.PRICES[key].items():
                totals[tier] += cost.call_cost(call, table["global" if where == "global" else "non-global"])
    return {"calls": calls, **totals}


class Budget:
    def __init__(self, out, model, location, cap):
        self.out, self.model, self.location, self.cap = out, model, location, cap

    def status(self) -> tuple[bool, str]:
        s = spent(self.out, self.model, self.location)
        standard = s["standard, from 2027-01-01"]
        intro = s["introductory, to 2026-12-31"]
        text = f"spent ${intro:.2f} at the introductory price, ${standard:.2f} at the standard price ({s['calls']} calls)"
        return standard >= self.cap, text


async def m1_run(i, writer, reviewer, month):
    started = time.perf_counter()
    result = await run_pipeline(writer_model=writer, reviewer_model=reviewer, data_dir=str(DATA), fund_id=FUND, month=month)
    return {"run": i, "started_at": _now(), "wall_ms": round((time.perf_counter() - started) * 1000), "result": result}


async def m6_run(i, writer, sheet, month, snapshot=None):
    text = inputs_instruction(sheet, snapshot) if snapshot is not None else baseline_instruction(sheet)
    agent = make_writer(writer, instruction=lambda ctx: text, description=BASELINE_DESCRIPTION)
    run = await run_agent(agent, {}, FUND, month)
    return {"run": i, "started_at": _now(), "raw": run["state"].get("draft_raw") or "", "usage": run["usage"],
            "elapsed_ms": run["elapsed_ms"]}


def main(argv=None, models=None, baseline_writer=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--n", type=int, default=40)
    p.add_argument("--phases", default="m1,m6,m6b,m5")
    p.add_argument("--max-failures", type=int, default=6, help="stop after this many failures in a row")
    p.add_argument("--pause-after-failure", type=float, default=30.0,
                   help="seconds to wait after a failed call, to let a busy endpoint recover")
    p.add_argument("--out", default="measure_out/batch")
    p.add_argument("--cap-usd", type=float, default=15.0, help="stop when the standard-price cost reaches this")
    p.add_argument("--month", default="2026-08")
    args = p.parse_args(argv)
    model = os.environ.get("FACTLOCK_MODEL", "gemini-3.8-flash")
    location = os.environ.get("FACTLOCK_LOCATION", "global")
    if cost.price_key(model) is None:
        print(f"NG: no list price is recorded for {model}; the cost cap cannot work. Stopping.")
        return 2
    if models is None:
        from scripts.check_gemini import detect_project
        project = detect_project()
        if not project:
            print("NG: No Google Cloud project is selected. Run: gcloud config set project factlock-buildercup")
            return 2
        models = build_models("gemini", model=model, reviewer_model=os.environ.get("FACTLOCK_REVIEWER_MODEL"),
                              project=project, location=location, fallback_location=FALLBACK)
    # One event loop for the whole batch: the model client is created once and must not outlive its loop.
    return asyncio.run(_run_all(args, model, location, models, baseline_writer))


async def _run_all(args, model, location, models, baseline_writer) -> int:
    writer, reviewer = models
    baseline_writer = baseline_writer or writer
    out = pathlib.Path(args.out)
    phases = set(args.phases.split(","))
    budget = Budget(out, model, location, args.cap_usd)
    sheet = build_fact_sheet(load_snapshot(DATA, FUND, args.month))
    snapshot = json.loads((DATA / f"{FUND.lower()}_{args.month}.json").read_text(encoding="utf-8"))
    print(f"Model {model} ({location}{', falls back to ' + FALLBACK if FALLBACK and FALLBACK != location else ''}); "
          f"n = {args.n}; phases {sorted(phases)}; cap ${args.cap_usd:.2f} at the standard price.",
          flush=True)

    async def attempt(label, path, make):
        if path.exists():
            return "skipped"
        over, text = budget.status()
        if over:
            print(f"STOP: the cost cap is reached ({text}).", flush=True)
            return "cap"
        try:
            record = await make()
        except Exception as exc:  # noqa: BLE001 - record the failure and keep going
            _save(path.with_name(path.stem + ".error.json"),
                  {"at": _now(), "error": f"{type(exc).__name__}: {str(exc)[:500]}"})
            print(f"{label}: FAILED ({type(exc).__name__}: {str(exc)[:160]})", flush=True)
            return "failed"
        _save(path, record)
        err = path.with_name(path.stem + ".error.json")
        if err.exists():
            err.unlink()
        return record

    failures = 0
    for i in range(1, args.n + 1):
        for phase in ("m1", "m6", "m6b"):
            if phase not in phases:
                continue
            path = out / phase / f"run_{i:03d}.json"
            label = f"[{phase} {i:02d}/{args.n}]"
            if phase == "m1":
                make = (lambda i=i: m1_run(i, writer, reviewer, args.month))
            elif phase == "m6":
                make = (lambda i=i: m6_run(i, baseline_writer, sheet, args.month))
            else:
                make = (lambda i=i: m6_run(i, baseline_writer, sheet, args.month, snapshot))
            got = await attempt(label, path, make)
            if got == "cap":
                return 4
            if got == "failed":
                failures += 1
                if failures >= args.max_failures:
                    print(f"STOP: {failures} failures in a row. Run the same command again later to continue.", flush=True)
                    return 5
                await asyncio.sleep(args.pause_after_failure)   # let a busy endpoint recover before the next call
                continue
            failures = 0
            if got != "skipped":
                if phase == "m1":
                    r = got["result"]
                    reasons = sorted({e["code"] for h in r["history"] if not h["passed"] for e in h["errors"]})
                    why = f"; sent back for {', '.join(reasons)}" if reasons else ""
                    print(f"{label} {r['status']} after {r['iterations']} draft(s), {r['elapsed_ms'] / 1000:.1f} s{why}; "
                          f"{budget.status()[1]}", flush=True)
                else:
                    print(f"{label} comparison draft, {got['elapsed_ms'] / 1000:.1f} s; {budget.status()[1]}", flush=True)

    if "m5" in phases:
        catalog = load_catalog()
        base = demo_draft(set(sheet.facts))
        cases = catalog["faults"] + catalog["legit"]
        for k, case in enumerate(cases, start=1):
            path = out / "m5" / f"{case['id']}.json"
            label = f"[m5 {k:02d}/{len(cases)}] {case['id']}"
            got = await attempt(label, path, lambda case=case: review_case(case, base, sheet, reviewer, FUND, args.month))
            if got == "cap":
                return 4
            if got == "failed":
                failures += 1
                if failures >= args.max_failures:
                    print(f"STOP: {failures} failures in a row. Run the same command again later to continue.", flush=True)
                    return 5
                await asyncio.sleep(args.pause_after_failure)   # let a busy endpoint recover before the next call
                continue
            failures = 0
            if got != "skipped":
                n_issues = len((got.get("reviewer") or {}).get("issues") or [])
                print(f"{label}: reviewer raised {n_issues} issue(s); {budget.status()[1]}", flush=True)

    left = sorted(p.name for p in out.rglob("*.error.json"))
    print(f"Finished. {budget.status()[1]}.", flush=True)
    if left:
        print(f"{len(left)} run(s) failed and can be retried by running the same command again: {', '.join(left[:10])}")
        return 3
    print(f"All results are in {out}. Next: python3 scripts/measure_report.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
