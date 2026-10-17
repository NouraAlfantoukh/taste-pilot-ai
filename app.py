"""TastePilotAI - Arabic-first conversational restaurant assistant (Streamlit UI).

The UI only renders and relays: every safety decision is made by the orchestration layer.

Accounts (when Supabase is configured): each browser session signs in with its OWN Supabase client and keeps
it, with the user's identity, in st.session_state. Identity always comes from Supabase's response to the
sign-in, never from a form field. Without Supabase configured, or as a guest, the app works as before and
saves nothing.
"""

import logging
from typing import NamedTuple

import streamlit as st

import config
from agent import orchestrator
from agent.orchestrator import GENERIC_ERROR_AR, NOT_CONFIGURED_AR, OrchestratorResponse, ResponseType
from agent.persistence import PreferenceSync, PreferencesLoadError
from agent.state import ConversationState
from db import database
from services import auth_service, llm_service
from services.memory_service import MemoryStoreError, SupabaseStore
from ui import components, styles

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("tastepilot.app")
# HTTP client libraries log every request URL at INFO. For Supabase that URL contains the signed-in user's id
# (`...user_preferences?...&user_id=eq.<uuid>`), which should not end up in the hosting logs.
for _noisy in ("httpx", "httpx2", "httpcore", "hpack"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

MAX_INPUT_CHARS = 1000

st.set_page_config(page_title="TastePilotAI", page_icon="🍽️", layout="centered", initial_sidebar_state="collapsed")
st.markdown(styles.CSS, unsafe_allow_html=True)

session = st.session_state


class Outcome(NamedTuple):
    signed_in: bool
    message: str = ""
    warn: bool = True


def _render(html: str) -> None:
    st.markdown(html, unsafe_allow_html=True)


# ---- accounts ----

def _begin_signed_in_session(client, user: auth_service.AuthenticatedUser) -> None:
    """Replace the whole session state with this user's: nothing from a previous visitor survives."""
    sync = PreferenceSync(SupabaseStore(client, user.user_id), user.user_id)
    notice = ""
    try:
        sync.load()
        conversation = sync.new_state()
    except PreferencesLoadError:
        sync.disable()  # never overwrite a stored profile we could not read
        conversation, notice = ConversationState(profile_unavailable=True), components.PROFILE_UNREADABLE_AR
    except MemoryStoreError:
        sync.disable()
        conversation, notice = ConversationState(profile_unavailable=True), components.PROFILE_UNAVAILABLE_AR
    session.clear()
    session.update(
        {"auth": {"client": client, "user": user}, "sync": sync, "conversation": conversation, "messages": [], "awaiting": False}
    )
    if notice:
        session["notice"] = notice


def _new_client():
    """A fresh client per attempt and per session, never shared. None if unusable (not configured, or a privileged key)."""
    try:
        return database.create_supabase_client()
    except database.PrivilegedKeyError:
        return None


def _sign_in(email: str, password: str) -> Outcome:
    client = _new_client()
    if client is None:
        return Outcome(False, components.CONFIG_ERROR_AR)
    try:
        user = auth_service.sign_in(client, email, password)
    except auth_service.AuthError as exc:
        return Outcome(False, components.AUTH_MESSAGES_AR[exc.reason])
    _begin_signed_in_session(client, user)
    return Outcome(True)


def _sign_up(email: str, password: str) -> Outcome:
    client = _new_client()
    if client is None:
        return Outcome(False, components.CONFIG_ERROR_AR)
    try:
        result = auth_service.sign_up(client, email, password)
    except auth_service.AuthError as exc:
        return Outcome(False, components.AUTH_MESSAGES_AR[exc.reason])
    if result.user is None:
        return Outcome(False, components.SIGNUP_CONFIRM_AR, warn=False)
    _begin_signed_in_session(client, result.user)
    return Outcome(True)


def _continue_as_guest() -> None:
    # The flag must NOT share its name with a widget key: the guest button's key is "guest", and Streamlit gives a
    # widget's key to the widget (its value is False on every run after the click) and deletes it when the widget is
    # not rendered, which silently ended guest mode on the next message.
    session["guest_mode"] = True


def _sign_out() -> None:
    auth = session.get("auth")
    if auth:
        auth_service.sign_out(auth["client"])
    session.clear()


def _expire_session() -> None:
    session.clear()
    session["auth_notice"] = components.SESSION_EXPIRED_AR


def _auth_screen() -> None:
    _render(components.brand_header_html())
    notice = session.pop("auth_notice", None)
    if notice:
        _render(components.notice_html(notice, warn=True))
    _render(components.auth_intro_html())
    sign_in_tab, sign_up_tab = st.tabs(["تسجيل الدخول", "حساب جديد"])
    outcome = None
    with sign_in_tab:
        with st.form("sign_in_form", clear_on_submit=True):
            email = st.text_input("البريد الإلكتروني", key="sign_in_email")
            password = st.text_input("كلمة المرور", type="password", key="sign_in_password")
            if st.form_submit_button("دخول", use_container_width=True):
                outcome = _sign_in(email, password)
    with sign_up_tab:
        with st.form("sign_up_form", clear_on_submit=True):
            email = st.text_input("البريد الإلكتروني", key="sign_up_email")
            password = st.text_input("كلمة المرور (٨ أحرف على الأقل)", type="password", key="sign_up_password")
            if st.form_submit_button("إنشاء حساب", use_container_width=True):
                outcome = _sign_up(email, password)
    if outcome is not None:
        if outcome.signed_in:
            st.rerun()
        _render(components.notice_html(outcome.message, warn=outcome.warn))
    st.button("المتابعة كضيف (بدون حفظ)", on_click=_continue_as_guest, use_container_width=True, key="guest")


# ---- conversation ----

def _init_session() -> None:
    session.setdefault("conversation", ConversationState())
    session.setdefault("messages", [])
    session.setdefault("awaiting", False)


def _reset_conversation() -> None:
    for key in ("messages", "awaiting", "queued_prompt"):
        session.pop(key, None)
    sync = session.get("sync")
    # A new conversation keeps the durable profile and drops every per-request constraint.
    conversation = sync.new_state() if sync else ConversationState()
    conversation.profile_unavailable = bool(sync and not sync.enabled)
    session["conversation"] = conversation


def _queue_prompt(text: str) -> None:
    session["queued_prompt"] = text


def _answer(user_text: str) -> OrchestratorResponse:
    try:
        return orchestrator.handle_message(session.conversation, user_text, generate=orchestrator.default_generate)
    except Exception as exc:  # the UI must never show a stack trace
        logger.error("Unexpected failure while answering: %s", type(exc).__name__)
        return OrchestratorResponse(ResponseType.ERROR, GENERIC_ERROR_AR)


def _persist() -> None:
    """Save the confirmed durable preferences. Failures never break the conversation."""
    sync = session.get("sync")
    if sync is None:
        return
    try:
        sync.save(session.conversation)
        if sync.enabled:
            session.pop("notice", None)  # a previous "could not save" warning no longer applies
    except MemoryStoreError as exc:
        if exc.kind == "session_expired":
            _expire_session()
        else:
            session["notice"] = components.SAVE_FAILED_AR


persistence_configured = config.is_supabase_configured()
if persistence_configured and "auth" not in session and not session.get("guest_mode"):
    _auth_screen()
    st.stop()

_init_session()
configured = llm_service.any_provider_configured()
auth = session.get("auth")
sync = session.get("sync")

if auth:
    brand_col, new_col, out_col = st.columns([3, 1, 1], vertical_alignment="center")
else:
    brand_col, new_col = st.columns([4, 1], vertical_alignment="center")
    out_col = None
with brand_col:
    _render(components.brand_header_html())
    if auth:
        _render(components.account_html(auth["user"].email))
with new_col:
    st.button("محادثة جديدة", on_click=_reset_conversation, use_container_width=True, key="new_chat")
if out_col is not None:
    with out_col:
        st.button("تسجيل الخروج", on_click=_sign_out, use_container_width=True, key="sign_out")

if not configured:
    _render(components.notice_html(NOT_CONFIGURED_AR + " أضف مفاتيح الخدمة في ملف الإعدادات ثم أعد تشغيل التطبيق.", warn=True))
if session.get("notice"):
    _render(components.notice_html(session["notice"], warn=True))
if persistence_configured and not auth:
    _render(components.notice_html(components.GUEST_NOTE_AR))

_render(components.preferences_html(session.conversation))

if auth and sync is not None:
    with st.expander(components.PROFILE_TITLE_AR):
        _render(
            components.profile_html(
                sync.saved,
                unsaved=sync.enabled and sync.has_unsaved_changes(session.conversation),
                save_failed=session.get("notice") == components.SAVE_FAILED_AR,
                unavailable=not sync.enabled,
            )
        )

if not session.messages:
    if sync is not None and sync.had_saved_preferences and sync.enabled:
        _render(components.notice_html(components.WELCOME_BACK_AR))
    _render(components.hero_html())
    _render('<div class="tp-root tp-suggest-label">جرّب مثلًا:</div>')
    for column, suggestion in zip(st.columns(len(components.SUGGESTIONS)), components.SUGGESTIONS):
        with column:
            st.button(suggestion, on_click=_queue_prompt, args=(suggestion,), use_container_width=True, disabled=not configured)

for message in session.messages:
    if message["role"] == "user":
        _render(components.user_message_html(message["text"]))
    else:
        _render(components.assistant_message_html(message["response"]))

typed = st.chat_input("اكتب رسالتك هنا...", max_chars=MAX_INPUT_CHARS, disabled=(not configured) or session.awaiting)
new_text = typed or session.pop("queued_prompt", None)
if new_text and configured and not session.awaiting:
    session.messages.append({"role": "user", "text": new_text.strip()})
    session.awaiting = True
    st.rerun()

if session.awaiting:
    typing = st.empty()
    typing.markdown(components.typing_html(), unsafe_allow_html=True)
    response = _answer(session.messages[-1]["text"])
    session.messages.append({"role": "assistant", "text": response.reply_ar, "response": response})
    session.awaiting = False
    _persist()
    st.rerun()

if session.messages:
    _render(
        components.footer_html(
            persistent=bool(sync is not None and sync.enabled),
            save_failed=session.get("notice") == components.SAVE_FAILED_AR,
        )
    )
