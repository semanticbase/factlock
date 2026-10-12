import json
import warnings

import pytest
from google.genai import errors

from scripts import check_gemini, run_once

warnings.filterwarnings("ignore", category=DeprecationWarning)


class FakeResponse:
    def __init__(self, text):
        self.text = text


class FakeModels:
    def __init__(self, behaviour):
        self.behaviour, self.calls = behaviour, []

    def generate_content(self, *, model, contents, config):
        self.calls.append(model)
        outcome = self.behaviour[model]
        if isinstance(outcome, Exception):
            raise outcome
        return FakeResponse(outcome)


class FakeClient:
    def __init__(self, behaviour):
        self.models = FakeModels(behaviour)


def not_found():
    return errors.ClientError(404, {"error": {"code": 404, "message": "model not found", "status": "NOT_FOUND"}})


def denied():
    return errors.ClientError(403, {"error": {"code": 403, "message": "permission denied", "status": "PERMISSION_DENIED"}})


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("FACTLOCK_MODEL", raising=False)
    check_gemini.RESULT_FILE = tmp_path / "check.json"
    run_once.RESULT_FILE = tmp_path / "run.json"
    yield


def run_check(behaviour):
    client = FakeClient(behaviour)
    code = check_gemini.main(client_factory=lambda project, location: client, project="demo-project")
    return code, client.models.calls, json.loads(check_gemini.RESULT_FILE.read_text())


def test_first_model_answers():
    code, calls, record = run_check({"gemini-3.8-flash": '{"ok": true, "sentence": "Hi."}'})
    assert (code, calls, record["model"], record["attempts"][0]["json_ok"]) == (0, ["gemini-3.8-flash"], "gemini-3.8-flash", True)


def test_falls_back_when_model_is_not_found():
    code, calls, record = run_check({"gemini-3.8-flash": not_found(), "gemini-3.5-flash": '{"ok": true}'})
    assert (code, calls, record["model"]) == (0, ["gemini-3.8-flash", "gemini-3.5-flash"], "gemini-3.5-flash")
    assert record["attempts"][0]["error_kind"] == "model_not_found"


def test_permission_error_stops_immediately():
    code, calls, record = run_check({"gemini-3.8-flash": denied(), "gemini-3.5-flash": '{"ok": true}'})
    assert (code, calls, record["result"]) == (1, ["gemini-3.8-flash"], "failed")
    assert "aiplatform.googleapis.com" in record["attempts"][0]["advice"]


def test_non_json_reply_is_reported_but_accepted():
    code, _, record = run_check({"gemini-3.8-flash": 'Sure: {"ok": true}'})
    assert code == 0 and record["attempts"][0]["json_ok"] is False


def test_all_models_missing():
    code, calls, record = run_check({"gemini-3.8-flash": not_found(), "gemini-3.5-flash": not_found()})
    assert (code, len(calls), record["result"]) == (1, 2, "failed")


def test_preferred_model_from_environment_is_tried_first(monkeypatch):
    monkeypatch.setenv("FACTLOCK_MODEL", "gemini-3.5-flash")
    assert check_gemini.candidates() == ["gemini-3.5-flash", "gemini-3.8-flash"]


def test_no_project(monkeypatch):
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.setattr(check_gemini, "detect_project", lambda: None)
    assert check_gemini.main(client_factory=lambda p, l: None) == 2


def test_run_once_saves_the_full_record():
    from app.llm import build_models
    models = build_models("demo", model="x", reviewer_model=None, project=None, location="global",
                          demo_scenario="fix_once")
    assert run_once.main(models=models) == 0
    saved = json.loads(run_once.RESULT_FILE.read_text())
    assert saved["status"] == "passed" and len(saved["history"]) == 2 and saved["history"][0]["draft_raw"]


def test_run_once_records_a_failure():
    class Broken:
        pass
    assert run_once.main(models=(Broken(), Broken())) == 1
    assert "error" in json.loads(run_once.RESULT_FILE.read_text())
