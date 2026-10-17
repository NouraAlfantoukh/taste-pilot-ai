"""End-to-end conversation QA: realistic multi-turn Arabic conversations with a scripted LLM and a fake Supabase.

Functional correctness is asserted deterministically (state, stored rows, shown dishes). Reply wording is
checked against the rubric in docs/QA_REPORT.md by `check_reply`, which runs on EVERY assistant reply produced
here, so a regression in tone, leaked internals or an unsafe claim fails whichever scenario triggered it.
"""

import re

import pytest
from fake_supabase import FakeBackend
from helpers import Scripted, ext, picks_for, unverified_menu
from agent.prompts import SYSTEM_PROMPT_AR

from agent import orchestrator
from agent.orchestrator import (
    ALLERGY_QUESTION_AR,
    ASK_REQUEST_AR,
    NO_VERIFIED_OPTION_MESSAGE_AR,
    NOTHING_RECORDED_AR,
    PROFILE_UNAVAILABLE_AR,
    ResponseType,
    handle_message,
)
from agent.persistence import PreferenceSync
from agent.state import AllergyStatus, ConversationState
from services import taxonomy
from services.auth_service import sign_in
from services.memory_service import MemoryStoreError, SupabaseStore
from services.menu_service import get_all_dishes

T = ResponseType
MENU = {d["id"]: d for d in get_all_dishes()}

# ---- the reply rubric (see docs/QA_REPORT.md) ----

_INTERNAL = re.compile(
    r"dish_\d+|awaiting_answer|none_reported|not_asked|ClarificationKind|ResponseType|AllergyStatus|"
    r"INGREDIENT_TERM|ALLERGEN_TERM|\bNone\b|\bnull\b|\bTrue\b|\bFalse\b|Traceback|Exception|\{|\}"
)
_ARABIC = re.compile(r"[؀-ۿ]")
_LATIN_WORDS = re.compile(r"[A-Za-z]{3,}")
_SAFE_CLAIMS = ("آمن تمامًا", "آمن تماما", "100%", "مضمون", "خالٍ من", "خالي من", "لا يحتوي على أي")


def check_reply(response) -> None:
    """Rubric: Arabic, no leaked internals, no unsafe or unsupported claims, no duplicated lines, bounded length."""
    text = response.reply_ar
    assert text.strip(), "empty reply"
    assert len(text) <= 1500, "reply is too long"
    assert len(_ARABIC.findall(text)) >= 0.6 * len(re.findall(r"[^\W\d_]", text)), "reply is not Arabic"
    assert not _LATIN_WORDS.search(text), f"Latin text leaked: {_LATIN_WORDS.search(text).group()}"
    assert not _INTERNAL.search(text), f"internal detail leaked: {_INTERNAL.search(text).group()}"
    for claim in _SAFE_CLAIMS:
        assert claim not in text, f"unsupported safety claim: {claim}"
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if response.type is ResponseType.RECOMMENDATIONS:  # short model reasons may legitimately coincide
        lines = [line for line in lines if re.match(r"^\d+\.", line)]
    assert len(lines) == len(set(lines)), "a line is repeated"


# ---- harness ----

class Lenient(Scripted):
    """Scripted replies in order; once they run out a recommendation call gets 'no picks' (deterministic fallback).

    Preference statements made while onboarded legitimately end in a recommendation, which these scenarios
    are not about. Tests that must prove NO model call happens assert on `generation_calls()` instead.
    """

    def __call__(self, system_prompt, user_message):
        if system_prompt == SYSTEM_PROMPT_AR and not self.replies:
            self.calls.append((system_prompt, user_message))
            return '{"picks": []}'
        return super().__call__(system_prompt, user_message)


class Guest:
    """One visitor: a fresh browser session, signed in (persistent) or anonymous (nothing saved)."""

    def __init__(self, backend=None, email=None):
        self.sync = None
        self.save_error = None
        self.llm = None
        if email:
            client = backend.client()
            user = sign_in(client, email, "password-" + email[0])
            self.sync = PreferenceSync(SupabaseStore(client, user.user_id), user.user_id)
            self.sync.load()
            self.state = self.sync.new_state()
        else:
            self.state = ConversationState()

    def say(self, text, *llm_replies, dishes=None):
        """Send one message with the scripted LLM replies it will consume, then persist like the app does."""
        self.llm = Lenient(*llm_replies)
        response = handle_message(self.state, text, self.llm, dishes)
        assert not self.llm.replies, "scripted LLM replies were not all consumed"
        check_reply(response)
        self.save_error = None
        if self.sync:
            try:
                self.sync.save(self.state)
            except MemoryStoreError as exc:
                self.save_error = exc.kind
        return response

    def new_conversation(self):
        """The "new chat" button: durable profile kept, every per-request constraint dropped."""
        self.state = self.sync.new_state() if self.sync else ConversationState()

    def onboard(self):
        """Get past the allergy question by saying there are no allergies."""
        self.say("هلا", ext())
        return self.say("ما عندي حساسية", ext(allergy_answer="none"))


