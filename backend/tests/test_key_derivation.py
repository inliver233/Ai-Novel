"""backend-api#4 / backend-core#8：密钥域分离（HKDF-SHA256 + 版本化 info 标签）。

会话签名密钥与限流身份 HMAC 密钥必须从主密钥派生且互不相同，
也不得等于 Fernet 加密密钥原文（消除密钥域混用）。
"""

from __future__ import annotations

import base64
import unittest
from unittest.mock import patch

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app.core.config import settings
from app.core.key_derivation import (
    derive_auth_session_key,
    derive_rate_limit_key,
    hash_rate_limit_identity,
    hkdf_sha256,
)


class TestHkdfSha256(unittest.TestCase):
    def test_matches_rfc5869_appendix_a1(self) -> None:
        ikm = bytes.fromhex("0b" * 22)
        salt = bytes.fromhex("000102030405060708090a0b0c")
        info = bytes.fromhex("f0f1f2f3f4f5f6f7f8f9")
        expected = bytes.fromhex(
            "3cb25f25faacd57a90434f64d0362f2a"
            "2d2d0a90cf1a5a4c5db02d56ecc4c5bf"
            "34007208d5b887185865"
        )
        self.assertEqual(hkdf_sha256(ikm, salt=salt, info=info, length=42), expected)

    def test_matches_cryptography_hkdf_with_empty_salt(self) -> None:
        ikm = b"master-key-material"
        info = b"ainovel/test/v1"
        reference = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=info).derive(ikm)
        self.assertEqual(hkdf_sha256(ikm, info=info, length=32), reference)


class TestDomainSeparation(unittest.TestCase):
    def test_session_and_rate_limit_keys_are_domain_separated(self) -> None:
        fernet_key = Fernet.generate_key().decode("ascii")
        with patch.object(settings, "auth_session_signing_key", None), patch.object(
            settings, "secret_encryption_key", fernet_key
        ):
            session_key = derive_auth_session_key()
            rate_limit_key = derive_rate_limit_key()
        raw_master = base64.urlsafe_b64decode(fernet_key.encode("ascii"))
        self.assertNotEqual(session_key, rate_limit_key)
        self.assertNotEqual(session_key, raw_master)
        self.assertNotEqual(rate_limit_key, raw_master)

    def test_hash_rate_limit_identity_is_deterministic_hex(self) -> None:
        first = hash_rate_limit_identity("user@example.com")
        second = hash_rate_limit_identity("user@example.com")
        other = hash_rate_limit_identity("other@example.com")
        self.assertEqual(first, second)
        self.assertNotEqual(first, other)
        self.assertEqual(len(first), 64)
        int(first, 16)  # 必须是合法 hex

    def test_rotating_master_key_changes_rate_limit_hash(self) -> None:
        key_a = Fernet.generate_key().decode("ascii")
        key_b = Fernet.generate_key().decode("ascii")
        with patch.object(settings, "auth_session_signing_key", None):
            with patch.object(settings, "secret_encryption_key", key_a):
                hash_a = hash_rate_limit_identity("user@example.com")
            with patch.object(settings, "secret_encryption_key", key_b):
                hash_b = hash_rate_limit_identity("user@example.com")
        self.assertNotEqual(hash_a, hash_b)

    def test_explicit_signing_key_takes_priority_over_encryption_key(self) -> None:
        with patch.object(settings, "auth_session_signing_key", "operator-signing-key"), patch.object(
            settings, "secret_encryption_key", Fernet.generate_key().decode("ascii")
        ):
            with_signing = derive_auth_session_key()
        with patch.object(settings, "auth_session_signing_key", None), patch.object(
            settings, "secret_encryption_key", Fernet.generate_key().decode("ascii")
        ):
            without_signing = derive_auth_session_key()
        self.assertNotEqual(with_signing, without_signing)


if __name__ == "__main__":
    unittest.main()
