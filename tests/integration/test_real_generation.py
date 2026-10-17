"""Real-LLM generation checks (opt-in). Each case that calls the model costs one small Claude call.

Run explicitly (Claude only, no Gemini fallback, no SDK retries):
    python -m pytest -m integration tests/integration/test_real_generation.py -s -p no:cacheprovider
"""

import pytest

from agent.guard import _load_picks
from agent.orchestrator import NO_MATCHING_DISH_MESSAGE_AR, compose_reply
from services.constraints import Constraints
from services.menu_service import format_price_sar, get_all_dishes
from services.recommendation_service import recommend
from services.taxonomy import normalize_text

pytestmark = pytest.mark.integration


def assert_grounded(composed, result, menu, raw):
    """Everything displayed must come from eligible application data."""
    eligible = {d["id"]: d for d in result.eligible}
    assert composed.dishes, "no dishes were shown"
    for shown in composed.dishes:
        assert shown["id"] in eligible and shown == eligible[shown["id"]]
        assert shown["name_ar"] in composed.text
        assert format_price_sar(shown["price_sar"]) in composed.text
    text = normalize_text(composed.text)
    for dish in menu:
        if dish["id"] not in eligible:
            assert normalize_text(dish["name_ar"]) not in text, f"ineligible dish shown: {dish['id']}"
    assert _load_picks(raw) is not None, "the model did not return the expected JSON picks"


def test_generation_returns_structured_picks_that_render_from_validated_data(claude):
    menu = get_all_dishes()
    result = recommend(menu, Constraints(vegetarian_only=True, max_price_sar=25))
    assert len(result.eligible) >= 3
    before = claude.calls["count"]
    composed = compose_reply(result, "أبغى شيء خفيف وطيب", claude, menu)
    assert claude.calls["count"] - before == 1
    raw = claude.calls["raw"][-1][1]
    assert_grounded(composed, result, menu, raw)


def test_requesting_an_ineligible_dish_cannot_make_it_appear(claude):
    menu = get_all_dishes()
    result = recommend(menu, Constraints(vegetarian_only=True))
    composed = compose_reply(result, "أبغى كبسة دجاج أو مندي لحم", claude, menu)
    raw = claude.calls["raw"][-1][1]
    assert_grounded(composed, result, menu, raw)
    assert "كبسة" not in composed.text and "مندي" not in composed.text


def test_no_generation_call_when_nothing_is_eligible(claude):
    menu = get_all_dishes()
    result = recommend(menu, Constraints(max_price_sar=1))
    before = claude.calls["count"]
    composed = compose_reply(result, "رشّح لي", claude, menu)
    assert claude.calls["count"] == before
    assert composed.text == NO_MATCHING_DISH_MESSAGE_AR and composed.dishes == ()
