import json

import pytest

from agent.guard import MAX_RECOMMENDATIONS, RECOMMENDATION_INTRO_AR, render_recommendations
from services.menu_service import format_price_sar


def picks(*items):
    """items: dish ids, or (dish_id, reason) tuples."""
    out = []
    for item in items:
        dish_id, reason = item if isinstance(item, tuple) else (item, "مناسب لطلبك")
        out.append({"dish_id": dish_id, "reason_ar": reason})
    return json.dumps({"picks": out}, ensure_ascii=False)


@pytest.fixture
def menu(make_dish):
    eligible = [
        make_dish(id="a", name_ar="كبسة اختبار", price_sar=30),
        make_dish(id="b", name_ar="برياني اختبار", price_sar=20),
    ]
    ineligible = [make_dish(id="bad", name_ar="طبق الحليب الممنوع", price_sar=15, allergens=["milk"])]
    return eligible, ineligible


def render(menu, raw):
    eligible, ineligible = menu
    rendered = render_recommendations(eligible, raw, eligible + ineligible)
    return rendered.text, rendered.dishes


def test_eligible_picks_are_rendered_from_the_validated_objects(menu):
    text, shown = render(menu, picks("b", "a"))
    assert text.startswith(RECOMMENDATION_INTRO_AR)
    assert [d["id"] for d in shown] == ["b", "a"]
    assert f"برياني اختبار — {format_price_sar(20)}" in text and f"كبسة اختبار — {format_price_sar(30)}" in text
    assert "مناسب لطلبك" in text


def test_ineligible_and_unknown_picks_are_dropped(menu):
    text, shown = render(menu, picks("bad", "ghost_dish", "a"))
    assert [d["id"] for d in shown] == ["a"]
    assert "الممنوع" not in text and "ghost" not in text


def test_when_every_pick_is_unsupported_it_falls_back_to_eligible_dishes_without_llm_prose(menu):
    text, shown = render(menu, picks(("bad", "أنصحك بهذا الطبق"), "ghost"))
    assert {d["id"] for d in shown} == {"a", "b"}
    assert "الممنوع" not in text and "أنصحك" not in text


@pytest.mark.parametrize("raw", ["مرحبا، أنصحك بطبق الحليب الممنوع", "", "[]", '{"picks": "a"}', '{"picks": [1, 2]}', None, 5])
def test_malformed_output_never_shows_free_text_and_falls_back(menu, raw):
    text, shown = render(menu, raw)
    assert "الممنوع" not in text and "أنصحك" not in text
    assert {d["id"] for d in shown} == {"a", "b"}


def test_prices_cannot_be_invented_or_altered(menu):
    raw = json.dumps(
        {"picks": [{"dish_id": "a", "reason_ar": "السعر ٥ ريال فقط", "price_sar": 1, "name_ar": "طبق مزيف"}]},
        ensure_ascii=False,
    )
    text, shown = render(menu, raw)
    assert format_price_sar(30) in text and "٥ ريال" not in text and "طبق مزيف" not in text
    assert shown[0]["price_sar"] == 30 and shown[0]["name_ar"] == "كبسة اختبار"


@pytest.mark.parametrize(
    "reason",
    [
        "يناسب ميزانيتك بسعر 12",
        "٢٠ ر.س فقط",
        "خالٍ من الحليب",
        "لا يحتوي على حليب",
        "هذا الطبق آمن تمامًا",
        "ولا تتضمن مكوناته المسجلة أي فطر",  # worded like a real model reply seen in the integration run
        "لا يتضمن أي منتجات ألبان",
        "ما تشمل وصفته مكسرات",
        "لا تحوي الوصفة غلوتين",
        "خالية من البيض",
        "جرّب أيضًا طبق الحليب الممنوع",
        "مثل dish bad",
        "x" * 200,
    ],
)
def test_unsafe_reasons_are_dropped_but_the_valid_dish_is_still_shown(menu, reason):
    text, shown = render(menu, picks(("a", reason)))
    assert [d["id"] for d in shown] == ["a"] and reason not in text


def test_legitimate_arabic_prose_with_generic_food_words_is_kept(menu):
    reason = "طبق أرز وخضار خفيف يناسب من يفضّل الأكل المتوازن"
    text, _ = render(menu, picks(("a", reason)))
    assert reason in text


def test_duplicates_are_shown_once_and_the_list_is_capped(make_dish):
    dishes = [make_dish(id=f"d{i}", name_ar=f"طبق رقم {i}x") for i in range(8)]
    rendered = render_recommendations(dishes, picks("d0", "d0", *[f"d{i}" for i in range(1, 8)]), dishes)
    shown = rendered.dishes
    assert [d["id"] for d in shown][:2] == ["d0", "d1"] and len(shown) == MAX_RECOMMENDATIONS


def test_fenced_json_is_accepted(menu):
    _, shown = render(menu, "```json\n" + picks("a") + "\n```")
    assert [d["id"] for d in shown] == ["a"]
