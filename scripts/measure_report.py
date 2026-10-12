"""Summarise a batch made by scripts/measure_batch.py. No model calls, no network.

    python3 scripts/measure_report.py                 # reads measure_out/batch
Writes measure_out/batch_report.json and prints a summary.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import warnings

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
warnings.filterwarnings("ignore", category=DeprecationWarning)

from measure.report import summarize, summary_lines  # noqa: E402


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--batch", default="measure_out/batch")
    p.add_argument("--out", default="measure_out/batch_report.json")
    p.add_argument("--snapshot", default=str(ROOT / "data" / "snapshots" / "dgbf_2026-08.json"))
    args = p.parse_args(argv)
    batch = pathlib.Path(args.batch)
    if not batch.exists():
        print(f"NG: {batch} does not exist. Run scripts/measure_batch.py first.")
        return 2
    snapshot = json.loads(pathlib.Path(args.snapshot).read_text(encoding="utf-8"))
    report = summarize(batch, snapshot, ROOT)
    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n".join(summary_lines(report)))
    print(f"Saved {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
