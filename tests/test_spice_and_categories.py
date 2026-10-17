"""Spice floors/ceilings, switching between kinds of food, and colloquial category words (مالح، أكل)."""

import json

import pytest
from helpers import Scripted, ext, picks_for

from agent.extraction import ExtractionError, parse_extraction
from agent.orchestrator import ResponseType, constraint_labels_ar, handle_message
from agent.state import AllergyStatus, ClarificationKind, ConversationState
from services import taxonomy
from services.constraints import Constraints, ExclusionReason, InvalidConstraintError, build_constraints
from services.menu_service import get_all_dishes
from services.recommendation_service import filter_dishes
from ui import components

T = ResponseType
C = ExclusionReason
MENU = get_all_dishes()
BY_ID = {d["id"]: d for d in MENU}
SPICE_RANK = {"none": 0, "mild": 1, "medium": 2, "hot": 3}
DESSERTS = {d["id"] for d in MENU if d["category_ar"] == "الحلويات"}
SAVORY = {d["id"] for d in MENU if d["category_ar"] in ("الأطباق الرئيسية", "المقبلات")}
GREEDY = json.dumps({"picks": [{"dish_id": d["id"], "reason_ar": ""} for d in MENU]}, ensure_ascii=False)


def none_state(**constraints):
    return ConversationState(
        constraints=Constraints(**constraints), allergy_status=AllergyStatus.NONE_REPORTED
    )


def shown(response):
    return [d["id"] for d in response.recommendations]


# ---- 1. spice floor and ceiling ----

def test_spicy_medium_excludes_every_dish_below_medium():
    state = none_state()
    llm = Scripted(ext(min_spice_level="medium", wants_recommendations=True), GREEDY)
    response = handle_message(state, "أبي أكل سبايسي وسط", llm)
    assert response.type is T.RECOMMENDATIONS
    assert shown(response) and all(SPICE_RANK[d["spice_level"]] >= 2 for d in response.recommendations)
    assert state.constraints.min_spice_level == "medium" and state.constraints.max_spice_level is None
    assert not set(shown(response)) & DESSERTS


def test_bare_spicy_requires_at_least_some_spice():
    state = none_state()
    llm = Scripted(ext(min_spice_level="mild", wants_recommendations=True), GREEDY)
    response = handle_message(state, "أبي شيء حار", llm)
    assert shown(response) and all(d["spice_level"] != "none" for d in response.recommendations)
    result = filter_dishes(MENU, Constraints(min_spice_level="mild"))
    assert {e.dish_id for e in result.excluded if C.TOO_MILD in e.reasons} == {d["id"] for d in MENU if d["spice_level"] == "none"}


def test_strong_means_hot_only():
    result = filter_dishes(MENU, build_constraints(min_spice_level="hot"))
    assert {d["spice_level"] for d in result.eligible} == {"hot"}


def test_no_spicy_stays_a_ceiling():
    llm = Scripted(ext(max_spice_level="none", wants_recommendations=True), GREEDY)
    response = handle_message(none_state(), "ما أبي حار", llm)
    assert shown(response) and all(d["spice_level"] == "none" for d in response.recommendations)


def test_nothing_hotter_than_medium_is_a_ceiling_not_a_floor():
    result = filter_dishes(MENU, Constraints(max_spice_level="medium"))
    assert {d["spice_level"] for d in result.eligible} == {"none", "mild", "medium"}
    assert {e.dish_id for e in result.excluded} == {d["id"] for d in MENU if d["spice_level"] == "hot"}
    assert all(e.reasons == (C.TOO_SPICY,) for e in result.excluded)


def test_both_bounds_are_enforced_together():
    result = filter_dishes(MENU, Constraints(min_spice_level="mild", max_spice_level="medium"))
    assert {d["spice_level"] for d in result.eligible} == {"mild", "medium"}


def test_invalid_spice_bounds_are_rejected():
    for bad in ({"min_spice_level": "extreme"}, {"min_spice_level": "hot", "max_spice_level": "mild"}):
        with pytest.raises(InvalidConstraintError):
            Constraints(**bad)


def test_a_new_bound_replaces_a_contradicting_older_one():
    state = none_state(max_spice_level="none")
    handle_message(state, "لا، ابي حار", Scripted(ext(min_spice_level="medium", wants_recommendations=True), GREEDY))
    assert (state.constraints.min_spice_level, state.constraints.max_spice_level) == ("medium", None)
    handle_message(state, "لا، ما أبي حار", Scripted(ext(max_spice_level="none", wants_recommendations=True), GREEDY))
    assert (state.constraints.min_spice_level, state.constraints.max_spice_level) == (None, "none")


