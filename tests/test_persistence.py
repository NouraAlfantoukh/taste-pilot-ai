import pytest
from fake_supabase import FakeBackend
from helpers import Scripted, ext, picks_for

from agent.orchestrator import ALLERGY_QUESTION_AR, ResponseType, handle_message
from agent.persistence import PreferenceSync, PreferencesLoadError, apply_preferences, durable_snapshot
from agent.state import AllergyStatus, ClarificationKind, ConversationState, PendingClarification, PendingTerm
from services.auth_service import sign_in
from services.constraints import Constraints, build_constraints
from services.memory_service import InMemoryStore, MemoryStoreError, SupabaseStore, UserPreferences

T = ResponseType


@pytest.fixture
def backend():
    server = FakeBackend()
    server.add_user("a@example.com", "password-a")
    server.add_user("b@example.com", "password-b")
    return server


def session(backend, email):
    """A brand-new browser session: its own client, store and sync, loaded from the stored profile."""
    client = backend.client()
    user = sign_in(client, email, "password-" + email[0])
    sync = PreferenceSync(SupabaseStore(client, user.user_id), user.user_id)
    sync.load()
    return sync, sync.new_state()


def menu(make_dish):
    return [make_dish(id="ok", name_ar="طبق نظيف")]


# ---- what is loaded ----

def test_a_new_user_starts_with_a_blank_profile(backend):
    sync, state = session(backend, "a@example.com")
    assert state.constraints == Constraints() and state.allergy_status is AllergyStatus.NOT_ASKED
    assert sync.had_saved_preferences is False


def test_the_durable_profile_is_loaded_and_request_constraints_start_clean():
    prefs = UserPreferences(
        user_id="u", excluded_allergens=["milk"], allergy_ingredients=["lentils"], excluded_ingredients=["cheese"],
        vegetarian_only=True, min_spice_level="mild", max_spice_level="hot", allergy_status="reported",
    )
    state = ConversationState()
    apply_preferences(state, prefs)
    c = state.constraints
    assert (c.excluded_allergens, c.allergy_ingredients, c.excluded_ingredients) == ({"milk"}, {"lentils"}, {"cheese"})
    assert (c.vegetarian_only, c.min_spice_level, c.max_spice_level) == (True, "mild", "hot")
    assert (c.categories, c.max_price_sar, c.serving_temperature) == (frozenset(), None, None)
    assert state.allergy_status is AllergyStatus.REPORTED


@pytest.mark.parametrize(
    "stored,loaded",
    [("not_asked", AllergyStatus.NOT_ASKED), ("none_reported", AllergyStatus.NONE_REPORTED), ("declined", AllergyStatus.NOT_ASKED)],
)
def test_allergy_status_mapping_on_load(stored, loaded):
    state = ConversationState()
    apply_preferences(state, UserPreferences(user_id="u", allergy_status=stored))
    assert state.allergy_status is loaded  # a saved "declined" is asked again in a new session


def test_reported_without_any_recorded_allergy_is_not_trusted():
    state = ConversationState()
    apply_preferences(state, UserPreferences(user_id="u", allergy_status="reported"))
    assert state.allergy_status is AllergyStatus.NOT_ASKED


def test_allergies_always_win_over_an_inconsistent_status():
    state = ConversationState()
    apply_preferences(state, UserPreferences(user_id="u", excluded_allergens=["milk"], allergy_status="not_asked"))
    assert state.allergy_status is AllergyStatus.REPORTED and state.constraints.excluded_allergens == {"milk"}


def test_an_unreadable_stored_profile_fails_loudly_instead_of_dropping_an_allergy(backend):
    uid = backend.add_user("c@example.com", "password-c")
    backend.rows[uid] = {**UserPreferences(user_id=uid).to_row(), "user_id": uid,
                         "excluded_allergens": ["renamed_allergen"], "allergy_status": "reported"}
    client = backend.client()
    sign_in(client, "c@example.com", "password-c")
    sync = PreferenceSync(SupabaseStore(client, uid), uid)
    with pytest.raises(PreferencesLoadError):
        sync.load()
    sync.disable()
    state = ConversationState()
    assert sync.save(state) is False and backend.writes == []  # the stored record is never overwritten


# ---- what is saved ----

def test_only_durable_fields_are_in_the_snapshot():
    state = ConversationState(constraints=build_constraints(
        categories=["حلا"], max_price_sar=20, serving_temperature="warm", allergens=["حليب"], min_spice_level="mild"))
    state.allergy_status = AllergyStatus.REPORTED
    snapshot = durable_snapshot(state)
    assert set(snapshot) == {"excluded_allergens", "allergy_ingredients", "excluded_ingredients", "vegetarian_only",
                             "min_spice_level", "max_spice_level", "allergy_status"}
    assert snapshot["excluded_allergens"] == ["milk"] and snapshot["min_spice_level"] == "mild"


def test_an_unresolved_allergy_term_is_never_saved_or_guessed():
    state = ConversationState(constraints=Constraints())
    state.allergy_status = AllergyStatus.REPORTED
    state.pending = PendingClarification(ClarificationKind.ALLERGEN_TERM, (PendingTerm("مكسرات", ("peanuts", "tree_nuts")),))
    for _ in range(2):
        snapshot = durable_snapshot(state)
        # the unconfirmed term is never stored, but a stale "no allergies" is not kept either: the next session asks again
        assert snapshot["excluded_allergens"] == [] and snapshot["allergy_ingredients"] == []
        assert snapshot["allergy_status"] == "not_asked"
        state.pending = None


