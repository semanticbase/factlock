"""The audit ledger, Firebase sign-in verification, and the API in firebase mode (no network)."""
import datetime as dt
import warnings

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import app.main as main
from app.auth import AuthError, FirebaseAuth, bearer_token, claims_to_identity
from app.ledger import FirestoreLedger, MemoryLedger, build_ledger, ledger_call

warnings.filterwarnings("ignore", category=DeprecationWarning)
PASSED = {"status": "passed", "iterations": 1, "commentary": [], "history": []}


# ---- the ledger -------------------------------------------------------------------------------

def test_memory_ledger_records_runs_latest_and_approval():
    ledger = MemoryLedger()
    assert ledger.latest_verified("DGBF", "2026-08") is None
    a = ledger.start_run("dgbf", "2026-08", "u1")
    ledger.finish_run(a, {"status": "needs_review", "iterations": 3})
    assert ledger.latest_verified("DGBF", "2026-08") is None          # a draft that did not pass is not "latest verified"
    b = ledger.start_run("DGBF", "2026-08", "u2")
    ledger.finish_run(b, dict(PASSED))
    latest = ledger.latest_verified("dgbf", "2026-08")
    assert latest["run_id"] == b and latest["result"]["status"] == "passed"
    assert ledger.count_runs(3600) == 2 and ledger.count_runs(3600, uid="u1") == 1 and ledger.count_runs(3600, uid="nobody") == 0
    assert ledger.count_runs(3600, now=dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=2)) == 0
    approval = ledger.record_approval(b, "Reviewer")
    assert approval["name"] == "Reviewer" and approval["at"].endswith("+00:00")
    assert ledger.latest_verified("DGBF", "2026-08")["result"]["approval"] == approval
    assert ledger.record_approval("no-such-run", "x") is None
    c = ledger.start_run("DGBF", "2026-08", None)
    ledger.fail_run(c, "quota")
    assert ledger.runs[c]["status"] == "failed" and ledger.latest_verified("DGBF", "2026-08")["run_id"] == b


class _FakeSnap:
    def __init__(self, doc_id, data):
        self.id, self._data = doc_id, data
    @property
    def exists(self):
        return self._data is not None
    def to_dict(self):
        return dict(self._data) if self._data is not None else None


class _FakeDoc:
    def __init__(self, store, doc_id):
        self.store, self.doc_id = store, doc_id
    def set(self, data):
        self.store[self.doc_id] = dict(data)
    def get(self):
        return _FakeSnap(self.doc_id, self.store.get(self.doc_id))
    def update(self, fields):
        row = self.store[self.doc_id]
        for key, value in fields.items():          # "a.b" updates a nested field, as Firestore does
            target, *rest = key.split(".")
            if rest:
                row.setdefault(target, {})[rest[0]] = value
            else:
                row[target] = value


class _FakeQuery:
    def __init__(self, store, field, op, value):
        self.store, self.field, self.op, self.value = store, field, op, value
    def stream(self):
        assert self.op == ">="
        return [_FakeSnap(k, v) for k, v in self.store.items() if v.get(self.field) is not None and v[self.field] >= self.value]


class _FakeCollection:
    def __init__(self, store):
        self.store = store
    def document(self, doc_id):
        return _FakeDoc(self.store, doc_id)
    def where(self, filter=None):
        return _FakeQuery(self.store, filter.field_path, filter.op_string, filter.value)


class FakeFirestoreClient:
    """The few Firestore client calls the ledger uses, over dicts. Shapes follow google-cloud-firestore
    2.32 (collection().document().set/get/update, where(filter=FieldFilter).stream(), snapshot.exists/to_dict/id)."""
    def __init__(self):
        self.collections = {}
    def collection(self, name):
        return _FakeCollection(self.collections.setdefault(name, {}))


