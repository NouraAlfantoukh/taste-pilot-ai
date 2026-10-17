import json

import pytest

from agent.extraction import ExtractionError, build_extraction_input, parse_extraction
from agent.orchestrator import (
    ALLERGEN_DETAIL_AR,
    ALLERGY_CLARIFY_AR,
    ALLERGY_QUESTION_AR,
    ASK_REQUEST_AR,
    EMPTY_MESSAGE_AR,
    MISUNDERSTOOD_AR,
    NO_VERIFIED_OPTION_MESSAGE_AR,
    NOT_CONFIGURED_AR,
    SERVICE_BUSY_AR,
    UNKNOWN_ALLERGY_FOOTER_AR,
    ResponseType,
    exclusion_note_ar,
    handle_message,
)
from agent.state import MAX_HISTORY_MESSAGES, AllergyStatus, ClarificationKind, ConversationState
from services.constraints import Constraints
from services.llm_service import ProviderNotConfiguredError, RecoverableProviderError
from helpers import Scripted, ext, picks_for, unverified_menu
from services.recommendation_service import recommend

T = ResponseType


@pytest.fixture
def menu(make_dish, incomplete_dish):
    return [
        make_dish(id="clean", name_ar="طبق نظيف", price_sar=10),
        make_dish(
            id="milky", name_ar="طبق الحليب", price_sar=12, allergens=["milk"],
            ingredients_ar=["حليب"], ingredient_ids=["fresh_milk"],
        ),
        incomplete_dish,
    ]


def none_state():
    return ConversationState(allergy_status=AllergyStatus.NONE_REPORTED)


# ---- initial allergy question ----

def test_allergy_question_is_asked_once_per_session(menu):
    state = ConversationState()
    llm = Scripted(ext(), ext(allergy_answer="none"), ext(wants_recommendations=True), "ترشيح")
    first = handle_message(state, "هلا", llm, menu)
    assert (first.type, first.reply_ar) == (T.CLARIFICATION, ALLERGY_QUESTION_AR)
    assert state.allergy_status is AllergyStatus.AWAITING_ANSWER

    second = handle_message(state, "لا ما عندي", llm, menu)
    assert state.allergy_status is AllergyStatus.NONE_REPORTED
    assert second.reply_ar == ASK_REQUEST_AR

    third = handle_message(state, "أبغى شيء", llm, menu)
    assert third.type is T.RECOMMENDATIONS
    assert all(ALLERGY_QUESTION_AR not in r.reply_ar for r in (second, third))


def test_request_made_before_the_allergy_answer_is_served_after_it(menu):
    state = ConversationState()
    llm = Scripted(ext(wants_recommendations=True), ext(allergy_answer="none"), "ترشيح")
    assert handle_message(state, "أبغى عشاء", llm, menu).reply_ar == ALLERGY_QUESTION_AR
    response = handle_message(state, "لا", llm, menu)
    assert response.type is T.RECOMMENDATIONS and len(llm.generation_calls()) == 1


def test_allergy_in_the_first_message_skips_the_initial_question(menu):
    state = ConversationState()
    llm = Scripted(ext(allergens=["حليب"], wants_recommendations=True), "ترشيح")
    response = handle_message(state, "عندي حساسية حليب، وش تنصحني؟", llm, menu)
    assert response.type is T.RECOMMENDATIONS
    assert ALLERGY_QUESTION_AR not in response.reply_ar
    assert state.allergy_status is AllergyStatus.REPORTED


def test_reported_allergy_is_retained_and_applied_to_later_requests(menu):
    state = ConversationState()
    llm = Scripted(
        ext(allergens=["حليب"], wants_recommendations=True), "رد ١",
        ext(wants_recommendations=True, max_price_sar=11), "رد ٢",
    )
    first = handle_message(state, "عندي حساسية حليب", llm, menu)
    second = handle_message(state, "وش الأرخص؟", llm, menu)
    assert state.constraints.excluded_allergens == {"milk"}
    assert state.constraints.max_price_sar == 11
    for response in (first, second):
        assert [d["id"] for d in response.recommendations] == ["clean"]
    for _, prompt in llm.generation_calls():
        assert "طبق نظيف" in prompt and "طبق الحليب" not in prompt


