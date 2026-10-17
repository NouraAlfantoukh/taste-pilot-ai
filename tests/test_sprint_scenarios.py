"""Exploratory QA sprint: the manually observed conversation plus scenarios found by probing the app.

Mocked LLM and a fake Supabase (see tests/fake_supabase.py); the real-service counterpart of the main scenario is
tests/integration/test_real_sprint_scenario.py.
"""

from helpers import ext, picks_for
from test_conversation_qa import (  # noqa: F401  (fixtures are re-used)
    RECALL_SENTENCE,
    Guest,
    backend,
    launch,
    log_in,
    page,
    stored,
    type_message,
)

from agent.orchestrator import NO_VERIFIED_OPTION_MESSAGE_AR, ResponseType
from agent.state import AllergyStatus
from services.menu_service import get_all_dishes
from ui import components

T = ResponseType
MENU = {d["id"]: d for d in get_all_dishes()}
CHEESE_DISH = dict(MENU["dish_007"], id="cheese_pie", name_ar="فطيرة جبن", ingredient_ids=["mozzarella"], allergen_data_status="verified")
LENTIL = MENU["dish_010"]
MUSHROOM_SENTENCE = "ما أحب الفطر أبدًا، ولا تقترح علي أي طبق يحتوي عليه."
DISLIKE_LABEL = "مكونات لا تحبها أو تستبعدها"


def saved_dislikes_text(at):
    """The text of the profile's dislike row only."""
    return page(at).split(DISLIKE_LABEL)[1].split("الحساسية المسجّلة")[0]


# ---- the exact conversation from manual testing ----

def run_manual_conversation(backend):
    guest = Guest(backend, "a@example.com")
    out = {}
    out["t1"] = guest.say(MUSHROOM_SENTENCE, ext(ingredients=["الفطر"], wants_recommendations=True))
    out["t2"] = guest.say("ما عندي حساسية من الأطعمة.", ext(allergy_answer="none", wants_recommendations=True), picks_for("dish_010", "dish_004"))
    guest.new_conversation()
    out["t3"] = guest.say("اقترح لي اطباق فيها فطر", ext(requested_ingredients=["فطر"], wants_recommendations=True))
    out["t3_generation_calls"] = len(guest.llm.generation_calls())
    out["t5"] = guest.say(RECALL_SENTENCE, ext(memory_query=True))
    out["t6"] = guest.say("معليش ترا عندي حساسيه بس ماقلت لك اول مره", ext(unclear_allergy=True))
    out["row_after_t6"] = dict(stored(backend, "a@example.com"))
    out["t7"] = guest.say("الحليب", ext(allergens=["الحليب"], wants_recommendations=True), picks_for("dish_002", "dish_014", "dish_001", "dish_010"))
    return guest, out


def test_the_manual_conversation_step_by_step(backend):
    _, out = run_manual_conversation(backend)
    assert out["t2"].type is T.RECOMMENDATIONS and all("mushroom" not in d["ingredient_ids"] for d in out["t2"].recommendations)
    t3 = out["t3"]  # a request FOR mushrooms is never answered with unrelated dishes
    assert t3.type is T.NO_RESULTS and not t3.recommendations and out["t3_generation_calls"] == 0
    assert "ما لقيت في القائمة طبقًا فيه فطر" in t3.reply_ar and "ما تحبها" in t3.reply_ar
    assert "حسب مكوّنات الأطباق المسجّلة" in t3.reply_ar and "شوربة" not in t3.reply_ar and "برياني" not in t3.reply_ar
    assert "فطر" in out["t5"].reply_ar and "حساسية مسجّلة" not in out["t5"].reply_ar  # recall undisturbed
    assert out["t6"].type is T.CLARIFICATION and "وش نوعها" in out["t6"].reply_ar
    row = out["row_after_t6"]  # the correction replaces the stale "no allergies" at once
    assert row["allergy_status"] == "not_asked" and row["excluded_allergens"] == [] and row["excluded_ingredients"] == ["mushroom"]
    # reviewed data exists now: dishes with milk are never offered, mushroom-free dishes without milk are
    assert out["t7"].type is T.RECOMMENDATIONS and out["t7"].recommendations
    assert all("milk" not in d["allergens"] and "mushroom" not in d["ingredient_ids"] for d in out["t7"].recommendations)
    final = stored(backend, "a@example.com")
    assert final["excluded_allergens"] == ["milk"] and final["excluded_ingredients"] == ["mushroom"]
    assert final["allergy_status"] == "reported" and final["allergy_ingredients"] == []


