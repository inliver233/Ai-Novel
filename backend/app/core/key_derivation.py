"""Key-domain separation for HMAC consumers (backend-api#4 / backend-core#8).

The session-cookie signing key and the rate-limit identity key are both
derived from the master key material via HKDF-SHA256 with versioned info
labels, so neither equals the Fernet encryption key and each domain can be
rotated by bumping its label independently.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import secrets

from app.core.config import settings
from app.core.logging import log_event

logger = logging.getLogger("ainovel")

_AUTH_SESSION_INFO = b"ainovel/auth-session/v3"
_RATE_LIMIT_INFO = b"ainovel/rate-limit/v1"

_DEV_MASTER_KEY: bytes | None = None


def hkdf_sha256(ikm: bytes, *, salt: bytes = b"", info: bytes, length: int = 32) -> bytes:
    """RFC 5869 HKDF (extract-then-expand) over SHA-256."""
    if length <= 0 or length > 255 * hashlib.sha256().digest_size:
        raise ValueError("invalid HKDF output length")
    prk = hmac.new(salt or bytes(hashlib.sha256().digest_size), ikm, hashlib.sha256).digest()
    okm = b""
    block = b""
    counter = 1
    while len(okm) < length:
        block = hmac.new(prk, block + info + bytes([counter]), hashlib.sha256).digest()
        okm += block
        counter += 1
    return okm[:length]


def _master_key_material() -> bytes:
    if settings.auth_session_signing_key:
        return settings.auth_session_signing_key.encode("utf-8")

    if settings.secret_encryption_key:
        try:
            return base64.urlsafe_b64decode(settings.secret_encryption_key.encode("ascii"))
        except Exception:
            # 非 base64 的 SECRET_ENCRYPTION_KEY 按原始字节使用（向后兼容），但必须留痕。
            log_event(
                logger,
                "warning",
                event="KEY_DERIVATION",
                action="master_key_b64_decode_failed",
                fallback="raw_utf8_bytes",
            )
            return settings.secret_encryption_key.encode("utf-8")

    global _DEV_MASTER_KEY
    if _DEV_MASTER_KEY is None:
        _DEV_MASTER_KEY = secrets.token_bytes(32)
    return _DEV_MASTER_KEY


def derive_auth_session_key() -> bytes:
    return hkdf_sha256(_master_key_material(), info=_AUTH_SESSION_INFO)


def derive_rate_limit_key() -> bytes:
    return hkdf_sha256(_master_key_material(), info=_RATE_LIMIT_INFO)


def hash_rate_limit_identity(identity: str) -> str:
    """Public rate-limit identity digest (hex) — the stable API for rate limiting."""
    return hmac.new(derive_rate_limit_key(), identity.encode("utf-8"), hashlib.sha256).hexdigest()
