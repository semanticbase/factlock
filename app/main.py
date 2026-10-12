"""FactLock web app (FastAPI), deployable to Cloud Run."""
from __future__ import annotations

import asyncio
import collections
import hashlib
import hmac
import logging
import os
import pathlib
import time
import warnings

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.auth import AuthError, FirebaseAuth
from app.exhibits import build_exhibits
from app.facts import available_reports, build_fact_sheet, load_snapshot
from app.ledger import build_ledger, ledger_call
from app.llm import build_models, is_quota_error
from app.service import run_pipeline

# ADK 2.x marks SequentialAgent/LoopAgent as deprecated in favour of Workflow;
# they remain supported in the pinned version. Keep the logs readable.
warnings.filterwarnings("ignore", category=DeprecationWarning, module=r"google\.adk.*")
warnings.filterwarnings("ignore", message=r".*is deprecated in favor of Workflow.*")

ROOT = pathlib.Path(__file__).resolve().parent.parent
WEB_DIR = ROOT / "web"


def asset_version(web_dir: pathlib.Path = WEB_DIR) -> str:
    """A short digest of the page files. It goes on every script and stylesheet URL, so a page always
    loads the scripts it was written for; a cached page cannot run a newer script (or the reverse)."""
    digest = hashlib.sha256()
    for name in ("styles.css", "format.js", "app.js", "index.html", "login.html"):
        digest.update((web_dir / name).read_bytes())
    return digest.hexdigest()[:10]


ASSET_VERSION = asset_version()


def page(name: str) -> "HTMLResponse":
    return HTMLResponse((WEB_DIR / name).read_text(encoding="utf-8").replace("__ASSET_VERSION__", ASSET_VERSION))
log = logging.getLogger("factlock")


def _detect_project() -> str | None:
    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    if project:
        return project
    try:
        import google.auth
        return google.auth.default()[1]
    except Exception:  # noqa: BLE001 - no credentials locally is expected
        return None


class Settings:
    def __init__(self) -> None:
        self.project = _detect_project()
        default_mode = "gemini" if self.project else "demo"
        self.mode = os.environ.get("FACTLOCK_LLM_MODE", default_mode)
        self.model = os.environ.get("FACTLOCK_MODEL", "gemini-3.8-flash")
        self.reviewer_model = os.environ.get("FACTLOCK_REVIEWER_MODEL") or None
        self.location = os.environ.get("FACTLOCK_LOCATION", "global")
        # Same model at a second location when the first answers 429. Off by default: on 2026-09-24
        # gemini-3.8-flash was served from the global endpoint only (checked in 5 regions), so there is
        # no second endpoint for the same model yet. Set this when one exists.
        fallback = os.environ.get("FACTLOCK_FALLBACK_LOCATION", "none").strip()
        self.fallback_location = None if fallback.lower() in ("", "none") else fallback
        self.max_iterations = int(os.environ.get("FACTLOCK_MAX_ITERATIONS", "3"))
        # Usage limits for live drafts, counted in the ledger: for everyone together, and per visitor
        # (per-visitor needs an identity, i.e. FACTLOCK_AUTH=firebase).
        self.runs_per_hour = int(os.environ.get("FACTLOCK_RUNS_PER_HOUR", "30"))
        self.runs_per_hour_per_user = int(os.environ.get("FACTLOCK_RUNS_PER_HOUR_PER_USER", "5"))
        self.data_dir = os.environ.get("FACTLOCK_DATA_DIR", str(ROOT / "data" / "snapshots"))
        # When set, the whole app is behind a single shared passphrase.
        self.passphrase = os.environ.get("FACTLOCK_PASSPHRASE", "").strip()
        # Who is calling: "passphrase" (the shared passphrase above), "firebase" (anonymous sign-in with
        # Firebase Authentication, for a public demo) or "none". See app/auth.py.
        self.auth_mode = os.environ.get("FACTLOCK_AUTH", "passphrase" if self.passphrase else "none").strip().lower()
        self.firebase_api_key = os.environ.get("FACTLOCK_FIREBASE_API_KEY", "").strip()
        self.firebase_auth_domain = os.environ.get("FACTLOCK_FIREBASE_AUTH_DOMAIN", "").strip()
        self.firebase_app_id = os.environ.get("FACTLOCK_FIREBASE_APP_ID", "").strip()
        # Where runs, verdicts, approvals and the latest verified draft are kept: "memory" or "firestore".
        self.ledger_kind = os.environ.get("FACTLOCK_LEDGER", "memory").strip().lower()


