"""Serving temperature is a SOFT preference: matches first, labelled alternatives second, hard constraints always."""

import json

import pytest
from helpers import Scripted, assessments, ext, picks_for

from agent.orchestrator import (
    ALTERNATIVES_LEAD_AR,
    NO_VERIFIED_OPTION_MESSAGE_AR,
    ResponseType,
    handle_message,
)
from agent.prompts import build_menu_context
from agent.state import AllergyStatus, ClarificationKind, ConversationState
from agent.temperature import assess_temperatures
from services.constraints import Constraints, ExclusionReason, build_constraints
from services.llm_service import RecoverableProviderError
from services.menu_service import get_all_dishes
from services.recommendation_service import (
    CONFIRMED,
    LIKELY,
    UNCERTAIN,
    TemperatureJudgement as J,
    filter_dishes,
    recommend,
    temperature_fit,
)
from ui import components

T = ResponseType
C = ExclusionReason
# The production menu now records serving_temperature for every dish, so these tests (which exercise the
# INFERENCE path used for dishes without structured data) run on the same menu with that field removed.
# The structured path on the real menu is covered at the end of this file.
MENU = [{k: v for k, v in d.items() if k != "serving_temperature"} for d in get_all_dishes()]
BY_ID = {d["id"]: d for d in MENU}
UM_ALI, KUNAFA, FRUIT, SOUP = "dish_012", "dish_011", "dish_013", "dish_010"
DESSERT = "الحلويات"
GREEDY = json.dumps({"picks": [{"dish_id": d["id"], "reason_ar": ""} for d in MENU]}, ensure_ascii=False)
WARM = Constraints(serving_temperature="warm")


@pytest.fixture(autouse=True)
def menu_without_structured_temperature(request, monkeypatch):
    if "structured" not in request.node.name:
        monkeypatch.setattr("agent.orchestrator.get_all_dishes", lambda: MENU)


def none_state():
    return ConversationState(allergy_status=AllergyStatus.NONE_REPORTED)


def shown(response):
    return [d["id"] for d in response.recommendations]


def ask_warm_dessert(*assessor_and_later, extra=None):
    """Script: extraction for 'warm dessert', then the given replies."""
    first = ext(categories=["حلا"], serving_temperature="warm", wants_recommendations=True, **(extra or {}))
    return Scripted(first, *assessor_and_later)


@pytest.fixture
def desserts(make_dish):
    def dish(dish_id, price, **extra):
        return make_dish(id=dish_id, category_ar=DESSERT, price_sar=price, **extra)

    return {
        "menu_warm": dish("menu_warm", 25, serving_temperature="warm"),
        "high_warm": dish("high_warm", 18),
        "mid_warm": dish("mid_warm", 20),
        "unknown": dish("unknown", 12),
        "low_warm": dish("low_warm", 10),
        "high_cold": dish("high_cold", 15),
        "low_cold": dish("low_cold", 14),
        "menu_cold": dish("menu_cold", 9, serving_temperature="cold"),
    }


JUDGEMENTS = {
    "high_warm": J("warm", "high", "دافئة"),
    "mid_warm": J("warm", "medium"),
    "unknown": J("unknown", "low"),
    "low_warm": J("warm", "low"),
    "high_cold": J("cold", "high"),
    "low_cold": J("cold", "low"),
}


# ---- ranking: matches before alternatives ----

def test_matches_come_first_then_uncertain_alternatives(desserts):
    result = recommend(list(desserts.values()), WARM, temperature_judgements=JUDGEMENTS)
    levels = [(d["id"], result.temperature_fit[d["id"]].level) for d in result.eligible]
    assert levels == [
        ("menu_warm", CONFIRMED),
        ("high_warm", LIKELY), ("mid_warm", LIKELY),  # confident inference, price order inside the group
        ("low_warm", UNCERTAIN), ("unknown", UNCERTAIN), ("low_cold", UNCERTAIN),
    ]


def test_confidently_contrary_dishes_are_never_shown_not_even_to_fill_the_list(desserts):
    result = recommend(list(desserts.values()), WARM, temperature_judgements=JUDGEMENTS)
    contrary = {e.dish_id: e.reasons for e in result.excluded}
    assert contrary == {"high_cold": (C.WRONG_TEMPERATURE,), "menu_cold": (C.WRONG_TEMPERATURE,)}
    assert {"high_cold", "menu_cold"}.isdisjoint(d["id"] for d in result.eligible)


