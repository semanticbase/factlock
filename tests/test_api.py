import os
import warnings

warnings.filterwarnings("ignore", category=DeprecationWarning)
os.environ["FACTLOCK_LLM_MODE"] = "demo"

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

client = TestClient(app)


def test_health_reports_offline_demo():
    body = client.get("/api/health").json()
    assert body["mode"] == "demo" and "no AI model" in body["model"]


def test_reports_and_facts():
    assert {"fund_id": "DGBF", "month": "2026-08"} in client.get("/api/reports").json()["reports"]
    facts = client.get("/api/facts", params={"month": "2026-08"}).json()
    assert "fund.ret_1m" in facts["facts"] and facts["period_end"] == "2026-08-31"


def test_generate_then_latest():
    assert client.get("/api/latest", params={"month": "2026-08"}).json() == {"result": None}
    body = client.post("/api/generate", json={"month": "2026-08", "scenario": "fix_once"}).json()
    assert body["status"] == "passed" and body["iterations"] == 2
    assert body["engine"]["mode"] == "demo"
    latest = client.get("/api/latest", params={"month": "2026-08"}).json()["result"]
    assert latest["iterations"] == 2


def test_unknown_month_is_404():
    assert client.post("/api/generate", json={"month": "1999-01"}).status_code == 404
    assert client.get("/api/facts", params={"month": "1999-01"}).status_code == 404


def test_index_is_served():
    response = client.get("/")
    assert response.status_code == 200 and "<html" in response.text.lower()
