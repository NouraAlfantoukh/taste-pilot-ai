"""Real-Claude extraction of category / temperature / budget / exclusions (opt-in, 5 small calls)."""

import pytest

from agent.extraction import extract
from agent.state import AllergyStatus, ClarificationKind, ConversationState, PendingClarification
from services import taxonomy

pytestmark = pytest.mark.integration


def fresh_state():
    return ConversationState(allergy_status=AllergyStatus.NONE_REPORTED)


def categories(ex):
    return {taxonomy.resolve_category(t) for t in ex.categories}


def test_warm_dessert(claude):
    print("\n[8] أبغى حلا دافي")
    ex = extract("أبغى حلا دافي", fresh_state(), claude)
    assert categories(ex) == {"desserts"} and ex.serving_temperature == "warm"
    assert ex.allergens == () and ex.max_price_sar is None


def test_dessert_with_a_budget(claude):
    print("\n[9] أبغى حلا ما يتعدى 20 ريال")
    ex = extract("أبغى حلا ما يتعدى 20 ريال", fresh_state(), claude)
    assert categories(ex) == {"desserts"} and ex.max_price_sar == 20 and ex.serving_temperature is None


def test_without_chicken_has_no_category(claude):
    print("\n[10] أبغى شيء بدون دجاج")
    ex = extract("أبغى شيء بدون دجاج", fresh_state(), claude)
    assert ex.categories == () and ex.allergens == () and ex.vegetarian is not True
    assert any("chicken" in taxonomy.resolve_ingredient(t) for t in ex.ingredients)


def test_everything_together(claude):
    print("\n[11] أبغى حلا دافي ما يتعدى 20 ريال")
    ex = extract("أبغى حلا دافي ما يتعدى 20 ريال", fresh_state(), claude)
    assert categories(ex) == {"desserts"} and ex.serving_temperature == "warm" and ex.max_price_sar == 20


def test_accepting_the_relax_offer_clears_only_the_temperature(claude):
    state = fresh_state()
    state.pending = PendingClarification(ClarificationKind.TEMPERATURE_RELAX)
    print("\n[12] إيه ارشح لي بدون شرط الحرارة")
    ex = extract("إيه ارشح لي بدون شرط الحرارة", state, claude)
    assert "temperature" in ex.clear and "category" not in ex.clear and ex.wants_recommendations