def test_an_uncertain_or_unknown_dish_is_never_a_match(desserts):
    for judgement in (J("unknown", "high"), J("warm", "low"), J("cold", "low"), J("room", "low"), None):
        fit = temperature_fit(desserts["unknown"], "warm", judgement)
        assert fit is not None and fit.level == UNCERTAIN and not fit.is_match


def test_confident_cold_or_room_is_a_mismatch_for_a_warm_wish_and_the_mirror_for_cold(desserts):
    for judgement in (J("cold", "high"), J("room", "medium")):
        assert temperature_fit(desserts["unknown"], "warm", judgement) is None
    assert temperature_fit(desserts["unknown"], "cold", J("warm", "high")) is None
    assert temperature_fit(desserts["unknown"], "cold", J("cold", "medium")).level == LIKELY


def test_the_structured_field_is_authoritative_over_any_inference(desserts):
    assert temperature_fit(desserts["menu_warm"], "warm", J("cold", "high")).level == CONFIRMED
    assert temperature_fit(desserts["menu_cold"], "warm", J("warm", "high")) is None
    assert temperature_fit({**desserts["unknown"], "serving_temperature": "hot"}, "warm", None).level == CONFIRMED


def test_without_a_wish_nothing_changes(desserts):
    result = recommend(list(desserts.values()), Constraints(), temperature_judgements=JUDGEMENTS)
    assert len(result.eligible) == len(desserts) and result.temperature_fit == {}


# ---- hard constraints are independent of the preference ----

def test_hard_constraints_exclude_dishes_even_if_they_match_the_temperature_perfectly(make_dish):
    perfect = {"temperature": "warm", "confidence": "high"}
    dishes = [
        make_dish(id="pricey", category_ar=DESSERT, price_sar=99, serving_temperature="warm"),
        make_dish(id="soup", category_ar="المقبلات", price_sar=10, serving_temperature="warm"),
        make_dish(id="milky", category_ar=DESSERT, price_sar=10, serving_temperature="warm", allergens=["milk"]),
        make_dish(id="unreviewed", category_ar=DESSERT, price_sar=10, serving_temperature="warm", allergen_data_status="unverified"),
        make_dish(id="cheesy", category_ar=DESSERT, price_sar=10, serving_temperature="warm",
                  ingredients_ar=["جبن عكاوي"], ingredient_ids=["akkawi_cheese"], allergens=["milk"]),
        make_dish(id="ok", category_ar=DESSERT, price_sar=10),
    ]
    constraints = build_constraints(
        categories=["حلا"], serving_temperature="warm", max_price_sar=30, allergens=["حليب"], ingredients=["جبن"]
    )
    result = recommend(dishes, constraints, temperature_judgements={d["id"]: J(**perfect) for d in dishes})
    assert [d["id"] for d in result.eligible] == ["ok"]
    reasons = {e.dish_id: set(e.reasons) for e in result.excluded}
    assert C.OVER_BUDGET in reasons["pricey"] and C.WRONG_CATEGORY in reasons["soup"]
    assert C.CONFIRMED_ALLERGEN in reasons["milky"] and C.INCOMPLETE_ALLERGEN_DATA in reasons["unreviewed"]
    assert C.EXCLUDED_INGREDIENT in reasons["cheesy"]


# ---- the assessor: sanitizing, never trusting ----

def test_verdicts_carry_confidence_and_a_short_rationale(make_dish):
    dish = make_dish(id="x")
    reply = assessments(("x", "warm", "high", "الاسم والوصف يدلان على أنها تقدم دافئة"))
    assert assess_temperatures([dish], lambda s, u: reply)["x"] == J("warm", "high", "الاسم والوصف يدلان على أنها تقدم دافئة")


@pytest.mark.parametrize(
    "reply",
    [
        "not json", "[]", "{}", '{"assessments": "x"}',
        assessments(("ghost", "warm", "high")),
        assessments(("x", "scorching", "high")),
    ],
)
def test_malformed_or_foreign_replies_mean_unknown_not_certainty(make_dish, reply):
    assert assess_temperatures([make_dish(id="x")], lambda s, u: reply)["x"] == J("unknown", "low")


def test_bad_confidence_defaults_to_low_and_unsafe_rationales_are_dropped(make_dish):
    dish = make_dish(id="x")
    for rationale in ("تكلف ٢٠ ريال", "x" * 300, "السعر 5 SAR"):
        verdict = assess_temperatures([dish], lambda s, u, r=rationale: assessments(("x", "warm", "high", r)))["x"]
        assert verdict == J("warm", "high", "")
    assert assess_temperatures([dish], lambda s, u: assessments(("x", "warm", "certain")))["x"].confidence == "low"


