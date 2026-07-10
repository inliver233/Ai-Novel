import unittest
from unittest.mock import MagicMock, patch

from app import bootstrap
from app.core.errors import AppError


class TestAdminBootstrapFailSoft(unittest.TestCase):
    def test_dev_short_password_skips_admin_bootstrap(self) -> None:
        dummy_db = MagicMock()

        with (
            patch.object(bootstrap, "SessionLocal", return_value=dummy_db),
            patch.object(bootstrap, "ensure_admin_user", side_effect=AppError.validation("密码长度至少 8 位")),
            patch.object(bootstrap.settings, "app_env", "dev"),
            patch.object(bootstrap.settings, "auth_admin_password", "short"),
            patch.object(bootstrap, "log_event") as log_event,
        ):
            bootstrap.ensure_configured_admin_user()

        dummy_db.close.assert_called()
        self.assertTrue(log_event.called)
        _args, kwargs = log_event.call_args
        self.assertEqual(kwargs.get("event"), "AUTH_ADMIN_BOOTSTRAP")
        self.assertEqual(kwargs.get("action"), "skipped")
        self.assertEqual(kwargs.get("reason"), "invalid_password")

    def test_prod_short_password_raises(self) -> None:
        dummy_db = MagicMock()

        with (
            patch.object(bootstrap, "SessionLocal", return_value=dummy_db),
            patch.object(bootstrap, "ensure_admin_user", side_effect=AppError.validation("密码长度至少 8 位")),
            patch.object(bootstrap.settings, "app_env", "prod"),
            patch.object(bootstrap.settings, "auth_admin_password", "short"),
        ):
            with self.assertRaises(AppError):
                bootstrap.ensure_configured_admin_user()

        dummy_db.close.assert_called()