settings = Settings()
app = FastAPI(title="FactLock", version="0.1.0")
app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
ledger = build_ledger(settings.ledger_kind, project=settings.project)
firebase_auth: FirebaseAuth | None = (
    FirebaseAuth(settings.project, settings.firebase_api_key, settings.firebase_auth_domain, settings.firebase_app_id)
    if settings.auth_mode == "firebase" else None)


ACCESS_COOKIE = "factlock_access"
# /api/logout is open so that signing out always works, even after the session has expired.
# /api/config is open because the page needs it before it can sign the visitor in.
OPEN_PATHS = ("/login", "/api/login", "/api/logout", "/api/health", "/api/config", "/static/", "/favicon.ico", "/robots.txt")
SESSION_SECONDS = 12 * 3600
CLOCK_SKEW_SECONDS = 300
MAX_FAILED_LOGINS, LOGIN_WINDOW_SECONDS = 20, 600
_failed_logins: collections.deque = collections.deque()


def _signature(passphrase: str, issued: int) -> str:
    return hmac.new(passphrase.encode("utf-8"), f"factlock-access-v2|{issued}".encode("ascii"),
                    hashlib.sha256).hexdigest()


def access_token(passphrase: str, issued: int | None = None) -> str:
    """"<issued>.<signature>": nobody can produce it without the passphrase, and the server itself
    refuses it after SESSION_SECONDS, whatever the browser does with the cookie's own expiry.
    Changing the passphrase invalidates every token at once."""
    issued = int(time.time()) if issued is None else int(issued)
    return f"{issued}.{_signature(passphrase, issued)}"


