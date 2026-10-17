"""Accounts and persistent preferences through the Streamlit app, with a fake Supabase (no network).

Every AppTest instance is an independent browser session with its own session state, so two instances
stand for two visits or two users.
"""

from pathlib import Path

import pytest
from fake_supabase import FakeBackend
from helpers import Scripted, ext
from streamlit.testing.v1 import AppTest

from agent.orchestrator import ALLERGY_QUESTION_AR
from agent.state import AllergyStatus
from db.database import PrivilegedKeyError
from ui import components

APP = str(Path(__file__).resolve().parent.parent / "app.py")
DURABLE_COLUMNS = {
    "user_id", "excluded_allergens", "allergy_ingredients", "excluded_ingredients", "vegetarian_only",
    "min_spice_level", "max_spice_level", "allergy_status",
}


@pytest.fixture
def backend():
    server = FakeBackend()
    server.add_user("a@example.com", "password-a")
    server.add_user("b@example.com", "password-b")
    return server


@pytest.fixture
def launch(monkeypatch, backend):
    def _launch(llm=None, supabase=True):
        monkeypatch.setattr("config.is_supabase_configured", lambda: supabase)
        monkeypatch.setattr("db.database.create_supabase_client", backend.client)
        monkeypatch.setattr("services.llm_service.any_provider_configured", lambda: True)
        monkeypatch.setattr("agent.orchestrator.default_generate", llm or Scripted())
        return AppTest.from_file(APP, default_timeout=30).run()

    return _launch


def html(at):
    return "".join(m.value for m in at.markdown)


def button(at, label=None, key=None):
    return next(b for b in at.button if (label is not None and b.label == label) or (key is not None and b.key == key))


def sign_in(at, email, password):
    at.text_input(key="sign_in_email").input(email)
    at.text_input(key="sign_in_password").input(password)
    button(at, label="دخول").click().run()
    return at


def say(at, text):
    at.chat_input[0].set_value(text).run()
    return at


def uid(backend, email):
    return backend.users[email]["id"]


# ---- the gate ----

def test_with_supabase_configured_the_app_asks_who_you_are_first(launch):
    at = launch()
    assert not at.exception and len(at.chat_input) == 0
    page = html(at)
    assert "tp-hero" in page and [t.key for t in at.text_input] == [
        "sign_in_email", "sign_in_password", "sign_up_email", "sign_up_password"]
    assert {b.label for b in at.button} >= {"دخول", "إنشاء حساب", "المتابعة كضيف (بدون حفظ)"}


def test_without_supabase_the_app_behaves_as_before(launch):
    at = launch(supabase=False)
    assert not at.exception and len(at.chat_input) == 1 and "sign_in_email" not in [t.key for t in at.text_input]


def test_a_wrong_password_is_refused_and_nothing_is_opened(launch, backend):
    at = sign_in(launch(), "a@example.com", "wrong-password")
    assert components.AUTH_MESSAGES_AR[__import__("services.auth_service", fromlist=["x"]).AuthFailure.INVALID_CREDENTIALS] in html(at)
    assert "auth" not in at.session_state and len(at.chat_input) == 0
    assert all(client.uid is None for client in backend.clients)


def test_signing_in_opens_the_chat_with_the_identity_supabase_reports(launch, backend):
    at = sign_in(launch(), "a@example.com", "password-a")
    assert not at.exception and len(at.chat_input) == 1
    assert at.session_state["auth"]["user"].user_id == uid(backend, "a@example.com")
    assert "a@example.com" in html(at) and "sign_in_password" not in at.session_state  # credentials are gone


def test_signing_in_discards_every_leftover_session_key(launch):
    at = launch()
    at.session_state["notice"] = "leftover from someone else"
    at.session_state["queued_prompt"] = "leftover prompt"
    sign_in(at, "a@example.com", "password-a")
    assert "leftover" not in html(at) and "queued_prompt" not in at.session_state and at.session_state["messages"] == []


