"""Deployment step: check that this project can use Firestore as the ledger, before deploying with
FACTLOCK_LEDGER=firestore.

    gcloud config set project factlock-buildercup
    cd ~/v127/factlock && python3 scripts/check_firestore.py

It writes a probe run for a fund called PROBE, reads it back (positive control), checks that a run that
was never written is reported absent (negative control), records an approval, and deletes what it wrote.
Prints OK or NG with the reason in plain words. Exit code 0 on OK.
"""
from __future__ import annotations

import datetime as dt
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def project_id() -> str | None:
    if os.environ.get("GOOGLE_CLOUD_PROJECT"):
        return os.environ["GOOGLE_CLOUD_PROJECT"]
    try:
        import google.auth
        return google.auth.default()[1]
    except Exception:  # noqa: BLE001
        return None


def main() -> int:
    try:
        from google.cloud import firestore  # noqa: F401
    except ImportError:
        print("NG: the google-cloud-firestore library is not installed. Run: pip install -r requirements.txt")
        return 2
    project = project_id()
    if not project:
        print("NG: No Google Cloud project is selected. Run: gcloud config set project factlock-buildercup")
        return 2
    from app.ledger import FirestoreLedger
    try:
        ledger = FirestoreLedger(project=project)
        month = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m")
        # Positive control: what we write comes back.
        run_id = ledger.start_run("PROBE", month, "probe-user")
        ledger.finish_run(run_id, {"status": "passed", "iterations": 1, "commentary": [], "history": [], "probe": True})
        latest = ledger.latest_verified("PROBE", month)
        if not latest or latest.get("run_id") != run_id or not latest["result"].get("probe"):
            print(f"NG: the probe run was written but did not come back as the latest verified draft (got {latest!r}).")
            return 1
        if ledger.count_runs(3600, uid="probe-user") < 1:
            print("NG: the probe run is not counted in the last hour (the started_at query did not find it).")
            return 1
        approval = ledger.record_approval(run_id, "Probe Reviewer")
        if not approval or ledger.latest_verified("PROBE", month)["result"].get("approval") != approval:
            print("NG: the approval was not recorded on the run and the latest draft.")
            return 1
        # Negative control: something never written is reported absent, not invented.
        if ledger.latest_verified("PROBE", "1999-01") is not None or ledger.record_approval("never-written", "x") is not None:
            print("NG: a run that was never written was reported as present.")
            return 1
    except Exception as exc:  # noqa: BLE001 - say what to do, in plain words
        name = type(exc).__name__
        if "PermissionDenied" in name or "403" in str(exc):
            print(f"NG: Firestore refused ({name}). Give this account roles/datastore.user on the project, then retry.")
        elif "NotFound" in name or "does not exist" in str(exc):
            print(f"NG: no Firestore database in project {project} ({name}). Run: gcloud firestore databases create "
                  "--location=asia-northeast1 --type=firestore-native")
        else:
            print(f"NG: {name}: {str(exc)[:300]}")
        return 1
    finally:
        try:
            client = ledger.client  # type: ignore[name-defined]
            client.collection("runs").document(run_id).delete()  # type: ignore[name-defined]
            client.collection("latest").document(f"PROBE_{month}").delete()  # type: ignore[name-defined]
        except Exception:  # noqa: BLE001 - cleanup is best-effort
            pass
    print(f"OK: Firestore ledger works in project {project} (wrote, read back, counted, approved, and cleaned up).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
