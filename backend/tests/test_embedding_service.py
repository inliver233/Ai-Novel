import importlib.util
import json
import unittest
from unittest.mock import patch

import httpx

from app.services.embedding_service import embed_texts


class TestEmbeddingService(unittest.TestCase):
    def test_openai_compatible_embeddings(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.method, "POST")
            self.assertEqual(str(request.url), "http://stubbed-openai.local/v1/embeddings")
            self.assertEqual(request.headers.get("authorization"), "Bearer openai-test-SECRET")

            payload = json.loads(request.content.decode("utf-8"))
            self.assertEqual(payload.get("model"), "text-embedding-test")
            self.assertEqual(payload.get("input"), ["a", "b"])

            return httpx.Response(200, json={"data": [{"embedding": [0.1, 0.2]}, {"embedding": [0.3, 0.4]}]})

        transport = httpx.MockTransport(handler)
        with httpx.Client(transport=transport) as client:
            with patch("app.services.embedding_service.get_llm_http_client", return_value=client):
                out = embed_texts(
                    ["a", "b"],
                    embedding={
                        "provider": "openai_compatible",
                        "base_url": "http://stubbed-openai.local/v1/",
                        "model": "text-embedding-test",
                        "api_key": "openai-test-SECRET",
                        "expected_dimension": 2,
                    },
                )

        self.assertTrue(out.get("enabled"))
        self.assertIsNone(out.get("disabled_reason"))
        self.assertEqual(out.get("vectors"), [[0.1, 0.2], [0.3, 0.4]])

    def test_azure_openai_embeddings(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.method, "POST")
            self.assertEqual(request.url.path, "/openai/deployments/embd-depl/embeddings")
            self.assertEqual(request.url.params.get("api-version"), "2023-05-15")
            self.assertEqual(request.headers.get("api-key"), "azure-test-SECRET")
            self.assertIsNone(request.headers.get("authorization"))

            payload = json.loads(request.content.decode("utf-8"))
            self.assertEqual(payload.get("input"), ["a"])
            self.assertNotIn("model", payload)

            return httpx.Response(200, json={"data": [{"embedding": [1.0, 2.0, 3.0]}]})

        transport = httpx.MockTransport(handler)
        with httpx.Client(transport=transport) as client:
            with patch("app.services.embedding_service.get_llm_http_client", return_value=client):
                out = embed_texts(
                    ["a"],
                    embedding={
                        "provider": "azure_openai",
                        "base_url": "http://stubbed-azure.local",
                        "api_key": "azure-test-SECRET",
                        "azure_deployment": "embd-depl",
                        "azure_api_version": "2023-05-15",
                        "expected_dimension": 3,
                    },
                )

        self.assertTrue(out.get("enabled"))
        self.assertIsNone(out.get("disabled_reason"))
        self.assertEqual(out.get("vectors"), [[1.0, 2.0, 3.0]])

    def test_google_gemini_batch_embeddings(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.method, "POST")
            self.assertEqual(request.url.path, "/v1beta/models/text-embedding-004:batchEmbedContents")
            self.assertEqual(request.url.query, b"")
            self.assertEqual(request.headers.get("x-goog-api-key"), "google-test-SECRET")

            payload = json.loads(request.content.decode("utf-8"))
            reqs = payload.get("requests")
            self.assertIsInstance(reqs, list)
            self.assertEqual(len(reqs), 2)
            self.assertEqual(reqs[0]["content"]["parts"][0]["text"], "a")
            self.assertEqual(reqs[1]["content"]["parts"][0]["text"], "b")

            return httpx.Response(200, json={"embeddings": [{"values": [0.0, 0.5]}, {"values": [0.25, 0.75]}]})

        transport = httpx.MockTransport(handler)
        with httpx.Client(transport=transport) as client:
            with patch("app.services.embedding_service.get_llm_http_client", return_value=client):
                out = embed_texts(
                    ["a", "b"],
                    embedding={
                        "provider": "google",
                        "base_url": "http://stubbed-gemini.local",
                        "model": "text-embedding-004",
                        "api_key": "google-test-SECRET",
                        "expected_dimension": 2,
                    },
                )

        self.assertTrue(out.get("enabled"))
        self.assertIsNone(out.get("disabled_reason"))
        self.assertEqual(out.get("vectors"), [[0.0, 0.5], [0.25, 0.75]])

    def test_missing_config_is_fail_soft(self) -> None:
        out = embed_texts(["a"], embedding={"provider": "openai_compatible"})
        self.assertFalse(out.get("enabled"))
        self.assertEqual(out.get("disabled_reason"), "embedding_base_url_missing")
        self.assertEqual(out.get("vectors"), [])

    def test_sentence_transformers_optional_dependency_is_fail_soft(self) -> None:
        if importlib.util.find_spec("sentence_transformers") is not None:
            self.skipTest("sentence_transformers already installed in this environment")

        out = embed_texts(
            ["hello"],
            embedding={
                "provider": "sentence_transformers",
                "sentence_transformers_model": "all-MiniLM-L6-v2",
            },
        )
        self.assertFalse(out.get("enabled"))
        self.assertEqual(out.get("disabled_reason"), "dependency_missing")
        self.assertEqual(out.get("vectors"), [])


if __name__ == "__main__":
    unittest.main()
