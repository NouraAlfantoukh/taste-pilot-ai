"""Streamlit flow tests with a scripted LLM (no network)."""

from pathlib import Path

import pytest
from helpers import Scripted, ext, picks_for
from streamlit.testing.v1 import AppTest

from agent import orchestrator
from agent.orchestrator import (
    ALLERGY_QUESTION_AR,
    ASK_REQUEST_AR,
    GENERIC_ERROR_AR,
    NO_VERIFIED_OPTION_MESSAGE_AR,
    SERVICE_BUSY_AR,
)
from agent.state import AllergyStatus, ConversationState
from services.llm_service import RecoverableProviderError


APP = str(Path(__file__).resolve().parent.parent / "app.py")


def html(at):
    return "".join(m.value for m in at.markdown)


@pytest.fixture
def launch(monkeypatch):
    def _launch(llm, configured=True, state=None):
        monkeypatch.setattr("services.llm_service.any_provider_configured", lambda: configured)
        monkeypatch.setattr("config.is_supabase_configured", lambda: False)
        monkeypatch.setattr("agent.orchestrator.default_generate", llm)
        at = AppTest.from_file(APP, default_timeout=30)
        if state is not None:
            at.session_state["conversation"] = state
        return at.run()

    return _launch


def send(at, text):
    at.chat_input[0].set_value(text).run()
    return at


def test_initial_screen_is_a_welcoming_empty_state(launch):
    at = launch(Scripted())
    assert not at.exception
    page = html(at)
    assert "وش تبغى تاكل اليوم" in page and "TastePilotAI" in page
    assert ALLERGY_QUESTION_AR not in page
    assert len(at.button) == 4 and at.chat_input[0].disabled is False


def test_allergy_question_is_asked_once_across_reruns(launch):
    llm = Scripted(ext(), ext(allergy_answer="none"), ext())
    at = launch(llm)
    send(at, "هلا")
    assert html(at).count(ALLERGY_QUESTION_AR) == 1
    send(at, "لا ما عندي")
    send(at, "شكرًا")
    assert not at.exception
    assert html(at).count(ALLERGY_QUESTION_AR) == 1
    assert at.session_state["conversation"].allergy_status is AllergyStatus.NONE_REPORTED
    assert [m["role"] for m in at.session_state["messages"]] == ["user", "assistant"] * 3


def test_reported_milk_allergy_shows_only_reviewed_dishes_without_milk(launch):
    llm = Scripted(ext(allergens=["حليب"], wants_recommendations=True), picks_for("dish_002", "dish_014", "dish_001", "dish_010"))
    at = launch(llm)
    send(at, "عندي حساسية من الحليب، رشّح لي")
    page = html(at)
    assert NO_VERIFIED_OPTION_MESSAGE_AR not in page and "حساسية: الحليب" in page
    assert "كبسة دجاج" in page and "شوربة عدس" in page
    assert "مندي لحم" not in page and "شاي كرك" not in page  # the dishes that contain milk are never shown


def test_recommendations_render_as_cards_from_menu_data(launch):
    state = ConversationState(allergy_status=AllergyStatus.NONE_REPORTED)
    llm = Scripted(ext(vegetarian=True, max_price_sar=20, wants_recommendations=True), picks_for("dish_010", "dish_015"))
    at = launch(llm, state=state)
    send(at, "نباتي وأقل من ٢٠ ريال")
    page = html(at)
    assert not at.exception
    assert page.count('<article class="tp-card">') == 2
    assert "شوربة عدس" in page and "١٤ ر.س" in page and "عصير ليمون بالنعناع" in page
    assert "dish_010" not in page and "dish_015" not in page


def test_provider_error_is_shown_in_arabic_and_the_conversation_continues(launch):
    llm = Scripted(RecoverableProviderError("x"), ext())
    at = launch(llm)
    send(at, "هلا")
    assert SERVICE_BUSY_AR in html(at) and "tp-error" in html(at)
    send(at, "هلا مرة ثانية")
    assert ALLERGY_QUESTION_AR in html(at)


def test_unexpected_failure_never_shows_a_stack_trace(launch, monkeypatch):
    at = launch(Scripted())

    def boom(*args, **kwargs):
        raise RuntimeError("secret internal detail")

    monkeypatch.setattr(orchestrator, "handle_message", boom)
    send(at, "هلا")
    assert not at.exception
    assert GENERIC_ERROR_AR in html(at) and "secret internal detail" not in html(at)


def test_missing_provider_configuration_is_explained_and_input_is_disabled(launch):
    at = launch(Scripted(), configured=False)
    assert not at.exception
    assert at.chat_input[0].disabled is True
    assert "غير مهيأة" in html(at)
    assert all(b.proto.disabled for b in at.button if b.label != "محادثة جديدة")


def test_suggestion_button_sends_its_text(launch):
    at = launch(Scripted(ext()))
    suggestion = next(b for b in at.button if "نباتي" in b.label)
    suggestion.click().run()
    assert at.session_state["messages"][0] == {"role": "user", "text": suggestion.label}
    assert ALLERGY_QUESTION_AR in html(at)


def test_new_conversation_resets_everything_including_the_allergy_question(launch):
    llm = Scripted(ext(allergens=["بيض"]), picks_for("dish_001"), ext())
    at = launch(llm)
    send(at, "حساسية بيض")
    assert at.session_state["conversation"].constraints.excluded_allergens
    next(b for b in at.button if b.label == "محادثة جديدة").click().run()
    assert at.session_state["messages"] == []
    assert at.session_state["conversation"].constraints.excluded_allergens == frozenset()
    assert at.session_state["conversation"].allergy_status is AllergyStatus.NOT_ASKED
    send(at, "هلا")
    assert html(at).count(ALLERGY_QUESTION_AR) == 1


def test_ask_request_reply_after_no_allergies(launch):
    llm = Scripted(ext(), ext(allergy_answer="none"))
    at = launch(llm)
    send(at, "هلا")
    send(at, "ما عندي حساسية")
    assert ASK_REQUEST_AR in html(at)
