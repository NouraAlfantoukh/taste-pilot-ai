"""Allergies to recognized ingredients (e.g. lentils) that are not top-level allergen categories."""

import pytest
from helpers import Scripted, ext, picks_for, unverified_menu

from agent.extraction import ExtractionError, parse_extraction
from agent.orchestrator import (
    ALLERGEN_FOOTER_AR,
    NO_VERIFIED_OPTION_MESSAGE_AR,
    ResponseType,
    exclusion_note_ar,
    handle_message,
)
from agent.state import AllergyStatus, ClarificationKind, ConversationState
from services import taxonomy
from services.constraints import Constraints, ExclusionReason, InvalidConstraintError, build_constraints
from services.recommendation_service import filter_dishes, recommend
from ui import components

T = ResponseType
C = ExclusionReason


@pytest.fixture
def menu(make_dish):
    return [
        make_dish(id="clean", name_ar="طبق نظيف", price_sar=10),
        make_dish(
            id="soup", name_ar="شوربة العدس", price_sar=12,
            ingredients_ar=["عدس", "بصل"], ingredient_ids=["lentils", "onion"],
        ),
        make_dish(id="milky", name_ar="طبق الحليب", price_sar=14, allergens=["milk"]),
    ]


def none_state():
    return ConversationState(allergy_status=AllergyStatus.NONE_REPORTED)


def state_with(*, allergens=(), allergy_ingredients=()):
    return ConversationState(
        constraints=Constraints(
            excluded_allergens=frozenset(allergens), allergy_ingredients=frozenset(allergy_ingredients)
        ),
        allergy_status=AllergyStatus.REPORTED,
    )


# ---- 1. the original bug ----

def test_lentil_allergy_does_not_loop_on_clarification(menu):
    """Original bug: 'عدس' is an ingredient, not an allergen category, so it was re-asked forever."""
    state = none_state()
    llm = Scripted(ext(allergens=["عدس"], wants_recommendations=True), picks_for("clean"))
    response = handle_message(state, "عندي حساسية من العدس", llm, menu)
    assert response.type is T.RECOMMENDATIONS, response.reply_ar
    assert state.pending is None


def test_lentil_allergy_resolves_to_the_ingredient_and_tells_the_user_what_was_understood(menu):
    state = none_state()
    llm = Scripted(
        ext(allergens=["عدس"], wants_recommendations=True), picks_for("clean"),
        ext(allergens=["العدس"], wants_recommendations=True), picks_for("clean"),
    )
    first = handle_message(state, "عندي حساسية من العدس", llm, menu)
    assert state.constraints.allergy_ingredients == {"lentils"}
    assert state.constraints.excluded_allergens == frozenset()
    assert first.lead_ar.startswith("فهمت إن عندك حساسية من عدس")
    assert [d["id"] for d in first.recommendations] == ["clean"] and "soup" not in first.reply_ar

    again = handle_message(state, "قلتلك العدس", llm, menu)  # repeating it neither loops nor re-announces
    assert again.type is T.RECOMMENDATIONS and "فهمت" not in again.reply_ar
    assert state.constraints.allergy_ingredients == {"lentils"}


# ---- 2. dislike is not an allergy ----

def test_disliking_lentils_is_an_ingredient_exclusion_not_an_allergy(menu):
    state = none_state()
    llm = Scripted(ext(ingredients=["عدس"], wants_recommendations=True), picks_for("clean", "milky"))
    response = handle_message(state, "ما أحب العدس", llm, menu)
    assert state.constraints.excluded_ingredients == {"lentils"}
    assert state.constraints.allergy_ingredients == frozenset()
    assert state.constraints.excluded_allergens == frozenset()
    assert ALLERGEN_FOOTER_AR not in response.reply_ar and "فهمت" not in response.reply_ar
    result = filter_dishes(menu, state.constraints)
    assert {e.dish_id: e.reasons for e in result.excluded} == {"soup": (C.EXCLUDED_INGREDIENT,)}


