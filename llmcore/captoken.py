"""Capability tokens — HMAC-SHA256 signed scopes the kernel hands to workers.

Wire form: ``wlwl.captok.v1.<base64url(payload_json)>.<hex_sig>``

Payload is a canonical-JSON dict (sorted keys, no whitespace). HMAC-SHA256
over the bytes of the payload. We deliberately do not use JWT to dodge
algorithm-confusion footguns.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass

_PREFIX = "wlwl.captok.v1."


@dataclass(frozen=True)
class CapToken:
    issuer: str
    subject: str
    capabilities: tuple[str, ...]
    issued_at: int
    expires_at: int
    nonce: str

    def to_payload(self) -> dict:
        return {
            "iss": self.issuer,
            "sub": self.subject,
            "cap": list(self.capabilities),
            "iat": self.issued_at,
            "exp": self.expires_at,
            "nonce": self.nonce,
        }

    def expired(self, *, now: int | None = None, grace: int = 0) -> bool:
        return (now or int(time.time())) > (self.expires_at + grace)

    def covers(self, required: str) -> bool:
        if required in self.capabilities:
            return True
        # glob suffix: required="vision.ocr.*" or capability="forum.mod.*"
        for cap in self.capabilities:
            if cap.endswith(".*") and required.startswith(cap[:-1]):
                return True
            if required.endswith(".*") and cap.startswith(required[:-1]):
                return True
        return False


def _canonical(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def sign(token: CapToken, signing_key: bytes) -> str:
    payload = _canonical(token.to_payload())
    sig = hmac.new(signing_key, payload, hashlib.sha256).hexdigest()
    return f"{_PREFIX}{_b64url(payload)}.{sig}"


def verify(wire: str, signing_key: bytes) -> CapToken:
    if not wire.startswith(_PREFIX):
        raise ValueError("not a captok v1 token")
    body = wire[len(_PREFIX):]
    try:
        payload_b64, sig = body.rsplit(".", 1)
    except ValueError as e:
        raise ValueError("malformed token") from e
    payload_bytes = _b64url_decode(payload_b64)
    expected = hmac.new(signing_key, payload_bytes, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        raise ValueError("signature mismatch")
    p = json.loads(payload_bytes)
    return CapToken(
        issuer=p["iss"], subject=p["sub"],
        capabilities=tuple(p["cap"]),
        issued_at=int(p["iat"]), expires_at=int(p["exp"]),
        nonce=p["nonce"],
    )


def mint(*, issuer: str, subject: str, capabilities: tuple[str, ...],
         signing_key: bytes, ttl_sec: int = 3600) -> tuple[CapToken, str]:
    now = int(time.time())
    tok = CapToken(
        issuer=issuer, subject=subject, capabilities=tuple(capabilities),
        issued_at=now, expires_at=now + ttl_sec,
        nonce=secrets.token_hex(8),
    )
    return tok, sign(tok, signing_key)


def random_signing_key() -> bytes:
    """In-memory signing key for first-run / auto-promote scenarios.

    Real deployments resolve `kernel.delegation.signing_key` through
    `launcher.config_store` keyring. This helper exists for tests +
    ephemeral kernels that should not write a key to disk."""
    return os.urandom(32)