def test_ambiguous_allergy_answer_triggers_clarification(menu):
    state = ConversationState()
    llm = Scripted(ext(), ext(allergy_answer="unclear"))
    handle_message(state, "هلا", llm, menu)
    response = handle_message(state, "همم يمكن", llm, menu)
    assert (response.type, response.reply_ar) == (T.CLARIFICATION, ALLERGY_CLARIFY_AR)
    assert state.allergy_status is AllergyStatus.AWAITING_ANSWER
    assert not state.constraints.excluded_allergens


def test_repeated_non_answers_stop_the_questions_without_assuming_no_allergy(menu):
    state = ConversationState()
    llm = Scripted(ext(), ext(allergy_answer="unclear"), ext())
    for text in ("هلا", "همم", "؟"):
        response = handle_message(state, text, llm, menu)
    assert state.allergy_status is AllergyStatus.DECLINED
    assert response.reply_ar == ASK_REQUEST_AR


def test_declined_answer_is_not_treated_as_no_allergies(menu):
    state = ConversationState()
    llm = Scripted(ext(), ext(allergy_answer="declined"), ext(wants_recommendations=True), "ترشيح")
    handle_message(state, "هلا", llm, menu)
    handle_message(state, "ما أبغى أجاوب", llm, menu)
    assert state.allergy_status is AllergyStatus.DECLINED
    response = handle_message(state, "رشّح لي", llm, menu)
    assert response.type is T.RECOMMENDATIONS
    assert UNKNOWN_ALLERGY_FOOTER_AR in response.reply_ar
    assert ALLERGY_QUESTION_AR not in response.reply_ar


def test_allergy_raised_after_declining_is_applied(menu):
    state = ConversationState(allergy_status=AllergyStatus.DECLINED)
    llm = Scripted(ext(allergens=["بيض"], wants_recommendations=True), "ترشيح")
    handle_message(state, "اتذكرت عندي حساسية بيض", llm, menu)
    assert state.allergy_status is AllergyStatus.REPORTED
    assert state.constraints.excluded_allergens == {"eggs"}


# ---- constraint updates ----

def test_new_constraints_merge_with_existing_ones_and_can_be_corrected(menu):
    state = none_state()
    llm = Scripted(
        ext(max_price_sar=30, max_spice_level="mild"), "ر١",
        ext(vegetarian=True), "ر٢",
        ext(ingredients=["جبن"]), "ر٣",
        ext(clear=["budget"], vegetarian=False, remove_ingredients=["جبن"]), "ر٤",
    )
    handle_message(state, "ميزانيتي ٣٠ وما أبغى حار", llm, menu)
    handle_message(state, "نباتي", llm, menu)
    handle_message(state, "بدون جبن", llm, menu)
    c = state.constraints
    assert (c.max_price_sar, c.max_spice_level, c.vegetarian_only) == (30, "mild", True)
    assert c.excluded_ingredients == {"cheese"}

    handle_message(state, "لا تهمني الميزانية ولا أبغى نباتي، والجبن عادي", llm, menu)
    c = state.constraints
    assert (c.max_price_sar, c.max_spice_level, c.vegetarian_only) == (None, "mild", False)
    assert c.excluded_ingredients == frozenset()


def test_food_dislike_does_not_become_an_allergy(menu):
    state = none_state()
    llm = Scripted(ext(ingredients=["جبن"], wants_recommendations=True), "رد")
    handle_message(state, "ما أحب الجبن", llm, menu)
    assert state.constraints.excluded_allergens == frozenset()
    assert state.constraints.excluded_ingredients == {"cheese"}


def test_a_dislike_misclassified_as_allergy_is_restricted_conservatively_and_announced(menu):
    state = none_state()
    llm = Scripted(ext(allergens=["جبن"], wants_recommendations=True), "ترشيح")
    response = handle_message(state, "ما أحب الجبن", llm, menu)
    # Python cannot tell a dislike from an allergy: it records the stricter reading and says so.
    assert state.constraints.allergy_ingredients == {"cheese"}
    assert state.constraints.excluded_allergens == frozenset()
    assert "فهمت إن عندك حساسية من جبن" in response.reply_ar and "قل لي" in response.reply_ar