def test_the_assessor_never_receives_spice_price_or_allergy_data():
    sent = []
    assess_temperatures([BY_ID[UM_ALI], BY_ID["dish_003"]], lambda s, u: sent.append(u) or "{}")
    payload = json.loads(sent[0])["dishes"]
    assert all(set(d) == {"dish_id", "name_ar", "category_ar", "description_ar"} for d in payload)
    assert "غير حار" not in sent[0] and "spice" not in sent[0] and "price" not in sent[0] and "allergen" not in sent[0]


def test_provider_errors_in_the_assessment_are_surfaced():
    state = none_state()
    llm = ask_warm_dessert(RecoverableProviderError("x"))
    response = handle_message(state, "أبغى حلا دافي", llm)
    assert response.type is T.ERROR and llm.generation_calls() == []
    assert state.constraints.serving_temperature == "warm"


# ---- end to end ----

def test_warm_dessert_request_ranks_likely_warm_desserts_before_an_uncertain_alternative():
    llm = ask_warm_dessert(
        assessments((UM_ALI, "warm", "high", "حلوى دافئة"), (KUNAFA, "warm", "high", "تقدم ساخنة"), (FRUIT, "unknown", "low")),
        GREEDY,
    )
    response = handle_message(none_state(), "أبغى حلا دافي", llm)
    assert response.type is T.RECOMMENDATIONS
    assert set(shown(response)[:2]) == {UM_ALI, KUNAFA} and shown(response)[2] == FRUIT  # matches first, in the LLM's order
    assert response.serving_levels == (LIKELY, LIKELY, UNCERTAIN)
    assert all(note.startswith("غالبًا دافئ") for note in response.serving_notes[:2])
    assert any("حلوى دافئة" in note for note in response.serving_notes[:2])
    assert response.serving_notes[2] == "بديل: حرارة التقديم غير مؤكدة"
    assert "الموسومة «بديل»" in response.notes_ar
    assert response.lead_ar != ALTERNATIVES_LEAD_AR.format(wanted="دافئًا")  # real matches exist


def test_too_few_matches_are_filled_with_labelled_alternatives_that_cannot_claim_a_temperature():
    picks = json.dumps({"picks": [
        {"dish_id": FRUIT, "reason_ar": "حلوى دافئة ولذيذة"},
        {"dish_id": KUNAFA, "reason_ar": "طعمها غني"},
        {"dish_id": UM_ALI, "reason_ar": "تناسب طلبك"},
    ]}, ensure_ascii=False)
    llm = ask_warm_dessert(assessments((UM_ALI, "warm", "high"), (KUNAFA, "unknown", "low")), picks)
    response = handle_message(none_state(), "أبغى حلا دافي", llm)
    assert shown(response) == [UM_ALI, FRUIT, KUNAFA][:1] + [FRUIT, KUNAFA]  # the match first, alternatives after
    assert response.serving_levels == (LIKELY, UNCERTAIN, UNCERTAIN)
    reasons = dict(zip(shown(response), response.reasons))
    # Reasons now come from menu data, never from the model: nothing the model wrote (including its claim of
    # warmth) is shown, and no alternative's reason says anything about temperature.
    assert "دافئة" not in response.reply_ar and "طعمها غني" not in response.reply_ar
    assert not any(word in reasons[d] for d in (FRUIT, KUNAFA) for word in ("دافئ", "ساخن", "بارد"))


def test_a_cold_dish_is_never_shown_as_a_warm_match_to_fill_the_list():
    llm = ask_warm_dessert(
        assessments((UM_ALI, "warm", "high"), (KUNAFA, "unknown", "low"), (FRUIT, "cold", "high", "سلطة فواكه باردة")),
        GREEDY,
    )
    response = handle_message(none_state(), "أبغى حلا دافي", llm)
    assert shown(response) == [UM_ALI, KUNAFA] and FRUIT not in shown(response)
    assert "سلطة فواكه" not in response.reply_ar


def test_without_any_match_only_alternatives_are_offered_and_they_say_so():
    llm = ask_warm_dessert(assessments(), GREEDY)  # the assessor knows nothing
    response = handle_message(none_state(), "أبغى حلا دافي", llm)
    assert response.type is T.RECOMMENDATIONS and set(response.serving_levels) == {UNCERTAIN}
    assert response.lead_ar == ALTERNATIVES_LEAD_AR.format(wanted="دافئًا")
    assert all(note.startswith("بديل") for note in response.serving_notes)
    assert "غالبًا دافئ" not in response.reply_ar and "دافئ" not in response.serving_notes


