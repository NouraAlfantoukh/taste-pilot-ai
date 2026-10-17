"""The manually observed conversation, against REAL services (opt-in; not run by default).

Part 1 uses a scripted LLM and real Supabase (accounts B and C; only their own `user_preferences` rows are touched).
Part 2 sends three short extractions to real Claude (a few cents).

    python -m pytest -m integration tests/integration/test_real_sprint_scenario.py -v -p no:cacheprovider
"""

import pytest
from helpers import ext, picks_for
from test_real_conversation_memory import RECALL, chat, clean, settings, stored_row, visit  # noqa: F401

from agent.extraction import extract
from agent.orchestrator import ResponseType
from agent.state import ConversationState
from services import taxonomy

pytestmark = pytest.mark.integration


def test_the_manual_conversation_with_real_supabase(settings):
    _, sync, state, user_id = visit(settings, "C")
    chat(sync, state, "ما أحب الفطر أبدًا، ولا تقترح علي أي طبق يحتوي عليه.", ext(ingredients=["الفطر"], wants_recommendations=True))
    chat(sync, state, "ما عندي حساسية من الأطعمة.", ext(allergy_answer="none", wants_recommendations=True), picks_for("dish_010", "dish_004"))

    client, sync2, state2, _ = visit(settings, "C")  # a new conversation in a new session
    mushrooms = chat(sync2, state2, "اقترح لي اطباق فيها فطر", ext(requested_ingredients=["فطر"], wants_recommendations=True))
    assert mushrooms.type is ResponseType.NO_RESULTS and not mushrooms.recommendations
    assert "ما لقيت في القائمة طبقًا فيه فطر" in mushrooms.reply_ar
    recall = chat(sync2, state2, RECALL, ext(memory_query=True))
    assert recall.type is ResponseType.INFO and "فطر" in recall.reply_ar

    chat(sync2, state2, "معليش ترا عندي حساسيه بس ماقلت لك اول مره", ext(unclear_allergy=True))
    row = stored_row(client, user_id)[0]
    assert row["allergy_status"] == "not_asked" and row["excluded_ingredients"] == ["mushroom"]  # stale "none" replaced
    offered = chat(sync2, state2, "الحليب", ext(allergens=["الحليب"], wants_recommendations=True), picks_for("dish_002", "dish_014", "dish_001", "dish_010"))
    assert offered.type is ResponseType.RECOMMENDATIONS and offered.recommendations  # reviewed dishes without milk
    assert all("milk" not in d["allergens"] and "mushroom" not in d["ingredient_ids"] for d in offered.recommendations)

    client3, _, state3, _ = visit(settings, "C")  # fresh session
    assert state3.constraints.excluded_ingredients == {"mushroom"} and state3.constraints.excluded_allergens == {"milk"}
    final = stored_row(client3, user_id)[0]
    assert (final["excluded_allergens"], final["excluded_ingredients"], final["allergy_status"]) == (["milk"], ["mushroom"], "reported")

    _, _, state_b, _ = visit(settings, "B")  # another user sees none of it
    assert state_b.constraints.excluded_ingredients == frozenset() and not state_b.constraints.excluded_allergens


@pytest.mark.parametrize(
    "message,field",
    [
        ("اقترح لي اطباق فيها فطر", "requested"),
        ("معليش ترا عندي حساسيه بس ماقلت لك اول مره", "unclear_allergy"),
        ("ما أحب الفطر أبدًا، ولا تقترح علي أي طبق يحتوي عليه.", "dislike"),
    ],
)
def test_real_claude_reads_the_three_key_sentences(claude, message, field):
    ex = extract(message, ConversationState(), claude)
    if field == "requested":
        assert taxonomy.resolve_ingredient(ex.requested_ingredients[0]) == {"mushroom"} and ex.ingredients == ()
    elif field == "unclear_allergy":
        assert ex.unclear_allergy is True or ex.allergy_answer == "has"
        assert ex.allergens == ()
    else:
        assert ex.requested_ingredients == () and taxonomy.resolve_ingredient(ex.ingredients[0]) == {"mushroom"}


# ---- allergy clarification loops (real Claude for the wording, real Supabase for the persisted result) ----

def test_the_milk_and_cream_conversation_with_real_claude_ends_without_an_open_question(claude):
    from agent.orchestrator import handle_message
    from agent.state import AllergyStatus

    state = ConversationState(allergy_status=AllergyStatus.NONE_REPORTED)
    handle_message(state, "عندي حساسية حليب ومشتقاته، بس أغلب شيء الحليب والكريمة", claude)
    if state.pending is not None:  # whatever the model left open, "حليب" then "خلاص بس حليب" must close it
        handle_message(state, "حليب", claude)
    if state.pending is not None:
        handle_message(state, "خلاص بس حليب", claude)
    assert state.pending is None, state.pending
    assert state.constraints.excluded_allergens == {"milk"} and not state.constraints.allergy_ingredients


def test_the_pistachio_conversation_with_real_claude_ends_with_tree_nuts_only(claude):
    from agent.orchestrator import handle_message
    from agent.state import AllergyStatus

    state = ConversationState()
    handle_message(state, "هلا", claude)  # the assistant asks about allergies; the guest answers with the bare phrase
    assert state.allergy_status is AllergyStatus.AWAITING_ANSWER
    handle_message(state, "فستق بستاشيو", claude)
    if state.pending is not None:
        handle_message(state, "المكسرات الشجرية", claude)
    assert state.pending is None, state.pending
    assert state.constraints.excluded_allergens == {"tree_nuts"}


def test_a_settled_allergy_conversation_persists_on_real_supabase(settings):
    _, sync, state, user_id = visit(settings, "C")
    chat(sync, state, "هلا", ext())
    chat(sync, state, "ما عندي حساسية", ext(allergy_answer="none"))
    chat(sync, state, "حساسية حليب وكاسترد", ext(allergens=["حليب", "كاسترد"]))
    assert state.pending is not None
    chat(sync, state, "خلاص بس حليب", ext(allergens=["حليب"], settles_pending=True))
    assert state.pending is None
    client, _, state2, _ = visit(settings, "C")
    row = stored_row(client, user_id)[0]
    assert (row["excluded_allergens"], row["allergy_ingredients"], row["allergy_status"]) == (["milk"], [], "reported")
    assert state2.pending is None and state2.constraints.excluded_allergens == {"milk"}