def test_a_contradictory_pair_in_one_message_is_an_explicit_error():
    state = none_state()
    response = handle_message(state, "x", Scripted(ext(min_spice_level="hot", max_spice_level="mild")))
    assert response.type is T.ERROR and state.constraints == Constraints()


def test_clearing_spice_removes_both_bounds():
    state = none_state(min_spice_level="mild", max_spice_level="hot")
    handle_message(state, "ما يهمني الحار", Scripted(ext(clear=["spice"], wants_recommendations=True), GREEDY))
    assert (state.constraints.min_spice_level, state.constraints.max_spice_level) == (None, None)


def test_spice_is_not_serving_temperature():
    state = none_state()
    llm = Scripted(ext(min_spice_level="medium", wants_recommendations=True), GREEDY)
    handle_message(state, "سبايسي وسط", llm)
    assert state.constraints.serving_temperature is None and llm.assessment_calls() == []


def test_spice_floor_does_not_weaken_budget_or_ingredient_exclusions():
    state = none_state(max_price_sar=40, excluded_ingredients=frozenset({"chicken"}))
    llm = Scripted(ext(min_spice_level="medium", wants_recommendations=True), GREEDY)
    response = handle_message(state, "سبايسي وسط", llm)
    assert response.recommendations
    assert all(d["price_sar"] <= 40 and "chicken" not in d["ingredient_ids"] for d in response.recommendations)


def test_extraction_parses_the_floor():
    assert parse_extraction('{"min_spice_level": "medium"}').min_spice_level == "medium"
    assert parse_extraction('{"min_spice_level": "none"}').min_spice_level is None
    with pytest.raises(ExtractionError):
        parse_extraction('{"min_spice_level": "volcanic"}')


# ---- 2. category transitions ----

def test_switching_from_dessert_to_spicy_food_drops_the_dessert_category():
    state = ConversationState()
    llm = Scripted(
        ext(categories=["حلا"], wants_recommendations=True),
        ext(allergy_answer="none"), GREEDY,
        ext(min_spice_level="medium", wants_recommendations=True),  # the model forgot to clear the category
        GREEDY,
    )
    handle_message(state, "أبغى حلا", llm)
    first = handle_message(state, "لا", llm)
    assert set(shown(first)) <= DESSERTS
    second = handle_message(state, "طيب ابي اكل سبايسي وسط", llm)
    assert state.constraints.categories == frozenset()
    assert shown(second) and not set(shown(second)) & DESSERTS
    assert all(SPICE_RANK[d["spice_level"]] >= 2 for d in second.recommendations)
    prompt = llm.generation_calls()[-1][1]
    assert "طيب ابي اكل سبايسي وسط" in prompt and "الفئة: الحلويات" not in prompt


def test_a_switch_keeps_allergies_and_budget(make_dish):
    menu = [
        make_dish(id="dessert", category_ar="الحلويات", price_sar=10),
        make_dish(id="cheap_spicy", category_ar="المقبلات", price_sar=20, spice_level="hot"),
        make_dish(id="pricey_spicy", category_ar="المقبلات", price_sar=90, spice_level="hot"),
        make_dish(id="milky_spicy", category_ar="المقبلات", price_sar=20, spice_level="hot", allergens=["milk"]),
        make_dish(id="mild_one", category_ar="المقبلات", price_sar=20, spice_level="none"),
    ]
    state = ConversationState(
        constraints=build_constraints(categories=["حلا"], max_price_sar=40, allergens=["حليب"]),
        allergy_status=AllergyStatus.REPORTED,
    )
    llm = Scripted(ext(min_spice_level="medium", wants_recommendations=True), picks_for("dessert", "pricey_spicy", "milky_spicy", "mild_one", "cheap_spicy"))
    response = handle_message(state, "أبي أكل سبايسي", llm, menu)
    assert shown(response) == ["cheap_spicy"]
    c = state.constraints
    assert c.max_price_sar == 40 and c.excluded_allergens == {"milk"} and c.categories == frozenset()


def test_refinements_without_a_new_kind_of_food_keep_the_category():
    state = none_state(categories=frozenset({"desserts"}))
    handle_message(state, "أرخص", Scripted(ext(max_price_sar=20, wants_recommendations=True), GREEDY))
    assert state.constraints.categories == {"desserts"}


