import os
import warnings

warnings.filterwarnings("ignore", category=DeprecationWarning)
os.environ["FACTLOCK_LLM_MODE"] = "demo"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import (ACCESS_COOKIE, SESSION_SECONDS, _failed_logins, access_token, app,  # noqa: E402
                      settings, token_is_valid)

PASSPHRASE = "builder cup 2026"


@pytest.fixture()
def gated():
    settings.passphrase = PASSPHRASE
    _failed_logins.clear()
    with TestClient(app) as client:
        yield client
    settings.passphrase = ""
    _failed_logins.clear()


@pytest.fixture()
def open_app():
    settings.passphrase = ""
    with TestClient(app) as client:
        yield client


def test_without_the_gate_everything_is_reachable(open_app):
    assert open_app.get("/").status_code == 200
    assert open_app.get("/api/facts", params={"month": "2026-08"}).status_code == 200


def test_pages_redirect_and_apis_refuse(gated):
    page = gated.get("/", follow_redirects=False)
    assert page.status_code == 303 and page.headers["location"] == "/login"
    api = gated.get("/api/facts", params={"month": "2026-08"})
    assert api.status_code == 401 and "passphrase" in api.json()["detail"]
    assert gated.post("/api/generate", json={"month": "2026-08"}).status_code == 401


def test_the_login_page_and_its_assets_stay_open(gated):
    assert gated.get("/login").status_code == 200
    assert gated.get("/static/styles.css").status_code == 200
    assert gated.get("/api/health").json()["gated"] is True


def test_wrong_passphrase_is_refused_and_grants_nothing(gated):
    response = gated.post("/api/login", json={"passphrase": "not it"})
    assert response.status_code == 401
    assert ACCESS_COOKIE not in gated.cookies
    assert gated.get("/", follow_redirects=False).status_code == 303


def test_correct_passphrase_opens_the_app(gated):
    response = gated.post("/api/login", json={"passphrase": f"  {PASSPHRASE} "})  # spaces are trimmed
    assert response.status_code == 200
    assert gated.get("/").status_code == 200
    assert gated.get("/api/facts", params={"month": "2026-08"}).status_code == 200


def test_a_made_up_cookie_does_not_work(gated):
    gated.cookies.set(ACCESS_COOKIE, "0" * 64)
    assert gated.get("/", follow_redirects=False).status_code == 303


def test_the_cookie_cannot_be_derived_from_a_different_passphrase():
    issued = 1_790_000_000
    assert access_token(PASSPHRASE, issued) != access_token(PASSPHRASE + "!", issued)
    assert not token_is_valid(access_token(PASSPHRASE + "!", issued), PASSPHRASE, now=issued)
    stamp, _, signature = access_token(PASSPHRASE, issued).partition(".")
    assert stamp == str(issued) and len(signature) == 64


# ---- the token expires on the server, whatever the browser does ------------------------------

@pytest.mark.parametrize("age,valid", [(0, True), (SESSION_SECONDS - 1, True), (SESSION_SECONDS + 1, False),
                                       (-60, True), (-3600, False)])
def test_the_token_expires_on_the_server(age, valid):
    now = 1_790_000_000
    assert token_is_valid(access_token(PASSPHRASE, now - age), PASSPHRASE, now=now) is valid


@pytest.mark.parametrize("mangle", [
    lambda t: t[:-1] + ("0" if t[-1] != "0" else "1"),            # signature changed
    lambda t: str(int(t.split(".")[0]) + 3600) + "." + t.split(".")[1],   # issue time moved, signature kept
    lambda t: t.split(".")[1],                                      # the old format (signature only)
    lambda t: "", lambda t: "abc.def", lambda t: "12x.00"])
def test_altered_tokens_are_refused(mangle):
    now = 1_790_000_000
    assert not token_is_valid(mangle(access_token(PASSPHRASE, now)), PASSPHRASE, now=now)


def test_an_expired_cookie_is_sent_to_the_passphrase_page(gated):
    import time
    gated.cookies.set(ACCESS_COOKIE, access_token(PASSPHRASE, int(time.time()) - SESSION_SECONDS - 10))
    assert gated.get("/", follow_redirects=False).status_code == 303
    assert gated.get("/api/facts", params={"month": "2026-08"}).status_code == 401


