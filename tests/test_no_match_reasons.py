"""When nothing is eligible the reply must name the real reason (live smoke-test regression).

Reported: a guest asked for seafood, then "سمك", and was told allergen data is incomplete and to tell the staff about
their allergy, although no allergy was active in the conversation. Cause: every exclusion reason that is not an allergy
(a saved dislike) selected the "allergen data not verified" message. Seafood wording was also treated as an unknown food.
"""

import pytest
from helpers import Scripted, ext, picks_for, unverified_menu

from agent.orchestrator import NO_MATCHING_DISH_MESSAGE_AR, NO_VERIFIED_OPTION_MESSAGE_AR, OVERRIDE_QUESTION_AR, ResponseType, handle_message
from agent.state import AllergyStatus, ConversationState
from services import taxonomy
from services.constraints import Constraints
from services.menu_service import get_all_dishes

T = ResponseType
ALLERGY_WORDS = ("حساسية", "حساسيتك", "موظف", "موثّقة", "موثقة")


def guest(allergy=AllergyStatus.NONE_REPORTED, **constraints):
    return ConversationState(constraints=Constraints(**{k: frozenset(v) if isinstance(v, set) else v for k, v in constraints.items()}), allergy_status=allergy)


def ask(state, message="اطباق رئيسية", *replies, dishes=None):
    llm = Scripted(ext(wants_recommendations=True), *replies)
    return handle_message(state, message, llm, dishes), llm


# ---- the reported defect: a saved dislike was reported as an allergen-data problem ----

def test_a_saved_dislike_that_excludes_every_dish_is_named_as_the_reason_not_reported_as_an_allergy_issue():
    response, llm = ask(guest(excluded_ingredients={"fish"}, categories={"mains"}), dishes=[d for d in get_all_dishes() if d["id"] == "dish_005"])
    assert response.type is T.NO_RESULTS and response.reply_ar != NO_VERIFIED_OPTION_MESSAGE_AR
    assert "بدون سمك" in response.reply_ar and "الأطباق الرئيسية" in response.reply_ar
    assert not any(word in response.reply_ar for word in ALLERGY_WORDS)  # no allergy was active
    assert not llm.generation_calls()


def test_a_saved_dislike_alone_is_not_an_allergy_message_on_the_real_menu():
    response, _ = ask(guest(excluded_ingredients={"fish", "rice"}, categories={"mains"}))
    assert response.type is T.NO_RESULTS and not any(word in response.reply_ar for word in ALLERGY_WORDS)
    assert "بدون" in response.reply_ar


def test_a_confirmed_allergy_that_excludes_every_dish_is_named_as_the_reason_and_is_never_offered_for_relaxing():
    only_fish = [d for d in get_all_dishes() if d["id"] == "dish_005"]
    response, llm = ask(guest(AllergyStatus.REPORTED, excluded_allergens={"fish"}, categories={"mains"}), dishes=only_fish)
    assert response.type is T.NO_RESULTS and response.reply_ar != NO_VERIFIED_OPTION_MESSAGE_AR
    assert "حساسيتك من الأسماك" in response.reply_ar and "غير موثق" not in response.reply_ar  # the data is reviewed; the allergen is present
    assert "ما راح أقترح طبقًا يخالف حساسيتك" in response.reply_ar
    assert "أخفف" not in response.reply_ar.split("الأطباق الرئيسية")[0]  # an allergy is never offered for relaxing
    assert not llm.generation_calls()


def test_an_allergy_and_a_dislike_together_are_both_named():
    only_fish = [d for d in get_all_dishes() if d["id"] == "dish_005"]
    response, _ = ask(guest(AllergyStatus.REPORTED, excluded_allergens={"fish"}, excluded_ingredients={"lemon"}), dishes=only_fish)
    assert "حساسيتك من الأسماك" in response.reply_ar and "بدون ليمون" in response.reply_ar


def test_unreviewed_allergen_data_still_gets_the_cannot_verify_message():
    response, llm = ask(guest(AllergyStatus.REPORTED, excluded_allergens={"milk"}), dishes=unverified_menu())
    assert response.reply_ar == NO_VERIFIED_OPTION_MESSAGE_AR and not llm.generation_calls()


def test_nothing_matching_and_no_labels_keeps_the_generic_message():
    response, _ = ask(guest(), dishes=[])  # an empty menu is reported before this point; a priced-out menu uses labels
    assert response.type is T.ERROR or response.reply_ar == NO_MATCHING_DISH_MESSAGE_AR


