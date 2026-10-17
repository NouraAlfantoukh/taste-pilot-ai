"""Conversation memory against a REAL Supabase project (opt-in; not run by default). The LLM is scripted: no API cost.

Uses account C for the conversation flows and B as the "other user". Every test only deletes and writes the
`user_preferences` row of the test account that is signed in (RLS enforces that too); auth users are never touched.

    python -m pytest -m integration tests/integration/test_real_conversation_memory.py -v -p no:cacheprovider
"""

import sys
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # shared helpers live in tests/

from helpers import Scripted, ext, picks_for  # noqa: E402

from agent.orchestrator import ResponseType, handle_message  # noqa: E402
from agent.persistence import PreferenceSync  # noqa: E402
from agent.prompts import SYSTEM_PROMPT_AR  # noqa: E402
from config import get_setting  # noqa: E402
from db.database import create_supabase_client, is_privileged_key  # noqa: E402
from services import auth_service  # noqa: E402
from services.memory_service import MemoryStoreError, SupabaseStore  # noqa: E402

pytestmark = pytest.mark.integration
TABLE = "user_preferences"
REQUIRED = ("SUPABASE_URL", "SUPABASE_KEY") + tuple(f"SUPABASE_TEST_USER_{w}_{f}" for w in "BC" for f in ("EMAIL", "PASSWORD"))
APP = str(Path(__file__).resolve().parent.parent.parent / "app.py")
RECALL = "وش الأكلات اللي تعرف إني ما أحبها؟"


class Lenient(Scripted):
    """Scripted replies; a recommendation call past the script gets no picks (deterministic fallback)."""

    def __call__(self, system_prompt, user_message):
        if system_prompt == SYSTEM_PROMPT_AR and not self.replies:
            return '{"picks": []}'
        return super().__call__(system_prompt, user_message)


@pytest.fixture(scope="module")
def settings():
    missing = [name for name in REQUIRED if not get_setting(name)]
    if missing:
        pytest.skip("Real Supabase test settings are not configured: " + ", ".join(missing))
    if is_privileged_key(get_setting("SUPABASE_KEY")):
        pytest.fail("SUPABASE_KEY is a privileged key; it would bypass RLS and make this test meaningless")
    return {name: get_setting(name) for name in REQUIRED}


def credentials(settings, who):
    return settings[f"SUPABASE_TEST_USER_{who}_EMAIL"], settings[f"SUPABASE_TEST_USER_{who}_PASSWORD"]


def visit(settings, who):
    """A brand-new browser visit: new client, signed in, profile loaded the way the app does it."""
    client = create_supabase_client()
    user = auth_service.sign_in(client, *credentials(settings, who))
    sync = PreferenceSync(SupabaseStore(client, user.user_id), user.user_id)
    sync.load()
    return client, sync, sync.new_state(), user.user_id


def wipe(settings, who):
    client = create_supabase_client()
    user = auth_service.sign_in(client, *credentials(settings, who))
    client.table(TABLE).delete().eq("user_id", user.user_id).execute()  # own row only
    auth_service.sign_out(client)


@pytest.fixture(autouse=True)
def clean(settings):
    for who in "BC":
        wipe(settings, who)
    yield
    for who in "BC":
        wipe(settings, who)


def chat(sync, state, text, *replies):
    llm = Lenient(*replies)
    response = handle_message(state, text, llm)
    sync.save(state)
    return response


def stored_row(client, user_id):
    return client.table(TABLE).select("*").eq("user_id", user_id).execute().data


# ---- the reported conversation, across real sessions ----

def test_the_mushroom_dislike_is_saved_and_recalled_in_a_fresh_real_session(settings):
    _, sync, state, user_id = visit(settings, "C")
    chat(sync, state, "ما أحب الفطر أبدًا، ولا تقترح علي أي طبق يحتوي عليه.", ext(ingredients=["الفطر"], wants_recommendations=True))
    chat(sync, state, "ما عندي حساسية من الأطعمة.", ext(allergy_answer="none", wants_recommendations=True), picks_for("dish_004"))

    client, sync2, state2, same_id = visit(settings, "C")  # a different client and session
    assert same_id == user_id
    assert state2.constraints.excluded_ingredients == {"mushroom"} and not state2.constraints.excluded_allergens
    answer = chat(sync2, state2, RECALL, ext(memory_query=True))
    assert answer.type is ResponseType.INFO and "فطر" in answer.reply_ar and "حساسية مسجّلة" not in answer.reply_ar
    row = stored_row(client, user_id)[0]
    assert row["excluded_ingredients"] == ["mushroom"] and row["allergy_status"] == "none_reported"


def test_updating_a_preference_overwrites_the_real_row_and_the_next_session_sees_the_latest(settings):
    _, sync, state, user_id = visit(settings, "C")
    chat(sync, state, "هلا", ext())
    chat(sync, state, "ما عندي حساسية", ext(allergy_answer="none"))
    chat(sync, state, "ما أحب الجبن", ext(ingredients=["الجبن"]))
    chat(sync, state, "ما أبي حار", ext(max_spice_level="none"))
    chat(sync, state, "غيرت رأيي، أحب الجبن وأبي حار", ext(remove_ingredients=["الجبن"], max_spice_level=None, min_spice_level="mild"))
    client, _, state2, _ = visit(settings, "C")
    c = state2.constraints
    assert c.excluded_ingredients == frozenset() and c.min_spice_level == "mild" and c.max_spice_level is None
    assert len(stored_row(client, user_id)) == 1  # still exactly one record


