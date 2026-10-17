"""Real-Claude check that an ingredient allergy and a dislike are extracted differently (opt-in, 2 calls)."""

import pytest

from agent.extraction import extract
from agent.state import AllergyStatus, ConversationState
from services import taxonomy

pytestmark = pytest.mark.integration


def fresh_state():
    return ConversationState(allergy_status=AllergyStatus.NONE_REPORTED)


def test_lentil_allergy_is_an_allergy_and_lentil_dislike_is_not(claude):
    print("\n[6] عندي حساسية من العدس")
    allergic = extract("عندي حساسية من العدس", fresh_state(), claude)
    assert allergic.ingredients == ()
    resolved = [taxonomy.resolve_allergy_term(term) for term in allergic.allergens]
    assert {i for _, ingredients in resolved for i in ingredients} == {"lentils"}

    print("\n[7] ما أحب العدس")
    dislike = extract("ما أحب العدس", fresh_state(), claude)
    assert dislike.allergens == (), "a dislike must not be extracted as an allergy"
    assert any("lentils" in taxonomy.resolve_ingredient(term) for term in dislike.ingredients)