def test_a_budget_that_excludes_everything_is_still_explained_by_the_budget():
    response, _ = ask(guest(max_price_sar=1.0))
    assert response.type is T.NO_RESULTS and "حتى ١ ر.س" in response.reply_ar and not any(w in response.reply_ar for w in ALLERGY_WORDS)


# ---- the conversation as observed live, with the extractions the real model produced ----

OBSERVED = (
    ("ابي وجبه بحريه", {"wants_recommendations": True, "categories": ["بحريه"]}),
    ("اطباق رئيسية بس بحريات يعني", {"wants_recommendations": True, "categories": ["أطباق رئيسية"], "requested_ingredients": ["بحريات"]}),
)


def test_seafood_wording_is_a_food_not_an_unknown_category_or_ingredient():
    state = guest()
    r1 = handle_message(state, *[OBSERVED[0][0]], Scripted(ext(**OBSERVED[0][1]), picks_for("dish_005")))
    assert r1.type is T.RECOMMENDATIONS and [d["id"] for d in r1.recommendations] == ["dish_005"]  # no category question
    assert "ما عندي فئة" not in r1.reply_ar
    r2 = handle_message(state, OBSERVED[1][0], Scripted(ext(**OBSERVED[1][1]), picks_for("dish_005")))
    assert r2.type is T.RECOMMENDATIONS and [d["id"] for d in r2.recommendations] == ["dish_005"]
    assert "ما لقيت" not in r2.reply_ar  # "no dish contains seafood" was wrong: the grilled fish is seafood


@pytest.mark.parametrize("term", ["بحريات", "بحريه", "بحرية", "مأكولات بحرية", "المأكولات البحرية", "ثمار البحر", "seafood"])
def test_seafood_terms_resolve_to_one_family_that_contains_fish_and_shrimp(term):
    (seafood,) = taxonomy.resolve_ingredient(term)
    assert seafood in taxonomy.ingredient_closure("fish") and seafood in taxonomy.ingredient_closure("shrimp")
    assert seafood in taxonomy.ingredient_closure("hammour")


def test_the_bare_seafood_word_stays_an_ambiguous_ALLERGY_term():
    with pytest.raises(taxonomy.AmbiguousTermError) as error:
        taxonomy.resolve_allergy_term("بحريات")
    assert error.value.candidates == {"fish", "crustaceans", "molluscs"}  # unchanged: an allergy still asks which one


def test_a_seafood_dislike_excludes_the_fish_dish():
    state = guest(AllergyStatus.NONE_REPORTED)
    response = handle_message(state, "ما أبي بحريات", Scripted(ext(ingredients=["بحريات"], wants_recommendations=True), picks_for("dish_005", "dish_001")))
    assert response.type is T.RECOMMENDATIONS and "dish_005" not in {d["id"] for d in response.recommendations}


def test_asking_for_fish_with_no_restriction_recommends_the_fish_dish():
    response = handle_message(guest(categories={"mains"}), "اقصد سمك", Scripted(ext(requested_ingredients=["سمك"], wants_recommendations=True), picks_for("dish_005")))
    assert [d["id"] for d in response.recommendations] == ["dish_005"] and "فيها سمك" in response.lead_ar


def test_asking_for_fish_with_a_saved_fish_dislike_asks_before_overriding():
    state = guest(excluded_ingredients={"fish"}, categories={"mains"})
    response = handle_message(state, "اقصد سمك", Scripted(ext(requested_ingredients=["سمك"], wants_recommendations=True)))
    assert response.type is T.CLARIFICATION and "أتجاوز" in response.reply_ar and not response.recommendations


def test_asking_for_fish_with_a_saved_fish_allergy_is_refused_for_that_reason():
    state = guest(AllergyStatus.REPORTED, excluded_allergens={"fish"}, categories={"mains"})
    response = handle_message(state, "اقصد سمك", Scripted(ext(requested_ingredients=["سمك"], wants_recommendations=True)))
    assert response.type is T.NO_RESULTS and "حساسية" in response.reply_ar and not response.recommendations


def test_a_category_word_that_is_really_a_food_does_not_trigger_the_category_question():
    state = guest()
    response = handle_message(state, "ابي شي بحري", Scripted(ext(categories=["بحرية"], wants_recommendations=True), picks_for("dish_005")))
    assert "ما عندي فئة" not in response.reply_ar and [d["id"] for d in response.recommendations] == ["dish_005"]
    assert state.constraints.categories == frozenset()  # it was a food, so no category constraint was invented


def test_a_genuinely_unknown_category_still_asks():
    response = handle_message(guest(), "ابي شي فضائي", Scripted(ext(categories=["فضائي"], wants_recommendations=True)))
    assert response.type is T.CLARIFICATION and "ما عندي فئة" in response.reply_ar