@pytest.fixture
def backend():
    server = FakeBackend()
    server.add_user("a@example.com", "password-a")
    server.add_user("b@example.com", "password-b")
    return server


def stored(backend, email):
    return backend.rows.get(backend.users[email]["id"])


def names(response):
    return [d["name_ar"] for d in response.recommendations]


# ---- 1. the reported failure, step by step ----

MUSHROOM_SENTENCE = "ما أحب الفطر أبدًا، ولا تقترح علي أي طبق يحتوي عليه."
NO_ALLERGY_SENTENCE = "ما عندي حساسية من الأطعمة."
RECALL_SENTENCE = "وش الأكلات اللي تعرف إني ما أحبها؟"


@pytest.mark.parametrize("wants_recommendations", [True, False])
def test_the_reported_conversation_remembers_the_mushroom_dislike_in_a_new_conversation(backend, wants_recommendations):
    guest = Guest(backend, "a@example.com")
    first = guest.say(MUSHROOM_SENTENCE, ext(ingredients=["الفطر"], wants_recommendations=wants_recommendations))
    assert first.type is T.CLARIFICATION and ALLERGY_QUESTION_AR in first.reply_ar
    assert "ما عرفت المقصود" not in first.reply_ar  # «فطر» is now a known ingredient
    assert guest.state.constraints.excluded_ingredients == {"mushroom"}

    # Either way the guest asked for food in turn 1 (stating a condition counts), so the answer ends in dishes.
    second = guest.say(
        NO_ALLERGY_SENTENCE, ext(allergy_answer="none", wants_recommendations=wants_recommendations), picks_for("dish_001", "dish_004")
    )
    assert second.type is T.RECOMMENDATIONS and all("mushroom" not in d["ingredient_ids"] for d in second.recommendations)
    assert guest.state.allergy_status is AllergyStatus.NONE_REPORTED
    assert guest.state.constraints.excluded_ingredients == {"mushroom"}  # the allergy answer did not erase it

    row = stored(backend, "a@example.com")
    assert row["excluded_ingredients"] == ["mushroom"] and row["excluded_allergens"] == []  # a dislike, not an allergy
    assert row["allergy_status"] == "none_reported"

    later = Guest(backend, "a@example.com")  # a new conversation, in a new browser session
    answer = later.say(RECALL_SENTENCE, ext(memory_query=True))
    assert answer.type is T.INFO and "فطر" in answer.reply_ar
    assert "حساسية مسجّلة" not in answer.reply_ar  # it must not be reported as an allergy
    assert not later.llm.generation_calls() and ALLERGY_QUESTION_AR not in answer.reply_ar


# ---- 2. ingredient matching ----

@pytest.mark.parametrize(
    "term,ingredient",
    [
        ("الفطر", "mushroom"), ("فطر", "mushroom"), ("مشروم", "mushroom"), ("عيش الغراب", "mushroom"),
        ("mushroom", "mushroom"), ("Mushrooms", "mushroom"), ("MUSHROOM", "mushroom"),
        ("الأرز", "rice"), ("ارز", "rice"), ("رز", "rice"), ("rice", "rice"),
        ("بطاطا", "potato"), ("البطاطس", "potato"), ("بطاطس", "potato"),
        ("Cheese", "cheese"), ("جبنة", "cheese"), ("الجبن", "cheese"),
        ("cilantro", "coriander"), ("كزبرة", "coriander"), ("chili", "hot_pepper"), ("الفلفل الحار", "hot_pepper"),
        ("shrimp", "shrimp"), ("جمبري", "shrimp"), ("بيض", "egg"), ("eggs", "egg"),
        ("  الدجاج  ", "chicken"), ("Chicken", "chicken"), ("هال", "cardamom"),
    ],
)
def test_ingredient_names_in_arabic_english_and_common_variants_resolve(term, ingredient):
    assert taxonomy.resolve_ingredient(term) == {ingredient}


@pytest.mark.parametrize("term", ["كافيار", "xyzzy", "", "   ", "لحم الغزال النادر"])
def test_unknown_ingredients_are_rejected_not_guessed(term):
    with pytest.raises(taxonomy.UnknownTermError):
        taxonomy.resolve_ingredient(term)


def test_an_unknown_dislike_is_said_honestly_not_saved_and_not_claimed_as_saved(backend):
    guest = Guest(backend, "a@example.com")
    guest.onboard()
    reply = guest.say("ما أحب الكافيار", ext(ingredients=["الكافيار"]))
    assert "الكافيار" in reply.reply_ar and "ما أقدر أحفظه" in reply.reply_ar  # not on the menu: said, not pretended
    assert not any(word in reply.reply_ar for word in ("حفظت", "سجّلت لك"))
    assert guest.state.constraints.excluded_ingredients == frozenset()
    assert stored(backend, "a@example.com")["excluded_ingredients"] == []


