"""Ambiguous allergen scope, allergy corrections, and the generation guard, through the orchestrator."""

import pytest
from helpers import Scripted, ext, picks_for

from agent.orchestrator import ASK_REQUEST_AR, ResponseType, handle_message
from agent.state import AllergyStatus, ClarificationKind, ConversationState
from services.constraints import Constraints

T = ResponseType


@pytest.fixture
def menu(make_dish):
    return [
        make_dish(id="clean", name_ar="طبق نظيف", price_sar=10),
        make_dish(id="milky", name_ar="طبق الحليب", price_sar=12, allergens=["milk"]),
        make_dish(id="eggy", name_ar="طبق البيض", price_sar=14, allergens=["eggs"]),
    ]


def none_state():
    return ConversationState(allergy_status=AllergyStatus.NONE_REPORTED)


def reported_state(*allergens):
    return ConversationState(
        constraints=Constraints(excluded_allergens=frozenset(allergens)), allergy_status=AllergyStatus.REPORTED
    )


# ---- ambiguous allergen scope ----

def test_specific_term_resolves_without_clarification(menu):
    state = none_state()
    llm = Scripted(ext(allergens=["فستق حلبي"], wants_recommendations=True), picks_for("clean"))
    response = handle_message(state, "عندي حساسية فستق حلبي", llm, menu)
    assert response.type is T.RECOMMENDATIONS
    assert state.constraints.excluded_allergens == {"tree_nuts"}


@pytest.mark.parametrize("term", ["مكسرات", "فستق"])
def test_broad_or_ambiguous_term_asks_which_allergen_and_records_nothing(menu, term):
    state = none_state()
    llm = Scripted(ext(allergens=[term], wants_recommendations=True))
    response = handle_message(state, f"عندي حساسية من {term}", llm, menu)
    assert response.type is T.CLARIFICATION
    assert f"«{term}»" in response.reply_ar
    assert "الفول السوداني" in response.reply_ar and "المكسرات الشجرية" in response.reply_ar
    assert state.constraints.excluded_allergens == frozenset()
    assert state.pending.blocking and llm.generation_calls() == []


def test_ambiguity_is_resolved_across_consecutive_messages(menu):
    state = none_state()
    llm = Scripted(
        ext(allergens=["مكسرات"], wants_recommendations=True),
        ext(allergens=["فول سوداني"]),
        picks_for("clean"),
    )
    first = handle_message(state, "حساسية مكسرات", llm, menu)
    assert first.type is T.CLARIFICATION
    second = handle_message(state, "الفول السوداني بس", llm, menu)
    assert second.type is T.RECOMMENDATIONS
    assert state.constraints.excluded_allergens == {"peanuts"}
    assert state.pending is None


def test_user_can_choose_both_candidates(menu):
    state = none_state()
    llm = Scripted(
        ext(allergens=["فستق"], wants_recommendations=True),
        ext(allergens=["فول سوداني", "مكسرات شجرية"]),
        picks_for("clean"),
    )
    handle_message(state, "حساسية فستق", llm, menu)
    handle_message(state, "الاثنين", llm, menu)
    assert state.constraints.excluded_allergens == {"peanuts", "tree_nuts"}


def test_ambiguity_stays_unresolved_until_actually_answered(menu):
    state = none_state()
    llm = Scripted(ext(allergens=["مكسرات"]), ext(wants_recommendations=True), ext(max_price_sar=40))
    handle_message(state, "حساسية مكسرات", llm, menu)
    for text in ("رشّح لي", "ميزانيتي ٤٠"):
        response = handle_message(state, text, llm, menu)
        assert response.type is T.CLARIFICATION and "«مكسرات»" in response.reply_ar
    assert llm.generation_calls() == [] and state.constraints.excluded_allergens == frozenset()


def test_answering_one_of_two_ambiguous_terms_keeps_the_other_pending(menu):
    state = none_state()
    llm = Scripted(ext(allergens=["مكسرات", "مأكولات بحرية"]), ext(allergens=["فول سوداني"]))
    handle_message(state, "حساسية مكسرات ومأكولات بحرية", llm, menu)
    response = handle_message(state, "المكسرات أقصد الفول السوداني", llm, menu)
    assert state.constraints.excluded_allergens == {"peanuts"}
    assert response.type is T.CLARIFICATION
    assert "«مأكولات بحرية»" in response.reply_ar and "«مكسرات»" not in response.reply_ar


def test_ingredient_dislike_stays_separate_from_allergies(menu):
    state = none_state()
    llm = Scripted(ext(ingredients=["لوز"], wants_recommendations=True), picks_for("clean"))
    handle_message(state, "ما أحب اللوز", llm, menu)
    assert state.constraints.excluded_ingredients == {"almond"}
    assert state.constraints.excluded_allergens == frozenset()


# ---- correcting recorded allergies ----

def test_explicit_partial_correction_removes_only_that_allergy(menu):
    state = reported_state("milk", "eggs")
    llm = Scripted(ext(remove_allergens=["حليب"]), picks_for("milky", "clean"))
    response = handle_message(state, "ما عندي حساسية من الحليب", llm, menu)
    assert state.constraints.excluded_allergens == {"eggs"}
    assert response.reply_ar.startswith("تمام، أزلت حساسية الحليب من قيودك.")
    prompt = llm.generation_calls()[0][1]
    assert "طبق الحليب" in prompt and "طبق نظيف" in prompt and "طبق البيض" not in prompt