# ---- 3 / 4. deterministic enforcement ----

def test_dish_containing_lentils_is_excluded_for_a_lentil_allergy(menu):
    result = filter_dishes(menu, Constraints(allergy_ingredients={"lentils"}))
    excluded = {e.dish_id: e for e in result.excluded}
    assert excluded["soup"].reasons == (C.CONFIRMED_ALLERGEN,)
    assert excluded["soup"].confirmed_allergy_ingredients == {"lentils"}
    assert {d["id"] for d in result.eligible} == {"clean", "milky"}


def test_ingredient_allergy_covers_families(make_dish):
    cheese = make_dish(id="c", ingredients_ar=["جبن موزاريلا"], ingredient_ids=["mozzarella"], allergens=["milk"])
    result = filter_dishes([cheese, make_dish(id="ok")], build_constraints(allergens=["جبن"]))
    assert [d["id"] for d in result.eligible] == ["ok"]
    assert result.excluded[0].confirmed_allergy_ingredients == {"cheese"}


def test_unverified_dish_is_never_suitable_for_an_ingredient_allergy(incomplete_dish):
    result = filter_dishes([incomplete_dish], Constraints(allergy_ingredients={"lentils"}))
    assert result.eligible == []
    assert result.excluded[0].reasons == (C.INCOMPLETE_ALLERGEN_DATA,)


def test_verified_dish_with_unitemized_content_is_not_suitable_for_an_ingredient_allergy(make_dish):
    herbs = make_dish(id="h", ingredients_ar=["أعشاب"], ingredient_ids=["herbs"])  # bypasses menu validation
    result = filter_dishes([herbs], Constraints(allergy_ingredients={"lentils"}))
    assert result.excluded[0].reasons == (C.INCOMPLETE_ALLERGEN_DATA,)
    assert filter_dishes([herbs], Constraints(excluded_allergens={"milk"})).eligible == [herbs]  # categories unchanged


def test_unknown_ingredient_data_fails_closed(make_dish):
    dish = make_dish(ingredient_ids=["unobtainium"])
    assert filter_dishes([dish], Constraints(allergy_ingredients={"lentils"})).eligible == []


def test_verified_data_without_the_ingredient_is_only_eligible_for_further_evaluation(make_dish):
    assert filter_dishes([make_dish()], Constraints(allergy_ingredients={"lentils"})).eligible


def test_whole_production_menu_with_a_lentil_allergy_gives_the_deterministic_no_results_reply():
    state = none_state()
    llm = Scripted(ext(allergens=["عدس"], wants_recommendations=True))
    response = handle_message(state, "عندي حساسية من العدس", llm, unverified_menu())  # data nobody reviewed
    assert (response.type, response.recommendations) == (T.NO_RESULTS, ())
    assert NO_VERIFIED_OPTION_MESSAGE_AR in response.reply_ar
    assert llm.generation_calls() == []
    result = recommend(unverified_menu(), state.constraints)
    assert any(e.confirmed_allergy_ingredients == {"lentils"} for e in result.excluded)  # the lentil soup


def test_exclusion_note_names_the_ingredient_without_internal_ids(menu):
    note = exclusion_note_ar(recommend(menu, Constraints(allergy_ingredients={"lentils"})))
    assert "تحتوي على عدس" in note and "lentils" not in note


# ---- 5. no top-level allergen category required ----

def test_lentils_need_no_allergen_category():
    assert "lentils" not in taxonomy.get_taxonomy().allergen_names
    with pytest.raises(taxonomy.UnknownTermError):
        taxonomy.resolve_allergen("عدس")
    assert taxonomy.resolve_allergy_term("عدس") == (frozenset(), {"lentils"})
    assert Constraints(allergy_ingredients={"lentils"}).allergy_ingredients == {"lentils"}
    with pytest.raises(InvalidConstraintError):
        Constraints(allergy_ingredients={"unobtainium"})