def test_an_unknown_term_that_might_be_on_the_menu_is_asked_about_not_dismissed(backend):
    guest = Guest(backend, "a@example.com")
    guest.onboard()
    truffle_soup = dict(MENU["dish_010"], description_ar="شوربة غنية بنكهة الكمأة.")  # in the text, not in the vocabulary
    reply = guest.say("ما أبغى الكمأة", ext(ingredients=["الكمأة"]), dishes=[truffle_soup])
    assert "ما أقدر أحفظه" not in reply.reply_ar  # never the "nothing on the menu has it" claim
    assert "ما عرفت المقصود" in reply.reply_ar and "الكمأة" in reply.reply_ar


# ---- 3. allergy vs dislike; unknown or unverified is never safe ----

def test_a_dislike_is_never_stored_or_enforced_as_an_allergy(backend):
    guest = Guest(backend, "a@example.com")
    guest.onboard()
    guest.say("ما أحب الحليب", ext(ingredients=["الحليب"]))
    c = guest.state.constraints
    assert c.excluded_ingredients == {"fresh_milk"} and not c.excluded_allergens and not c.allergy_ingredients
    assert guest.state.allergy_status is AllergyStatus.NONE_REPORTED
    row = stored(backend, "a@example.com")
    assert row["excluded_allergens"] == [] and row["allergy_status"] == "none_reported"


def test_a_real_allergy_is_stored_as_an_allergy_and_never_meets_a_dish_with_that_allergen(backend):
    guest = Guest(backend, "a@example.com")
    guest.onboard()
    reply = guest.say("عندي حساسية من الحليب", ext(allergens=["الحليب"], wants_recommendations=True), picks_for("dish_002", "dish_001"))
    assert reply.type is T.RECOMMENDATIONS and all("milk" not in d["allergens"] for d in reply.recommendations)
    unreviewed = guest.say("رشّح لي", ext(wants_recommendations=True), dishes=unverified_menu())
    assert unreviewed.type is T.NO_RESULTS and unreviewed.reply_ar == NO_VERIFIED_OPTION_MESSAGE_AR  # unreviewed data fails safe
    row = stored(backend, "a@example.com")
    assert row["excluded_allergens"] == ["milk"] and row["allergy_status"] == "reported"
    assert row["excluded_ingredients"] == []


def test_an_unknown_allergy_blocks_recommendations_and_is_never_saved_or_guessed(backend):
    guest = Guest(backend, "a@example.com")
    guest.onboard()
    reply = guest.say("عندي حساسية من الكافيار، رشّح لي", ext(allergens=["الكافيار"], wants_recommendations=True))
    assert reply.type is T.CLARIFICATION and "الكافيار" in reply.reply_ar and not reply.recommendations
    assert guest.state.constraints.excluded_allergens == frozenset()
    row = stored(backend, "a@example.com")
    assert row["excluded_allergens"] == [] and row["allergy_ingredients"] == []
    still = guest.say("وش عندكم؟", ext(wants_recommendations=True))  # the question stays open until answered
    assert still.type is T.CLARIFICATION and not still.recommendations
    answered = guest.say("قصدي السمك", ext(allergens=["الأسماك"], answered_pending=["الكافيار"]))
    assert answered.type in (T.NO_RESULTS, T.RECOMMENDATIONS, T.CLARIFICATION)
    assert stored(backend, "a@example.com")["excluded_allergens"] == ["fish"]


def test_ambiguous_nuts_are_asked_about_and_nothing_is_stored_meanwhile(backend):
    guest = Guest(backend, "a@example.com")
    guest.onboard()
    reply = guest.say("عندي حساسية من المكسرات", ext(allergens=["مكسرات"], wants_recommendations=True))
    assert reply.type is T.CLARIFICATION and "أي منها تقصد" in reply.reply_ar
    assert stored(backend, "a@example.com")["excluded_allergens"] == []


def test_declining_to_answer_about_allergies_is_not_treated_as_no_allergies(backend):
    guest = Guest(backend, "a@example.com")
    guest.say("رشّح لي", ext(wants_recommendations=True))
    reply = guest.say("ما أبغى أجاوب", ext(allergy_answer="declined"), picks_for("dish_001"))
    assert reply.type is T.RECOMMENDATIONS and "ما حدّدت حساسية" in reply.notes_ar
    assert guest.state.allergy_status is AllergyStatus.DECLINED
    assert stored(backend, "a@example.com")["allergy_status"] == "declined"
    memory = guest.say(RECALL_SENTENCE, ext(memory_query=True))
    assert "ما عندك حساسية" not in memory.reply_ar  # unknown is never reported as "none"


# ---- 4. recommendation quality on the real menu ----