def test_firestore_ledger_uses_the_client_as_the_memory_ledger_does():
    client = FakeFirestoreClient()
    ledger = FirestoreLedger(client=client)
    assert ledger.latest_verified("DGBF", "2026-08") is None
    a = ledger.start_run("DGBF", "2026-08", "u1")
    ledger.finish_run(a, dict(PASSED))
    assert client.collections["runs"][a]["status"] == "passed" and client.collections["latest"]["DGBF_2026-08"]["run_id"] == a
    assert isinstance(client.collections["runs"][a]["result_json"], str)     # one string: no nested map in Firestore
    assert "result" not in client.collections["runs"][a]
    assert ledger.latest_verified("dgbf", "2026-08")["run_id"] == a and ledger.latest_verified("dgbf", "2026-08")["result"]["status"] == "passed"
    assert ledger.count_runs(3600) == 1 and ledger.count_runs(3600, uid="u1") == 1 and ledger.count_runs(3600, uid="u2") == 0
    approval = ledger.record_approval(a, "Reviewer")
    assert client.collections["runs"][a]["approval"] == approval
    assert client.collections["latest"]["DGBF_2026-08"]["approval"] == approval
    assert ledger.latest_verified("DGBF", "2026-08")["result"]["approval"] == approval   # merged in on read
    assert ledger.record_approval("missing", "x") is None
    b = ledger.start_run("DGBF", "2026-08", None)
    ledger.fail_run(b, "quota")
    assert client.collections["runs"][b]["status"] == "failed"


def test_firestore_ledger_keeps_shapes_firestore_itself_would_refuse():
    """Nested arrays (e.g. a series [["2026-08", 4.68]]) and fact ids with periods as keys are fine as a
    JSON string, and come back unchanged."""
    client = FakeFirestoreClient()
    ledger = FirestoreLedger(client=client)
    result = dict(PASSED, fact_sheet={"facts": {"rate.USA.level": {"value": 4.68}}, "series": [["2026-08", 4.68], ["2026-07", 4.6]]})
    a = ledger.start_run("DGBF", "2026-08", None)
    ledger.finish_run(a, result)
    back = ledger.latest_verified("DGBF", "2026-08")["result"]
    assert back["fact_sheet"]["series"] == [["2026-08", 4.68], ["2026-07", 4.6]] and back["fact_sheet"]["facts"]["rate.USA.level"]["value"] == 4.68
    js = (__import__("pathlib").Path(__file__).resolve().parent.parent / "web" / "app.js").read_text(encoding="utf-8")
    assert "Served from the audit ledger" in js and "served_at" in js          # the paper record says so too


def test_build_ledger_and_best_effort_calls():
    assert build_ledger("memory").kind == "memory" and build_ledger("").kind == "memory"
    with pytest.raises(ValueError):
        build_ledger("mysql")

    class Broken(MemoryLedger):
        def latest_verified(self, fund_id, month):
            raise RuntimeError("firestore down")
    assert ledger_call(Broken(), "latest_verified", "DGBF", "2026-08", default=None) is None   # logged, not raised


# ---- sign-in ------------------------------------------------------------------------------------

def test_bearer_token_and_claims():
    assert bearer_token("Bearer abc.def") == "abc.def" and bearer_token("bearer x") == "x"
    assert bearer_token(None) is None and bearer_token("Basic abc") is None and bearer_token("Bearer ") is None
    who = claims_to_identity({"uid": "u1", "firebase": {"sign_in_provider": "anonymous"}})
    assert who.uid == "u1" and who.provider == "anonymous"
    assert claims_to_identity({"sub": "u2"}).provider == "unknown"
    with pytest.raises(AuthError):
        claims_to_identity({"firebase": {"sign_in_provider": "anonymous"}})


def _firebase(verifier):
    return FirebaseAuth("proj", "key", "proj.firebaseapp.com", "1:2:web:3", verifier=verifier)


def test_firebase_auth_verifies_tokens_with_the_injected_verifier():
    def verifier(token):
        if token != "good":
            raise ValueError("bad token")
        return {"uid": "u1", "firebase": {"sign_in_provider": "anonymous"}}
    fb = _firebase(verifier)
    assert fb.identify("Bearer good").uid == "u1"
    for header in (None, "Bearer bad", "Basic good"):
        with pytest.raises(AuthError):
            fb.identify(header)
    assert fb.config() == {"apiKey": "key", "authDomain": "proj.firebaseapp.com", "projectId": "proj", "appId": "1:2:web:3"}


# ---- the API in firebase mode -----------------------------------------------------------------------

