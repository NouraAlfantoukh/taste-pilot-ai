"""Personalized recommendation explanations: grounded in menu data, never invented, never a safety claim."""

import re

import pytest
from fake_supabase import FakeBackend
from helpers import Scripted, ext, picks_for, unverified_menu

from agent.explain import DEFAULT_INTRO_AR, UNVERIFIED_CLAUSE_AR, dish_reason_ar, intro_ar
from agent.orchestrator import ResponseType, handle_message
from agent.persistence import PreferenceSync
from agent.state import AllergyStatus, ConversationState
from services.auth_service import sign_in
from services.constraints import Constraints
from services.memory_service import SupabaseStore
from services.menu_service import get_all_dishes

MENU = {d["id"]: d for d in get_all_dishes()}
SAVED_WORDS = ("حفظت", "تم حفظ", "محفوظ", "سجّلت لك", "سأتذكر")
SAFETY_CLAIMS = ("آمن", "خالٍ من", "خالي من", "لا يحتوي", "لا تحتوي", "مضمون", "بدون حساسية")


def recommend(constraints, picks, message="رشّح لي", status=AllergyStatus.NONE_REPORTED, dishes=None, request=None):
    state = ConversationState(constraints=constraints, allergy_status=status)
    llm = Scripted(ext(wants_recommendations=True, **(request or {})), picks)
    response = handle_message(state, message, llm, dishes)
    return response, llm


def reason_of(response, dish_id):
    return dict(zip((d["id"] for d in response.recommendations), response.reasons))[dish_id]


def assert_no_overclaims(response):
    for word in SAFETY_CLAIMS + SAVED_WORDS:
        assert word not in response.reply_ar, word


# ---- personalized introduction ----

def test_the_intro_names_the_dislike_the_diet_the_spice_limit_and_the_current_request():
    c = Constraints(excluded_ingredients={"mushroom"}, vegetarian_only=True, max_spice_level="none", max_price_sar=25)
    response, _ = recommend(c, picks_for("dish_006", "dish_009"))
    assert response.type is ResponseType.RECOMMENDATIONS
    lead = response.lead_ar
    assert "ما تحب فطر" in lead and "تفضّل الأكل النباتي" in lead and "ما تبغى حار" in lead and "حتى ٢٥ ر.س" in lead
    assert response.reply_ar.startswith(lead) and len(lead) <= 200
    assert_no_overclaims(response)


def test_the_intro_separates_saved_style_preferences_from_the_current_request():
    assert "وبحسب طلبك الحالي" not in intro_ar(Constraints(excluded_ingredients={"cheese"}))
    c = Constraints(excluded_ingredients={"cheese"}, max_price_sar=30)
    text = intro_ar(c)
    assert text.index("ما تحب جبن") < text.index("وبحسب طلبك الحالي (حتى ٣٠ ر.س)")


def test_without_any_preference_or_condition_the_intro_is_the_plain_default_and_no_reason_is_invented():
    response, _ = recommend(Constraints(), picks_for("dish_001", "dish_005"))
    assert response.lead_ar == DEFAULT_INTRO_AR
    assert response.reasons == ("", "")  # nothing specific is true, so nothing is said


def test_an_allergy_is_acknowledged_without_claiming_safety():
    dish = dict(MENU["dish_001"], allergen_data_status="verified", id="v1", name_ar="كبسة موثّقة")
    response, _ = recommend(Constraints(excluded_allergens={"milk"}), picks_for("v1"), status=AllergyStatus.REPORTED, dishes=[dish])
    assert response.type is ResponseType.RECOMMENDATIONS and "عندك حساسية من الحليب" in response.lead_ar
    assert_no_overclaims(response)
    assert "أبلغ موظف المطعم" in response.reply_ar  # the standing caveat


# ---- per-dish reasons come from the recorded metadata ----