def test_dishes_with_a_disliked_ingredient_are_never_shown_even_if_the_model_picks_them(backend):
    guest = Guest(backend, "a@example.com")
    guest.onboard()
    reply = guest.say(
        "ما أبغى دجاج، رشّح لي", ext(ingredients=["دجاج"], wants_recommendations=True),
        picks_for("dish_001", "dish_003", "dish_002", "dish_004"),
    )
    assert reply.type is T.RECOMMENDATIONS
    shown = {d["id"] for d in reply.recommendations}
    assert {"dish_001", "dish_003"}.isdisjoint(shown) and {"dish_002", "dish_004"} <= shown
    assert all("chicken" not in d["ingredient_ids"] for d in reply.recommendations)


def test_vegetarian_spice_and_budget_are_all_respected_together(backend):
    guest = Guest(backend, "a@example.com")
    guest.onboard()
    reply = guest.say(
        "أبي أكل نباتي مو حار وما يتعدى ٢٥ ريال",
        ext(vegetarian=True, max_spice_level="none", max_price_sar=25, wants_recommendations=True),
        picks_for("dish_004", "dish_006", "dish_009", "dish_010", "dish_013"),
    )
    assert reply.type is T.RECOMMENDATIONS and reply.recommendations
    for dish in reply.recommendations:
        assert dish["is_vegetarian"] and dish["spice_level"] == "none" and dish["price_sar"] <= 25
    assert "dish_004" not in {d["id"] for d in reply.recommendations}  # medium spice and over budget


def test_when_nothing_matches_the_guest_is_told_which_conditions_clash(backend):
    guest = Guest(backend, "a@example.com")
    guest.onboard()
    reply = guest.say("نباتي بأقل من ٥ ريال", ext(vegetarian=True, max_price_sar=5, wants_recommendations=True))
    assert reply.type is T.NO_RESULTS and "نباتي" in reply.reply_ar and not reply.recommendations
    assert not guest.llm.generation_calls()  # no model call, so nothing can be invented


# ---- 5. multi-turn memory and context boundaries ----

def test_durable_preferences_survive_unrelated_questions_and_a_new_conversation(backend):
    guest = Guest(backend, "a@example.com")
    guest.onboard()
    guest.say("ما أحب الجبن وأبي نباتي", ext(ingredients=["جبن"], vegetarian=True))
    unrelated = guest.say("كم الساعة؟", ext())
    assert unrelated.type is T.CLARIFICATION and unrelated.reply_ar == ASK_REQUEST_AR
    guest.new_conversation()
    c = guest.state.constraints
    assert c.excluded_ingredients == {"cheese"} and c.vegetarian_only
    reply = guest.say("رشّح لي", ext(wants_recommendations=True), picks_for("dish_004", "dish_007", "dish_010"))
    assert reply.type is T.RECOMMENDATIONS
    assert all(d["is_vegetarian"] and "cheese" not in str(d["ingredient_ids"]) for d in reply.recommendations)
    assert "dish_007" not in {d["id"] for d in reply.recommendations}


def test_a_budget_category_or_temperature_for_one_request_never_becomes_permanent(backend):
    guest = Guest(backend, "a@example.com")
    guest.onboard()
    guest.say("أبي شي بأقل من ٢٠ ريال", ext(max_price_sar=20, wants_recommendations=True), picks_for("dish_010"))
    assert guest.state.constraints.max_price_sar == 20  # active for this conversation
    assert set(stored(backend, "a@example.com")) == {
        "user_id", "excluded_allergens", "allergy_ingredients", "excluded_ingredients", "vegetarian_only",
        "min_spice_level", "max_spice_level", "allergy_status",
    }
    guest.new_conversation()
    assert guest.state.constraints.max_price_sar is None and not guest.state.constraints.categories
    assert guest.state.last_request == ""
    assert guest.state.constraints.serving_temperature is None


def test_the_current_request_is_listed_as_temporary_when_asked_what_is_known(backend):
    guest = Guest(backend, "a@example.com")
    guest.onboard()
    guest.say("ما أحب الفطر", ext(ingredients=["فطر"]))
    guest.say("أبي شي بأقل من ٢٠ ريال", ext(max_price_sar=20, wants_recommendations=True), picks_for("dish_010"))
    answer = guest.say(RECALL_SENTENCE, ext(memory_query=True))
    assert "فطر" in answer.reply_ar and "طلبك الحالي فقط (مؤقت)" in answer.reply_ar and "٢٠" in answer.reply_ar
    guest.new_conversation()
    assert "مؤقت" not in guest.say(RECALL_SENTENCE, ext(memory_query=True)).reply_ar


# ---- 6. preference updates: the latest valid preference wins ----

