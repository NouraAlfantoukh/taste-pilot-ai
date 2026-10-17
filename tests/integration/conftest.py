import pytest

from agent.prompts import EXTRACTION_PROMPT_AR
from services.llm_service import ClaudeProvider


@pytest.fixture(scope="session")
def claude():
    """Real Claude only, with production settings: no Gemini fallback, no SDK retries (max_retries=0), effort low."""
    provider = ClaudeProvider()
    if not provider.is_configured():
        pytest.skip("ANTHROPIC_API_KEY is not configured")
    calls = {"count": 0, "raw": []}

    def generate(system_prompt, user_message):
        calls["count"] += 1
        text = provider.generate(system_prompt, user_message)
        kind = "extraction" if system_prompt == EXTRACTION_PROMPT_AR else "generation"
        calls["raw"].append((kind, text))
        print(f"\n    raw {kind}: {text.strip()}")
        return text

    generate.calls = calls
    return generate
