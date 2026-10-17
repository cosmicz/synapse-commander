"""Bounded OpenAI-compatible HTTPS routes for the live commander.

The routes deliberately expose only one non-streaming chat completion call.
They do not retry, select a fallback provider, or inspect model catalogues:
the caller chooses an explicit provider and model, and a failed call remains a
failed model decision.
"""
from __future__ import annotations

import json
import math
import os
import time
from typing import Any, Callable, Mapping
from urllib import error, request

from .commander import DEFAULT_CALL_TIMEOUT_S, ModelReply


MAX_RESPONSE_BYTES = 65_536
MAX_OUTPUT_CHARS = 32_768
MAX_MODEL_CHARS = 256
MAX_PROMPT_CHARS = 65_536
MAX_SYSTEM_PROMPT_CHARS = 16_384


def _reject_constant(_value: str) -> None:
    raise ValueError("non-finite JSON")


class _NoRedirect(request.HTTPRedirectHandler):
    """Never forward a bearer header to a redirect target."""

    def redirect_request(self, _req: Any, _fp: Any, _code: int, _msg: str,
                         _headers: Any, _newurl: str) -> None:
        return None


_NO_REDIRECT_OPENER = request.build_opener(_NoRedirect()).open


class OpenAICompatibleRoute:
    """One bounded non-streaming OpenAI chat/completions request.

    ``environment`` and ``opener`` are injectable solely for deterministic
    tests.  A key is read only at call time and is never placed in a prompt,
    returned error, ledger value, or raw response record.
    """

    def __init__(self, *, name: str, model: str, endpoint: str, key_env: str,
                 system_prompt: str, timeout_s: float = DEFAULT_CALL_TIMEOUT_S,
                 environment: Mapping[str, str] | None = None,
                 opener: Callable[..., Any] = _NO_REDIRECT_OPENER) -> None:
        if not isinstance(name, str) or not name:
            raise ValueError("route name is required")
        if not isinstance(model, str) or not model or len(model) > MAX_MODEL_CHARS:
            raise ValueError("model must be a bounded non-empty string")
        if not isinstance(endpoint, str) or not endpoint.startswith("https://"):
            raise ValueError("endpoint must use HTTPS")
        if not isinstance(key_env, str) or not key_env:
            raise ValueError("key environment variable is required")
        if not isinstance(system_prompt, str) or not system_prompt or len(system_prompt) > MAX_SYSTEM_PROMPT_CHARS:
            raise ValueError("system_prompt must be a bounded non-empty string")
        if (not isinstance(timeout_s, (int, float)) or isinstance(timeout_s, bool)
                or not math.isfinite(timeout_s) or not 0 < timeout_s <= 600):
            raise ValueError("timeout_s must be a positive finite value no greater than 600")
        self.name, self.model, self.endpoint = name, model, endpoint.rstrip("/")
        self.key_env, self.system_prompt, self.timeout_s = key_env, system_prompt, float(timeout_s)
        self._environment = os.environ if environment is None else environment
        self._opener = opener

    def _missing_key(self, begin: float) -> ModelReply:
        return ModelReply("", [], _ms_since(begin), "", error=f"missing {self.key_env}")

    def call(self, prompt: str) -> ModelReply:
        begin = time.monotonic()
        api_key = self._environment.get(self.key_env)
        if not isinstance(api_key, str) or not api_key:
            return self._missing_key(begin)
        if not isinstance(prompt, str) or len(prompt) > MAX_PROMPT_CHARS:
            return ModelReply("", [], _ms_since(begin), "", error="invalid prompt")
        try:
            body = json.dumps({
                "model": self.model,
                "messages": [
                    {"role": "system", "content": self.system_prompt},
                    {"role": "user", "content": prompt},
                ],
                "stream": False,
                "max_tokens": 512,
            }, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (TypeError, ValueError, UnicodeError):
            return ModelReply("", [], _ms_since(begin), "", error="invalid request")
        wire = request.Request(self.endpoint + "/chat/completions", data=body, method="POST",
                               headers={"Authorization": "Bearer " + api_key,
                                        "Content-Type": "application/json", "Accept": "application/json"})
        try:
            response = self._opener(wire, timeout=self.timeout_s)
            with response:
                raw_bytes = response.read(MAX_RESPONSE_BYTES + 1)
        except error.HTTPError as exc:
            return ModelReply("", [], _ms_since(begin), "", error=f"http status {exc.code}")
        except (error.URLError, TimeoutError, OSError):
            return ModelReply("", [], _ms_since(begin), "", error="route unavailable")
        if len(raw_bytes) > MAX_RESPONSE_BYTES:
            return ModelReply("", [], _ms_since(begin), "", error="response too large")
        try:
            raw = raw_bytes.decode("utf-8")
            value = json.loads(raw, parse_constant=_reject_constant)
        except (UnicodeError, ValueError, json.JSONDecodeError):
            return ModelReply("", [], _ms_since(begin), "", error="response is not valid JSON")
        if not isinstance(value, dict):
            return ModelReply("", [], _ms_since(begin), raw, error="response has invalid shape")
        reported_model = value.get("model")
        choices = value.get("choices")
        if (not isinstance(reported_model, str) or not reported_model or len(reported_model) > MAX_MODEL_CHARS
                or not isinstance(choices, list) or not choices or not isinstance(choices[0], dict)
                or not isinstance(choices[0].get("message"), dict)
                or not isinstance(choices[0]["message"].get("content"), str)):
            return ModelReply("", [], _ms_since(begin), raw, error="response has invalid shape")
        text = choices[0]["message"]["content"]
        if len(text) > MAX_OUTPUT_CHARS:
            return ModelReply("", [reported_model], _ms_since(begin), raw, error="model output too large")
        return ModelReply(text, [reported_model], _ms_since(begin), raw)


class OpenRouterRoute(OpenAICompatibleRoute):
    """OpenRouter's documented OpenAI-compatible chat route."""

    def __init__(self, model: str, *, system_prompt: str, timeout_s: float = DEFAULT_CALL_TIMEOUT_S,
                 environment: Mapping[str, str] | None = None,
                 opener: Callable[..., Any] = _NO_REDIRECT_OPENER) -> None:
        super().__init__(name="openrouter", model=model, endpoint="https://openrouter.ai/api/v1",
                         key_env="OPENROUTER_API_KEY", system_prompt=system_prompt, timeout_s=timeout_s,
                         environment=environment, opener=opener)


class CrusoeRoute(OpenAICompatibleRoute):
    """Crusoe Intelligence API's documented OpenAI-compatible chat route."""

    def __init__(self, model: str, *, system_prompt: str, timeout_s: float = DEFAULT_CALL_TIMEOUT_S,
                 environment: Mapping[str, str] | None = None,
                 opener: Callable[..., Any] = _NO_REDIRECT_OPENER) -> None:
        super().__init__(name="crusoe", model=model, endpoint="https://api.inference.crusoecloud.com/v1",
                         key_env="CRUSOE_API_KEY", system_prompt=system_prompt, timeout_s=timeout_s,
                         environment=environment, opener=opener)


def _ms_since(begin: float) -> int:
    return int((time.monotonic() - begin) * 1000)


__all__ = ["OpenAICompatibleRoute", "OpenRouterRoute", "CrusoeRoute"]
