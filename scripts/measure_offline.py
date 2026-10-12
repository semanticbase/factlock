"""Measurements that need no network and no model call.

  M3  every figure in the commentary recomputed independently from the snapshot (measure/independent.py),
      after planted-mistake controls show the check can see mistakes (measure/controls.py)
  M4  every figure traced to a source, a date and a calculation, and shown in its margin note
  M5  fault injection against the rule checks (measure/fault_catalog.json)
  M15 the data checks run before drafting, each recomputed independently
  M7  tokens and list-price cost, when the result comes from a model run

Run from the repository folder:
    python3 scripts/measure_offline.py                      # the offline demo draft (no model)
    python3 scripts/measure_offline.py --result run_once_result.json --label gemini-01
Writes measure_out/offline_<label>.json and prints a summary.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys
import warnings

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
warnings.filterwarnings("ignore", category=DeprecationWarning)

from measure import cost, datachecks, faults, provenance  # noqa: E402
from measure.controls import run_controls  # noqa: E402
from measure.independent import check_commentary  # noqa: E402

DATA = ROOT / "data" / "snapshots"


def demo_result(month: str = "2026-08") -> dict:
    from app.llm import build_models
    from app.service import run_pipeline
    writer, reviewer = build_models("demo", model="demo", reviewer_model=None, project=None, location="global")
    return asyncio.run(run_pipeline(writer_model=writer, reviewer_model=reviewer, data_dir=str(DATA),
                                    fund_id="DGBF", month=month))


def measure(result: dict, label: str) -> dict:
    sheet = result["fact_sheet"]
    snapshot_path = DATA / f"{sheet['fund_id'].lower()}_{sheet['month']}.json"
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    controls = run_controls(result["commentary"], snapshot) if label == "demo" else None
    return {
        "label": label,
        "status": result.get("status"),
        "iterations": result.get("iterations"),
        "snapshot": snapshot_path.name,
        "m3_controls": controls,
        "m3": check_commentary(result["commentary"], snapshot),
        "m4": provenance.check_provenance(result, ROOT),
        "m15": datachecks.check_datachecks(result, snapshot),
        "m7": cost.summarize(result, (result.get("engine") or {}).get("model")),
    }


def summary_lines(record: dict) -> list[str]:
    m3, m4, m15 = record["m3"], record["m4"], record["m15"]
    lines = [f"[{record['label']}] draft status: {record['status']} after {record['iterations']} draft(s)"]
    c = record.get("m3_controls")
    if c is not None:
        seen = sum(x["detected"] for x in c["controls"] if x["expect"] == "detected")
        want = sum(x["expect"] == "detected" for x in c["controls"])
        ok_neg = sum(not x["detected"] for x in c["controls"] if x["expect"] == "accepted")
        want_neg = sum(x["expect"] == "accepted" for x in c["controls"])
        lines.append(f"M3 controls: {seen}/{want} planted mistakes found; {ok_neg}/{want_neg} correct renderings accepted")
    lines.append(f"M3: numbers {m3['numbers_ok']}/{m3['numbers']} correct; text with figures "
                 f"{m3['text_with_figures_ok']}/{m3['text_with_figures']}; other text {m3['other_text_tokens_ok']}/"
                 f"{m3['other_text_tokens']}; digits between tokens {m3['digits_outside_tokens']}; "
                 f"unrendered {m3['unrendered_placeholders']}; not covered {len(m3['uncovered'])}")
    notes = m4["figure_tokens_note_complete"]
    lines.append(f"M4: figures traced {m4['figure_tokens_traced']}/{m4['figure_tokens']}; notes complete "
                 f"{notes if notes is not None else 'n/a'}/{m4['figure_tokens']} ({m4['note_rendering']})")
    lines.append(f"M15: {m15['checks']} checks {m15['by_status']}; recomputed and agreeing {m15['recomputed_agree']}/"
                 f"{m15['checks']}; verifications (identity excluded) {m15['verifications']}")
    lines += ["M7: " + line for line in cost.summary_lines(record["m7"])]
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--result", help="a result saved by scripts/run_once.py or the batch script")
    parser.add_argument("--label", default=None)
    parser.add_argument("--out", default="measure_out")
    parser.add_argument("--skip-faults", action="store_true")
    args = parser.parse_args(argv)
    if args.result:
        result = json.loads(pathlib.Path(args.result).read_text(encoding="utf-8"))
        label = args.label or pathlib.Path(args.result).stem
    else:
        result, label = demo_result(), "demo"
    record = measure(result, label)
    if not args.skip_faults:
        record["m5"] = faults.run(DATA)
    out_dir = pathlib.Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"offline_{label}.json"
    out.write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
    for line in summary_lines(record):
        print(line)
    if "m5" in record:
        f, l = record["m5"]["faults"], record["m5"]["legit"]
        for key in ("fatal", "caution"):
            t = f[key]
            print(f"M5 {key}: {t['cases']} cases — blocked by the intended rule {t['blocked_target']}, blocked by "
                  f"another rule {t['blocked_other']}, flagged {t['flagged']}, passed the rules {t['passed']}")
        print(f"M5 legitimate: {l['cases']} cases — wrongly blocked {l['false_rejections']}, "
              f"flagged {l['flagged_for_review']}, passed {l['passed']} (catalog md5 {record['m5']['catalog_md5']})")
    print(f"Saved {out}")
    controls_ok = record["m3_controls"] is None or record["m3_controls"]["all_as_expected"]
    return 0 if controls_ok else 1


if __name__ == "__main__":
    sys.exit(main())