def test_the_manual_conversation_survives_a_fresh_session_and_stays_isolated(backend):
    run_manual_conversation(backend)
    again = Guest(backend, "a@example.com")
    c = again.state.constraints
    assert c.excluded_ingredients == {"mushroom"} and c.excluded_allergens == {"milk"}
    assert again.state.allergy_status is AllergyStatus.REPORTED
    answer = again.say(RECALL_SENTENCE, ext(memory_query=True)).reply_ar
    assert "فطر" in answer and "حساسية مسجّلة: الحليب" in answer
    other = Guest(backend, "b@example.com")
    assert other.state.constraints.excluded_ingredients == frozenset() and not other.state.constraints.excluded_allergens
    assert "فطر" not in other.say(RECALL_SENTENCE, ext(memory_query=True)).reply_ar


def test_an_abandoned_allergy_correction_never_leaves_no_allergies_on_file(backend):
    guest = Guest(backend, "a@example.com")
    guest.onboard()
    assert stored(backend, "a@example.com")["allergy_status"] == "none_reported"
    guest.say("معليش عندي حساسية بس ماقلت", ext(unclear_allergy=True))  # ...and the user leaves
    assert Guest(backend, "a@example.com").state.allergy_status is AllergyStatus.NOT_ASKED  # asked again next time


# ---- the profile shows only what is really saved ----

def test_the_profile_shows_saved_dislike_and_allergy_separately_and_the_rest_as_unspecified(launch, backend):
    at = log_in(launch(ext(), ext(allergy_answer="none"), ext(ingredients=["الفطر"]), picks_for("dish_006"), ext(allergens=["الحليب"])), "a@example.com")
    for text in ("هلا", "ما عندي حساسية", "ما أحب الفطر", "عندي حساسية من الحليب"):
        type_message(at, text)
    assert components.PROFILE_TITLE_AR in [e.label for e in at.expander]
    row = stored(backend, "a@example.com")
    assert row["excluded_ingredients"] == ["mushroom"] and row["excluded_allergens"] == ["milk"]
    profile = page(at)
    assert "فطر" in saved_dislikes_text(at)
    assert "الحليب" in profile.split("الحساسية المسجّلة")[1].split("النظام الغذائي")[0]
    assert "حليب" not in saved_dislikes_text(at)  # the allergy is not mixed into the dislikes
    for needle in ("النظام الغذائي", "حدة البهارات"):
        assert needle in profile, needle
    assert profile.count(components.UNSPECIFIED_AR) >= 2
    assert "المكونات المفضّلة" not in profile and "ميزانيتك المعتادة" not in profile  # only what is really saved is listed
    assert components.PROFILE_UNSAVED_AR not in profile and components.PROFILE_SAVE_FAILED_AR not in profile


def test_a_dislike_is_not_in_the_profile_until_it_is_actually_saved(launch, backend):
    at = log_in(launch(ext(), ext(allergy_answer="none"), ext(ingredients=["الفطر"]), ext()), "a@example.com")
    type_message(at, "هلا")
    type_message(at, "ما عندي حساسية")
    backend.outage = True
    type_message(at, "ما أحب الفطر")
    assert components.PROFILE_SAVE_FAILED_AR in page(at)
    assert "فطر" not in saved_dislikes_text(at) and components.UNSPECIFIED_AR in saved_dislikes_text(at)
    backend.outage = False
    type_message(at, "شكرًا")
    assert components.PROFILE_SAVE_FAILED_AR not in page(at) and "فطر" in saved_dislikes_text(at)


