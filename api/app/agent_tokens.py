from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request, status

from app.config import Settings, get_settings

TOKEN_PREFIX = "agent-v1"
ISSUER = "agent-service"
AUDIENCE = "notes"


@dataclass(frozen=True)
class AgentTokenClaims:
    subject: str
    scope: str
    expires_at: int


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _sign(payload: str, secret: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), payload.encode("ascii"), hashlib.sha256).digest()
    return _b64encode(digest)


def encode_agent_token(
    *, secret: str, subject: str, scope: str, expires_at: int | None = None
) -> str:
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": subject,
        "scope": scope,
        "iat": now,
        "exp": expires_at if expires_at is not None else now + 300,
    }
    payload = _b64encode(json.dumps(claims, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    return f"{TOKEN_PREFIX}.{payload}.{_sign(payload, secret)}"


def decode_agent_token(token: str, *, secret: str) -> AgentTokenClaims | None:
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != TOKEN_PREFIX:
        return None
    payload, signature = parts[1], parts[2]
    if not hmac.compare_digest(signature, _sign(payload, secret)):
        return None
    try:
        raw = json.loads(_b64decode(payload))
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict) or raw.get("iss") != ISSUER or raw.get("aud") != AUDIENCE:
        return None
    subject, scope, expires_at = raw.get("sub"), raw.get("scope"), raw.get("exp")
    if not isinstance(subject, str) or not isinstance(scope, str) or not isinstance(expires_at, int):
        return None
    if not subject or expires_at <= int(time.time()):
        return None
    return AgentTokenClaims(subject=subject, scope=scope, expires_at=expires_at)


def require_agent_scope(scope: str):
    def dependency(
        request: Request,
        settings: Settings = Depends(get_settings),
    ) -> AgentTokenClaims:
        authorization = request.headers.get("authorization", "")
        token = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
        if not token or not settings.agent_integration_token_secret:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid agent token.")
        claims = decode_agent_token(token, secret=settings.agent_integration_token_secret)
        if claims is None or claims.scope != scope:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid agent token.")
        return claims

    return dependency