def token_is_valid(token: str, passphrase: str, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    issued_text, _, signature = (token or "").partition(".")
    if not issued_text.isdigit() or not signature:
        return False
    issued = int(issued_text)
    if not hmac.compare_digest(signature, _signature(passphrase, issued)):
        return False
    return issued - CLOCK_SKEW_SECONDS <= now <= issued + SESSION_SECONDS


def secure_cookie(request: Request) -> bool:
    """Mark the cookie Secure on HTTPS, and always on Cloud Run (which sets K_SERVICE), where TLS ends
    at Google's front end. The Dockerfile also lets uvicorn trust the forwarded scheme; either is enough."""
    return request.url.scheme == "https" or bool(os.environ.get("K_SERVICE"))


@app.middleware("http")
async def access_gate(request: Request, call_next):
    request.state.identity = None
    if request.url.path.startswith(OPEN_PATHS):
        return await call_next(request)
    if settings.auth_mode == "firebase" and firebase_auth is not None:
        # The pages are public; every other API call must carry a verified Firebase ID token.
        if request.url.path.startswith("/api/"):
            try:
                request.state.identity = firebase_auth.identify(request.headers.get("authorization"))
            except AuthError as exc:
                return JSONResponse({"detail": exc.detail}, status_code=401)
        return await call_next(request)
    if not settings.passphrase:
        return await call_next(request)
    if token_is_valid(request.cookies.get(ACCESS_COOKIE, ""), settings.passphrase):
        return await call_next(request)
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": "This demo is protected by a passphrase."}, status_code=401)
    return RedirectResponse("/login", status_code=303)


# Keep the demo out of search results: robots.txt for crawlers that read it,
# and a header on every response (redirects and 401s included) for those that do not.
ROBOTS_TXT = "User-agent: *\nDisallow: /\n"
NOINDEX = "noindex, nofollow"
# Not framed by other sites; the demo's address is not passed to the source sites the notes link to;
# no content-type guessing. Pages and API answers are not stored, so "back" after signing out shows nothing.
SECURITY_HEADERS = {"X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff"}


@app.middleware("http")
async def no_indexing(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Robots-Tag"] = NOINDEX
    for name, value in SECURITY_HEADERS.items():
        response.headers[name] = value
    if not request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/robots.txt")
def robots() -> PlainTextResponse:
    return PlainTextResponse(ROBOTS_TXT)


class LoginRequest(BaseModel):
    passphrase: str


@app.get("/login")
def login_page():
    if settings.auth_mode == "firebase":
        return RedirectResponse("/", status_code=303)   # the page signs the visitor in itself
    return page("login.html")


@app.get("/api/config")
def config() -> dict:
    """What the page needs before its first call: how to sign in. Nothing here is a secret."""
    return {"auth": settings.auth_mode,
            "firebase": firebase_auth.config() if firebase_auth is not None else None}


@app.post("/api/login")
async def login(body: LoginRequest, request: Request) -> JSONResponse:
    if not settings.passphrase:
        return JSONResponse({"ok": True, "gate": "disabled"})
    now = time.time()
    while _failed_logins and now - _failed_logins[0] > LOGIN_WINDOW_SECONDS:
        _failed_logins.popleft()
    if len(_failed_logins) >= MAX_FAILED_LOGINS:
        raise HTTPException(status_code=429, detail="Too many attempts. Please wait ten minutes.")
    if not hmac.compare_digest(body.passphrase.strip(), settings.passphrase):
        _failed_logins.append(now)
        await asyncio.sleep(0.5)  # slow down guessing
        raise HTTPException(status_code=401, detail="That passphrase is not correct.")
    response = JSONResponse({"ok": True})
    response.set_cookie(ACCESS_COOKIE, access_token(settings.passphrase), max_age=SESSION_SECONDS, path="/",
                        httponly=True, samesite="lax", secure=secure_cookie(request))
    return response


@app.post("/api/logout")
async def logout(request: Request) -> JSONResponse:
    """Remove the cookie from this browser. Deleted with the same attributes it was set with, or some
    browsers keep it. Signing everyone out at once means changing the passphrase."""
    response = JSONResponse({"ok": True})
    response.delete_cookie(ACCESS_COOKIE, path="/", httponly=True, samesite="lax", secure=secure_cookie(request))
    return response


class GenerateRequest(BaseModel):
    fund_id: str = "DGBF"
    month: str
    mode: str = "auto"          # "auto" uses the server setting; "demo" forces the offline demo
    scenario: str = "clean"     # demo only: "clean" or "fix_once"


@app.get("/")
def index() -> HTMLResponse:
    return page("index.html")


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "mode": settings.mode,
            "gated": settings.auth_mode != "firebase" and bool(settings.passphrase),
            "auth": settings.auth_mode, "ledger": ledger.kind,
            "model": settings.model if settings.mode == "gemini" else "offline demo (no AI model)",
            "location": settings.location if settings.mode == "gemini" else None}


@app.get("/api/reports")
def reports() -> dict:
    return {"reports": available_reports(pathlib.Path(settings.data_dir))}


@app.get("/api/facts")
def facts(month: str, fund_id: str = "DGBF") -> dict:
    try:
        snapshot = load_snapshot(pathlib.Path(settings.data_dir), fund_id, month)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"No data for {fund_id} {month}.")
    sheet = build_fact_sheet(snapshot)
    return {**sheet.model_dump(), "exhibits": build_exhibits(sheet)}


@app.get("/api/latest")
def latest(month: str, fund_id: str = "DGBF") -> dict:
    """The latest verified draft for this report from the ledger, or {"result": null} if none exists yet."""
    found = ledger_call(ledger, "latest_verified", fund_id, month, default=None)
    if not found:
        return {"result": None}
    result = dict(found["result"])
    result.setdefault("run_id", found.get("run_id"))
    result["served_from"] = "ledger"
    return {"result": result}


