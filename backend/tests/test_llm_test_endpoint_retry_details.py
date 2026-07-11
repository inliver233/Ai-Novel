from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.testclient import TestClient

from app.api.deps import get_enabled_current_user_id
from app.api.routes import llm as llm_routes
from app.core.errors import AppError
from app.main import app_error_handler, validation_error_handler


def _make_test_app() -> FastAPI:
    app = FastAPI()

    def _test_user_id(request: Request) -> str:
        return str(request.state.user_id)

    @app.middleware("http")
    async def _test_user_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
        request.state.request_id = "rid-test"
        user_id = request.headers.get("X-Test-User") or "u_test"
        request.state.user_id = user_id
        request.state.authenticated_user_id = user_id
        request.state.session_expire_at = None
        request.state.auth_source = "test"
        return await call_next(request)

    app.add_exception_handler(AppError, app_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.dependency_overrides[get_enabled_current_user_id] = _test_user_id
    app.include_router(llm_routes.router, prefix="/api")
    return app


class TestLlmTestEndpointRetryDetails(unittest.TestCase):
    def setUp(self) -> None:
        self.app = _make_test_app()

    def _req_body(self) -> dict[str, object]:
        return {"provider": "openai", "model": "gpt-test", "timeout_seconds": 5}

    def test_llm_test_retries_then_succeeds(self) -> None:
        client = TestClient(self.app)

        timeout_exc = AppError(code="LLM_TIMEOUT", message="timeout", status_code=504)
        ok = SimpleNamespace(text="pong", latency_ms=1, finish_reason="stop", dropped_params=[])

        with (
            patch("app.services.llm_test_app_service.task_llm_max_attempts", return_value=2),
            patch("app.services.llm_test_app_service.compute_backoff_seconds", return_value=0),
            patch("app.services.llm_test_app_service.time.sleep") as mock_sleep,
            patch("app.services.llm_test_app_service.call_llm", side_effect=[timeout_exc, ok]) as mock_call,
        ):
            resp = client.post(
                "/api/llm/test",
                headers={"X-Test-User": "u1", "X-LLM-API-Key": "sk-test"},
                json=self._req_body(),
            )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(mock_call.call_count, 2)
        mock_sleep.assert_not_called()
        payload = resp.json()
        self.assertTrue(payload.get("ok"))
        data = payload.get("data") or {}
        self.assertEqual((data.get("text") or "").strip(), "pong")

    def test_llm_test_exhausted_error_includes_attempts(self) -> None:
        client = TestClient(self.app)

        timeout_exc = AppError(
            code="LLM_TIMEOUT",
            message="timeout",
            status_code=504,
            details={"status_code": 504, "upstream_error": "gateway timeout"},
        )
        with (
            patch("app.services.llm_test_app_service.task_llm_max_attempts", return_value=2),
            patch("app.services.llm_test_app_service.compute_backoff_seconds", return_value=0),
            patch("app.services.llm_test_app_service.time.sleep"),
            patch("app.services.llm_test_app_service.call_llm", side_effect=[timeout_exc, timeout_exc]) as mock_call,
            patch("app.services.llm_test_app_service.log_event") as service_log_event,
            patch("app.main.log_event") as main_log_event,
        ):
            resp = client.post(
                "/api/llm/test",
                headers={"X-Test-User": "u1", "X-LLM-API-Key": "sk-test"},
                json=self._req_body(),
            )

        self.assertEqual(resp.status_code, 504)
        self.assertEqual(mock_call.call_count, 2)
        payload = resp.json()
        self.assertFalse(payload.get("ok"))
        details = ((payload.get("error") or {}).get("details") or {}) if isinstance(payload, dict) else {}
        attempts = details.get("attempts") or []
        self.assertEqual(details.get("provider"), "openai")
        self.assertEqual(details.get("model"), "gpt-test")
        self.assertEqual(details.get("base_url_host"), "api.openai.com")
        self.assertEqual(details.get("timeout_seconds"), 5)
        self.assertEqual(details.get("attempt_max"), 2)
        self.assertEqual(len(attempts), 2)
        self.assertEqual((attempts[0] or {}).get("error_code"), "LLM_TIMEOUT")
        self.assertEqual(service_log_event.call_count, 2)
        _service_args, service_kwargs = service_log_event.call_args
        self.assertEqual(service_kwargs.get("event"), "LLM_TEST_ATTEMPT_FAILED")
        self.assertEqual(service_kwargs.get("attempt"), 2)
        self.assertEqual(service_kwargs.get("attempt_max"), 2)
        self.assertEqual(service_kwargs.get("provider"), "openai")
        self.assertEqual(service_kwargs.get("model"), "gpt-test")
        self.assertEqual(service_kwargs.get("base_url_host"), "api.openai.com")
        self.assertEqual(service_kwargs.get("details"), {"status_code": 504, "upstream_error": "gateway timeout"})
        _main_args, main_kwargs = main_log_event.call_args
        self.assertEqual(main_kwargs.get("details", {}).get("provider"), "openai")
        self.assertEqual(main_kwargs.get("details", {}).get("attempt_max"), 2)
        self.assertEqual(len(main_kwargs.get("details", {}).get("attempts") or []), 2)

    def test_llm_test_does_not_retry_non_retryable(self) -> None:
        client = TestClient(self.app)

        bad = AppError(code="LLM_BAD_REQUEST", message="bad", status_code=400)
        with (
            patch("app.services.llm_test_app_service.task_llm_max_attempts", return_value=3),
            patch("app.services.llm_test_app_service.compute_backoff_seconds", return_value=0),
            patch("app.services.llm_test_app_service.time.sleep"),
            patch("app.services.llm_test_app_service.call_llm", side_effect=[bad]) as mock_call,
        ):
            resp = client.post(
                "/api/llm/test",
                headers={"X-Test-User": "u1", "X-LLM-API-Key": "sk-test"},
                json=self._req_body(),
            )

        self.assertEqual(resp.status_code, 400)
        self.assertEqual(mock_call.call_count, 1)
        payload = resp.json()
        self.assertFalse(payload.get("ok"))
        details = ((payload.get("error") or {}).get("details") or {}) if isinstance(payload, dict) else {}
        attempts = details.get("attempts") or []
        self.assertEqual(len(attempts), 1)
        self.assertFalse(bool((attempts[0] or {}).get("retryable")))


if __name__ == "__main__":
    unittest.main()