# ---- 6. known categories unchanged ----

def test_known_allergen_category_behaves_as_before(menu):
    assert taxonomy.resolve_allergy_term("حليب") == ({"milk"}, frozenset())
    state = none_state()
    llm = Scripted(ext(allergens=["حليب"], wants_recommendations=True), picks_for("clean", "soup"))
    response = handle_message(state, "عندي حساسية من الحليب", llm, menu)
    assert state.constraints.excluded_allergens == {"milk"}
    assert state.constraints.allergy_ingredients == frozenset()
    assert "milky" not in [d["id"] for d in response.recommendations]
    assert "فهمت" not in response.reply_ar  # categories are not announced as ingredient guesses


# ---- 7 / 8. ambiguous and unknown terms ----

def test_ambiguous_term_still_asks_and_records_nothing(menu):
    with pytest.raises(taxonomy.AmbiguousTermError):
        taxonomy.resolve_allergy_term("مكسرات")
    state = none_state()
    llm = Scripted(ext(allergens=["مكسرات"], wants_recommendations=True))
    response = handle_message(state, "حساسية مكسرات", llm, menu)
    assert response.type is T.CLARIFICATION and "«مكسرات»" in response.reply_ar
    assert state.constraints == Constraints() and llm.generation_calls() == []


def test_unknown_term_stays_unresolved_and_never_leads_to_recommendations(menu):
    state = none_state()
    llm = Scripted(ext(allergens=["كذا"], wants_recommendations=True), ext(wants_recommendations=True))
    first = handle_message(state, "عندي حساسية من كذا", llm, menu)
    second = handle_message(state, "رشّح لي", llm, menu)
    for response in (first, second):
        assert response.type is T.CLARIFICATION and "«كذا»" in response.reply_ar
    assert state.pending.kind is ClarificationKind.ALLERGEN_TERM and llm.generation_calls() == []


def test_a_resolved_ingredient_allergy_does_not_unblock_an_unresolved_term(menu):
    state = none_state()
    llm = Scripted(ext(allergens=["عدس", "كذا"], wants_recommendations=True))
    response = handle_message(state, "حساسية عدس وكذا", llm, menu)
    assert state.constraints.allergy_ingredients == {"lentils"}
    assert response.type is T.CLARIFICATION and "«كذا»" in response.reply_ar and "«عدس»" not in response.reply_ar
    assert llm.generation_calls() == []


# ---- 9. several terms at once ----

def test_multiple_terms_resolve_independently_without_losing_pending_ones_or_earlier_allergies(menu):
    state = none_state()
    llm = Scripted(
        ext(allergens=["عدس", "حليب", "مكسرات", "كذا"], wants_recommendations=True),
        ext(allergens=["فول سوداني"]),
        ext(allergens=["بيض"], answered_pending=["كذا"]),
        picks_for("clean"),
    )
    first = handle_message(state, "حساسية عدس وحليب ومكسرات وكذا", llm, menu)
    assert state.constraints.excluded_allergens == {"milk"} and state.constraints.allergy_ingredients == {"lentils"}
    assert {t.text for t in state.pending.terms} == {"مكسرات", "كذا"}
    assert first.type is T.CLARIFICATION

    second = handle_message(state, "الفول السوداني", llm, menu)
    assert state.constraints.excluded_allergens == {"milk", "peanuts"}
    assert {t.text for t in state.pending.terms} == {"كذا"}
    assert "«كذا»" in second.reply_ar and "«مكسرات»" not in second.reply_ar

    third = handle_message(state, "كذا يعني البيض", llm, menu)
    assert state.pending is None and third.type is T.RECOMMENDATIONS
    assert state.constraints.excluded_allergens == {"milk", "peanuts", "eggs"}
    assert state.constraints.allergy_ingredients == {"lentils"}