def test_removing_the_only_allergy_updates_the_status(menu):
    state = reported_state("milk")
    llm = Scripted(ext(remove_allergens=["الحليب"]), picks_for("clean"))
    handle_message(state, "ما عندي حساسية من الحليب", llm, menu)
    assert state.constraints.excluded_allergens == frozenset()
    assert state.allergy_status is AllergyStatus.NONE_REPORTED


def test_new_allergy_does_not_remove_unrelated_ones(menu):
    state = reported_state("milk")
    llm = Scripted(ext(allergens=["بيض"], wants_recommendations=True), picks_for("clean"))
    handle_message(state, "وعندي حساسية بيض", llm, menu)
    assert state.constraints.excluded_allergens == {"milk", "eggs"}


def test_generic_no_allergy_statement_does_not_delete_recorded_allergies(menu):
    state = reported_state("milk")
    llm = Scripted(ext(allergy_answer="none", wants_recommendations=True))
    response = handle_message(state, "ما عندي حساسية", llm, menu)
    assert response.type is T.CLARIFICATION and "الحليب" in response.reply_ar and "تبغى أشيلها" in response.reply_ar
    assert state.constraints.excluded_allergens == {"milk"}
    assert state.pending.kind is ClarificationKind.ALLERGY_CORRECTION
    assert llm.generation_calls() == []


def test_correction_can_be_confirmed_in_the_next_message(menu):
    state = reported_state("milk")
    llm = Scripted(
        ext(allergy_answer="none", wants_recommendations=True),
        ext(remove_allergens=["حليب"]),
        picks_for("clean", "milky"),
    )
    handle_message(state, "ما عندي حساسية", llm, menu)
    response = handle_message(state, "إيه، الحليب ما عاد يضرني", llm, menu)
    assert state.constraints.excluded_allergens == frozenset()
    assert state.pending is None and response.type is T.RECOMMENDATIONS


def test_contradiction_in_one_message_keeps_the_allergy_and_asks(menu):
    state = reported_state("milk")
    llm = Scripted(ext(allergens=["حليب"], remove_allergens=["حليب"], wants_recommendations=True))
    response = handle_message(state, "عندي حساسية حليب... لا ما عندي", llm, menu)
    assert state.constraints.excluded_allergens == {"milk"}
    assert response.type is T.CLARIFICATION and "تبغى أشيلها" in response.reply_ar


def test_ambiguous_removal_term_keeps_recorded_allergies_and_asks(menu):
    state = reported_state("peanuts", "tree_nuts")
    llm = Scripted(ext(remove_allergens=["مكسرات"], wants_recommendations=True))
    response = handle_message(state, "ما عندي حساسية من المكسرات", llm, menu)
    assert state.constraints.excluded_allergens == {"peanuts", "tree_nuts"}
    assert response.type is T.CLARIFICATION and llm.generation_calls() == []


def test_removing_an_allergy_that_was_never_recorded_changes_nothing(menu):
    state = reported_state("eggs")
    llm = Scripted(ext(remove_allergens=["حليب"]))
    response = handle_message(state, "ما عندي حساسية من الحليب", llm, menu)
    assert state.constraints.excluded_allergens == {"eggs"}
    assert response.reply_ar == ASK_REQUEST_AR


def test_a_correction_never_turns_an_unanswered_question_into_no_allergies(menu):
    state = ConversationState(allergy_status=AllergyStatus.DECLINED)
    llm = Scripted(ext(remove_allergens=["حليب"]))
    handle_message(state, "ما عندي حساسية من الحليب", llm, menu)
    assert state.allergy_status is AllergyStatus.DECLINED


# ---- generation guard through the orchestrator ----

def test_ineligible_dish_picked_by_the_llm_is_never_shown(menu):
    state = ConversationState()
    llm = Scripted(ext(allergens=["حليب"], wants_recommendations=True), picks_for("milky", "clean"))
    response = handle_message(state, "حساسية حليب", llm, menu)
    assert [d["id"] for d in response.recommendations] == ["clean"]
    assert "طبق الحليب" not in response.reply_ar.split("ملاحظة")[0] and "طبق نظيف" in response.reply_ar


def test_prose_naming_a_dish_instead_of_json_is_not_shown(menu):
    state = ConversationState()
    llm = Scripted(ext(allergens=["حليب"], wants_recommendations=True), "أنصحك بـ طبق الحليب وبرياني الدجاج الخاص")
    response = handle_message(state, "حساسية حليب", llm, menu)
    assert "طبق الحليب" not in response.reply_ar and "برياني" not in response.reply_ar
    assert {d["id"] for d in response.recommendations} == {"clean", "eggy"}  # eggs were not reported


def test_response_exposes_validated_pieces_for_a_ui(menu):
    state = ConversationState()
    llm = Scripted(ext(allergens=["حليب"], wants_recommendations=True), picks_for("clean"))
    response = handle_message(state, "حساسية حليب", llm, menu)
    assert response.lead_ar and response.lead_ar in response.reply_ar
    assert len(response.reasons) == len(response.recommendations) == 1
    assert "الحليب" in response.notes_ar and "غير مكتملة" not in response.notes_ar
    assert response.notes_ar in response.reply_ar


def test_removal_acknowledgement_is_part_of_the_lead_line(menu):
    state = reported_state("milk", "eggs")
    llm = Scripted(ext(remove_allergens=["حليب"]), picks_for("clean"))
    response = handle_message(state, "ما عندي حساسية من الحليب", llm, menu)
    assert response.lead_ar.startswith("تمام، أزلت حساسية الحليب")