def test_signing_out_and_switching_accounts_never_shows_the_previous_profile(launch, backend):
    at = log_in(launch(ext(), ext(allergy_answer="none"), ext(ingredients=["الفطر"])), "a@example.com")
    for text in ("هلا", "ما عندي حساسية", "ما أحب الفطر"):
        type_message(at, text)
    assert "فطر" in saved_dislikes_text(at)
    next(b for b in at.button if b.key == "sign_out").click().run()
    assert "فطر" not in page(at) and components.PROFILE_TITLE_AR not in [e.label for e in at.expander]
    at.text_input(key="sign_in_email").input("b@example.com")
    at.text_input(key="sign_in_password").input("password-b")
    next(b for b in at.button if b.label == "دخول").click().run()
    assert "فطر" not in page(at) and components.UNSPECIFIED_AR in saved_dislikes_text(at)


def test_an_unreadable_profile_is_reported_as_unavailable_not_as_empty(launch, backend):
    uid = backend.users["a@example.com"]["id"]
    backend.rows[uid] = {"user_id": uid, "excluded_allergens": ["unobtainium"], "allergy_ingredients": [], "excluded_ingredients": [],
                         "vegetarian_only": False, "min_spice_level": None, "max_spice_level": None, "allergy_status": "reported"}
    at = log_in(launch(ext()), "a@example.com")
    shown = page(at)
    assert components.PROFILE_UNAVAILABLE_AR in shown and DISLIKE_LABEL not in shown


def test_the_guest_has_no_profile_section(launch):
    at = launch()
    next(b for b in at.button if b.key == "guest").click().run()
    assert components.PROFILE_TITLE_AR not in [e.label for e in at.expander]


# ---- "dishes with X": explored scenarios ----

def onboarded_with_cheese_dislike(backend):
    guest = Guest(backend, "a@example.com")
    guest.onboard()
    guest.say("ما أحب الجبن", ext(ingredients=["جبن"]), dishes=[CHEESE_DISH, LENTIL])
    return guest


def test_asking_for_a_disliked_food_that_exists_asks_before_overriding_and_saves_nothing_new(backend):
    guest = onboarded_with_cheese_dislike(backend)
    before = dict(stored(backend, "a@example.com"))
    ask = guest.say("أبي شي فيه جبن", ext(requested_ingredients=["جبن"], wants_recommendations=True), dishes=[CHEESE_DISH, LENTIL])
    assert ask.type is T.CLARIFICATION and "أتجاوز" in ask.reply_ar and not ask.recommendations
    yes = guest.say("ايه", ext(confirm_override=True), picks_for("cheese_pie"), dishes=[CHEESE_DISH, LENTIL])
    assert yes.type is T.RECOMMENDATIONS and [d["id"] for d in yes.recommendations] == ["cheese_pie"]
    assert "فيها جبن" in yes.lead_ar
    assert stored(backend, "a@example.com") == before  # the override is for this request only
    assert guest.state.constraints.excluded_ingredients == {"cheese"}
    later = guest.say("رشّح لي", ext(wants_recommendations=True), dishes=[CHEESE_DISH, LENTIL])
    assert all(d["id"] != "cheese_pie" for d in later.recommendations)  # the dislike is back in force


def test_declining_the_override_keeps_the_dislike(backend):
    guest = onboarded_with_cheese_dislike(backend)
    guest.say("أبي شي فيه جبن", ext(requested_ingredients=["جبن"], wants_recommendations=True), dishes=[CHEESE_DISH, LENTIL])
    no = guest.say("لا خلاص، رشّح لي شي ثاني", ext(wants_recommendations=True), dishes=[CHEESE_DISH, LENTIL])
    assert no.type is T.RECOMMENDATIONS and all(d["id"] != "cheese_pie" for d in no.recommendations)