def test_confirmed_allergies_are_saved_even_while_another_term_awaits_clarification():
    state = ConversationState(constraints=build_constraints(allergens=["حليب"]))
    state.allergy_status = AllergyStatus.REPORTED
    state.pending = PendingClarification(ClarificationKind.ALLERGEN_TERM, (PendingTerm("مكسرات", ("peanuts", "tree_nuts")),))
    snapshot = durable_snapshot(state)
    assert snapshot["excluded_allergens"] == ["milk"] and snapshot["allergy_status"] == "reported"
    assert "tree_nuts" not in snapshot["excluded_allergens"] and "peanuts" not in snapshot["excluded_allergens"]


def test_status_mapping_on_save():
    state = ConversationState()
    for status, stored in [(AllergyStatus.NOT_ASKED, "not_asked"), (AllergyStatus.AWAITING_ANSWER, "not_asked"),
                           (AllergyStatus.NONE_REPORTED, "none_reported"), (AllergyStatus.DECLINED, "declined")]:
        state.allergy_status = status
        assert durable_snapshot(state)["allergy_status"] == stored


def test_save_writes_only_when_something_durable_changed(backend):
    sync, state = session(backend, "a@example.com")
    assert sync.save(state) is False and backend.writes == []
    state.constraints = build_constraints(categories=["حلا"], max_price_sar=20)  # per-request only
    assert sync.save(state) is False and backend.writes == []
    state.constraints = build_constraints(ingredients=["جبن"])
    assert sync.save(state) is True and len(backend.writes) == 1
    assert sync.save(state) is False and len(backend.writes) == 1  # unchanged: no second write


def test_a_store_failure_surfaces_and_does_not_pretend_to_have_saved(backend):
    sync, state = session(backend, "a@example.com")
    state.constraints = build_constraints(ingredients=["جبن"])
    backend.outage = True
    with pytest.raises(MemoryStoreError):
        sync.save(state)
    backend.outage = False
    assert sync.save(state) is True  # retried later because nothing was recorded as saved


# ---- end to end through the orchestrator ----

def test_an_allergy_told_in_one_session_is_remembered_in_the_next(backend, make_dish):
    sync, state = session(backend, "a@example.com")
    llm = Scripted(ext(), ext(allergens=["حليب"], ingredients=["جبن"], vegetarian=True, wants_recommendations=True),
                   picks_for("ok"))
    handle_message(state, "هلا", llm, menu(make_dish))
    assert state.allergy_status is AllergyStatus.AWAITING_ANSWER
    handle_message(state, "عندي حساسية من الحليب، ما أحب الجبن، ونباتي", llm, menu(make_dish))
    assert sync.save(state) is True

    sync2, state2 = session(backend, "a@example.com")  # a new visit
    assert sync2.had_saved_preferences
    assert state2.constraints.excluded_allergens == {"milk"} and state2.constraints.excluded_ingredients == {"cheese"}
    assert state2.constraints.vegetarian_only and state2.allergy_status is AllergyStatus.REPORTED
    fresh = Scripted(ext(wants_recommendations=True), picks_for("ok"))
    response = handle_message(state2, "رشّح لي", fresh, menu(make_dish))
    assert ALLERGY_QUESTION_AR not in response.reply_ar  # the initial question is not asked again


def test_a_removed_allergy_is_forgotten_next_time(backend, make_dish):
    sync, state = session(backend, "a@example.com")
    state.constraints = build_constraints(allergens=["حليب"])
    state.allergy_status = AllergyStatus.REPORTED
    sync.save(state)
    sync2, state2 = session(backend, "a@example.com")
    handle_message(state2, "ما عندي حساسية من الحليب",
                   Scripted(ext(remove_allergens=["حليب"]), picks_for("ok")), menu(make_dish))
    assert sync2.save(state2) is True
    _, state3 = session(backend, "a@example.com")
    assert state3.constraints.excluded_allergens == frozenset() and state3.allergy_status is AllergyStatus.NONE_REPORTED


def test_two_users_keep_separate_profiles(backend):
    sync_a, state_a = session(backend, "a@example.com")
    sync_b, state_b = session(backend, "b@example.com")
    state_a.constraints = build_constraints(allergens=["حليب"])
    state_a.allergy_status = AllergyStatus.REPORTED
    sync_a.save(state_a)
    _, reloaded_b = session(backend, "b@example.com")
    _, reloaded_a = session(backend, "a@example.com")
    assert reloaded_b.constraints.excluded_allergens == frozenset()
    assert reloaded_a.constraints.excluded_allergens == {"milk"}


def test_a_new_conversation_keeps_the_profile_but_drops_request_constraints(backend):
    sync, state = session(backend, "a@example.com")
    state.constraints = build_constraints(allergens=["حليب"], categories=["حلا"], max_price_sar=15)
    state.allergy_status = AllergyStatus.REPORTED
    sync.save(state)
    again = sync.new_state()
    assert again.constraints.excluded_allergens == {"milk"} and again.allergy_status is AllergyStatus.REPORTED
    assert again.constraints.categories == frozenset() and again.constraints.max_price_sar is None


def test_the_sync_works_with_any_memory_store():
    sync = PreferenceSync(InMemoryStore(), "u1")
    sync.load()
    state = sync.new_state()
    state.constraints = build_constraints(ingredients=["لحم"])
    assert sync.save(state) is True and sync.new_state().constraints.excluded_ingredients == {"meat"}
