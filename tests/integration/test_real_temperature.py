"""Real-Claude serving-temperature inference on the actual menu (opt-in, 3 small calls)."""

import pytest

from agent.orchestrator import ResponseType, handle_message
from agent.state import AllergyStatus, ConversationState
from agent.temperature import assess_temperatures
from services.menu_service import get_all_dishes

pytestmark = pytest.mark.integration
BY_ID = {d["id"]: d for d in get_all_dishes()}


def test_inference_reads_names_and_descriptions(claude):
    ids = ["dish_011", "dish_012", "dish_013", "dish_014", "dish_015", "dish_003"]
    verdicts = assess_temperatures([BY_ID[i] for i in ids], claude)
    for dish_id in ids:
        print(f"    {dish_id} {BY_ID[dish_id]['name_ar']}: {verdicts[dish_id]}")
    assert verdicts["dish_012"].temperature == "warm"  # حلوى دافئة
    assert verdicts["dish_011"].temperature == "warm"  # تقدم ساخنة
    assert verdicts["dish_014"].temperature == "warm"  # يقدم ساخنًا
    assert verdicts["dish_015"].temperature in ("cold", "room")  # with ice
    fruit = verdicts["dish_013"]
    assert not (fruit.temperature == "warm" and fruit.confidence == "high")  # fresh fruit is not warm


def test_warm_dessert_request_end_to_end(claude):
    state = ConversationState(allergy_status=AllergyStatus.NONE_REPORTED)
    print("\n[13] أبغى حلا دافي")
    response = handle_message(state, "أبغى حلا دافي", claude)
    print("    shown:", [d["id"] for d in response.recommendations])
    print("    levels:", response.serving_levels, "| notes:", response.serving_notes)
    assert response.type is ResponseType.RECOMMENDATIONS, response.reply_ar
    assert all(d["category_ar"] == "الحلويات" for d in response.recommendations)
    order = {"confirmed": 0, "likely": 1, "uncertain": 2}
    ranks = [order[level] for level in response.serving_levels]
    assert ranks == sorted(ranks), "matches must precede alternatives"
    assert response.serving_levels[0] in ("confirmed", "likely")
    assert {d["id"] for d, level in zip(response.recommendations, response.serving_levels) if level == "likely"} <= {"dish_011", "dish_012"}
