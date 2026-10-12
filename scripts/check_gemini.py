"""Deployment step 1: check that this Google Cloud project can call Gemini.

Run in Cloud Shell from the repository folder:
    python3 scripts/check_gemini.py

It tries the candidate models in order, saves every raw response to
check_gemini_result.json immediately, and prints the model to use.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import subprocess
import sys
import time

LOCATION = os.environ.get("FACTLOCK_LOCATION", "global")
RESULT_FILE = pathlib.Path("check_gemini_result.json")
PROMPT = 'Return JSON only, exactly in this shape: {"ok": true, "sentence": "<one short English sentence>"}'


def candidates() -> list[str]:
    preferred = os.environ.get("FACTLOCK_MODEL")
    models = ([preferred] if preferred else []) + ["gemini-3.8-flash", "gemini-3.5-flash"]
    return list(dict.fromkeys(models))


def detect_project() -> str | None:
    if os.environ.get("GOOGLE_CLOUD_PROJECT"):
        return os.environ["GOOGLE_CLOUD_PROJECT"]
    try:
        out = subprocess.run(["gcloud", "config", "get-value", "project"], capture_output=True, text=True, timeout=30)
        value = out.stdout.strip()
        return value if value and value != "(unset)" else None
    except (OSError, subprocess.SubprocessError):
        return None


def classify(exc: Exception) -> tuple[str, str]:
    from google.auth.exceptions import DefaultCredentialsError
    from google.genai import errors

    if isinstance(exc, DefaultCredentialsError):
        return "credentials", "No Google credentials found. Run this in Cloud Shell, or run: gcloud auth application-default login"
    if isinstance(exc, errors.ClientError):
        if exc.code == 404:
            return "model_not_found", "This model name is not available here; trying the next candidate."
        if exc.code == 403:
            return "permission", ("Permission denied. Enable the API with: gcloud services enable aiplatform.googleapis.com "
                                  "and check that your account has the Vertex AI User role.")
        if exc.code == 429:
            return "quota", "Quota or rate limit reached. Wait a minute and run the check again."
        return "client_error", "The request was rejected. Send the saved result file to the developer."
    if isinstance(exc, errors.ServerError):
        return "server_error", "Temporary error on the Google side. Wait a minute and run the check again."
    return "unexpected", "Unexpected error. Send the saved result file to the developer."


def try_model(client, model: str) -> dict:
    from google.genai import types

    started = time.perf_counter()
    try:
        response = client.models.generate_content(
            model=model, contents=PROMPT,
            config=types.GenerateContentConfig(temperature=0, response_mime_type="application/json"))
        text = response.text or ""
        try:
            json.loads(text)
            json_ok = True
        except ValueError:
            json_ok = False
        return {"model": model, "ok": True, "json_ok": json_ok, "raw": text[:1000],
                "ms": round((time.perf_counter() - started) * 1000)}
    except Exception as exc:  # noqa: BLE001 - every failure is reported, not raised
        kind, advice = classify(exc)
        return {"model": model, "ok": False, "error_kind": kind, "advice": advice,
                "error": f"{type(exc).__name__}: {str(exc)[:500]}"}


def default_client(project: str, location: str):
    from google import genai
    from google.genai import types
    return genai.Client(enterprise=True, project=project, location=location,
                        http_options=types.HttpOptions(retry_options=types.HttpRetryOptions(
                            attempts=3, initial_delay=1.0, max_delay=8.0,
                            http_status_codes=[429, 500, 502, 503, 504])))


def save(record: dict) -> None:
    RESULT_FILE.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")


def main(client_factory=default_client, project: str | None = None) -> int:
    project = project or detect_project()
    record = {"checked_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
              "project": project, "location": LOCATION, "attempts": []}
    if not project:
        record["result"] = "no_project"
        save(record)
        print("NG: No Google Cloud project is selected. Run: gcloud config set project YOUR_PROJECT_ID")
        return 2
    try:
        client = client_factory(project, LOCATION)
    except Exception as exc:  # noqa: BLE001
        kind, advice = classify(exc)
        record.update(result=kind, error=f"{type(exc).__name__}: {exc}")
        save(record)
        print(f"NG: {advice}")
        return 1
    for model in candidates():
        attempt = try_model(client, model)
        record["attempts"].append(attempt)
        save(record)  # keep the raw result even if a later step fails
        if attempt["ok"]:
            record["result"] = "ok"
            record["model"] = model
            save(record)
            note = "" if attempt["json_ok"] else " (the reply was not plain JSON; FactLock tolerates this)"
            print(f"OK: {model} answered in {attempt['ms']} ms{note}.")
            print(f"Next step: use FACTLOCK_MODEL={model} when deploying.")
            return 0
        print(f"NG: {model}: {attempt['advice']}")
        if attempt["error_kind"] != "model_not_found":
            break
    record["result"] = "failed"
    save(record)
    print(f"No model worked. The details are saved in {RESULT_FILE}.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