def test_unknown_allergen_triggers_targeted_clarification_then_resolves(menu):
    state = none_state()
    llm = Scripted(
        ext(allergens=["كذا"], wants_recommendations=True), ext(allergens=["بيض"], answered_pending=["كذا"]), "ترشيح"
    )
    first = handle_message(state, "عندي حساسية من كذا", llm, menu)
    assert first.type is T.CLARIFICATION and "«كذا»" in first.reply_ar
    assert state.pending.kind is ClarificationKind.ALLERGEN_TERM and llm.generation_calls() == []

    second = handle_message(state, "قصدي البيض", llm, menu)
    assert second.type is T.RECOMMENDATIONS
    assert state.constraints.excluded_allergens == {"eggs"} and state.pending is None


def test_unspecified_allergy_blocks_recommendations_until_resolved(menu):
    state = ConversationState()
    llm = Scripted(ext(unclear_allergy=True), ext(wants_recommendations=True))
    first = handle_message(state, "عندي حساسية بس ما أدري من وش", llm, menu)
    assert first.reply_ar == ALLERGEN_DETAIL_AR
    second = handle_message(state, "رشّح لي", llm, menu)
    assert second.type is T.CLARIFICATION and llm.generation_calls() == []


def test_unknown_ingredient_is_reported_back_not_dropped(menu):
    state = none_state()
    llm = Scripted(ext(ingredients=["كبدة"], wants_recommendations=True))
    response = handle_message(state, "بدون كبدة", llm, menu)
    assert response.type is T.CLARIFICATION and "«كبدة»" in response.reply_ar


def test_conflicting_none_answer_does_not_drop_a_reported_allergen(menu):
    state = ConversationState(allergy_status=AllergyStatus.AWAITING_ANSWER)
    llm = Scripted(ext(allergy_answer="none", allergens=["حليب"]), "ترشيح")
    handle_message(state, "لا... بس الحليب يتعبني", llm, menu)
    assert state.constraints.excluded_allergens == {"milk"}
    assert state.allergy_status is AllergyStatus.REPORTED


# ---- recommendation flow and exclusion explanations ----

def test_only_eligible_dishes_reach_generation_and_the_response(menu):
    state = ConversationState()
    llm = Scripted(ext(allergens=["حليب"], wants_recommendations=True), "ترشيح")
    response = handle_message(state, "حساسية حليب", llm, menu)
    sent = llm.generation_calls()[0][1]
    assert "طبق نظيف" in sent
    assert "طبق الحليب" not in sent and "طبق اختبار (بيانات" not in sent and "test_incomplete_001" not in sent
    assert [d["id"] for d in response.recommendations] == ["clean"]


def test_exclusion_note_mentions_only_the_reported_allergen_and_real_reasons(make_dish, incomplete_dish):
    dishes = [
        make_dish(id="ok"),
        make_dish(id="mg", allergens=["milk", "gluten"]),
        incomplete_dish,
    ]
    note = exclusion_note_ar(recommend(dishes, Constraints(excluded_allergens={"milk"})))
    assert "الحليب" in note and "غير مكتملة" in note
    assert "الغلوتين" not in note and "mg" not in note and "confirmed" not in note


def test_exclusion_note_for_each_reason(make_dish, incomplete_dish):
    milk = Constraints(excluded_allergens={"milk"})
    only_confirmed = exclusion_note_ar(recommend([make_dish(allergens=["milk"])], milk))
    assert "تحتوي على الحليب" in only_confirmed and "غير مكتملة" not in only_confirmed
    only_incomplete = exclusion_note_ar(recommend([incomplete_dish], milk))
    assert "غير مكتملة" in only_incomplete and "تحتوي" not in only_incomplete
    assert exclusion_note_ar(recommend([make_dish(price_sar=99)], Constraints(max_price_sar=5))) == ""


def test_recommendation_reply_explains_exclusions_without_internal_details(menu):
    state = ConversationState()
    llm = Scripted(ext(allergens=["حليب"], wants_recommendations=True), "ترشيح")
    reply = handle_message(state, "حساسية حليب", llm, menu).reply_ar
    assert "الحليب" in reply and "غير مكتملة" in reply
    for leak in ("milky", "clean", "test_incomplete", "confirmed_allergen", "incomplete_allergen_data", "طبق الحليب"):
        assert leak not in reply


def test_no_eligible_dishes_returns_deterministic_reply_without_generation_call():
    state = ConversationState()
    llm = Scripted(ext(allergens=["حليب"], wants_recommendations=True))
    response = handle_message(state, "حساسية حليب، رشّح لي", llm, unverified_menu())  # data nobody reviewed
    assert (response.type, response.reply_ar) == (T.NO_RESULTS, NO_VERIFIED_OPTION_MESSAGE_AR)
    assert response.recommendations == ()
    assert len(llm.calls) == 1 and llm.generation_calls() == []


