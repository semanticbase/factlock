"""Who is calling: Firebase Authentication (anonymous sign-in) for the public demo.

Modes (FACTLOCK_AUTH):
* passphrase — one shared passphrase, cookie session (app.main; the original demo gate).
* firebase   — the page signs the visitor in anonymously with the Firebase JS SDK and sends the ID token
               as "Authorization: Bearer <token>" on every API call. The server verifies the token with
               the Firebase Admin SDK and uses the uid for per-visitor usage limits and the ledger.
* none       — no gate (local previews).

The submission rules ask for a publicly accessible prototype; anonymous sign-in keeps it public while
still giving each visitor an identity to count against, and a record of who drafted what.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Callable

log = logging.getLogger("factlock.auth")


class AuthError(Exception):
    """The request carries no valid identity. .detail is safe to show to the caller."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


@dataclass(frozen=True)
class Identity:
    uid: str
    provider: str


def bearer_token(authorization: str | None) -> str | None:
    """The token from an "Authorization: Bearer <token>" header, else None."""
    if not authorization:
        return None
    scheme, _, token = authorization.strip().partition(" ")
    token = token.strip()
    return token if scheme.lower() == "bearer" and token else None


def claims_to_identity(claims: dict) -> Identity:
    """The identity in verified ID-token claims (shape per the Firebase Admin SDK: uid at the top level,
    the provider under firebase.sign_in_provider). Missing pieces are an error, never a guess."""
    uid = claims.get("uid") or claims.get("sub")
    if not uid or not isinstance(uid, str):
        raise AuthError("The sign-in token has no user id.")
    provider = ((claims.get("firebase") or {}).get("sign_in_provider")) or "unknown"
    return Identity(uid=uid, provider=str(provider))


class FirebaseAuth:
    """Verifies Firebase ID tokens. The verifier is injectable so tests never touch the network."""

    def __init__(self, project: str | None, api_key: str, auth_domain: str, app_id: str,
                 verifier: Callable[[str], dict] | None = None) -> None:
        self.project, self.api_key, self.auth_domain, self.app_id = project, api_key, auth_domain, app_id
        self._verifier = verifier
        self._lock = threading.Lock()

    def config(self) -> dict:
        """What the page needs to initialise the Firebase JS SDK. The web API key is a public identifier
        by design (it is not a secret); the project's Authentication settings restrict its use."""
        return {"apiKey": self.api_key, "authDomain": self.auth_domain, "projectId": self.project, "appId": self.app_id}

    def _default_verifier(self) -> Callable[[str], dict]:
        import firebase_admin
        from firebase_admin import auth as fb_auth
        with self._lock:
            try:
                app = firebase_admin.get_app()
            except ValueError:
                options = {"projectId": self.project} if self.project else None
                app = firebase_admin.initialize_app(options=options)
        return lambda token: fb_auth.verify_id_token(token, app=app, clock_skew_seconds=60)

    def identify(self, authorization: str | None) -> Identity:
        token = bearer_token(authorization)
        if token is None:
            raise AuthError("Sign in first (no sign-in token was sent).")
        if self._verifier is None:
            self._verifier = self._default_verifier()
        try:
            claims = self._verifier(token)
        except Exception as exc:  # noqa: BLE001 - expired, malformed, wrong project: all are "not signed in"
            log.info("ID token rejected: %s", type(exc).__name__)
            raise AuthError("The sign-in token is not valid or has expired. Reload the page to sign in again.")
        return claims_to_identity(claims)
