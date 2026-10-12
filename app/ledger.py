"""The audit ledger: every drafting run, its verdict, its approval, and the latest verified draft.

Two implementations with one interface:

* MemoryLedger    — in the process only (tests, local previews, and the default when no ledger is set).
* FirestoreLedger — Firestore in Native mode, written and read by the server only. The browser never
                    talks to Firestore (the security rules deny all client access; see firestore.rules).

Why a ledger:
* A commentary that "passed the checks" should leave a record: which draft, which checks, who approved
  it and when. That record is the product's claim, so it lives outside the process.
* The latest verified draft survives restarts. Cloud Run scales the demo to zero between visits; before
  the ledger the latest draft lived in a dict and vanished with the instance.
* When Gemini is busy (429), the latest verified draft can be served instead of an error, clearly
  labelled as such (see app.main.generate).
* Usage limits are counted here, so they hold across instances.

Documents (Firestore):
  runs/{run_id}            started_at (timestamp), started_iso, uid, fund_id, month, status
                           ("running" | "passed" | "needs_review" | "failed"), iterations, elapsed_ms,
                           model, result_json (the whole pipeline result as one JSON string, on completion),
                           approval {name, at} (when recorded)
  latest/{FUND}_{month}    run_id, at, result_json, approval — the most recent run that passed the checks

The pipeline result is stored as one JSON string, not as a nested map. Firestore cannot hold an array
inside an array, indexes every field of a map (40,000 index entries per document at most), and quotes
field names with periods; a 60 KB result with ~1,800 fields fits today, but one string is immune to all
three, and nothing ever queries inside the result. (Firestore truncates indexed values over 1,500 bytes;
that only affects queries on the field, which there are none.) Small summary fields sit beside it so the
console stays readable.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import secrets
from typing import Any

log = logging.getLogger("factlock.ledger")


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _iso(when: dt.datetime) -> str:
    return when.astimezone(dt.timezone.utc).isoformat(timespec="seconds")


def _key(fund_id: str, month: str) -> str:
    return f"{fund_id.upper()}_{month}"


class Ledger:
    """The interface. Every method is synchronous and small; callers wrap failures (ledger_call)."""

    kind = "none"

    def start_run(self, fund_id: str, month: str, uid: str | None) -> str:
        raise NotImplementedError

    def finish_run(self, run_id: str, result: dict) -> None:
        raise NotImplementedError

    def fail_run(self, run_id: str, reason: str) -> None:
        raise NotImplementedError

    def latest_verified(self, fund_id: str, month: str) -> dict | None:
        """{"run_id", "at", "result"} for the most recent draft that passed, else None."""
        raise NotImplementedError

    def runs_since(self, cutoff: dt.datetime) -> list[dict]:
        """Runs started at or after cutoff: [{"run_id", "uid", "started_at"}]. Used for usage limits."""
        raise NotImplementedError

    def record_approval(self, run_id: str, name: str, at: dt.datetime | None = None) -> dict | None:
        """Attach an approval to a run; returns {"name", "at"} or None if the run is unknown."""
        raise NotImplementedError

    # -- derived helpers ---------------------------------------------------------------------------
    def count_runs(self, window_seconds: int, uid: str | None = None, now: dt.datetime | None = None) -> int:
        now = now or _now()
        cutoff = now - dt.timedelta(seconds=window_seconds)
        rows = self.runs_since(cutoff)
        return len([r for r in rows if uid is None or r.get("uid") == uid])


def _status_of(result: dict) -> str:
    return "passed" if result.get("status") == "passed" else "needs_review"


class MemoryLedger(Ledger):
    kind = "memory"

    def __init__(self) -> None:
        self.runs: dict[str, dict] = {}
        self.latest: dict[str, dict] = {}

    def start_run(self, fund_id: str, month: str, uid: str | None) -> str:
        run_id = secrets.token_hex(8)
        started = _now()
        self.runs[run_id] = {"run_id": run_id, "started_at": started, "started_iso": _iso(started), "uid": uid,
                             "fund_id": fund_id.upper(), "month": month, "status": "running"}
        return run_id

    def finish_run(self, run_id: str, result: dict) -> None:
        row = self.runs[run_id]
        row["status"] = _status_of(result)
        row["result"] = result
        if row["status"] == "passed":
            self.latest[_key(row["fund_id"], row["month"])] = {"run_id": run_id, "at": row["started_iso"], "result": result}

    def fail_run(self, run_id: str, reason: str) -> None:
        self.runs[run_id]["status"] = "failed"
        self.runs[run_id]["reason"] = reason

    def latest_verified(self, fund_id: str, month: str) -> dict | None:
        found = self.latest.get(_key(fund_id, month))
        return dict(found) if found else None

    def runs_since(self, cutoff: dt.datetime) -> list[dict]:
        return [{"run_id": r["run_id"], "uid": r.get("uid"), "started_at": r["started_at"]}
                for r in self.runs.values() if r["started_at"] >= cutoff]

    def record_approval(self, run_id: str, name: str, at: dt.datetime | None = None) -> dict | None:
        row = self.runs.get(run_id)
        if row is None:
            return None
        approval = {"name": name, "at": _iso(at or _now())}
        row["approval"] = approval
        if "result" in row:
            row["result"]["approval"] = approval
        latest = self.latest.get(_key(row["fund_id"], row["month"]))
        if latest and latest["run_id"] == run_id:
            latest["result"]["approval"] = approval
        return approval


def _pack(result: dict) -> str:
    return json.dumps(result, ensure_ascii=False, separators=(",", ":"))


def _unpack(row: dict) -> dict | None:
    """The result stored on a row, with the row's approval merged in; None when the row has none."""
    text = row.get("result_json")
    if not text:
        return None
    result = json.loads(text)
    if row.get("approval"):
        result["approval"] = row["approval"]
    return result


