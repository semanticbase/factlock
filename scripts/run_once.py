"""Deployment step 2: run the whole FactLock pipeline once with Gemini.

Run in Cloud Shell from the repository folder:
    FACTLOCK_MODEL=gemini-3.8-flash python3 scripts/run_once.py

The full result, including every raw draft from the model and the token usage
reported with each model response, is saved to run_once_result.json so that it
can be reviewed before deploying. The script also prints the tokens used and
their cost at Google's list price (measure/cost.py).
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import sys
import warnings

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
warnings.filterwarnings("ignore", category=DeprecationWarning)

from app.llm import build_models  # noqa: E402
from app.service import run_pipeline  # noqa: E402
from measure.cost import summarize as usage_summary, summary_lines as usage_lines  # noqa: E402
from scripts.check_gemini import detect_project  # noqa: E402

RESULT_FILE = pathlib.Path("run_once_result.json")


def summarize(result: dict) -> list[str]:
    lines = [f"Status: {result['status']} after {result['iterations']} draft(s), {result['elapsed_ms']} ms."]
    for entry in result["history"]:
        codes = [e["code"] for e in entry["errors"]]
        lines.append(f"  Draft {entry['iteration']}: {'passed' if entry['passed'] else 'sent back'}"
                     f"; reviewer {entry['reviewer']}; errors {codes or 'none'};"
                     f" warnings {[w['code'] for w in entry['warnings']] or 'none'}")
    return lines


def main(models=None, month: str = "2026-08") -> int:
    model_name = os.environ.get("FACTLOCK_MODEL", "gemini-3.8-flash")
    location = os.environ.get("FACTLOCK_LOCATION", "global")
    if models is None:
        project = detect_project()
        if not project:
            print("NG: No Google Cloud project is selected. Run: gcloud config set project YOUR_PROJECT_ID")
            return 2
        models = build_models("gemini", model=model_name,
                              reviewer_model=os.environ.get("FACTLOCK_REVIEWER_MODEL"), project=project,
                              location=location, fallback_location=(lambda f: None if f.lower() in ("", "none") else f)(
                                  os.environ.get("FACTLOCK_FALLBACK_LOCATION", "asia-northeast1").strip()))
    writer, reviewer = models
    try:
        result = asyncio.run(run_pipeline(writer_model=writer, reviewer_model=reviewer,
                                          data_dir=str(ROOT / "data" / "snapshots"), fund_id="DGBF", month=month))
    except Exception as exc:  # noqa: BLE001 - report and keep a record
        RESULT_FILE.write_text(json.dumps({"error": f"{type(exc).__name__}: {exc}"}, indent=2), encoding="utf-8")
        print(f"NG: the pipeline failed: {type(exc).__name__}: {str(exc)[:300]}")
        return 1
    RESULT_FILE.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n".join(summarize(result)))
    try:
        print("\n".join(usage_lines(usage_summary(result, model_name, location))))
    except Exception as exc:  # noqa: BLE001 - the result is already saved; never lose it over a summary
        print(f"Token usage summary failed ({type(exc).__name__}: {exc}); the raw usage is in the saved file.")
    print(f"Full record saved to {RESULT_FILE}.")
    return 0 if result["status"] == "passed" else 3


if __name__ == "__main__":
    sys.exit(main())
