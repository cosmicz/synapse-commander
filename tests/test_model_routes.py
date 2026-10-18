from __future__ import annotations

from io import BytesIO
import json
import unittest
from urllib.error import HTTPError

from synapse_commander.agents.model_routes import CrusoeRoute, OpenRouterRoute, _NoRedirect


class Response:
    def __init__(self, value: object) -> None:
        self.payload = json.dumps(value).encode("utf-8") if not isinstance(value, bytes) else value

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit: int) -> bytes:
        return self.payload


class OpenAICompatibleRouteTests(unittest.TestCase):
    def test_openrouter_reasoning_never_combines_effort_with_a_token_budget(self) -> None:
        """OpenRouter rejects the effort/budget combination for this route."""
        seen = {}

        def opener(wire, *, timeout):
            seen["body"] = json.loads(wire.data)
            return Response({"model": "nvidia/nemotron-3-nano-30b-a3b",
                             "choices": [{"message": {"content": "{}"}}]})

        reply = OpenRouterRoute("nvidia/nemotron-3-nano-30b-a3b", system_prompt="p",
                                environment={"OPENROUTER_API_KEY": "k"}, opener=opener).call("o")
        reasoning = seen["body"]["reasoning"]
        self.assertIsNone(reply.error)
        self.assertIn("max_tokens", reasoning)
        self.assertNotIn("effort", reasoning)

    def test_openrouter_posts_bounded_chat_with_the_injected_system_prompt(self) -> None:
        seen = {}

        def opener(wire, *, timeout):
            seen["url"], seen["timeout"] = wire.full_url, timeout
            seen["authorization"] = wire.get_header("Authorization")
            seen["body"] = json.loads(wire.data)
            return Response({"model": "openai/gpt-oss-120b", "choices": [{"message": {"content": "{\"action\":\"wait\"}"}}]})

        route = OpenRouterRoute("openai/gpt-oss-120b", system_prompt="bonded prompt", timeout_s=7,
                                environment={"OPENROUTER_API_KEY": "test-only-secret"}, opener=opener)
        reply = route.call("current bounded observation")
        self.assertEqual(reply.text, '{"action":"wait"}')
        self.assertEqual(reply.model_ids, ["openai/gpt-oss-120b"])
        self.assertEqual(route.name, "openrouter")
        self.assertEqual((seen["url"], seen["timeout"]),
                         ("https://openrouter.ai/api/v1/chat/completions", 7.0))
        self.assertEqual(seen["authorization"], "Bearer test-only-secret")
        self.assertEqual(seen["body"], {
            "model": "openai/gpt-oss-120b", "stream": False, "max_tokens": 4096,
            "reasoning": {"enabled": True, "max_tokens": 1500},
            "messages": [{"role": "system", "content": "bonded prompt"},
                         {"role": "user", "content": "current bounded observation"}],
        })

    def test_reported_usage_keeps_numeric_tokens_and_cost_only(self) -> None:
        def opener(_wire, *, timeout):
            return Response({"model": "nvidia/nemotron-3-nano-30b-a3b",
                             "choices": [{"message": {"content": "{}", "reasoning": "thinking"}}],
                             "usage": {"prompt_tokens": 900, "completion_tokens": 450, "total_tokens": 1350,
                                       "cost": 0.0001, "provider": "x",
                                       "completion_tokens_details": {"reasoning_tokens": 402}}})

        route = OpenRouterRoute("nvidia/nemotron-3-nano-30b-a3b", system_prompt="p",
                                environment={"OPENROUTER_API_KEY": "k"}, opener=opener)
        reply = route.call("o")
        self.assertIsNone(reply.error)
        self.assertEqual(reply.usage, {"prompt_tokens": 900, "completion_tokens": 450, "total_tokens": 1350,
                                       "cost": 0.0001, "reasoning_tokens": 402})

    def test_reasoning_that_exhausts_max_tokens_is_an_explicit_error(self) -> None:
        def opener(_wire, *, timeout):
            return Response({"model": "nvidia/nemotron-3-nano-30b-a3b",
                             "choices": [{"finish_reason": "length",
                                          "message": {"content": None, "reasoning": "We need to..."}}],
                             "usage": {"completion_tokens": 4096, "cost": 0.001}})

        reply = OpenRouterRoute("nvidia/nemotron-3-nano-30b-a3b", system_prompt="p",
                                environment={"OPENROUTER_API_KEY": "k"}, opener=opener).call("o")
        self.assertEqual((reply.error, reply.text), ("max_tokens exhausted", ""))
        self.assertEqual(reply.usage, {"completion_tokens": 4096, "cost": 0.001})

    def test_extra_body_cannot_replace_the_request_core(self) -> None:
        from synapse_commander.agents.model_routes import OpenAICompatibleRoute
        with self.assertRaises(ValueError):
            OpenAICompatibleRoute(name="x", model="m", endpoint="https://x", key_env="K", system_prompt="p",
                                  extra_body={"model": "other"}, environment={})

    def test_reasoning_request_never_sets_both_effort_and_a_token_budget(self) -> None:
        # OpenRouter answers HTTP 400 to every call that sets both.
        seen = {}

        def opener(wire, *, timeout):
            seen["body"] = json.loads(wire.data)
            return Response({"model": "m", "choices": [{"message": {"content": "{}"}}]})

        OpenRouterRoute("m", system_prompt="p", environment={"OPENROUTER_API_KEY": "k"}, opener=opener).call("o")
        reasoning = seen["body"]["reasoning"]
        self.assertTrue(reasoning.get("enabled"))
        self.assertFalse({"effort", "max_tokens"} <= set(reasoning), reasoning)

    def test_max_tokens_is_bounded(self) -> None:
        for bad in (0, 9000, True, 1.5):
            with self.assertRaises(ValueError):
                OpenRouterRoute("m", system_prompt="p", max_tokens=bad, environment={})

    def test_length_finish_is_an_explicit_retryable_budget_exhaustion(self) -> None:
        def opener(_wire, *, timeout):
            return Response({"model": "nvidia/nemotron-3-nano-30b-a3b",
                             "choices": [{"finish_reason": "length", "message": {"content": None}}],
                             "usage": {"completion_tokens_details": {"reasoning_tokens": 4096}}})

        reply = OpenRouterRoute("nvidia/nemotron-3-nano-30b-a3b", system_prompt="p",
                                environment={"OPENROUTER_API_KEY": "key"}, opener=opener).call("observation")
        self.assertEqual(reply.error, "max_tokens exhausted")
        self.assertEqual(reply.usage, {"reasoning_tokens": 4096})

    def test_crusoe_uses_its_explicit_endpoint_and_reports_the_provider_model(self) -> None:
        seen = {}

        def opener(wire, *, timeout):
            seen["url"] = wire.full_url
            return Response({"model": "nvidia/Nemotron-3-Nano-30B-A3B",
                             "choices": [{"message": {"content": "{}"}}]})

        route = CrusoeRoute("nvidia/Nemotron-3-Nano-30B-A3B", system_prompt="smoke prompt",
                            environment={"CRUSOE_API_KEY": "test-only-secret"}, opener=opener)
        reply = route.call("observation")
        self.assertEqual(route.name, "crusoe")
        self.assertEqual(seen["url"], "https://api.inference.crusoecloud.com/v1/chat/completions")
        self.assertEqual(reply.model_ids, ["nvidia/Nemotron-3-Nano-30B-A3B"])

    def test_missing_key_and_provider_failure_are_explicit_without_secret_leakage(self) -> None:
        missing = OpenRouterRoute("m", system_prompt="p", environment={}).call("prompt")
        self.assertEqual(missing.error, "missing OPENROUTER_API_KEY")

        def failing(wire, *, timeout):
            raise HTTPError(wire.full_url, 401, "no", None, BytesIO(b"test-only-secret"))

        failed = OpenRouterRoute("m", system_prompt="p", environment={"OPENROUTER_API_KEY": "test-only-secret"},
                                 opener=failing).call("prompt")
        self.assertEqual(failed.error, "http status 401")
        self.assertNotIn("test-only-secret", repr(failed))

    def test_redirects_are_refused_before_a_bearer_header_can_change_origin(self) -> None:
        handler = _NoRedirect()
        self.assertIsNone(handler.redirect_request(None, None, 302, "redirect", {}, "https://elsewhere.invalid"))

        def redirected(wire, *, timeout):
            raise HTTPError(wire.full_url, 302, "redirect", None, BytesIO(b"test-only-secret"))

        reply = OpenRouterRoute("m", system_prompt="p", environment={"OPENROUTER_API_KEY": "test-only-secret"},
                                opener=redirected).call("prompt")
        self.assertEqual(reply.error, "http status 302")
        self.assertNotIn("test-only-secret", repr(reply))

    def test_invalid_or_oversize_provider_output_never_becomes_a_decision(self) -> None:
        def malformed(_wire, *, timeout):
            return Response({"model": "m", "choices": []})

        def giant(_wire, *, timeout):
            return Response({"model": "m", "choices": [{"message": {"content": "x" * 32769}}]})

        for opener, expected in ((malformed, "response has invalid shape"), (giant, "model output too large")):
            with self.subTest(opener=opener.__name__):
                reply = OpenRouterRoute("m", system_prompt="p", environment={"OPENROUTER_API_KEY": "key"},
                                        opener=opener).call("prompt")
                self.assertEqual(reply.error, expected)
                self.assertEqual(reply.text, "")

    def test_oversize_prompt_is_rejected_before_any_provider_request(self) -> None:
        called = False

        def opener(_wire, *, timeout):
            nonlocal called
            called = True
            return Response({})

        reply = OpenRouterRoute("m", system_prompt="p", environment={"OPENROUTER_API_KEY": "key"},
                                opener=opener).call("x" * 65537)
        self.assertEqual(reply.error, "invalid prompt")
        self.assertFalse(called)


if __name__ == "__main__":
    unittest.main()