def test_a_budget_for_one_request_is_never_written_to_the_real_row(settings):
    _, sync, state, user_id = visit(settings, "C")
    chat(sync, state, "هلا", ext())
    chat(sync, state, "ما عندي حساسية", ext(allergy_answer="none"))
    chat(sync, state, "أبي شي بأقل من ٢٠ ريال", ext(max_price_sar=20, wants_recommendations=True))
    client, _, state2, _ = visit(settings, "C")
    assert state2.constraints.max_price_sar is None
    assert "max_price_sar" not in stored_row(client, user_id)[0]


# ---- isolation and switching accounts ----

def test_user_b_never_sees_or_changes_user_cs_memory(settings):
    _, sync_c, state_c, id_c = visit(settings, "C")
    chat(sync_c, state_c, "ما أحب الفطر", ext(ingredients=["الفطر"]))
    client_b, sync_b, state_b, id_b = visit(settings, "B")
    assert id_b != id_c and state_b.constraints.excluded_ingredients == frozenset()
    answer = chat(sync_b, state_b, RECALL, ext(memory_query=True))
    assert "فطر" not in answer.reply_ar
    chat(sync_b, state_b, "ما أحب العدس", ext(ingredients=["العدس"]))
    client_c, _, state_c2, _ = visit(settings, "C")
    assert state_c2.constraints.excluded_ingredients == {"mushroom"}  # untouched by B's write
    assert [r["user_id"] for r in stored_row(client_b, id_b)] == [id_b]


def test_a_store_forged_with_another_users_id_is_refused_by_the_database(settings):
    _, _, _, id_c = visit(settings, "C")
    client_b, _, _, _ = visit(settings, "B")
    forged = SupabaseStore(client_b, id_c)  # B's session, C's id: the app-side guard is bypassed on purpose
    with pytest.raises(MemoryStoreError) as error:
        forged.update_preferences(id_c, excluded_ingredients=["mushroom"])
    assert error.value.kind == "forbidden"
    assert forged.get_preferences(id_c).excluded_ingredients == []  # RLS shows B nothing of C's


def test_after_sign_out_the_old_client_can_no_longer_read_or_write(settings):
    client, sync, state, user_id = visit(settings, "C")
    chat(sync, state, "ما أحب الفطر", ext(ingredients=["الفطر"]))
    auth_service.sign_out(client)
    store = SupabaseStore(client, user_id)
    with pytest.raises(MemoryStoreError):
        store.get_preferences(user_id)
    with pytest.raises(MemoryStoreError):
        store.update_preferences(user_id, excluded_ingredients=["onion"])
    fresh, _, state2, _ = visit(settings, "C")
    assert state2.constraints.excluded_ingredients == {"mushroom"}  # the failed write changed nothing


def test_a_tampered_stored_row_is_refused_not_applied(settings):
    client, _, _, user_id = visit(settings, "C")
    # The database accepts any well-shaped id; the application must refuse one its taxonomy does not know.
    client.table(TABLE).upsert({"user_id": user_id, "excluded_allergens": ["unobtainium"], "allergy_status": "reported"}).execute()
    sync = PreferenceSync(SupabaseStore(client, user_id), user_id)
    with pytest.raises(Exception) as error:
        sync.load()
    assert "unobtainium" not in str(error.value)
    assert stored_row(client, user_id)[0]["excluded_allergens"] == ["unobtainium"]  # never "repaired" by overwriting


# ---- through the real app UI: switching accounts in one browser tab ----

def page(at):
    return "".join(m.value for m in at.markdown)


def ui_sign_in(at, settings, who):
    email, password = credentials(settings, who)
    at.text_input(key="sign_in_email").input(email)
    at.text_input(key="sign_in_password").input(password)
    next(b for b in at.button if b.label == "دخول").click().run()
    return at


def test_switching_accounts_in_the_app_never_shows_the_previous_users_memory(settings, monkeypatch):
    monkeypatch.setattr("services.llm_service.any_provider_configured", lambda: True)
    monkeypatch.setattr(
        "agent.orchestrator.default_generate",
        Lenient(ext(ingredients=["الفطر"]), ext(allergy_answer="none"), ext(memory_query=True), ext(memory_query=True)),
    )
    at = AppTest.from_file(APP, default_timeout=60).run()
    ui_sign_in(at, settings, "C")
    assert not at.exception and len(at.chat_input) == 1
    at.chat_input[0].set_value("ما أحب الفطر").run()
    at.chat_input[0].set_value("ما عندي حساسية").run()
    assert "بدون فطر" in page(at)

    next(b for b in at.button if b.key == "sign_out").click().run()
    assert len(at.chat_input) == 0 and "بدون فطر" not in page(at) and "conversation" not in at.session_state and "auth" not in at.session_state

    ui_sign_in(at, settings, "B")
    assert not at.exception and "بدون فطر" not in page(at)
    at.chat_input[0].set_value(RECALL).run()
    assert "فطر" not in page(at)