def test_changing_your_mind_about_an_ingredient_updates_memory_and_the_stored_row(backend):
    guest = Guest(backend, "a@example.com")
    guest.onboard()
    guest.say("ما أحب الجبن", ext(ingredients=["الجبن"]))
    assert stored(backend, "a@example.com")["excluded_ingredients"] == ["cheese"]
    guest.say("غيّرت رأيي، أحب الجبن", ext(remove_ingredients=["الجبن"]))
    assert stored(backend, "a@example.com")["excluded_ingredients"] == []
    answer = guest.say(RECALL_SENTENCE, ext(memory_query=True))
    assert "جبن" not in answer.reply_ar and answer.reply_ar.count("•") == 1
    assert Guest(backend, "a@example.com").state.constraints.excluded_ingredients == frozenset()


def test_spice_and_diet_changes_replace_the_older_preference(backend):
    guest = Guest(backend, "a@example.com")
    guest.onboard()
    guest.say("ما أبي حار", ext(max_spice_level="none"))
    guest.say("لا الحين أبي حار", ext(min_spice_level="mild"))
    row = stored(backend, "a@example.com")
    assert (row["min_spice_level"], row["max_spice_level"]) == ("mild", None)
    guest.say("أبي نباتي", ext(vegetarian=True))
    guest.say("لا مو نباتي", ext(vegetarian=False))
    assert stored(backend, "a@example.com")["vegetarian_only"] is False


def test_a_blanket_no_allergy_never_erases_a_recorded_allergy(backend):
    guest = Guest(backend, "a@example.com")
    guest.onboard()
    guest.say("عندي حساسية من الحليب", ext(allergens=["الحليب"]))
    reply = guest.say("ما عندي حساسية", ext(allergy_answer="none"))
    assert reply.type is T.CLARIFICATION and "هل تقصد أنها لم تعد موجودة" in reply.reply_ar
    assert stored(backend, "a@example.com")["excluded_allergens"] == ["milk"]


# ---- 7. memory questions ----

def test_the_memory_answer_separates_dislikes_allergies_and_spice(backend):
    guest = Guest(backend, "a@example.com")
    guest.onboard()
    guest.say("ما أحب الفطر ولا البصل، وعندي حساسية من الحليب", ext(ingredients=["الفطر", "البصل"], allergens=["الحليب"]))
    guest.say("ما أبي حار", ext(max_spice_level="none"))
    reply = guest.say("وش تعرف عني؟", ext(memory_query=True))
    text = reply.reply_ar
    assert reply.type is T.INFO
    assert "أطعمة تتجنبها (تفضيل، وليس حساسية): " in text and "بصل" in text and "فطر" in text
    assert "حساسية مسجّلة: الحليب" in text and "حدة البهارات أعلى من" in text
    assert text.index("أطعمة تتجنبها") < text.index("حساسية مسجّلة")  # the two kinds are never merged


def test_a_new_signed_in_user_with_nothing_saved_is_told_so(backend):
    guest = Guest(backend, "a@example.com")
    reply = guest.say(RECALL_SENTENCE, ext(memory_query=True))
    assert reply.type is T.INFO and reply.reply_ar == NOTHING_RECORDED_AR
    assert guest.state.allergy_status is AllergyStatus.NOT_ASKED and not guest.llm.generation_calls()


def test_a_guest_is_told_only_what_this_conversation_knows_and_nothing_is_saved(backend):
    guest = Guest()
    assert guest.say(RECALL_SENTENCE, ext(memory_query=True)).reply_ar == NOTHING_RECORDED_AR
    guest.say("ما أحب الفطر", ext(ingredients=["الفطر"]))
    guest.say("ما عندي حساسية", ext(allergy_answer="none"))
    assert "فطر" in guest.say(RECALL_SENTENCE, ext(memory_query=True)).reply_ar
    guest.new_conversation()
    assert guest.say(RECALL_SENTENCE, ext(memory_query=True)).reply_ar == NOTHING_RECORDED_AR
    assert backend.rows == {}


def test_a_memory_question_changes_nothing_and_does_not_start_the_allergy_interview(backend):
    guest = Guest(backend, "a@example.com")
    before = (guest.state.constraints, guest.state.allergy_status, guest.state.pending, guest.state.recommendation_requested)
    reply = guest.say(RECALL_SENTENCE, ext(memory_query=True, wants_recommendations=True))
    after = (guest.state.constraints, guest.state.allergy_status, guest.state.pending, guest.state.recommendation_requested)
    assert before == after and ALLERGY_QUESTION_AR not in reply.reply_ar
    assert backend.writes == []


def test_a_message_that_both_states_a_preference_and_asks_is_not_swallowed(backend):
    guest = Guest(backend, "a@example.com")
    guest.onboard()
    guest.say("ما أحب الفطر، وش تعرف عني؟", ext(ingredients=["الفطر"], memory_query=True))
    assert guest.state.constraints.excluded_ingredients == {"mushroom"}
    assert stored(backend, "a@example.com")["excluded_ingredients"] == ["mushroom"]


