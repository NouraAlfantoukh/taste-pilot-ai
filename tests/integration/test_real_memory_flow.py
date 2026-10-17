"""Real-LLM check of the reported mushroom conversation. Excluded from the default run; 4 small Claude calls.

    python -m pytest -m integration tests/integration/test_real_memory_flow.py -s -p no:cacheprovider
"""

import pytest

from agent.extraction import extract
from agent.orchestrator import ResponseType, handle_message
from agent.state import ConversationState
from services import taxonomy

pytestmark = pytest.mark.integration


def test_the_reported_conversation_with_a_real_model(claude):
    state = ConversationState()
    first = "ما أحب الفطر أبدًا، ولا تقترح علي أي طبق يحتوي عليه."
    ex = extract(first, state, claude)
    assert ex.allergens == (), "a dislike must not be extracted as an allergy"
    assert taxonomy.resolve_ingredient(ex.ingredients[0]) == {"mushroom"}

    handle_message(state, first, claude)
    handle_message(state, "ما عندي حساسية من الأطعمة.", claude)
    assert state.constraints.excluded_ingredients == {"mushroom"} and not state.constraints.excluded_allergens

    recall = "وش الأكلات اللي تعرف إني ما أحبها؟"
    assert extract(recall, state, claude).memory_query is True
    answer = handle_message(state, recall, claude)
    assert answer.type is ResponseType.INFO and "فطر" in answer.reply_ar
    print(f"\n    memory answer:\n{answer.reply_ar}")


@pytest.mark.parametrize(
    "message,is_memory_question",
    [
        ("ايش الأشياء اللي ما أحبها؟", True),
        ("وش تتذكر عن تفضيلاتي؟", True),
        ("هل عندك أي معلومة عن حساسيتي؟", True),
        ("What foods do I dislike?", True),
        ("رشّح لي شي بدون فطر", False),
        ("ما أحب الفطر", False),
    ],
)
def test_memory_questions_are_told_apart_from_requests_and_statements(claude, message, is_memory_question):
    state = ConversationState()
    ex = extract(message, state, claude)
    assert ex.memory_query is is_memory_question
    if is_memory_question:
        assert ex.ingredients == () and ex.allergens == () and ex.vegetarian is None