def test_creating_an_account_signs_in_when_no_confirmation_is_needed(launch, backend):
    at = launch()
    at.text_input(key="sign_up_email").input("new@example.com")
    at.text_input(key="sign_up_password").input("long-enough-pass")
    button(at, label="إنشاء حساب").click().run()
    assert "auth" in at.session_state and at.session_state["auth"]["user"].email == "new@example.com"


def test_creating_an_account_that_needs_email_confirmation_explains_what_to_do(monkeypatch, launch, backend):
    backend.confirm_email = True
    at = launch()
    at.text_input(key="sign_up_email").input("new@example.com")
    at.text_input(key="sign_up_password").input("long-enough-pass")
    button(at, label="إنشاء حساب").click().run()
    assert components.SIGNUP_CONFIRM_AR in html(at) and "auth" not in at.session_state


def test_a_guest_can_chat_and_nothing_is_ever_saved(launch, backend):
    llm = Scripted(ext(vegetarian=True, excluded_ingredients=None, wants_recommendations=True, allergens=["بيض"]))
    at = launch(llm)
    button(at, key="guest").click().run()
    assert len(at.chat_input) == 1 and components.GUEST_NOTE_AR in html(at)
    say(at, "نباتي وعندي حساسية بيض")
    assert backend.writes == [] and "sync" not in at.session_state


def test_a_privileged_key_is_refused_with_a_configuration_message(monkeypatch, launch):
    def refuse():
        raise PrivilegedKeyError("no")

    at = launch()
    monkeypatch.setattr("db.database.create_supabase_client", refuse)
    sign_in(at, "a@example.com", "password-a")
    assert components.CONFIG_ERROR_AR in html(at) and "auth" not in at.session_state


# ---- persistence across visits ----

def tell_the_assistant_about_milk(at):
    say(at, "نباتي وعندي حساسية من الحليب")  # extraction below is scripted


def test_preferences_survive_a_new_visit_and_the_allergy_question_is_not_repeated(launch, backend):
    first = sign_in(launch(Scripted(ext(allergens=["حليب"], vegetarian=True, wants_recommendations=True))), "a@example.com", "password-a")
    tell_the_assistant_about_milk(first)
    row = backend.rows[uid(backend, "a@example.com")]
    assert (row["excluded_allergens"], row["vegetarian_only"], row["allergy_status"]) == (["milk"], True, "reported")

    second = sign_in(launch(Scripted(ext(wants_recommendations=True))), "a@example.com", "password-a")  # a new browser visit
    conversation = second.session_state["conversation"]
    assert conversation.constraints.excluded_allergens == {"milk"} and conversation.constraints.vegetarian_only
    assert conversation.allergy_status is AllergyStatus.REPORTED
    page = html(second)
    assert "حساسية: الحليب" in page and components.WELCOME_BACK_AR in page
    say(second, "رشّح لي")
    assert ALLERGY_QUESTION_AR not in html(second)


def test_another_user_never_sees_those_preferences(launch, backend):
    sign_in_a = sign_in(launch(Scripted(ext(allergens=["حليب"], wants_recommendations=True))), "a@example.com", "password-a")
    tell_the_assistant_about_milk(sign_in_a)
    at_b = sign_in(launch(Scripted(ext(), ext())), "b@example.com", "password-b")
    constraints = at_b.session_state["conversation"].constraints
    assert constraints.excluded_allergens == frozenset() and components.WELCOME_BACK_AR not in html(at_b)
    say(at_b, "هلا")
    assert ALLERGY_QUESTION_AR in html(at_b)  # B is a new user: the question is asked
    assert uid(backend, "b@example.com") not in {w[0] for w in backend.writes if w[1].get("excluded_allergens")}


def test_only_durable_preferences_are_stored(launch, backend):
    llm = Scripted(ext(categories=["حلا"], max_price_sar=20, serving_temperature="warm", allergens=["بيض"], wants_recommendations=True))
    at = sign_in(launch(llm), "a@example.com", "password-a")
    say(at, "أبغى حلا دافي ما يتعدى 20 وعندي حساسية بيض")
    row = backend.rows[uid(backend, "a@example.com")]
    assert set(row) == DURABLE_COLUMNS and row["excluded_allergens"] == ["eggs"]
    assert all(set(payload) <= DURABLE_COLUMNS for _, payload in backend.writes)


