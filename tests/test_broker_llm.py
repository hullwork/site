from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from sites.broker import llm


class FakeLLMTests(unittest.TestCase):
    def test_records_calls_and_returns_configured_response(self) -> None:
        fake = llm.FakeLLM("hello", usage=llm.LLMUsage(input_tokens=3, output_tokens=4))
        result = fake.complete(model="glm-5.1", messages=[{"role": "user", "content": "hi"}])
        self.assertEqual(result.text, "hello")
        self.assertEqual(result.usage.input_tokens, 3)
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(fake.calls[0]["model"], "glm-5.1")

    def test_response_can_be_a_callable(self) -> None:
        fake = llm.FakeLLM(lambda messages: f"echo:{messages[0]['content']}")
        result = fake.complete(model="m", messages=[{"role": "user", "content": "x"}])
        self.assertEqual(result.text, "echo:x")

    def test_raises_configured_exception(self) -> None:
        fake = llm.FakeLLM(raises=llm.LLMUnavailable("boom"))
        with self.assertRaises(llm.LLMUnavailable):
            fake.complete(model="m", messages=[])


class _OpenAIStyleHandler(BaseHTTPRequestHandler):
    response_body: dict = {}
    status_code = 200
    last_request: dict | None = None

    def do_POST(self) -> None:  # noqa: N802 - stdlib method name
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        type(self).last_request = {
            "path": self.path,
            "body": json.loads(body) if body else None,
            "authorization": self.headers.get("Authorization"),
        }
        payload = json.dumps(type(self).response_body).encode("utf-8")
        self.send_response(type(self).status_code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args) -> None:  # silence stderr during tests
        pass


class HttpLLMClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = HTTPServer(("127.0.0.1", 0), _OpenAIStyleHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.thread.join, timeout=5)
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def test_completes_and_parses_usage(self) -> None:
        _OpenAIStyleHandler.response_body = {
            "choices": [{"message": {"content": "the answer"}}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 34},
        }
        client = llm.HttpLLMClient(base_url=self.base_url, api_key="secret-key")
        result = client.complete(model="glm-5.1", messages=[{"role": "user", "content": "hi"}])
        self.assertEqual(result.text, "the answer")
        self.assertEqual(result.usage.input_tokens, 12)
        self.assertEqual(result.usage.output_tokens, 34)
        self.assertEqual(_OpenAIStyleHandler.last_request["path"], "/chat/completions")
        self.assertEqual(_OpenAIStyleHandler.last_request["authorization"], "Bearer secret-key")

    def test_output_schema_requests_json_object_mode(self) -> None:
        _OpenAIStyleHandler.response_body = {
            "choices": [{"message": {"content": "{}"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }
        client = llm.HttpLLMClient(base_url=self.base_url)
        client.complete(model="m", messages=[], output_schema={"type": "object"})
        self.assertEqual(
            _OpenAIStyleHandler.last_request["body"]["response_format"], {"type": "json_object"}
        )

    def test_malformed_response_raises_llm_unavailable(self) -> None:
        _OpenAIStyleHandler.response_body = {"unexpected": "shape"}
        client = llm.HttpLLMClient(base_url=self.base_url)
        with self.assertRaises(llm.LLMUnavailable):
            client.complete(model="m", messages=[])

    def test_unreachable_host_raises_llm_unavailable(self) -> None:
        client = llm.HttpLLMClient(base_url="http://127.0.0.1:1", connect_timeout_seconds=0.5, read_timeout_seconds=0.5)
        with self.assertRaises(llm.LLMUnavailable):
            client.complete(model="m", messages=[])


class TierResolutionTests(unittest.TestCase):
    MODEL_TIERS = {
        "fast": {"model": "glm-5.1", "input_credits_per_1k": 1, "output_credits_per_1k": 2},
    }

    def test_resolves_known_tier(self) -> None:
        tier = llm.resolve_tier(self.MODEL_TIERS, "fast")
        self.assertIsNotNone(tier)
        self.assertEqual(tier.model, "glm-5.1")
        self.assertEqual(tier.input_credits_per_1k, 1)

    def test_unknown_tier_returns_none_not_a_crash(self) -> None:
        self.assertIsNone(llm.resolve_tier(self.MODEL_TIERS, "does-not-exist"))

    def test_malformed_tiers_container_returns_none(self) -> None:
        self.assertIsNone(llm.resolve_tier(None, "fast"))
        self.assertIsNone(llm.resolve_tier({"fast": "not-a-dict"}, "fast"))

    def test_bad_pricing_values_return_none(self) -> None:
        tiers = {"fast": {"model": "m", "input_credits_per_1k": "oops", "output_credits_per_1k": 1}}
        self.assertIsNone(llm.resolve_tier(tiers, "fast"))

    def test_price_credits_rounds_up(self) -> None:
        tier = llm.TierConfig(tier="fast", model="m", input_credits_per_1k=1, output_credits_per_1k=2)
        usage = llm.LLMUsage(input_tokens=100, output_tokens=100)  # 0.1 + 0.2 = 0.3 credits
        self.assertEqual(llm.price_credits(tier, usage), 1)

    def test_price_credits_zero_usage_is_zero(self) -> None:
        tier = llm.TierConfig(tier="fast", model="m", input_credits_per_1k=1, output_credits_per_1k=2)
        self.assertEqual(llm.price_credits(tier, llm.LLMUsage(input_tokens=0, output_tokens=0)), 0)


if __name__ == "__main__":
    unittest.main()
