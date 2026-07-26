from __future__ import annotations

import unittest

from app.core.config import Settings


class TestConfigEnvContract(unittest.TestCase):
    def _valid_prod_kwargs(self, **overrides):
        payload = {
            "app_env": "prod",
            "secret_encryption_key": "prod-secret-key",
            "auth_dev_fallback_user_id": None,
            "cors_origins": "https://app.example.com,https://admin.example.com",
            "auth_admin_password": "Stronger-Admin-Password-123",
            "task_queue_backend": "rq",
            "redis_url": "redis://redis:6379/0",
        }
        payload.update(overrides)
        return payload

    def test_accepts_test_environment_alias(self) -> None:
        settings = Settings(app_env="testing")
        self.assertEqual(settings.app_env, "test")

    def test_prod_requires_explicit_cors_allowlist(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            Settings(**self._valid_prod_kwargs(cors_origins=""))
        self.assertIn("CORS_ORIGINS", str(ctx.exception))

    def test_prod_rejects_dev_fallback_user(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            Settings(**self._valid_prod_kwargs(auth_dev_fallback_user_id="local-user"))
        self.assertIn("AUTH_DEV_FALLBACK_USER_ID", str(ctx.exception))

    def test_prod_rejects_weak_default_admin_password(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            Settings(**self._valid_prod_kwargs(auth_admin_password="ChangeMe123!"))
        self.assertIn("AUTH_ADMIN_PASSWORD", str(ctx.exception))

    def test_prod_rejects_env_example_template_admin_password(self) -> None:
        # deploy#8/M59: 历史模板密码 admin-pass 长度 >=8，必须靠黑名单拦截。
        with self.assertRaises(ValueError) as ctx:
            Settings(**self._valid_prod_kwargs(auth_admin_password="admin-pass"))
        self.assertIn("AUTH_ADMIN_PASSWORD", str(ctx.exception))

    def test_prod_rejects_wildcard_cors_origin(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            Settings(**self._valid_prod_kwargs(cors_origins="*"))
        self.assertIn("CORS_ORIGINS", str(ctx.exception))

    def test_prod_accepts_valid_security_contract(self) -> None:
        settings = Settings(**self._valid_prod_kwargs())
        self.assertEqual(settings.app_env, "prod")
        self.assertEqual(
            settings.cors_origins_list(),
            ["https://app.example.com", "https://admin.example.com"],
        )


if __name__ == "__main__":
    unittest.main()