def test_not_spicy_is_claimed_only_for_dishes_whose_recorded_spice_level_is_none():
    c = Constraints(max_spice_level="medium")
    response, _ = recommend(c, picks_for("dish_006", "dish_001", "dish_004"))
    shown = {d["id"]: d["spice_level"] for d in response.recommendations}
    assert {"dish_006", "dish_001", "dish_004"} <= set(shown)
    for dish_id, level in shown.items():
        reason = reason_of(response, dish_id)
        assert ("غير حار" in reason) is (level == "none"), (dish_id, level, reason)
    assert "حدّته (حار خفيف)" in reason_of(response, "dish_001")


def test_vegetarian_and_budget_reasons_appear_only_when_asked_and_true():
    response, _ = recommend(Constraints(vegetarian_only=True, max_price_sar=20), picks_for("dish_006", "dish_009", "dish_010"))
    for dish in response.recommendations:
        reason = reason_of(response, dish["id"])
        assert dish["is_vegetarian"] and dish["price_sar"] <= 20
        assert "نباتي" in reason and "ضمن ميزانيتك" in reason
    plain, _ = recommend(Constraints(), picks_for("dish_006"))
    assert "نباتي" not in plain.reply_ar.split("\n", 1)[1] and "ميزانيتك" not in plain.reply_ar


def test_reasons_do_not_repeat_the_ingredient_list_shown_on_the_card():
    c = Constraints(excluded_ingredients={"mushroom"}, vegetarian_only=True, max_spice_level="mild", max_price_sar=30, categories={"starters"})
    response, _ = recommend(c, picks_for("dish_006", "dish_010", "dish_009"))
    assert response.recommendations
    for dish in response.recommendations:
        reason = reason_of(response, dish["id"])
        assert not any(ingredient in reason for ingredient in dish["ingredients_ar"]), reason


def test_a_category_request_is_explained_with_the_dishs_recorded_category():
    c = Constraints(categories={"desserts"})
    response, _ = recommend(c, picks_for("dish_013"))
    assert "من فئة الحلويات" in reason_of(response, "dish_013")


# ---- the model cannot inject claims or override the engine ----

def test_the_models_own_reason_text_is_never_shown():
    unsafe = (
        '{"picks": [{"dish_id": "dish_010", "reason_ar": "ممتاز، بدون فطر وآمن تمامًا ولذيذ جدًا"},'
        ' {"dish_id": "dish_004", "reason_ar": "حار خفيف ونباتي وخالٍ من الحليب"}]}'
    )
    response, _ = recommend(Constraints(excluded_ingredients={"mushroom"}), unsafe)
    assert "لذيذ" not in response.reply_ar and "ممتاز" not in response.reply_ar
    assert response.reasons == ("", "")  # no constraint makes a specific statement true
    assert_no_overclaims(response)


def test_a_dish_the_engine_excluded_is_not_shown_or_explained_even_if_the_model_picks_it():
    response, _ = recommend(Constraints(excluded_ingredients={"chicken"}), picks_for("dish_001", "dish_004"))
    assert [d["id"] for d in response.recommendations] == ["dish_004"]
    assert all("chicken" not in d["ingredient_ids"] for d in response.recommendations)
    assert "كبسة دجاج" not in response.reply_ar


# ---- unverified / missing metadata ----

def test_unverified_allergen_data_is_stated_plainly_when_the_allergy_status_is_unknown():
    response, _ = recommend(Constraints(), picks_for("dish_001"), status=AllergyStatus.DECLINED, dishes=unverified_menu())
    assert response.type is ResponseType.RECOMMENDATIONS
    assert UNVERIFIED_CLAUSE_AR in response.notes_ar and "فلا أقدر أؤكد سلامتها" in response.reply_ar
    assert "آمن" not in response.reply_ar


def test_the_unverified_clause_is_absent_when_every_shown_dish_is_verified():
    dish = dict(MENU["dish_001"], allergen_data_status="verified")
    response, _ = recommend(Constraints(), picks_for("dish_001"), status=AllergyStatus.DECLINED, dishes=[dish])
    assert UNVERIFIED_CLAUSE_AR not in response.reply_ar


