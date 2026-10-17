"""LLM provider tests. Everything is mocked: no real API calls are made."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import anthropic
import pytest
from google.genai import errors as genai_errors

try:
    import httpx2 as httpx
except ImportError:  # older SDK versions use httpx
    import httpx

from services.llm_service import (
    DEFAULT_ANTHROPIC_MODEL,
    DEFAULT_GEMINI_MODEL,
    AuthenticationProviderError,
    ClaudeProvider,
    GeminiProvider,
    InvalidRequestProviderError,
    LLMProvider,
    ProviderNotConfiguredError,
    RecoverableProviderError,
    generate_with_fallback,
)


def _status_error(cls, status):
    request = httpx.Request("POST", "https://example.invalid/v1/messages")
    response = httpx.Response(status, request=request)
    return cls("error", response=response, body=None)


def _client_returning(*blocks):
    client = MagicMock()
    client.messages.create.return_value = SimpleNamespace(content=list(blocks))
    return client


def _client_raising(exc):
    client = MagicMock()
    client.messages.create.side_effect = exc
    return client


def _text(t):
    return SimpleNamespace(type="text", text=t)


# ---- ClaudeProvider.generate ----

def test_generate_returns_text_and_sends_expected_request(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
    client = _client_returning(_text("مرحبا"), _text(" بك"))
    result = ClaudeProvider(client=client).generate("sys", "hi")
    assert result == "مرحبا بك"
    kwargs = client.messages.create.call_args.kwargs
    assert kwargs["model"] == DEFAULT_ANTHROPIC_MODEL
    assert kwargs["system"] == "sys"
    assert kwargs["messages"] == [{"role": "user", "content": "hi"}]
    assert client.messages.create.call_count == 1


def test_model_can_be_overridden_from_settings(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_MODEL", "custom-model")
    client = _client_returning(_text("ok"))
    ClaudeProvider(client=client).generate("s", "u")
    assert client.messages.create.call_args.kwargs["model"] == "custom-model"


def test_non_text_blocks_are_ignored():
    client = _client_returning(SimpleNamespace(type="tool_use"), _text("ok"))
    assert ClaudeProvider(client=client).generate("s", "u") == "ok"


def test_empty_response_is_recoverable():
    with pytest.raises(RecoverableProviderError):
        ClaudeProvider(client=_client_returning()).generate("s", "u")


@pytest.mark.parametrize(
    "cls,status,expected",
    [
        (anthropic.RateLimitError, 429, RecoverableProviderError),
        (anthropic.InternalServerError, 500, RecoverableProviderError),
        (anthropic.InternalServerError, 529, RecoverableProviderError),
        (anthropic.AuthenticationError, 401, AuthenticationProviderError),
        (anthropic.PermissionDeniedError, 403, AuthenticationProviderError),
        (anthropic.BadRequestError, 400, InvalidRequestProviderError),
        (anthropic.NotFoundError, 404, InvalidRequestProviderError),
    ],
)
def test_status_errors_are_mapped(cls, status, expected):
    client = _client_raising(_status_error(cls, status))
    with pytest.raises(expected):
        ClaudeProvider(client=client).generate("s", "u")
    assert client.messages.create.call_count == 1  # never retried inside the provider


def test_connection_error_is_recoverable():
    request = httpx.Request("POST", "https://example.invalid/v1/messages")
    client = _client_raising(anthropic.APIConnectionError(request=request))
    with pytest.raises(RecoverableProviderError):
        ClaudeProvider(client=client).generate("s", "u")


# ---- GeminiProvider.generate ----

def _gemini_client_returning(text):
    client = MagicMock()
    client.models.generate_content.return_value = SimpleNamespace(text=text)
    return client


def _gemini_client_raising(exc):
    client = MagicMock()
    client.models.generate_content.side_effect = exc
    return client


def test_gemini_generate_returns_text_and_sends_expected_request(monkeypatch):
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    client = _gemini_client_returning("تمام")
    assert GeminiProvider(client=client).generate("sys", "hi") == "تمام"
    kwargs = client.models.generate_content.call_args.kwargs
    assert kwargs["model"] == DEFAULT_GEMINI_MODEL
    assert kwargs["contents"] == "hi"
    assert kwargs["config"].system_instruction == "sys"
    assert client.models.generate_content.call_count == 1


def test_gemini_model_can_be_overridden_from_settings(monkeypatch):
    monkeypatch.setenv("GEMINI_MODEL", "custom-gemini")
    client = _gemini_client_returning("ok")
    GeminiProvider(client=client).generate("s", "u")
    assert client.models.generate_content.call_args.kwargs["model"] == "custom-gemini"


@pytest.mark.parametrize("text", [None, ""])
def test_gemini_empty_response_is_recoverable(text):
    with pytest.raises(RecoverableProviderError):
        GeminiProvider(client=_gemini_client_returning(text)).generate("s", "u")


@pytest.mark.parametrize(
    "exc_cls,code,expected",
    [
        (genai_errors.ClientError, 429, RecoverableProviderError),
        (genai_errors.ServerError, 503, RecoverableProviderError),
        (genai_errors.ServerError, 500, RecoverableProviderError),
        (genai_errors.ClientError, 401, AuthenticationProviderError),
        (genai_errors.ClientError, 403, AuthenticationProviderError),
        (genai_errors.ClientError, 400, InvalidRequestProviderError),
        (genai_errors.ClientError, 404, InvalidRequestProviderError),
    ],
)
def test_gemini_status_errors_are_mapped(exc_cls, code, expected):
    client = _gemini_client_raising(exc_cls(code, {"error": {"message": "x"}}))
    with pytest.raises(expected):
        GeminiProvider(client=client).generate("s", "u")
    assert client.models.generate_content.call_count == 1


def test_gemini_network_error_is_recoverable():
    import httpx as real_httpx

    client = _gemini_client_raising(real_httpx.ConnectError("boom"))
    with pytest.raises(RecoverableProviderError):
        GeminiProvider(client=client).generate("s", "u")


# ---- generate_with_fallback ----

class FakeProvider(LLMProvider):
    def __init__(self, name, configured=True, result="ok", error=None):
        self.name = name
        self._configured = configured
        self._result = result
        self._error = error
        self.calls = 0

    def is_configured(self):
        return self._configured

    def generate(self, system_prompt, user_message):
        self.calls += 1
        if self._error:
            raise self._error
        return self._result


def test_primary_success_does_not_call_fallback():
    primary, fallback = FakeProvider("claude"), FakeProvider("gemini")
    response = generate_with_fallback("s", "u", primary, fallback)
    assert response.provider == "claude"
    assert fallback.calls == 0


def test_recoverable_error_falls_back_once():
    primary = FakeProvider("claude", error=RecoverableProviderError("x"))
    fallback = FakeProvider("gemini", result="from gemini")
    response = generate_with_fallback("s", "u", primary, fallback)
    assert (response.provider, response.text) == ("gemini", "from gemini")
    assert primary.calls == 1 and fallback.calls == 1


@pytest.mark.parametrize("error", [AuthenticationProviderError("x"), InvalidRequestProviderError("x")])
def test_non_recoverable_errors_do_not_fall_back(error):
    primary, fallback = FakeProvider("claude", error=error), FakeProvider("gemini")
    with pytest.raises(type(error)):
        generate_with_fallback("s", "u", primary, fallback)
    assert fallback.calls == 0


def test_unconfigured_primary_uses_fallback():
    primary = FakeProvider("claude", configured=False)
    fallback = FakeProvider("gemini")
    assert generate_with_fallback("s", "u", primary, fallback).provider == "gemini"
    assert primary.calls == 0


def test_no_provider_configured():
    with pytest.raises(ProviderNotConfiguredError):
        generate_with_fallback(
            "s", "u", FakeProvider("claude", configured=False), FakeProvider("gemini", configured=False)
        )


# ---- thinking / effort configuration ----

def test_claude_requests_low_effort_by_default(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_EFFORT", raising=False)
    client = _client_returning(_text("ok"))
    ClaudeProvider(client=client).generate("s", "u")
    assert client.messages.create.call_args.kwargs["output_config"] == {"effort": "low"}
    assert "thinking" not in client.messages.create.call_args.kwargs


def test_effort_can_be_changed_or_turned_off(monkeypatch):
    client = _client_returning(_text("ok"))
    monkeypatch.setenv("ANTHROPIC_EFFORT", "medium")
    ClaudeProvider(client=client).generate("s", "u")
    assert client.messages.create.call_args.kwargs["output_config"] == {"effort": "medium"}
    monkeypatch.setenv("ANTHROPIC_EFFORT", "off")
    ClaudeProvider(client=client).generate("s", "u")
    assert "output_config" not in client.messages.create.call_args.kwargs


def test_max_tokens_is_configurable():
    client = _client_returning(_text("ok"))
    ClaudeProvider(client=client, max_tokens=300).generate("s", "u")
    assert client.messages.create.call_args.kwargs["max_tokens"] == 300


def test_empty_response_error_reports_the_stop_reason():
    client = MagicMock()
    client.messages.create.return_value = SimpleNamespace(content=[], stop_reason="max_tokens")
    with pytest.raises(RecoverableProviderError, match="max_tokens"):
        ClaudeProvider(client=client).generate("s", "u")
