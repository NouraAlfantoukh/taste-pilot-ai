"""Real-LLM extraction checks. Excluded from the default run; each case costs one small Claude call.

Run explicitly (Claude only, no Gemini fallback, no SDK retries, effort low):
    .venv\\Scripts\\python.exe -m pytest -m integration tests/integration -s -p no:cacheprovider
"""

import pytest

from agent.extraction import extract
from agent.orchestrator import ResponseType, handle_message
from agent.state import AllergyStatus, ConversationState
from services import taxonomy

pytestmark = pytest.mark.integration

def fresh_state():
    return ConversationState(allergy_status=AllergyStatus.NONE_REPORTED)


def resolved(terms, resolver):
    ids = set()
    for term in terms:
        ids |= resolver(term)
    return ids


def test_budget_and_vegetarian(claude):
    message = "أبغى طبق نباتي وميزانيتي ما تتعدى ٣٠ ريال"
    print(f"\n[1] {message}")
    ex = extract(message, fresh_state(), claude)
    assert ex.vegetarian is True
    assert ex.max_price_sar == 30
    assert ex.allergens == ()
    assert ex.wants_recommendations is True


def test_ingredient_dislikes_are_not_allergies(claude):
    message = "ما أحب الجبن ولا أبغى لحم"
    print(f"\n[2] {message}")
    ex = extract(message, fresh_state(), claude)
    assert ex.allergens == (), "a dislike must not be extracted as an allergy"
    assert {"cheese", "meat"} <= resolved(ex.ingredients, taxonomy.resolve_ingredient)
    assert ex.vegetarian is not True, "avoiding meat must not make the user vegetarian"


def test_explicit_allergies(claude):
    message = "عندي حساسية من الحليب والبيض"
    print(f"\n[3] {message}")
    ex = extract(message, fresh_state(), claude)
    assert resolved(ex.allergens, taxonomy.resolve_allergen) == {"milk", "eggs"}
    assert ex.ingredients == ()


def test_ambiguous_allergen_is_clarified_then_resolved_across_two_messages(claude):
    state = fresh_state()
    first_message, second_message = "عندي حساسية من المكسرات", "أقصد الفول السوداني فقط"
    print(f"\n[4] {first_message}")
    first = handle_message(state, first_message, claude)
    assert first.type is ResponseType.CLARIFICATION, first.reply_ar
    assert state.constraints.excluded_allergens == frozenset()
    assert state.pending is not None and state.pending.blocking
    assert any(set(t.candidates) == {"peanuts", "tree_nuts"} for t in state.pending.terms)

    print(f"\n[5] {second_message}")
    calls_before = claude.calls["count"]
    second = handle_message(state, second_message, claude)
    assert state.constraints.excluded_allergens == {"peanuts"}
    assert state.pending is None
    # The dishes are reviewed, so the answer is a recommendation of dishes without peanuts (extraction + generation).
    assert second.type is ResponseType.RECOMMENDATIONS, second.reply_ar
    assert all("peanuts" not in d["allergens"] for d in second.recommendations)
    assert claude.calls["count"] - calls_before == 2, "an extraction call and one generation call are expected"


def test_avoiding_chicken_and_fish(claude):
    message = "ما أبغى دجاج ولا سمك"
    print(f"\n[2b] {message}")
    ex = extract(message, fresh_state(), claude)
    assert ex.allergens == ()
    assert {"chicken", "fish"} <= resolved(ex.ingredients, taxonomy.resolve_ingredient)
    assert ex.vegetarian is not True


def test_vegetarian_request_in_gulf_dialect(claude):
    message = "أبي أكل نباتي"
    print(f"\n[2c] {message}")
    ex = extract(message, fresh_state(), claude)
    assert ex.vegetarian is True
    assert ex.allergens == ()