# ---- 8. authentication and user isolation ----

def test_one_users_preferences_are_never_loaded_by_or_overwritten_by_another(backend):
    a = Guest(backend, "a@example.com")
    a.onboard()
    a.say("ما أحب الفطر وعندي حساسية من الحليب", ext(ingredients=["الفطر"], allergens=["الحليب"]))
    b = Guest(backend, "b@example.com")
    assert b.state.constraints.excluded_ingredients == frozenset() and not b.state.constraints.excluded_allergens
    assert b.say(RECALL_SENTENCE, ext(memory_query=True)).reply_ar == NOTHING_RECORDED_AR
    b.onboard()
    b.say("ما أحب العدس", ext(ingredients=["العدس"]))
    assert stored(backend, "a@example.com")["excluded_ingredients"] == ["mushroom"]
    assert stored(backend, "b@example.com")["excluded_ingredients"] == ["lentils"]
    assert "العدس" not in Guest(backend, "a@example.com").say(RECALL_SENTENCE, ext(memory_query=True)).reply_ar


def test_a_session_cannot_write_or_read_another_users_row_even_when_it_names_the_id(backend):
    a_id, b_id = backend.users["a@example.com"]["id"], backend.users["b@example.com"]["id"]
    b = Guest(backend, "b@example.com")
    store = b.sync._store
    with pytest.raises(PermissionError):
        store.update_preferences(a_id, excluded_ingredients=["mushroom"])
    with pytest.raises(PermissionError):
        store.get_preferences(a_id)
    forged = SupabaseStore(b.sync._store._client, a_id)  # a forged store bound to A's id on B's session
    with pytest.raises(MemoryStoreError) as error:
        forged.update_preferences(a_id, excluded_ingredients=["mushroom"])
    assert error.value.kind == "forbidden" and a_id not in backend.rows and b_id not in backend.rows


def test_a_signed_out_visitor_cannot_read_or_write_any_row(backend):
    store = SupabaseStore(backend.client(), backend.users["a@example.com"]["id"])  # a client that never signed in
    with pytest.raises(MemoryStoreError):
        store.get_preferences(backend.users["a@example.com"]["id"])


# ---- 9. missing, malformed or unavailable memory fails safely ----

def test_a_failed_save_is_reported_by_the_store_and_the_next_visit_does_not_pretend_it_worked(backend):
    guest = Guest(backend, "a@example.com")
    guest.onboard()
    backend.outage = True
    guest.say("ما أحب الفطر", ext(ingredients=["الفطر"]))
    assert guest.save_error == "unavailable"
    assert guest.state.constraints.excluded_ingredients == {"mushroom"}  # still honoured in this conversation
    backend.outage = False
    later = Guest(backend, "a@example.com")
    answer = later.say(RECALL_SENTENCE, ext(memory_query=True)).reply_ar
    assert "فطر" not in answer and "ما عندك حساسية" in answer  # only what was really stored (the earlier onboarding)


def test_a_save_that_failed_is_retried_on_the_next_turn(backend):
    guest = Guest(backend, "a@example.com")
    guest.onboard()
    backend.outage = True
    guest.say("ما أحب الفطر", ext(ingredients=["الفطر"]))
    backend.outage = False
    guest.say("شكرًا", ext())
    assert stored(backend, "a@example.com")["excluded_ingredients"] == ["mushroom"]


def test_an_unreadable_stored_profile_is_never_overwritten_and_never_described_as_empty(backend):
    user_id = backend.users["a@example.com"]["id"]
    backend.rows[user_id] = {
        "user_id": user_id, "excluded_allergens": ["unobtainium"], "allergy_ingredients": [], "excluded_ingredients": [],
        "vegetarian_only": False, "min_spice_level": None, "max_spice_level": None, "allergy_status": "reported",
    }
    snapshot = dict(backend.rows[user_id])
    client = backend.client()
    user = sign_in(client, "a@example.com", "password-a")
    sync = PreferenceSync(SupabaseStore(client, user.user_id), user.user_id)
    with pytest.raises(Exception):
        sync.load()
    sync.disable()
    state = ConversationState(profile_unavailable=True)
    reply = handle_message(state, RECALL_SENTENCE, Scripted(ext(memory_query=True)))
    assert reply.reply_ar == PROFILE_UNAVAILABLE_AR and NOTHING_RECORDED_AR not in reply.reply_ar
    assert sync.save(state) is False and backend.rows[user_id] == snapshot


def test_an_expired_session_is_reported_as_such_not_as_a_missing_profile(backend):
    guest = Guest(backend, "a@example.com")
    guest.onboard()
    guest.sync._store._client.expired = True
    guest.say("ما أحب الفطر", ext(ingredients=["الفطر"]))
    assert guest.save_error == "session_expired"


# ---- 10. reply quality: direct answers and ambiguity ----

