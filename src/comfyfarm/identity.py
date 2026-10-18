"""Identity (docs/design/08 'Identity providers', 09-security).

The router trusts the identity headers only when the gateway vouches for them with a shared secret; otherwise they are
stripped before any endpoint sees them. Roles travel with the user — rollout, profiles and worker administration are
admin-only, so an identity provider that forgot roles would leave those operations open.
"""
from __future__ import annotations

import base64
import contextvars
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass

USER_H, ROLES_H, GATEWAY_H = b"x-comfyfarm-user", b"x-comfyfarm-roles", b"x-comfyfarm-gateway"
current_roles: contextvars.ContextVar[frozenset] = contextvars.ContextVar("comfyfarm_roles", default=frozenset())


@dataclass(frozen=True)
class Identity:
    user: str
    roles: frozenset = frozenset()
    epoch: int | None = None        # local sessions: users.session_epoch at login (D-35)


class TrustGatewayHeaders:
    """ASGI middleware for the router: keep x-comfyfarm-user/roles only if x-comfyfarm-gateway carries the shared
    secret; strip them otherwise (a browser could send them itself)."""

    def __init__(self, app, secret: str | None, dev: bool = False):
        # dev = loopback development identity mode: headers are trusted without a gateway (tests, single-user laptop)
        self.app, self.secret, self.dev = app, secret.encode() if secret else None, dev

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            headers = scope.get("headers", [])
            loopback = (scope.get("client") or ("",))[0] in ("127.0.0.1", "::1")
            vouched = (self.dev and loopback) or (self.secret is not None and any(
                k == GATEWAY_H and hmac.compare_digest(v, self.secret) for k, v in headers))
            if not vouched:
                headers = [(k, v) for k, v in headers if k not in (USER_H, ROLES_H)]
            roles = frozenset(r.decode().strip() for k, v in headers if k == ROLES_H for r in v.split(b",") if r.strip()) \
                if vouched else frozenset()
            scope = dict(scope, headers=[(k, v) for k, v in headers if k != GATEWAY_H])
            token = current_roles.set(roles)
            try:
                await self.app(scope, receive, send)
            finally:
                current_roles.reset(token)
            return
        await self.app(scope, receive, send)


# ---- password hashing (local provider) -----------------------------------------------------------------------------
def hash_password(password: str, *, n: int = 2**14) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=n, r=8, p=1, dklen=32)
    return f"scrypt${n}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, salt, dk = stored.split("$")
        if scheme != "scrypt":
            return False
        got = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=8, p=1, dklen=32)
        return hmac.compare_digest(got, base64.b64decode(dk))
    except Exception:
        return False


# ---- signed session cookie ----------------------------------------------------------------------------------------
def sign_session(identity: Identity, key: bytes, ttl: int = 7 * 86400) -> str:
    body = base64.urlsafe_b64encode(json.dumps({"u": identity.user, "e": identity.epoch,
                                                 "exp": int(time.time()) + ttl}).encode()).decode()
    mac = hmac.new(key, body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{mac}"


def read_session(token: str | None, key: bytes) -> Identity | None:
    if not token or "." not in token:
        return None
    body, mac = token.rsplit(".", 1)
    # bytes: compare_digest raises on non-ASCII str, and a cookie is whatever the client sent
    if not hmac.compare_digest(mac.encode("utf-8", "replace"), hmac.new(key, body.encode("utf-8", "replace"), hashlib.sha256).hexdigest().encode()):
        return None
    try:
        d = json.loads(base64.urlsafe_b64decode(body.encode()))
    except ValueError:
        return None
    if d.get("exp", 0) < time.time():
        return None
    # roles are not trusted from the cookie: the gateway asks the router for the current ones (D-35)
    return Identity(d["u"], frozenset(), d.get("e"))
