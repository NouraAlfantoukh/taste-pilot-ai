"""Guest sessions in the Streamlit app (production bug: every message sent the guest back to the welcome screen).

Root cause: the guest flag was stored in st.session_state["guest"], the same name as the guest button's widget key.
Streamlit owns a widget's key (its value is False after the click) and removes it when the widget is not rendered, so guest
mode ended on the next run while "messages" and "conversation" stayed in the session, which is why choosing "guest" again
brought the old conversation back.
"""

import pytest
from helpers import ext
from test_conversation_qa import backend, launch, log_in, page, type_message  # noqa: F401  (fixtures re-used)

WELCOME_FORM_KEYS = {"sign_in_email", "sign_in_password", "sign_up_email", "sign_up_password"}
APP_SESSION_KEYS = {"auth", "sync", "conversation", "messages", "awaiting", "notice", "queued_prompt", "auth_notice", "guest_mode"}


def on_welcome_screen(at) -> bool:
    return len(at.chat_input) == 0 and WELCOME_FORM_KEYS <= {t.key for t in at.text_input}


def enter_as_guest(at):
    next(b for b in at.button if b.key == "guest").click().run()
    return at


def test_a_guest_can_send_many_consecutive_messages_without_returning_to_the_welcome_screen(launch):
    at = enter_as_guest(launch(*[ext() for _ in range(6)]))
    assert not at.exception and len(at.chat_input) == 1
    for count in range(1, 5):
        type_message(at, f"رسالة {count}")
        assert not at.exception
        assert not on_welcome_screen(at), f"sent back to the welcome screen after message {count}"
        assert len(at.session_state["messages"]) == 2 * count  # the user's message and the reply, every time
    shown = page(at)
    assert all(f"رسالة {n}" in shown for n in range(1, 5))  # the whole conversation is still on screen


def test_the_guest_flag_never_shares_a_name_with_a_widget_key(launch):
    welcome = launch()
    widget_keys = {w.key for w in (*welcome.button, *welcome.text_input) if w.key}
    assert not widget_keys & APP_SESSION_KEYS  # a session key that is also a widget key is owned (and deleted) by Streamlit
    at = enter_as_guest(welcome)
    assert at.session_state["guest_mode"] is True and "guest" not in at.session_state
    chat_keys = {w.key for w in (*at.button, *at.chat_input) if getattr(w, "key", None)}
    assert not chat_keys & APP_SESSION_KEYS


def test_the_guest_conversation_and_what_it_learned_survive_reruns_and_follow_up_messages(launch):
    at = enter_as_guest(launch(ext(ingredients=["الفطر"]), ext(), ext()))
    type_message(at, "ما أحب الفطر")
    type_message(at, "هلا")
    assert at.session_state["conversation"].constraints.excluded_ingredients == {"mushroom"}  # the earlier turn is remembered
    before = [m["text"] for m in at.session_state["messages"]]
    at.run()  # a plain rerun (for example a browser focus event) changes nothing
    assert [m["text"] for m in at.session_state["messages"]] == before and not on_welcome_screen(at)


def test_new_chat_resets_the_conversation_but_keeps_the_guest_in_the_app(launch):
    at = enter_as_guest(launch(ext(), ext()))
    type_message(at, "هلا")
    next(b for b in at.button if b.key == "new_chat").click().run()
    assert at.session_state["messages"] == [] and not on_welcome_screen(at) and len(at.chat_input) == 1
    type_message(at, "مرحبا")
    assert not on_welcome_screen(at) and len(at.session_state["messages"]) == 2


def test_a_new_browser_session_never_inherits_another_guests_conversation(launch):
    first = enter_as_guest(launch(ext(), ext()))
    type_message(first, "رسالة سرية من الضيف الأول")
    assert "رسالة سرية" in page(first)
    second = launch()  # an independent session: its own session state
    assert on_welcome_screen(second) and "guest_mode" not in second.session_state and "messages" not in second.session_state
    assert "رسالة سرية" not in page(second)
    enter_as_guest(second)
    assert second.session_state["messages"] == [] and "رسالة سرية" not in page(second)
    assert len(first.session_state["messages"]) == 2  # and the first guest is untouched


def test_a_signed_in_user_also_stays_in_the_chat_over_consecutive_messages(launch, backend):
    at = log_in(launch(*[ext() for _ in range(6)]), "a@example.com")
    for count in range(1, 4):
        type_message(at, f"رسالة {count}")
        assert not on_welcome_screen(at) and "auth" in at.session_state
        assert len(at.session_state["messages"]) == 2 * count


def test_without_accounts_configured_the_app_goes_straight_to_the_chat_and_stays_there(monkeypatch):
    from pathlib import Path

    from helpers import Scripted
    from streamlit.testing.v1 import AppTest

    monkeypatch.setattr("config.is_supabase_configured", lambda: False)
    monkeypatch.setattr("services.llm_service.any_provider_configured", lambda: True)
    monkeypatch.setattr("agent.orchestrator.default_generate", Scripted(*[ext() for _ in range(6)]))
    at = AppTest.from_file(str(Path(__file__).resolve().parent.parent / "app.py"), default_timeout=30).run()
    assert len(at.chat_input) == 1
    for count in range(1, 4):
        type_message(at, f"رسالة {count}")
        assert len(at.chat_input) == 1 and len(at.session_state["messages"]) == 2 * count