def test_a_new_conversation_keeps_the_profile_and_clears_the_request(launch, backend):
    at = sign_in(launch(Scripted(ext(allergens=["حليب"], categories=["حلا"], max_price_sar=15, wants_recommendations=True))),
                 "a@example.com", "password-a")
    say(at, "حلا بأقل من 15 وعندي حساسية حليب")
    button(at, key="new_chat").click().run()
    constraints = at.session_state["conversation"].constraints
    assert constraints.excluded_allergens == {"milk"} and at.session_state["conversation"].allergy_status is AllergyStatus.REPORTED
    assert constraints.categories == frozenset() and constraints.max_price_sar is None
    assert at.session_state["messages"] == []


# ---- leaving, expiry and failures ----

def test_signing_out_wipes_the_session_so_the_next_user_inherits_nothing(launch, backend):
    at = sign_in(launch(Scripted(ext(allergens=["حليب"], wants_recommendations=True))), "a@example.com", "password-a")
    say(at, "حساسية حليب")
    button(at, key="sign_out").click().run()
    assert not any(k in at.session_state for k in ("auth", "sync", "conversation", "messages")) and len(at.chat_input) == 0
    assert backend.sign_outs == [uid(backend, "a@example.com")]

    sign_in(at, "b@example.com", "password-b")  # the same browser, a different person
    assert at.session_state["conversation"].constraints.excluded_allergens == frozenset()
    assert at.session_state["auth"]["user"].user_id == uid(backend, "b@example.com")


def test_an_expired_session_sends_the_user_back_to_sign_in(launch, backend):
    at = sign_in(launch(Scripted(ext(vegetarian=True, wants_recommendations=True))), "a@example.com", "password-a")
    backend.clients[-1].expired = True
    say(at, "نباتي")
    assert components.SESSION_EXPIRED_AR in html(at) and "auth" not in at.session_state and len(at.chat_input) == 0


def test_a_failed_save_never_breaks_the_conversation_and_is_retried(launch, backend):
    at = sign_in(launch(Scripted(ext(vegetarian=True, wants_recommendations=True), ext(wants_recommendations=True))), "a@example.com", "password-a")
    backend.outage = True
    say(at, "نباتي")
    page = html(at)
    assert components.SAVE_FAILED_AR in page and "tp-assistant" in page and not at.exception
    backend.outage = False
    say(at, "كمّل")
    assert backend.rows[uid(backend, "a@example.com")]["vegetarian_only"] is True
    assert components.SAVE_FAILED_AR not in html(at)


def test_if_the_stored_profile_cannot_be_loaded_nothing_is_overwritten_and_safety_questions_return(launch, backend):
    backend.outage = True  # the sign-in succeeds, the preferences read does not
    at = sign_in(launch(Scripted(ext(vegetarian=True, wants_recommendations=True))), "a@example.com", "password-a")
    assert components.PROFILE_UNAVAILABLE_AR in html(at) and not at.session_state["sync"].enabled
    backend.outage = False
    say(at, "نباتي")
    assert backend.writes == []
    assert ALLERGY_QUESTION_AR in html(at)


def test_an_unreadable_stored_profile_is_not_partially_applied_or_overwritten(launch, backend):
    user_id = uid(backend, "a@example.com")
    backend.rows[user_id] = {"user_id": user_id, "excluded_allergens": ["renamed"], "allergy_ingredients": [],
                            "excluded_ingredients": [], "vegetarian_only": False, "min_spice_level": None,
                            "max_spice_level": None, "allergy_status": "reported"}
    at = sign_in(launch(Scripted(ext(vegetarian=True, wants_recommendations=True))), "a@example.com", "password-a")
    assert components.PROFILE_UNREADABLE_AR in html(at)
    say(at, "نباتي")
    assert backend.writes == [] and backend.rows[user_id]["excluded_allergens"] == ["renamed"]
