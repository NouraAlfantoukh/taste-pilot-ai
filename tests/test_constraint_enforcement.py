"""Category, temperature, budget and ingredient constraints must hold from the user's message to the UI cards."""

import json

import pytest
from helpers import Scripted, assessments, ext, picks_for

from agent.extraction import ExtractionError, parse_extraction
from agent.orchestrator import NO_MATCHING_DISH_MESSAGE_AR, ResponseType, compose_reply, handle_message
from agent.state import AllergyStatus, ClarificationKind, ConversationState
from services import taxonomy
from services.constraints import Constraints, ExclusionReason, InvalidConstraintError, build_constraints
from services.menu_service import dish_problems, get_all_dishes
from services.recommendation_service import filter_dishes, recommend
from ui import components

T = ResponseType
C = ExclusionReason
DESSERT, MAIN = "الحلويات", "الأطباق الرئيسية"
PRODUCTION = get_all_dishes()
DESSERT_IDS = {"dish_011", "dish_012", "dish_013"}
GREEDY = json.dumps({"picks": [{"dish_id": d["id"], "reason_ar": "جيد"} for d in PRODUCTION]}, ensure_ascii=False)


def none_state():
    return ConversationState(allergy_status=AllergyStatus.NONE_REPORTED)


def shown_ids(response):
    return [d["id"] for d in response.recommendations]


# ---- 1. deterministic filtering, independent of any LLM ----

def test_dessert_request_returns_only_dessert_dishes():
    result = filter_dishes(PRODUCTION, build_constraints(categories=["حلا"]))
    assert {d["id"] for d in result.eligible} == DESSERT_IDS
    assert all(d["category_ar"] == DESSERT for d in result.eligible)
    assert all(C.WRONG_CATEGORY in e.reasons for e in result.excluded)


@pytest.mark.parametrize("word", ["حلا", "حلى", "تحلية", "الحلويات", "حلويات"])
def test_dessert_words_all_resolve_to_the_same_category(word):
    assert taxonomy.resolve_category(word) == "desserts"


def test_dessert_with_a_20_riyal_maximum():
    result = filter_dishes(PRODUCTION, build_constraints(categories=["حلا"], max_price_sar=20))
    assert [d["id"] for d in result.eligible] == ["dish_013"]
    assert {e.dish_id: e.reasons for e in result.excluded}["dish_011"] == (C.OVER_BUDGET,)


def test_the_maximum_price_is_inclusive(make_dish):
    dishes = [make_dish(id="at", price_sar=20), make_dish(id="over", price_sar=20.5), make_dish(id="under", price_sar=19)]
    assert [d["id"] for d in filter_dishes(dishes, Constraints(max_price_sar=20)).eligible] == ["at", "under"]


def test_without_chicken_excludes_every_dish_whose_ingredients_include_chicken():
    result = filter_dishes(PRODUCTION, build_constraints(ingredients=["دجاج"]))
    with_chicken = {d["id"] for d in PRODUCTION if "chicken" in d["ingredient_ids"]}
    assert with_chicken == {"dish_001", "dish_003"}
    assert {e.dish_id for e in result.excluded} == with_chicken
    assert all(e.reasons == (C.EXCLUDED_INGREDIENT,) for e in result.excluded)
    assert all("chicken" not in d["ingredient_ids"] for d in result.eligible)


def test_nothing_matching_returns_nothing_not_an_unrelated_dish():
    result = recommend(PRODUCTION, build_constraints(categories=["حلا"], max_price_sar=5))
    assert result.eligible == []
    spy = Scripted()
    composed = compose_reply(result, "حلا", spy, PRODUCTION, build_constraints(categories=["حلا"], max_price_sar=5))
    assert composed.dishes == () and spy.calls == []
    assert "الفئة: الحلويات" in composed.text and "حتى ٥ ر.س" in composed.text and "أخفف" in composed.text


def test_new_constraint_values_are_validated():
    for bad in ({"serving_temperature": "tepid"}, {"categories": {"pizza"}}):
        with pytest.raises(InvalidConstraintError):
            Constraints(**bad)


def test_menu_accepts_but_never_invents_serving_temperature(make_dish):
    assert dish_problems(make_dish(serving_temperature="warm")) == []
    assert dish_problems(make_dish(serving_temperature="lukewarm"))
    assert all(d.get("serving_temperature") in ("hot", "warm", "cold") for d in PRODUCTION)  # recorded for every dish


def test_every_production_dish_belongs_to_a_known_category():
    known = list(taxonomy.get_taxonomy().category_names)
    assert all(taxonomy.dish_category_matches(d, known) for d in PRODUCTION)


# ---- 2. extraction schema ----