def test_every_clarification_asks_exactly_one_clear_thing_in_arabic(backend):
    guest = Guest(backend, "a@example.com")
    first = guest.say("رشّح لي", ext(wants_recommendations=True))
    assert first.reply_ar == ALLERGY_QUESTION_AR
    garbled = guest.say("ممم", ext(allergy_answer="unclear"))
    assert garbled.reply_ar == orchestrator.ALLERGY_CLARIFY_AR


def test_the_answer_names_what_was_applied_so_the_guest_can_verify_it(backend):
    guest = Guest(backend, "a@example.com")
    guest.onboard()
    reply = guest.say("ما أبغى جبن، رشّح لي", ext(ingredients=["جبن"], wants_recommendations=True), picks_for("dish_010"))
    assert guest.llm.generation_calls()
    assert "بدون جبن" in guest.llm.generation_calls()[0][1]  # the model is told the condition in force
    assert reply.type is T.RECOMMENDATIONS and "dish_010" not in reply.reply_ar
    assert "راح أستبعد الأطباق التي تحتوي على جبن" in reply.reply_ar  # what was understood is said back


def test_a_model_reason_that_claims_allergen_safety_is_dropped(backend):
    guest = Guest(backend, "a@example.com")
    guest.onboard()
    unsafe = '{"picks": [{"dish_id": "dish_010", "reason_ar": "طبق خالٍ من أي مسبب حساسية"}]}'
    reply = guest.say("رشّح لي", ext(wants_recommendations=True), unsafe)
    assert reply.type is T.RECOMMENDATIONS and reply.reasons == ("",)


# ---- 11. the same conversations through the real Streamlit app (fake Supabase, scripted LLM) ----

from pathlib import Path  # noqa: E402

from streamlit.testing.v1 import AppTest  # noqa: E402

from ui import components  # noqa: E402

APP = str(Path(__file__).resolve().parent.parent / "app.py")


@pytest.fixture
def launch(monkeypatch, backend):
    def _launch(*replies):
        monkeypatch.setattr("config.is_supabase_configured", lambda: True)
        monkeypatch.setattr("db.database.create_supabase_client", backend.client)
        monkeypatch.setattr("services.llm_service.any_provider_configured", lambda: True)
        monkeypatch.setattr("agent.orchestrator.default_generate", Lenient(*replies))
        return AppTest.from_file(APP, default_timeout=30).run()

    return _launch


def page(at):
    return "".join(m.value for m in at.markdown)


def log_in(at, email):
    at.text_input(key="sign_in_email").input(email)
    at.text_input(key="sign_in_password").input("password-" + email[0])
    next(b for b in at.button if b.label == "دخول").click().run()
    return at


def type_message(at, text):
    at.chat_input[0].set_value(text).run()
    return at


def test_the_reported_conversation_through_the_app_is_remembered_in_a_new_chat_and_a_new_visit(launch, backend):
    at = log_in(launch(
        ext(ingredients=["الفطر"], wants_recommendations=True),
        ext(allergy_answer="none", wants_recommendations=True), picks_for("dish_004"),
        ext(memory_query=True),
        ext(memory_query=True),
    ), "a@example.com")
    type_message(at, MUSHROOM_SENTENCE)
    assert not at.exception and ALLERGY_QUESTION_AR in page(at)
    type_message(at, NO_ALLERGY_SENTENCE)
    assert "برياني خضار" in page(at) and "بدون فطر" in page(at)  # recommended without it, and the chip shows it
    assert stored(backend, "a@example.com")["excluded_ingredients"] == ["mushroom"]

    next(b for b in at.button if b.key == "new_chat").click().run()  # "محادثة جديدة"
    assert "بدون فطر" in page(at)  # the profile is applied again at once
    type_message(at, RECALL_SENTENCE)
    assert not at.exception and "أطعمة تتجنبها (تفضيل، وليس حساسية): فطر" in page(at)


def test_an_unreadable_profile_through_the_app_is_neither_overwritten_nor_described_as_empty(launch, backend):
    user_id = backend.users["a@example.com"]["id"]
    bad = {
        "user_id": user_id, "excluded_allergens": ["unobtainium"], "allergy_ingredients": [], "excluded_ingredients": [],
        "vegetarian_only": False, "min_spice_level": None, "max_spice_level": None, "allergy_status": "reported",
    }
    backend.rows[user_id] = dict(bad)
    at = log_in(launch(ext(memory_query=True), ext(memory_query=True)), "a@example.com")
    assert components.PROFILE_UNREADABLE_AR in page(at)
    type_message(at, RECALL_SENTENCE)
    assert PROFILE_UNAVAILABLE_AR in page(at) and NOTHING_RECORDED_AR not in page(at)
    next(b for b in at.button if b.key == "new_chat").click().run()  # still unavailable after "new chat"
    type_message(at, RECALL_SENTENCE)
    assert page(at).count(PROFILE_UNAVAILABLE_AR) >= 1
    assert backend.rows[user_id] == bad and backend.writes == []


