"""The reported conversation (dessert, then spicy-medium, then savory + spicy) with real Claude (opt-in, ~7 calls)."""

import pytest

from agent.orchestrator import ResponseType, handle_message
from agent.state import ConversationState
from services.menu_service import get_all_dishes

pytestmark = pytest.mark.integration
MENU = get_all_dishes()
DESSERTS = {d["id"] for d in MENU if d["category_ar"] == "الحلويات"}
SAVORY = {d["id"] for d in MENU if d["category_ar"] in ("الأطباق الرئيسية", "المقبلات")}
RANK = {"none": 0, "mild": 1, "medium": 2, "hot": 3}


def test_dessert_then_spicy_medium_then_savory_and_spicy(claude):
    state = ConversationState()
    for message in ("أبغى حلا", "لا"):
        print(f"\n[14] {message}")
        response = handle_message(state, message, claude)
    assert {d["id"] for d in response.recommendations} <= DESSERTS and response.recommendations

    print("\n[15] طيب ابي اكل سبايسي وسط")
    response = handle_message(state, "طيب ابي اكل سبايسي وسط", claude)
    print("    shown:", [(d["id"], d["spice_level"]) for d in response.recommendations])
    assert response.type is ResponseType.RECOMMENDATIONS, response.reply_ar
    assert not {d["id"] for d in response.recommendations} & DESSERTS
    assert all(RANK[d["spice_level"]] >= 2 for d in response.recommendations)
    assert state.constraints.min_spice_level == "medium" and state.constraints.serving_temperature is None

    print("\n[16] لالا ابي اكل يعني مالح وسبايسي")
    response = handle_message(state, "لالا ابي اكل يعني مالح وسبايسي", claude)
    print("    shown:", [(d["id"], d["category_ar"], d["spice_level"]) for d in response.recommendations])
    assert response.type is ResponseType.RECOMMENDATIONS, response.reply_ar  # no "not a category" question
    assert {d["id"] for d in response.recommendations} <= SAVORY
    assert all(RANK[d["spice_level"]] >= 1 for d in response.recommendations)