def test_extraction_parses_category_and_temperature():
    parsed = parse_extraction('{"categories": ["حلا"], "serving_temperature": "warm", "clear": ["category", "temperature"]}')
    assert (parsed.categories, parsed.serving_temperature, parsed.clear) == (("حلا",), "warm", ("category", "temperature"))
    for bad in ('{"serving_temperature": "hot"}', '{"categories": "حلا"}', '{"clear": ["price"]}'):
        with pytest.raises(ExtractionError):
            parse_extraction(bad)


# ---- 3. end to end with a mocked LLM ----


def test_dessert_under_20_shows_only_dish_013_even_if_the_llm_picks_everything():
    state = none_state()
    llm = Scripted(ext(categories=["حلا"], max_price_sar=20, wants_recommendations=True), GREEDY)
    response = handle_message(state, "أبغى حلا ما يتعدى 20 ريال", llm)
    assert shown_ids(response) == ["dish_013"]
    assert all(name not in response.reply_ar for name in ("كبسة", "شوربة عدس", "حمص", "برياني"))
    prompt = llm.generation_calls()[0][1]
    assert "dish_013" in prompt and "dish_010" not in prompt and "dish_001" not in prompt


def test_without_chicken_never_shows_a_chicken_dish_even_if_the_llm_picks_them():
    state = none_state()
    llm = Scripted(ext(ingredients=["دجاج"], wants_recommendations=True), GREEDY)
    response = handle_message(state, "أبغى شيء بدون دجاج", llm)
    assert response.type is T.RECOMMENDATIONS and not {"dish_001", "dish_003"} & set(shown_ids(response))
    assert "كبسة دجاج" not in response.reply_ar and "مضغوط" not in response.reply_ar


def test_no_match_never_substitutes_another_dish():
    state = none_state()
    llm = Scripted(ext(categories=["حلا"], max_price_sar=5, wants_recommendations=True))
    response = handle_message(state, "حلا بخمسة ريال", llm)
    assert (response.type, response.recommendations) == (T.NO_RESULTS, ())
    assert "الفئة: الحلويات" in response.reply_ar and "تبغى أخفف" in response.reply_ar
    assert llm.generation_calls() == []


def test_constraints_and_the_original_request_survive_the_allergy_question():
    state = ConversationState()
    llm = Scripted(
        ext(categories=["حلا"], max_price_sar=20, wants_recommendations=True),
        ext(allergy_answer="none"), GREEDY,
    )
    first = handle_message(state, "أبغى حلا ما يتعدى 20 ريال", llm)
    assert first.type is T.CLARIFICATION and "حساسية" in first.reply_ar
    assert state.constraints.categories == {"desserts"} and state.constraints.max_price_sar == 20
    assert state.recommendation_requested and llm.generation_calls() == []

    second = handle_message(state, "لا", llm)
    assert second.type is T.RECOMMENDATIONS and shown_ids(second) == ["dish_013"]
    prompt = llm.generation_calls()[0][1]
    assert "أبغى حلا ما يتعدى 20 ريال" in prompt  # the original request, not just "لا"
    assert "الفئة: الحلويات" in prompt and "حتى ٢٠ ر.س" in prompt


def test_the_llm_cannot_return_a_dish_outside_the_eligible_candidates():
    state = none_state()
    llm = Scripted(
        ext(categories=["حلا"], wants_recommendations=True),
        picks_for("dish_001", "dish_999", "dish_010", "dish_012"),
    )
    response = handle_message(state, "حلا", llm)
    assert shown_ids(response) == ["dish_012"]
    assert not any(n in response.reply_ar for n in ("كبسة", "شوربة"))


def test_a_new_category_replaces_the_old_one_and_can_be_cleared():
    state = none_state()
    llm = Scripted(
        ext(categories=["حلا"], wants_recommendations=True), GREEDY,
        ext(categories=["مشروب"], wants_recommendations=True), GREEDY,
        ext(clear=["category"], wants_recommendations=True), GREEDY,
    )
    handle_message(state, "حلا", llm)
    handle_message(state, "لا، أبغى مشروب", llm)
    assert state.constraints.categories == {"drinks"}
    handle_message(state, "أي شيء", llm)
    assert state.constraints.categories == frozenset()


def test_unknown_category_is_asked_about_not_ignored():
    state = none_state()
    llm = Scripted(ext(categories=["بيتزا"], wants_recommendations=True))
    response = handle_message(state, "أبغى بيتزا", llm)
    assert response.type is T.CLARIFICATION and "«بيتزا»" in response.reply_ar
    assert "الحلويات" in response.reply_ar and state.constraints.categories == frozenset()
    assert llm.generation_calls() == []


def test_preference_chips_show_category_and_temperature():
    state = ConversationState(constraints=build_constraints(categories=["حلا"], serving_temperature="warm"))
    labels = [label for label, _ in components.preference_labels(state)]
    assert labels == ["الفئة: الحلويات", "دافئ"]


def test_generic_no_match_message_is_kept_when_no_constraints_are_known():
    assert compose_reply(recommend([], Constraints()), "x", Scripted()).text == NO_MATCHING_DISH_MESSAGE_AR