def test_a_generic_word_in_an_allergy_answer_does_not_wipe_the_original_request():
    state = ConversationState()
    llm = Scripted(ext(categories=["حلا"], wants_recommendations=True), ext(allergy_answer="none"), GREEDY)
    handle_message(state, "أبغى حلا", llm)
    response = handle_message(state, "لا ما عندي حساسية من أي أكل", llm)
    assert state.constraints.categories == {"desserts"} and set(shown(response)) <= DESSERTS


def test_an_unknown_term_does_not_leave_the_old_category_silently_active():
    state = none_state(categories=frozenset({"desserts"}), max_price_sar=40)
    llm = Scripted(ext(categories=["بيتزا"], wants_recommendations=True), ext(categories=["مالح"]), GREEDY)
    first = handle_message(state, "أبغى بيتزا", llm)
    assert first.type is T.CLARIFICATION and "«بيتزا»" in first.reply_ar and "مالح" in first.reply_ar
    assert state.constraints.categories == frozenset() and state.constraints.max_price_sar == 40
    assert state.pending.kind is ClarificationKind.CATEGORY_TERM and llm.generation_calls() == []
    second = handle_message(state, "مالح", llm)
    assert second.type is T.RECOMMENDATIONS and not set(shown(second)) & DESSERTS


# ---- 3. colloquial category words ----

def test_salty_means_main_dishes_and_appetizers_not_an_unknown_category():
    state = none_state()
    llm = Scripted(ext(categories=["مالح"], wants_recommendations=True), GREEDY)
    response = handle_message(state, "أبي شي مالح", llm)
    assert response.type is T.RECOMMENDATIONS
    assert state.constraints.categories == {"mains", "starters"}
    assert set(shown(response)) <= SAVORY and shown(response)
    assert "الفئة: الأطباق الرئيسية أو المقبلات" in " ".join(constraint_labels_ar(state.constraints))


@pytest.mark.parametrize("word", ["مالح", "مالحة", "شي مالح", "حاجة مالحة", "سيفوري"])
def test_salty_aliases_resolve_to_the_savory_group(word):
    assert taxonomy.resolve_categories(word) == {"mains", "starters"}


def test_plain_categories_are_unchanged_by_the_group_mechanism():
    assert taxonomy.resolve_categories("حلا") == {"desserts"} and taxonomy.resolve_category("حلا") == "desserts"


@pytest.mark.parametrize("word", ["أكل", "اكل", "وجبة", "طعام", "شي", "شيء"])
def test_generic_words_are_broad_intent_not_categories(word):
    assert taxonomy.is_generic_food_term(word)
    with pytest.raises(taxonomy.UnknownTermError):
        taxonomy.resolve_categories(word)
    assert build_constraints(categories=[word]).categories == frozenset()  # ignored, not "unresolved"


def test_a_generic_word_needs_no_clarification():
    state = none_state()
    llm = Scripted(ext(categories=["أكل"], wants_recommendations=True), GREEDY)
    response = handle_message(state, "أبي أكل", llm)
    assert response.type is T.RECOMMENDATIONS and state.pending is None
    assert state.constraints.categories == frozenset() and "ما عندي فئة" not in response.reply_ar


def test_a_generic_request_clears_the_previous_category_even_if_the_model_omits_it():
    state = none_state(categories=frozenset({"desserts"}))
    llm = Scripted(ext(wants_recommendations=True), GREEDY)  # no categories, no clear
    handle_message(state, "أبي أكل", llm)
    assert state.constraints.categories == frozenset()


def test_broad_intent_words_are_defined_in_the_taxonomy_data():
    assert taxonomy.has_new_request_intent("طيب ابي اكل سبايسي وسط")
    assert taxonomy.has_new_request_intent("أبي وجبة")
    assert not taxonomy.has_new_request_intent("أبغى حلا") and not taxonomy.has_new_request_intent("أرخص")


# ---- display ----

def test_spice_floor_and_ceiling_are_labelled_separately():
    labels = constraint_labels_ar(Constraints(min_spice_level="medium", max_spice_level="hot"))
    assert labels == ["حدة البهارات من حار متوسط فأكثر", "حدة البهارات حتى حار"]
    chips = [label for label, _ in components.preference_labels(none_state(min_spice_level="mild"))]
    assert chips == ["حدة البهارات من: حار خفيف"]