def test_ambiguous_or_missing_assessments_never_produce_certainty():
    for reply in ("مرحبا بك", "{}", assessments((UM_ALI, "warm", "low"))):
        llm = ask_warm_dessert(reply, GREEDY)
        response = handle_message(none_state(), "أبغى حلا دافي", llm)
        assert set(response.serving_levels) == {UNCERTAIN}


def test_when_every_candidate_is_confidently_the_wrong_temperature_the_user_is_offered_relaxation():
    state = none_state()
    llm = Scripted(
        ext(categories=["حلا"], serving_temperature="warm", max_price_sar=23, wants_recommendations=True),
        assessments((UM_ALI, "cold", "high"), (FRUIT, "cold", "medium")),
        ext(clear=["temperature"], wants_recommendations=True),
        GREEDY,
    )
    first = handle_message(state, "حلا دافي ما يتعدى 23", llm)
    assert first.type is T.NO_RESULTS and first.recommendations == ()
    assert "بدون شرط الحرارة" in first.reply_ar and "الحلويات" in first.reply_ar
    assert state.pending.kind is ClarificationKind.TEMPERATURE_RELAX and llm.generation_calls() == []

    second = handle_message(state, "إيه", llm)
    assert set(shown(second)) == {UM_ALI, FRUIT} and second.serving_notes == ()
    c = state.constraints
    assert (c.categories, c.max_price_sar, c.serving_temperature) == ({"desserts"}, 23, None)


def test_budget_is_strict_for_matches_and_alternatives_and_assessments_skip_over_budget_dishes():
    llm = ask_warm_dessert(
        assessments((UM_ALI, "warm", "high"), (KUNAFA, "warm", "high"), (FRUIT, "unknown", "low")),
        picks_for(KUNAFA, UM_ALI, FRUIT),
        extra={"max_price_sar": 23},
    )
    response = handle_message(none_state(), "حلا دافي ما يتعدى 23", llm)
    assert shown(response) == [UM_ALI, FRUIT] and all(d["price_sar"] <= 23 for d in response.recommendations)
    sent = {d["dish_id"] for d in json.loads(llm.assessment_calls()[0][1])["dishes"]}
    assert KUNAFA not in sent


def test_allergy_exclusions_are_strict_and_skip_the_temperature_inference_entirely():
    llm = ask_warm_dessert(extra={"allergens": ["حليب"]})
    unreviewed = [dict(d, allergen_data_status="unverified") for d in MENU]
    response = handle_message(none_state(), "حلا دافي وعندي حساسية حليب", llm, unreviewed)
    assert response.type is T.NO_RESULTS and NO_VERIFIED_OPTION_MESSAGE_AR in response.reply_ar
    assert llm.assessment_calls() == [] and llm.generation_calls() == []


def test_a_milk_allergy_with_a_warm_dessert_wish_never_offers_the_milk_desserts():
    llm = ask_warm_dessert(assessments((FRUIT, "unknown", "low")), GREEDY, extra={"allergens": ["حليب"]})
    response = handle_message(none_state(), "حلا دافي وعندي حساسية حليب", llm)
    assert set(shown(response)) == {FRUIT}  # the only milk-free dessert, offered as an uncertain alternative
    assert UM_ALI not in shown(response) and KUNAFA not in shown(response)
    assert set(response.serving_levels) == {UNCERTAIN}


def test_ingredient_exclusions_are_strict_for_alternatives_too():
    llm = ask_warm_dessert(
        assessments((UM_ALI, "warm", "high"), (FRUIT, "unknown", "low")), GREEDY, extra={"ingredients": ["جبن"]}
    )
    response = handle_message(none_state(), "حلا دافي بدون جبن", llm)
    assert KUNAFA not in shown(response) and set(shown(response)) == {UM_ALI, FRUIT}


def test_category_stays_strict_a_warm_soup_is_not_a_dessert():
    llm = ask_warm_dessert(
        assessments((UM_ALI, "warm", "high"), (SOUP, "warm", "high", "شوربة دافئة")), GREEDY
    )
    response = handle_message(none_state(), "أبغى حلا دافي", llm)
    assert SOUP not in shown(response) and all(d["category_ar"] == DESSERT for d in response.recommendations)
    assert all(SOUP not in d["dish_id"] for d in json.loads(llm.assessment_calls()[0][1])["dishes"])


def test_the_final_selection_cannot_introduce_dishes_outside_the_candidates():
    llm = ask_warm_dessert(assessments((UM_ALI, "warm", "high")), picks_for("dish_001", "dish_999", SOUP, UM_ALI))
    response = handle_message(none_state(), "أبغى حلا دافي", llm)
    assert set(shown(response)) <= {UM_ALI, KUNAFA, FRUIT} and shown(response)[0] == UM_ALI
    assert "كبسة" not in response.reply_ar and "شوربة" not in response.reply_ar