def test_missing_metadata_produces_no_claims_and_no_crash():
    bare = {"id": "x", "name_ar": "طبق"}
    c = Constraints(vegetarian_only=True, max_spice_level="none", max_price_sar=10, categories={"desserts"})
    assert dish_reason_ar(bare, c) == ""
    partial = {"id": "y", "name_ar": "طبق", "spice_level": "hot", "price_sar": 50, "is_vegetarian": False}
    reason = dish_reason_ar(partial, c)
    assert "غير حار" not in reason and "نباتي" not in reason and "ميزانيتك" not in reason  # data contradicts each claim
    assert dish_reason_ar(dict(partial, spice_level="unknown"), Constraints(max_spice_level="none")) == ""


# ---- conflicting or changing preferences ----

def test_a_changed_spice_preference_is_explained_with_the_latest_one_only():
    state = ConversationState(allergy_status=AllergyStatus.NONE_REPORTED)
    handle_message(state, "ما أبي حار", Scripted(ext(max_spice_level="none"), picks_for("dish_006")))
    response = handle_message(state, "لا الحين أبي حار", Scripted(ext(min_spice_level="mild", wants_recommendations=True), picks_for("dish_003", "dish_006")))
    assert "تبغى شي حار خفيف على الأقل" in response.lead_ar and "ما تبغى حار" not in response.lead_ar
    assert all(d["spice_level"] != "none" for d in response.recommendations)
    assert not any("غير حار" in r for r in response.reasons)


def test_conflicting_conditions_say_which_ones_clash_and_recommend_nothing():
    state = ConversationState(allergy_status=AllergyStatus.NONE_REPORTED)
    llm = Scripted(ext(vegetarian=True, max_price_sar=5, wants_recommendations=True))
    response = handle_message(state, "نباتي بأقل من ٥ ريال", llm)
    assert response.type is ResponseType.NO_RESULTS and "نباتي" in response.reply_ar and not response.recommendations
    assert not llm.generation_calls()


# ---- persistence claims ----

@pytest.fixture
def backend():
    server = FakeBackend()
    server.add_user("a@example.com", "password-a")
    return server


def test_no_reply_ever_says_something_was_saved_even_when_saving_succeeded(backend):
    client = backend.client()
    user = sign_in(client, "a@example.com", "password-a")
    sync = PreferenceSync(SupabaseStore(client, user.user_id), user.user_id)
    sync.load()
    state = sync.new_state()
    handle_message(state, "هلا", Scripted(ext()))
    handle_message(state, "ما عندي حساسية", Scripted(ext(allergy_answer="none")))
    response = handle_message(state, "ما أحب الفطر، رشّح لي", Scripted(ext(ingredients=["الفطر"], wants_recommendations=True), picks_for("dish_006")))
    assert sync.save(state) is True and backend.rows  # it really was saved...
    assert response.type is ResponseType.RECOMMENDATIONS and "ما تحب فطر" in response.lead_ar
    assert not any(word in response.reply_ar for word in SAVED_WORDS)  # ...but the assistant does not talk about storage
    backend.outage = True
    later = handle_message(state, "رشّح لي", Scripted(ext(wants_recommendations=True), picks_for("dish_006")))
    assert not any(word in later.reply_ar for word in SAVED_WORDS) and "ما تحب فطر" in later.lead_ar


# ---- tone ----

def test_the_wording_is_short_arabic_without_leaked_internals():
    c = Constraints(excluded_ingredients={"mushroom", "onion"}, vegetarian_only=True, max_spice_level="mild", max_price_sar=30)
    response, _ = recommend(c, picks_for("dish_006", "dish_009"))
    assert len(response.lead_ar) <= 200
    assert not re.search(r"[A-Za-z_]{3,}|dish_\d+|None|\{|\}", response.reply_ar)
    assert response.lead_ar.endswith(":") and response.lead_ar.count("،") <= 5