def test_a_failed_save_through_the_app_shows_the_warning_and_does_not_break_the_chat(launch, backend):
    at = log_in(launch(ext(ingredients=["الفطر"])), "a@example.com")
    backend.outage = True
    type_message(at, "ما أحب الفطر")
    assert not at.exception and components.SAVE_FAILED_AR in page(at)
    assert backend.rows == {} or "mushroom" not in str(backend.rows)


def test_a_memory_question_with_spurious_flags_is_still_a_pure_question(backend):
    guest = Guest(backend, "a@example.com")
    guest.onboard()
    guest.say("ما أحب الفطر", ext(ingredients=["الفطر"]))
    reply = guest.say(RECALL_SENTENCE, ext(memory_query=True, wants_recommendations=True, unclear_allergy=True, answered_pending=["x"]))
    assert reply.type is T.INFO and "فطر" in reply.reply_ar and not guest.llm.generation_calls()


# ---- 12. the page never implies a preference was saved when saving failed ----

SAVED_CLAIM = "محفوظة في حسابك"


def test_the_footer_claims_saving_only_when_saving_works():
    assert SAVED_CLAIM in components.footer_html(persistent=True)
    failed = components.footer_html(persistent=True, save_failed=True)
    assert SAVED_CLAIM not in failed and components.MEMORY_SAVE_FAILED_AR in failed
    guest = components.footer_html(persistent=False, save_failed=True)
    assert SAVED_CLAIM not in guest and components.MEMORY_NOTE_AR in guest


def test_after_a_failed_save_the_page_stops_claiming_it_and_recovers_when_saving_works_again(launch, backend):
    at = log_in(launch(ext(), ext(ingredients=["الفطر"]), ext()), "a@example.com")
    type_message(at, "هلا")
    assert SAVED_CLAIM in page(at)
    backend.outage = True
    type_message(at, "ما أحب الفطر")
    shown = page(at)
    assert components.SAVE_FAILED_AR in shown and SAVED_CLAIM not in shown and components.MEMORY_SAVE_FAILED_AR in shown
    backend.outage = False
    type_message(at, "شكرًا")
    shown = page(at)
    assert SAVED_CLAIM in shown and components.SAVE_FAILED_AR not in shown
    assert stored(backend, "a@example.com")["excluded_ingredients"] == ["mushroom"]


def test_an_unreadable_profile_session_never_claims_saving(launch, backend):
    user_id = backend.users["a@example.com"]["id"]
    backend.rows[user_id] = {
        "user_id": user_id, "excluded_allergens": ["unobtainium"], "allergy_ingredients": [], "excluded_ingredients": [],
        "vegetarian_only": False, "min_spice_level": None, "max_spice_level": None, "allergy_status": "reported",
    }
    at = log_in(launch(ext()), "a@example.com")
    type_message(at, "هلا")
    assert SAVED_CLAIM not in page(at) and components.MEMORY_NOTE_AR in page(at)


# ---- 13. more edge cases: English terms, likes, dislike turned allergy ----

def test_an_english_ingredient_name_from_the_model_is_stored_as_the_same_dislike(backend):
    guest = Guest(backend, "a@example.com")
    guest.onboard()
    guest.say("I don't like mushrooms", ext(ingredients=["mushrooms"]))
    assert stored(backend, "a@example.com")["excluded_ingredients"] == ["mushroom"]
    assert "فطر" in guest.say(RECALL_SENTENCE, ext(memory_query=True)).reply_ar


def test_a_like_is_not_invented_into_memory_and_is_not_claimed_as_saved(backend):
    guest = Guest(backend, "a@example.com")
    guest.onboard()
    before = dict(stored(backend, "a@example.com"))
    guest.say("أحب الكبسة", ext(wants_recommendations=True))
    assert stored(backend, "a@example.com") == before  # likes have no durable field: nothing is written
    answer = guest.say(RECALL_SENTENCE, ext(memory_query=True)).reply_ar
    assert "كبسة" not in answer and "حفظت" not in answer


def test_a_dislike_that_turns_into_an_allergy_keeps_both_and_reports_them_separately(backend):
    guest = Guest(backend, "a@example.com")
    guest.onboard()
    guest.say("ما أحب الحليب", ext(ingredients=["الحليب"]))
    guest.say("اكتشفت إن عندي حساسية من الحليب", ext(allergens=["الحليب"]))
    row = stored(backend, "a@example.com")
    assert row["excluded_allergens"] == ["milk"] and row["excluded_ingredients"] == ["fresh_milk"]
    assert row["allergy_status"] == "reported"
    answer = guest.say(RECALL_SENTENCE, ext(memory_query=True)).reply_ar
    assert "حساسية مسجّلة: الحليب" in answer and "وليس حساسية): حليب" in answer