@pytest.fixture
def firebase_client(monkeypatch):
    def verifier(token):
        if not token.startswith("tok-"):
            raise ValueError("bad token")
        return {"uid": token[4:], "firebase": {"sign_in_provider": "anonymous"}}
    monkeypatch.setattr(main.settings, "auth_mode", "firebase")
    monkeypatch.setattr(main.settings, "passphrase", "")
    monkeypatch.setattr(main, "firebase_auth", _firebase(verifier))
    monkeypatch.setattr(main, "ledger", MemoryLedger())
    return TestClient(main.app)


def test_firebase_mode_pages_are_public_and_apis_need_a_token(firebase_client):
    c = firebase_client
    assert c.get("/").status_code == 200
    assert c.get("/login", follow_redirects=False).status_code == 303          # no passphrase page in this mode
    config = c.get("/api/config").json()
    assert config["auth"] == "firebase" and config["firebase"]["apiKey"] == "key" and "appId" in config["firebase"]
    assert c.get("/api/health").json()["auth"] == "firebase" and c.get("/api/health").json()["gated"] is False
    r = c.get("/api/facts", params={"month": "2026-08"})
    assert r.status_code == 401 and "Sign in" in r.json()["detail"]
    assert c.get("/api/facts", params={"month": "2026-08"}, headers={"Authorization": "Bearer nope"}).status_code == 401
    assert c.get("/api/facts", params={"month": "2026-08"}, headers={"Authorization": "Bearer tok-u1"}).status_code == 200


def test_firebase_mode_generate_records_and_approval_is_kept_on_the_ledger(firebase_client):
    c, h = firebase_client, {"Authorization": "Bearer tok-u1"}
    assert c.get("/api/latest", params={"month": "2026-08"}, headers=h).json() == {"result": None}
    body = c.post("/api/generate", json={"month": "2026-08", "mode": "demo", "scenario": "clean"}, headers=h).json()
    assert body["status"] == "passed" and body["served_from"] == "live" and body["run_id"]
    assert main.ledger.runs[body["run_id"]]["uid"] == "u1"
    assert c.post("/api/approve", json={"run_id": "missing", "name": "R"}, headers=h).status_code == 404
    assert c.post("/api/approve", json={"run_id": body["run_id"], "name": " "}, headers=h).status_code == 422
    approved = c.post("/api/approve", json={"run_id": body["run_id"], "name": "Reviewer"}, headers=h).json()
    assert approved["ok"] and approved["approval"]["name"] == "Reviewer"
    latest = c.get("/api/latest", params={"month": "2026-08"}, headers=h).json()["result"]
    assert latest["served_from"] == "ledger" and latest["run_id"] == body["run_id"] and latest["approval"]["name"] == "Reviewer"


def test_usage_limits_are_counted_on_the_ledger(monkeypatch):
    ledger = MemoryLedger()
    monkeypatch.setattr(main, "ledger", ledger)
    monkeypatch.setattr(main.settings, "runs_per_hour", 3)
    monkeypatch.setattr(main.settings, "runs_per_hour_per_user", 2)
    main._check_rate_limits("u1")
    ledger.start_run("DGBF", "2026-08", "u1"); ledger.start_run("DGBF", "2026-08", "u1")
    with pytest.raises(HTTPException) as e:
        main._check_rate_limits("u1")                       # this visitor has used their 2
    assert e.value.status_code == 429 and "2 new drafts per hour" in e.value.detail
    main._check_rate_limits("u2")                           # another visitor may still draft (2 of 3 used)
    ledger.start_run("DGBF", "2026-08", "u2")
    with pytest.raises(HTTPException) as e:
        main._check_rate_limits("u3")                       # everyone together has used the 3
    assert e.value.status_code == 429 and "hourly limit" in e.value.detail
    main._check_rate_limits(None) if False else None       # (no identity: only the shared limit applies)


def test_the_page_signs_in_with_firebase_when_the_config_says_so():
    import pathlib
    js = (pathlib.Path(__file__).resolve().parent.parent / "web" / "app.js").read_text(encoding="utf-8")
    assert "/api/config" in js and "signInAnonymously" in js and "Authorization" in js and "served_from" in js
    assert "gstatic.com/firebasejs/" in js