class FirestoreLedger(Ledger):
    """Firestore, Native mode, default database. Requires roles/datastore.user on the service account.

    Only the document operations set / get / update and one range query on started_at are used, so no
    composite index is needed (an equality on uid is applied in Python over the last hour's rows)."""

    kind = "firestore"
    RUNS, LATEST = "runs", "latest"

    def __init__(self, project: str | None = None, client: Any | None = None) -> None:
        if client is None:
            from google.cloud import firestore  # imported here so the memory ledger needs no Firestore
            client = firestore.Client(project=project) if project else firestore.Client()
        self.client = client

    def start_run(self, fund_id: str, month: str, uid: str | None) -> str:
        run_id = secrets.token_hex(8)
        started = _now()
        self.client.collection(self.RUNS).document(run_id).set({
            "started_at": started, "started_iso": _iso(started), "uid": uid,
            "fund_id": fund_id.upper(), "month": month, "status": "running"})
        return run_id

    def finish_run(self, run_id: str, result: dict) -> None:
        ref = self.client.collection(self.RUNS).document(run_id)
        snap = ref.get()
        row = snap.to_dict() if snap.exists else {}
        status = _status_of(result)
        engine = result.get("engine") or {}
        ref.update({"status": status, "result_json": _pack(result), "iterations": result.get("iterations"),
                    "elapsed_ms": result.get("elapsed_ms"), "model": engine.get("model")})
        if status == "passed" and row:
            self.client.collection(self.LATEST).document(_key(row["fund_id"], row["month"])).set(
                {"run_id": run_id, "at": row["started_iso"], "result_json": _pack(result)})

    def fail_run(self, run_id: str, reason: str) -> None:
        self.client.collection(self.RUNS).document(run_id).update({"status": "failed", "reason": reason})

    def latest_verified(self, fund_id: str, month: str) -> dict | None:
        snap = self.client.collection(self.LATEST).document(_key(fund_id, month)).get()
        if not snap.exists:
            return None
        row = snap.to_dict() or {}
        result = _unpack(row)
        return {"run_id": row.get("run_id"), "at": row.get("at"), "result": result} if result else None

    def runs_since(self, cutoff: dt.datetime) -> list[dict]:
        from google.cloud.firestore_v1.base_query import FieldFilter
        query = self.client.collection(self.RUNS).where(filter=FieldFilter("started_at", ">=", cutoff))
        return [{"run_id": d.id, "uid": (d.to_dict() or {}).get("uid"), "started_at": (d.to_dict() or {}).get("started_at")}
                for d in query.stream()]

    def record_approval(self, run_id: str, name: str, at: dt.datetime | None = None) -> dict | None:
        ref = self.client.collection(self.RUNS).document(run_id)
        snap = ref.get()
        if not snap.exists:
            return None
        row = snap.to_dict() or {}
        approval = {"name": name, "at": _iso(at or _now())}
        ref.update({"approval": approval})
        latest_ref = self.client.collection(self.LATEST).document(_key(row.get("fund_id", ""), row.get("month", "")))
        latest = latest_ref.get()
        if latest.exists and (latest.to_dict() or {}).get("run_id") == run_id:
            latest_ref.update({"approval": approval})
        return approval


def build_ledger(kind: str, project: str | None = None) -> Ledger:
    kind = (kind or "memory").strip().lower()
    if kind == "firestore":
        return FirestoreLedger(project=project)
    if kind in ("memory", "", "none"):
        return MemoryLedger()
    raise ValueError(f"Unknown ledger kind {kind!r}; use memory or firestore.")


def ledger_call(ledger: Ledger, method: str, *args, default=None, **kwargs):
    """Call a ledger method; a ledger failure is logged and returns default, so a broken ledger never
    stops a draft (the draft is still checked and returned; only its record is missing)."""
    try:
        return getattr(ledger, method)(*args, **kwargs)
    except Exception:  # noqa: BLE001 - the ledger is best-effort
        log.exception("Ledger %s failed", method)
        return default