def test_an_allergy_can_never_be_overridden_for_a_requested_food(backend):
    guest = Guest(backend, "a@example.com")
    guest.onboard()
    guest.say("عندي حساسية من الحليب", ext(allergens=["الحليب"]), dishes=[CHEESE_DISH])
    refuse = guest.say("أبي شي فيه جبن", ext(requested_ingredients=["جبن"], wants_recommendations=True), dishes=[CHEESE_DISH])
    assert refuse.type is T.NO_RESULTS and "حساسية" in refuse.reply_ar and not refuse.recommendations
    again = guest.say("ايه تجاوز", ext(confirm_override=True), dishes=[CHEESE_DISH])
    assert not again.recommendations


def test_a_requested_food_with_no_dish_on_the_menu_is_stated_not_substituted(backend):
    guest = Guest(backend, "a@example.com")
    guest.onboard()
    reply = guest.say("أبي شي فيه روبيان", ext(requested_ingredients=["روبيان"], wants_recommendations=True))
    assert reply.type is T.NO_RESULTS and "ما لقيت في القائمة طبقًا فيه روبيان" in reply.reply_ar
    assert not reply.recommendations and not guest.llm.generation_calls()


def test_a_requested_food_that_exists_limits_the_list_to_dishes_that_contain_it(backend):
    guest = Guest(backend, "a@example.com")
    guest.onboard()
    reply = guest.say("أبي شي فيه عدس", ext(requested_ingredients=["عدس"], wants_recommendations=True), picks_for("dish_001", "dish_010"))
    assert [d["id"] for d in reply.recommendations] == ["dish_010"] and "فيها عدس" in reply.lead_ar
    nxt = guest.say("رشّح لي", ext(wants_recommendations=True), picks_for("dish_001"))
    assert "dish_001" in [d["id"] for d in nxt.recommendations]  # the request filter was per-request


def test_an_unknown_requested_food_that_is_nowhere_on_the_menu_is_said_plainly(backend):
    guest = Guest(backend, "a@example.com")
    guest.onboard()
    reply = guest.say("أبي شي فيه كمأة", ext(requested_ingredients=["كمأة"], wants_recommendations=True))
    assert "ما لقيت «كمأة» في مكونات أي طبق بالقائمة" in reply.reply_ar and not reply.recommendations


def test_a_requested_food_that_clashes_with_the_diet_finds_nothing_and_says_so(backend):
    guest = Guest(backend, "a@example.com")
    guest.onboard()
    guest.say("أبي نباتي", ext(vegetarian=True))
    reply = guest.say("أبي شي فيه لحم", ext(requested_ingredients=["لحم"], wants_recommendations=True))
    assert reply.type is T.NO_RESULTS and not reply.recommendations


# ---- more probes ----

def test_a_misspelled_dislike_is_not_silently_stored_or_confirmed(backend):
    guest = Guest(backend, "a@example.com")
    guest.onboard()
    reply = guest.say("ما احب الفطرر", ext(ingredients=["الفطرر"]))
    assert "الفطرر" in reply.reply_ar and stored(backend, "a@example.com")["excluded_ingredients"] == []


def test_a_memory_question_while_the_allergy_type_is_still_open_says_so(backend):
    guest = Guest(backend, "a@example.com")
    guest.onboard()
    guest.say("عندي حساسية", ext(unclear_allergy=True))
    assert "لسا ما حدّدت نوعها" in guest.say(RECALL_SENTENCE, ext(memory_query=True)).reply_ar


def test_many_preferences_in_one_message_are_all_applied_and_saved(backend):
    guest = Guest(backend, "a@example.com")
    guest.onboard()
    guest.say(
        "نباتي، ما أحب الفطر ولا البصل، ما أبي حار، وميزانيتي ٢٥",
        ext(vegetarian=True, ingredients=["الفطر", "البصل"], max_spice_level="none", max_price_sar=25, wants_recommendations=True),
        picks_for("dish_006", "dish_009"),
    )
    row = stored(backend, "a@example.com")
    assert row["vegetarian_only"] is True and row["excluded_ingredients"] == ["mushroom", "onion"] and row["max_spice_level"] == "none"
    assert "max_price_sar" not in row