def test_an_unrelated_answer_cannot_clear_an_unknown_pending_term(menu):
    state = none_state()
    llm = Scripted(ext(allergens=["كذا"], wants_recommendations=True), ext(allergens=["بيض"]), ext(answered_pending=["كذا"]))
    handle_message(state, "حساسية كذا", llm, menu)
    handle_message(state, "وعندي حساسية بيض أيضًا", llm, menu)  # the model did not say it addressed «كذا»
    assert {t.text for t in state.pending.terms} == {"كذا"}
    handle_message(state, "كذا", llm, menu)  # claiming it addressed «كذا» without resolving anything changes nothing
    assert {t.text for t in state.pending.terms} == {"كذا"}


# ---- 10. corrections ----

def test_explicit_removal_of_an_ingredient_allergy(menu):
    state = state_with(allergy_ingredients={"lentils"})
    llm = Scripted(ext(remove_allergens=["عدس"]), picks_for("soup", "clean"))
    response = handle_message(state, "ما عندي حساسية من العدس", llm, menu)
    assert state.constraints.allergy_ingredients == frozenset()
    assert state.allergy_status is AllergyStatus.NONE_REPORTED
    assert response.lead_ar.startswith("تمام، أزلت حساسية عدس")


def test_partial_correction_keeps_other_allergies(menu):
    state = state_with(allergens={"milk"}, allergy_ingredients={"lentils"})
    llm = Scripted(ext(remove_allergens=["حليب"]), picks_for("clean"))
    handle_message(state, "ما عندي حساسية من الحليب", llm, menu)
    assert state.constraints.excluded_allergens == frozenset()
    assert state.constraints.allergy_ingredients == {"lentils"}


def test_new_allergy_keeps_the_previous_ones(menu):
    state = state_with(allergens={"milk"})
    llm = Scripted(ext(allergens=["عدس"], wants_recommendations=True), picks_for("clean"))
    handle_message(state, "وعندي حساسية من العدس", llm, menu)
    assert state.constraints.excluded_allergens == {"milk"}
    assert state.constraints.allergy_ingredients == {"lentils"}


def test_generic_no_allergy_statement_keeps_an_ingredient_allergy_and_asks(menu):
    state = state_with(allergy_ingredients={"lentils"})
    llm = Scripted(ext(allergy_answer="none", wants_recommendations=True))
    response = handle_message(state, "ما عندي حساسية", llm, menu)
    assert response.type is T.CLARIFICATION and "عدس" in response.reply_ar and "تبغى أشيلها" in response.reply_ar
    assert state.constraints.allergy_ingredients == {"lentils"}


def test_contradictory_ingredient_allergy_in_one_message_is_kept_and_questioned(menu):
    state = state_with(allergy_ingredients={"lentils"})
    llm = Scripted(ext(allergens=["عدس"], remove_allergens=["عدس"], wants_recommendations=True))
    response = handle_message(state, "عندي حساسية من العدس... لا ما عندي", llm, menu)
    assert state.constraints.allergy_ingredients == {"lentils"}
    assert response.type is T.CLARIFICATION and "تبغى أشيلها" in response.reply_ar


# ---- extraction schema and UI ----

def test_answered_pending_is_parsed_and_validated():
    assert parse_extraction('{"answered_pending": ["كذا"]}').answered_pending == ("كذا",)
    assert parse_extraction("{}").answered_pending == ()
    with pytest.raises(ExtractionError):
        parse_extraction('{"answered_pending": "كذا"}')


def test_preference_chips_show_ingredient_allergies_as_allergies():
    labels = components.preference_labels(state_with(allergens={"milk"}, allergy_ingredients={"lentils"}))
    assert ("حساسية: الحليب", "warn") in labels and ("حساسية: عدس", "warn") in labels
    assert components.preference_labels(
        ConversationState(constraints=Constraints(excluded_ingredients={"lentils"}))
    ) == [("بدون عدس", "")]