def test_a_structured_field_wins_and_needs_no_assessment(make_dish):
    cold = make_dish(id="c", category_ar=DESSERT, description_ar="حلوى دافئة", serving_temperature="cold")
    warm = make_dish(id="w", category_ar=DESSERT, description_ar="حلوى باردة", serving_temperature="warm")
    llm = Scripted(ext(serving_temperature="warm", wants_recommendations=True), picks_for("c", "w"))
    response = handle_message(none_state(), "دافي", llm, [cold, warm])
    assert shown(response) == ["w"] and llm.assessment_calls() == []
    assert response.serving_levels == (CONFIRMED,) and response.serving_notes == ("دافئ",)


def test_one_assessment_call_at_most_and_none_without_a_wish():
    llm = ask_warm_dessert(assessments((UM_ALI, "warm", "high")), GREEDY)
    handle_message(none_state(), "أبغى حلا دافي", llm)
    assert (len(llm.extraction_calls()), len(llm.assessment_calls()), len(llm.generation_calls())) == (1, 1, 1)
    plain = Scripted(ext(categories=["حلا"], wants_recommendations=True), GREEDY)
    handle_message(none_state(), "حلا", plain)
    assert plain.assessment_calls() == []


# ---- spiciness is a different thing ----

def test_spice_never_influences_the_temperature_classification(make_dish):
    mild = make_dish(id="a", spice_level="none")
    spicy = make_dish(id="b", spice_level="hot")
    assert temperature_fit(mild, "warm", None) == temperature_fit(spicy, "warm", None)
    assert temperature_fit(mild, "cold", None).level == UNCERTAIN  # "not spicy" is not "cold"
    assert temperature_fit(spicy, "warm", None).level == UNCERTAIN  # "hot spice" is not "warm"


def test_spice_and_temperature_are_labelled_separately_for_the_user_and_the_llm():
    result = recommend([BY_ID[UM_ALI]], WARM, temperature_judgements={UM_ALI: J("warm", "high")})
    context = build_menu_context(result.eligible, result.temperature_fit)
    assert "حدة البهارات: غير حار" in context and "ملاءمة حرارة التقديم المطلوبة: مرجّحة (استنتاج)" in context
    assert "الوصف: حلوى دافئة" in context
    html = components.dish_card_html(BY_ID[UM_ALI], serving="غالبًا دافئ", level=LIKELY)
    assert "حدة البهارات" in html and "حرارة التقديم" in html and ">الحرارة<" not in html


def test_alternatives_are_visibly_marked_in_the_cards():
    alt = components.dish_card_html(BY_ID[FRUIT], serving="بديل: حرارة التقديم غير مؤكدة", level=UNCERTAIN)
    match = components.dish_card_html(BY_ID[UM_ALI], serving="غالبًا دافئ", level=LIKELY)
    assert "tp-card-alt" in alt and ">بديل<" in alt
    assert "tp-card-alt" not in match and ">بديل<" not in match


def test_menu_data_is_untouched_by_the_soft_preference():
    assert not any("serving_temperature" in d for d in MENU)
    assert [d["allergen_data_status"] for d in MENU] == [d["allergen_data_status"] for d in get_all_dishes()]
    assert filter_dishes(MENU, WARM).eligible == MENU  # the hard filter ignores the wish


# ---- the production menu records serving temperature, so no model inference is needed ----

def test_structured_menu_temperatures_are_complete_and_valid():
    assert all(d.get("serving_temperature") in ("hot", "warm", "cold") for d in get_all_dishes())


def test_structured_warm_dessert_request_needs_no_assessment_call():
    llm = Scripted(ext(categories=["حلا"], serving_temperature="warm", wants_recommendations=True), picks_for(UM_ALI, KUNAFA, FRUIT))
    response = handle_message(none_state(), "أبغى حلا دافي", llm)
    assert response.type is T.RECOMMENDATIONS
    assert shown(response) == [UM_ALI, KUNAFA] or set(shown(response)) == {UM_ALI, KUNAFA}  # the cold fruit salad is never offered
    assert not llm.assessment_calls() and set(response.serving_levels) == {CONFIRMED}


def test_structured_cold_drink_request_needs_no_assessment_call():
    llm = Scripted(ext(categories=["مشروب"], serving_temperature="cold", wants_recommendations=True), picks_for("dish_015", "dish_014"))
    response = handle_message(none_state(), "أبغى مشروب بارد", llm)
    assert shown(response) == ["dish_015"] and not llm.assessment_calls()
