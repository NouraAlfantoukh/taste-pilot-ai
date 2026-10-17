"""Mocked regressions for food-avoidance phrasing (the real-LLM behaviour is covered by tests/integration)."""

import pytest
from helpers import Scripted, ext, picks_for

from agent.orchestrator import handle_message
from agent.prompts import EXTRACTION_PROMPT_AR
from agent.state import AllergyStatus, ConversationState
from services.constraints import ExclusionReason
from services.recommendation_service import filter_dishes


@pytest.mark.parametrize(
    "ingredients,expected",
    [
        (["لحم"], {"meat"}),
        (["دجاج"], {"chicken"}),
        (["سمك"], {"fish"}),
        (["جبن", "لحم"], {"cheese", "meat"}),
    ],
)
def test_avoiding_a_food_excludes_that_ingredient_only(ingredients, expected, make_dish):
    state = ConversationState(allergy_status=AllergyStatus.NONE_REPORTED)
    llm = Scripted(ext(ingredients=ingredients, wants_recommendations=True), picks_for("ok"))
    handle_message(state, "ما أبغى", llm, [make_dish(id="ok")])
    assert state.constraints.excluded_ingredients == expected
    assert state.constraints.vegetarian_only is False
    assert state.constraints.excluded_allergens == frozenset()


def test_vegetarian_request_sets_the_preference(make_dish):
    state = ConversationState(allergy_status=AllergyStatus.NONE_REPORTED)
    llm = Scripted(ext(vegetarian=True, wants_recommendations=True), picks_for("ok"))
    handle_message(state, "أبي أكل نباتي", llm, [make_dish(id="ok")])
    assert state.constraints.vegetarian_only is True
    assert state.constraints.excluded_ingredients == frozenset()


def test_meat_exclusion_filters_red_meat_dishes_but_not_poultry_or_fish(make_dish):
    from services.constraints import Constraints

    dishes = [
        make_dish(id="lamb", ingredients_ar=["لحم ضأن"], ingredient_ids=["lamb"], is_vegetarian=False),
        make_dish(id="chicken", ingredients_ar=["دجاج"], ingredient_ids=["chicken"], is_vegetarian=False),
        make_dish(id="fish", ingredients_ar=["سمك هامور"], ingredient_ids=["hammour"], is_vegetarian=False),
    ]
    result = filter_dishes(dishes, Constraints(excluded_ingredients={"meat"}))
    assert [d["id"] for d in result.eligible] == ["chicken", "fish"]
    assert result.excluded[0].reasons == (ExclusionReason.EXCLUDED_INGREDIENT,)
    chicken_free = filter_dishes(dishes, Constraints(excluded_ingredients={"chicken"}))
    assert [d["id"] for d in chicken_free.eligible] == ["lamb", "fish"]
    fish_free = filter_dishes(dishes, Constraints(excluded_ingredients={"fish"}))
    assert [d["id"] for d in fish_free.eligible] == ["lamb", "chicken"]


def test_prompt_separates_avoided_foods_from_vegetarian_and_allergies():
    assert "«ما أبغى لحم» تعني ingredients" in EXTRACTION_PROMPT_AR
    assert "لا يجعله نباتيًا" in EXTRACTION_PROMPT_AR
    assert "لا تحوّل أبدًا رغبة في تجنب مكوّن إلى حساسية" in EXTRACTION_PROMPT_AR
