from helpers import unverified_menu
import json

from agent.orchestrator import (
    NO_MATCHING_DISH_MESSAGE_AR,
    NO_VERIFIED_OPTION_MESSAGE_AR,
    compose_reply,
)
from agent.prompts import SYSTEM_PROMPT_AR, build_menu_context
from services.constraints import Constraints, build_constraints
from services.menu_service import get_all_dishes
from services.recommendation_service import recommend


class SpyLLM:
    def __init__(self, reply="{}"):
        self.reply = reply
        self.calls: list[tuple[str, str]] = []

    def __call__(self, system_prompt, user_message):
        self.calls.append((system_prompt, user_message))
        return self.reply


def picks(*dish_ids):
    return json.dumps({"picks": [{"dish_id": d, "reason_ar": "مناسب لطلبك"} for d in dish_ids]}, ensure_ascii=False)


def test_no_eligible_dishes_returns_arabic_message_without_calling_llm(incomplete_dish):
    result = recommend([incomplete_dish], build_constraints(allergens=["حليب"]))
    spy = SpyLLM()
    composed = compose_reply(result, "أبغى شيء بدون حليب", spy)
    assert result.eligible == []
    assert composed.text == NO_VERIFIED_OPTION_MESSAGE_AR and composed.dishes == ()
    assert spy.calls == []


def test_no_verified_option_message_is_arabic_and_points_to_staff():
    assert any("؀" <= ch <= "ۿ" for ch in NO_VERIFIED_OPTION_MESSAGE_AR)
    assert "موظف" in NO_VERIFIED_OPTION_MESSAGE_AR


def test_incomplete_dish_never_reaches_the_llm_prompt_as_an_eligible_option(make_dish, incomplete_dish):
    clean = make_dish(id="clean_verified", name_ar="طبق موثق للاختبار")
    menu = [incomplete_dish, clean]
    result = recommend(menu, build_constraints(allergens=["حليب"]))
    spy = SpyLLM(picks("clean_verified"))
    composed = compose_reply(result, "ابغى شيء بدون حليب", spy, menu)
    assert len(spy.calls) == 1
    system_prompt, user_message = spy.calls[0]
    sent = system_prompt + user_message
    assert clean["name_ar"] in sent
    assert incomplete_dish["name_ar"] not in sent
    assert incomplete_dish["id"] not in sent
    assert clean["name_ar"] in composed.text and [d["id"] for d in composed.dishes] == ["clean_verified"]


def test_whole_unreviewed_menu_with_allergy_never_calls_llm():
    result = recommend(unverified_menu(), build_constraints(allergens=["الحليب"]))
    spy = SpyLLM()
    assert compose_reply(result, "أبغى حلى", spy).text == NO_VERIFIED_OPTION_MESSAGE_AR
    assert spy.calls == []


def test_empty_result_without_safety_reason_uses_generic_message():
    result = recommend(get_all_dishes(), Constraints(max_price_sar=1))
    spy = SpyLLM()
    assert compose_reply(result, "رخيص", spy).text == NO_MATCHING_DISH_MESSAGE_AR
    assert spy.calls == []


def test_menu_context_lists_only_the_dishes_it_is_given(make_dish):
    context = build_menu_context([make_dish(id="x", name_ar="طبق س")])
    assert "طبق س" in context and "حالة بيانات الحساسية: موثقة" in context


def test_menu_context_marks_unverified_data(incomplete_dish):
    assert "حالة بيانات الحساسية: غير موثقة" in build_menu_context([incomplete_dish])


def test_system_prompt_forbids_claiming_absolute_allergy_safety():
    assert "آمن تمامًا" in SYSTEM_PROMPT_AR