def _check_rate_limits(uid: str | None) -> None:
    """Live drafts are limited per hour, for everyone together and per visitor, counted in the ledger so the
    limits hold across instances. A ledger failure never blocks a draft (the count is then unknown)."""
    total = ledger_call(ledger, "count_runs", 3600, default=0)
    if total >= settings.runs_per_hour:
        raise HTTPException(status_code=429, detail="The hourly limit for new drafts has been reached. "
                                                     "Showing the latest verified draft is still possible.")
    if uid is not None:
        mine = ledger_call(ledger, "count_runs", 3600, uid, default=0)
        if mine >= settings.runs_per_hour_per_user:
            raise HTTPException(status_code=429, detail=(
                f"You have reached the limit of {settings.runs_per_hour_per_user} new drafts per hour. "
                "The latest verified draft is shown; please come back later for a new one."))


def _from_ledger_when_busy(fund_id: str, month: str, reason: str) -> dict | None:
    """When Gemini cannot answer, the latest verified draft from the ledger, marked as such, else None."""
    found = ledger_call(ledger, "latest_verified", fund_id, month, default=None)
    if not found:
        return None
    result = dict(found["result"])
    result.setdefault("run_id", found.get("run_id"))
    result["served_from"] = "ledger"
    result["served_reason"] = reason
    result["served_at"] = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
    return result


@app.post("/api/generate")
async def generate(req: GenerateRequest, request: Request) -> dict:
    mode = "demo" if req.mode == "demo" else settings.mode
    if not (pathlib.Path(settings.data_dir) / f"{req.fund_id.lower()}_{req.month}.json").exists():
        raise HTTPException(status_code=404, detail=f"No data for {req.fund_id} {req.month}.")
    identity = getattr(request.state, "identity", None)
    uid = identity.uid if identity is not None else None
    if mode == "gemini":
        _check_rate_limits(uid)
    run_id = ledger_call(ledger, "start_run", req.fund_id, req.month, uid, default=None)
    writer_model, reviewer_model = build_models(
        mode, model=settings.model, reviewer_model=settings.reviewer_model, project=settings.project,
        location=settings.location, demo_scenario=req.scenario, fallback_location=settings.fallback_location)
    try:
        result = await run_pipeline(writer_model=writer_model, reviewer_model=reviewer_model,
                                    data_dir=settings.data_dir, fund_id=req.fund_id.upper(),
                                    month=req.month, max_iterations=settings.max_iterations)
    except Exception as exc:  # noqa: BLE001 - report model/platform errors to the UI, in plain words
        log.exception("Pipeline failed")
        if run_id is not None:
            ledger_call(ledger, "fail_run", run_id, "quota" if is_quota_error(exc) else type(exc).__name__)
        # The cause in plain words (what happened and what to do), never the exception text itself.
        if is_quota_error(exc):
            busy = (f"Gemini was too busy to answer (429, quota exhausted on the {settings.location} endpoint after "
                    "retrying for about a minute).")
            served = _from_ledger_when_busy(req.fund_id, req.month, busy)
            if served is not None:
                return served   # the reader still gets a verified draft; the record says where it came from
            raise HTTPException(status_code=503, detail=busy + " No verified draft is on record yet. Please try again in a minute.")
        raise HTTPException(status_code=502, detail=(
            f"The draft could not be completed ({type(exc).__name__} while calling the model or building the draft). "
            "Nothing was saved. Please try again in a minute."))
    result["engine"] = {"mode": mode,
                        "model": settings.model if mode == "gemini" else "offline demo (no AI model)",
                        "scenario": req.scenario if mode == "demo" else None,
                        # Where the model calls were actually answered (the configured location, or the
                        # fallback after a 429), so the printed record never names an endpoint not used.
                        "locations": sorted({c["endpoint"] or settings.location for c in result.get("usage") or []})
                                     if mode == "gemini" else []}
    result["run_id"] = run_id
    result["served_from"] = "live"
    if run_id is not None:
        ledger_call(ledger, "finish_run", run_id, result)
    return result


class ApproveRequest(BaseModel):
    run_id: str
    name: str


@app.post("/api/approve")
def approve(body: ApproveRequest) -> dict:
    """Record who approved a draft and when. Only a draft on the ledger (a run that completed) can be approved."""
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="A reviewer name is needed.")
    approval = ledger_call(ledger, "record_approval", body.run_id, name, default=None)
    if approval is None:
        raise HTTPException(status_code=404, detail="That draft is not on the ledger, so its approval cannot be recorded.")
    return {"ok": True, "approval": approval}