# ---- cookie attributes and signing out ----------------------------------------------------------

def _set_cookie(response):
    return "; ".join(response.headers.get_list("set-cookie"))


@pytest.mark.parametrize("base_url,k_service,secure", [
    ("http://testserver", None, False), ("https://testserver", None, True), ("http://testserver", "factlock", True)])
def test_login_cookie_attributes(base_url, k_service, secure, monkeypatch):
    if k_service:
        monkeypatch.setenv("K_SERVICE", k_service)      # set by Cloud Run
    else:
        monkeypatch.delenv("K_SERVICE", raising=False)
    settings.passphrase = PASSPHRASE
    _failed_logins.clear()
    try:
        with TestClient(app, base_url=base_url) as client:
            cookie = _set_cookie(client.post("/api/login", json={"passphrase": PASSPHRASE}))
    finally:
        settings.passphrase = ""
    assert f"Max-Age={SESSION_SECONDS}" in cookie and "HttpOnly" in cookie and "Path=/" in cookie
    assert "samesite=lax" in cookie.lower()
    assert ("Secure" in cookie) is secure


def test_sign_out_removes_the_cookie_with_the_same_attributes(monkeypatch):
    monkeypatch.delenv("K_SERVICE", raising=False)
    settings.passphrase = PASSPHRASE
    _failed_logins.clear()
    try:
        with TestClient(app, base_url="https://testserver") as client:
            assert client.post("/api/login", json={"passphrase": PASSPHRASE}).status_code == 200
            assert client.get("/").status_code == 200
            out = client.post("/api/logout")
            cookie = _set_cookie(out)
            assert out.status_code == 200
            assert f"{ACCESS_COOKIE}=" in cookie and "Max-Age=0" in cookie
            assert "Path=/" in cookie and "HttpOnly" in cookie and "Secure" in cookie and "samesite=lax" in cookie.lower()
            assert client.get("/", follow_redirects=False).status_code == 303
    finally:
        settings.passphrase = ""


def test_sign_out_works_without_a_session_and_only_by_post(gated):
    assert gated.post("/api/logout").status_code == 200          # e.g. after the session expired
    assert gated.get("/api/logout").status_code == 405


# ---- response headers ------------------------------------------------------------------------------

@pytest.mark.parametrize("path,status,stored", [("/", 303, False), ("/login", 200, False),
                                                ("/api/facts?month=2026-08", 401, False),
                                                ("/api/health", 200, False), ("/static/styles.css", 200, True)])
def test_security_headers_on_every_response(gated, path, status, stored):
    r = gated.get(path, follow_redirects=False)
    assert r.status_code == status
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert (r.headers.get("cache-control") == "no-store") is not stored


def test_dockerfile_trusts_the_forwarded_scheme_on_cloud_run():
    from pathlib import Path
    cmd = [line for line in (Path(__file__).resolve().parent.parent / "Dockerfile").read_text().splitlines()
           if line.startswith("CMD")]
    assert len(cmd) == 1 and '--forwarded-allow-ips "*"' in cmd[0]


def test_the_page_offers_sign_out_only_behind_the_gate():
    from pathlib import Path
    web = Path(__file__).resolve().parent.parent / "web"
    index, js, login = ((web / f).read_text(encoding="utf-8") for f in ("index.html", "app.js", "login.html"))
    assert '<button type="button" class="band-signout" id="signout" hidden>Sign out</button>' in index
    assert '$("#signout").hidden = !state.health.gated;' in js
    assert 'fetch("/api/logout", { method: "POST" })' in js and 'window.location.replace("/login?signed_out=1")' in js
    assert 'id="signed-out" hidden' in login and 'has("signed_out")' in login


def test_repeated_guessing_is_blocked(gated):
    for _ in range(20):
        assert gated.post("/api/login", json={"passphrase": "guess"}).status_code == 401
    blocked = gated.post("/api/login", json={"passphrase": "guess"})
    assert blocked.status_code == 429
    # The block applies to the correct passphrase as well until the window passes.
    assert gated.post("/api/login", json={"passphrase": PASSPHRASE}).status_code == 429