# ---- errors ----

@pytest.mark.parametrize(
    "bad_output",
    [
        "مرحبا، كيف أساعدك؟",
        "[]",
        ext(max_price_sar="رخيص"),
        ext(max_price_sar=-5),
        ext(max_spice_level="extreme"),
        ext(allergens="حليب"),
        ext(vegetarian="نعم"),
    ],
)
def test_invalid_extraction_output_fails_explicitly_and_changes_nothing(menu, bad_output):
    state = ConversationState()
    llm = Scripted(bad_output)
    response = handle_message(state, "عندي حساسية حليب", llm, menu)
    assert (response.type, response.reply_ar) == (T.ERROR, MISUNDERSTOOD_AR)
    assert state == ConversationState()
    assert llm.generation_calls() == []


def test_extraction_provider_failure_returns_error_and_changes_nothing(menu):
    state = ConversationState()
    assert handle_message(state, "هلا", Scripted(RecoverableProviderError("x")), menu).reply_ar == SERVICE_BUSY_AR
    assert handle_message(state, "هلا", Scripted(ProviderNotConfiguredError("x")), menu).reply_ar == NOT_CONFIGURED_AR
    assert state == ConversationState()


def test_generation_failure_keeps_constraints_and_allows_retry(menu):
    state = ConversationState()
    llm = Scripted(
        ext(allergens=["حليب"], wants_recommendations=True), RecoverableProviderError("x"),
        ext(wants_recommendations=True), "ترشيح",
    )
    failed = handle_message(state, "حساسية حليب", llm, menu)
    assert (failed.type, failed.reply_ar) == (T.ERROR, SERVICE_BUSY_AR)
    assert state.constraints.excluded_allergens == {"milk"} and state.recommendation_requested
    retry = handle_message(state, "جرب مرة ثانية", llm, menu)
    assert retry.type is T.RECOMMENDATIONS and "طبق الحليب" not in llm.generation_calls()[-1][1]


def test_empty_menu_is_an_error_not_an_empty_recommendation():
    state = none_state()
    response = handle_message(state, "رشّح", Scripted(ext(wants_recommendations=True)), dishes=[])
    assert response.type is T.ERROR


def test_empty_message_makes_no_llm_call(menu):
    llm = Scripted()
    response = handle_message(ConversationState(), "   ", llm, menu)
    assert (response.type, response.reply_ar) == (T.CLARIFICATION, EMPTY_MESSAGE_AR) and llm.calls == []


def test_history_is_bounded(menu):
    state = none_state()
    llm = Scripted(*[ext()] * 10)
    for i in range(10):
        handle_message(state, f"رسالة {i}", llm, menu)
    assert len(state.history) == MAX_HISTORY_MESSAGES


# ---- extraction parsing ----

def test_parse_accepts_fenced_json_and_applies_defaults():
    parsed = parse_extraction('```json\n{"allergens": ["حليب"], "max_price_sar": 25}\n```')
    assert parsed.allergens == ("حليب",) and parsed.max_price_sar == 25
    assert parsed.vegetarian is None and parsed.allergy_answer is None and not parsed.wants_recommendations


@pytest.mark.parametrize(
    "raw",
    ["", "not json", "[]", '{"max_price_sar": true}', '{"max_price_sar": NaN}', '{"allergens": [1]}',
     '{"allergens": [""]}', '{"allergy_answer": "maybe"}', '{"clear": ["x"]}', '{"unclear_allergy": "yes"}'],
)
def test_parse_rejects_malformed_output(raw):
    with pytest.raises(ExtractionError):
        parse_extraction(raw)


def test_extraction_input_carries_pending_question_and_current_constraints():
    state = ConversationState(allergy_status=AllergyStatus.AWAITING_ANSWER)
    state.pending = __import__("agent.state", fromlist=["x"]).PendingClarification(ClarificationKind.ALLERGY_ANSWER)
    payload = json.loads(build_extraction_input("لا", state))
    assert payload["pending_question"] == "allergy_answer" and payload["message"] == "لا"
    assert payload["allergy_status"] == "awaiting_answer"
