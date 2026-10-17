"""LLM provider abstraction with Claude as primary and Gemini as fallback.

No real API calls are implemented yet. The fallback policy is already defined:
- Gemini is tried only when Claude fails with a recoverable error.
- Each provider is attempted at most once per request (no retry loops, no duplicate cost).
- Authentication and invalid-request errors are surfaced, never silently retried or masked.
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

from config import get_setting, is_anthropic_configured, is_gemini_configured

logger = logging.getLogger(__name__)


class ProviderError(Exception):
    """Base class for provider failures."""


class RecoverableProviderError(ProviderError):
    """Rate limit, exhausted quota, or temporary service error: fallback is allowed."""


class AuthenticationProviderError(ProviderError):
    """Invalid or missing credentials: fallback is not triggered automatically."""


class InvalidRequestProviderError(ProviderError):
    """Malformed request: retrying or switching provider will not help."""


class ProviderNotConfiguredError(ProviderError):
    """No usable provider is configured."""


@dataclass
class LLMResponse:
    text: str
    provider: str


class LLMProvider(ABC):
    name: str

    @abstractmethod
    def is_configured(self) -> bool:
        """Whether credentials for this provider are present."""

    @abstractmethod
    def generate(self, system_prompt: str, user_message: str) -> str:
        """Return the model's reply text, or raise a ProviderError subclass."""


DEFAULT_ANTHROPIC_MODEL = "claude-haiku-5-5"
DEFAULT_ANTHROPIC_EFFORT = "low"
DEFAULT_MAX_TOKENS = 2048
REQUEST_TIMEOUT_SECONDS = 30.0


class ClaudeProvider(LLMProvider):
    name = "claude"

    def __init__(self, client=None, max_tokens: int = DEFAULT_MAX_TOKENS, effort: Optional[str] = None) -> None:
        # `client` can be injected (Dependency Injection) so tests never hit the real API.
        self._client = client
        self._max_tokens = max_tokens
        self._effort = effort

    def is_configured(self) -> bool:
        return is_anthropic_configured()

    @property
    def model(self) -> str:
        return get_setting("ANTHROPIC_MODEL") or DEFAULT_ANTHROPIC_MODEL

    @property
    def effort(self) -> Optional[str]:
        """Thinking depth. 'low' keeps reasoning from consuming the small output budget (thinking shares
        max_tokens, and cannot be disabled on every model). ANTHROPIC_EFFORT=off omits the parameter."""
        value = self._effort or get_setting("ANTHROPIC_EFFORT") or DEFAULT_ANTHROPIC_EFFORT
        return None if value.lower() in ("off", "none") else value

    def _get_client(self):
        if self._client is None:
            import anthropic

            # max_retries=0: the SDK retries silently by default; retry policy is ours.
            self._client = anthropic.Anthropic(
                api_key=get_setting("ANTHROPIC_API_KEY"),
                max_retries=0,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        return self._client

    def generate(self, system_prompt: str, user_message: str) -> str:
        import anthropic

        extra = {"output_config": {"effort": self.effort}} if self.effort else {}
        try:
            response = self._get_client().messages.create(
                model=self.model,
                max_tokens=self._max_tokens,
                system=system_prompt,
                messages=[{"role": "user", "content": user_message}],
                **extra,
            )
        except anthropic.APIStatusError as exc:
            raise _classify_status_error(exc) from exc
        except anthropic.APIConnectionError as exc:  # includes timeouts
            raise RecoverableProviderError("Claude connection error") from exc

        text = "".join(b.text for b in response.content if getattr(b, "type", None) == "text")
        if not text:
            raise RecoverableProviderError(
                f"Claude returned an empty response (stop_reason={getattr(response, 'stop_reason', None)})"
            )
        return text


def _classify_status_error(exc) -> ProviderError:
    """Map an HTTP status from the Anthropic API to our error types (messages never include secrets)."""
    status = exc.status_code
    if status in (401, 403):
        return AuthenticationProviderError(f"Claude authentication failed ({status})")
    if status == 429 or status >= 500:  # 429 rate limit, 5xx incl. 529 overloaded
        return RecoverableProviderError(f"Claude temporary error ({status})")
    return InvalidRequestProviderError(f"Claude rejected the request ({status})")


DEFAULT_GEMINI_MODEL = "gemini-3.8-flash"


class GeminiProvider(LLMProvider):
    name = "gemini"

    def __init__(self, client=None) -> None:
        # Injectable so tests never hit the real API.
        self._client = client

    def is_configured(self) -> bool:
        return is_gemini_configured()

    @property
    def model(self) -> str:
        return get_setting("GEMINI_MODEL") or DEFAULT_GEMINI_MODEL

    def _get_client(self):
        if self._client is None:
            from google import genai
            from google.genai import types

            # No retry_options set, so the SDK does not retry on its own. Timeout is in ms.
            self._client = genai.Client(
                api_key=get_setting("GEMINI_API_KEY"),
                http_options=types.HttpOptions(timeout=int(REQUEST_TIMEOUT_SECONDS * 1000)),
            )
        return self._client

    def generate(self, system_prompt: str, user_message: str) -> str:
        from google.genai import errors, types

        try:
            response = self._get_client().models.generate_content(
                model=self.model,
                contents=user_message,
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    max_output_tokens=DEFAULT_MAX_TOKENS,
                ),
            )
        except errors.APIError as exc:
            raise _classify_gemini_error(exc.code) from exc
        except _network_errors() as exc:  # network failures and timeouts
            raise RecoverableProviderError("Gemini connection error") from exc

        text = response.text or ""
        if not text:
            raise RecoverableProviderError("Gemini returned an empty response")
        return text


def _network_errors() -> tuple:
    """Transport-level error classes from whichever httpx flavour the SDK uses."""
    classes: list = [OSError]
    for module_name in ("httpx", "httpx2"):
        try:
            classes.append(__import__(module_name).TransportError)
        except (ImportError, AttributeError):
            pass
    return tuple(classes)


def _classify_gemini_error(status: int) -> ProviderError:
    """Map an HTTP status from the Gemini API to our error types (no secrets in messages)."""
    if status in (401, 403):
        return AuthenticationProviderError(f"Gemini authentication failed ({status})")
    if status == 429 or status >= 500:
        return RecoverableProviderError(f"Gemini temporary error ({status})")
    return InvalidRequestProviderError(f"Gemini rejected the request ({status})")


def any_provider_configured() -> bool:
    return is_anthropic_configured() or is_gemini_configured()


def generate_with_fallback(
    system_prompt: str,
    user_message: str,
    primary: LLMProvider | None = None,
    fallback: LLMProvider | None = None,
) -> LLMResponse:
    """Try the primary provider once; fall back once on recoverable errors only."""
    primary = primary or ClaudeProvider()
    fallback = fallback or GeminiProvider()

    if primary.is_configured():
        logger.info("Using LLM provider: %s", primary.name)
        try:
            return LLMResponse(primary.generate(system_prompt, user_message), primary.name)
        except RecoverableProviderError as exc:
            logger.warning("Provider %s failed with a recoverable error: %s", primary.name, type(exc).__name__)
        except (AuthenticationProviderError, InvalidRequestProviderError):
            logger.error("Provider %s failed with a non-recoverable error.", primary.name)
            raise

    if fallback.is_configured():
        logger.info("Using LLM provider: %s", fallback.name)
        return LLMResponse(fallback.generate(system_prompt, user_message), fallback.name)

    raise ProviderNotConfiguredError("No LLM provider is available.")
