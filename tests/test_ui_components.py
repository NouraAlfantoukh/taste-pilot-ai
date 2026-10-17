import re

import pytest

from agent.orchestrator import OrchestratorResponse, ResponseType
from agent.state import AllergyStatus, ConversationState
from services.constraints import Constraints
from services.menu_service import format_price_sar, get_all_dishes
from ui import components


def recommendation_response(dishes, reasons=(), lead="رشّحت لك:", notes=""):
    return OrchestratorResponse(ResponseType.RECOMMENDATIONS, "x", tuple(dishes), tuple(reasons), lead, notes)


def test_card_shows_validated_dish_data():
    dish = get_all_dishes()[0]
    html = components.dish_card_html(dish)
    assert dish["name_ar"] in html and format_price_sar(dish["price_sar"]) in html
    assert dish["description_ar"] in html and dish["ingredients_ar"][0] in html


def test_card_never_leaks_internal_ids_or_codes(make_dish):
    html = components.dish_card_html(make_dish(id="dish_secret_123", allergens=["milk"]))
    for leak in ("dish_secret_123", "ingredient_ids", "milk", "unverified", "verified\"", "tree_nuts"):
        assert leak not in html
    assert "الحليب" in html  # recorded allergens are shown with Arabic names


def test_everything_interpolated_is_escaped(make_dish):
    nasty = '<script>alert(1)</script><img src=x onerror=alert(2)>'
    dish = make_dish(name_ar=nasty, description_ar=nasty)
    html = components.recommendations_html(recommendation_response([dish], reasons=[nasty], lead=nasty, notes=nasty))
    html += components.user_message_html(nasty) + components.notice_html(nasty)
    assert "<script" not in html and "<img" not in html
    assert "&lt;script&gt;" in html


def test_the_source_of_allergen_data_is_stated_naturally_without_development_wording(make_dish):
    unverified = components.dish_card_html(make_dish(allergen_data_status="unverified"))
    assert "مسببات الحساسية" in unverified and "حسب بيانات المطعم" in unverified
    assert not any(word in unverified for word in ("غير مُراجَعة", "تجريبية", "خيالية", "غير مكتملة"))
    assert "موثّقة" in components.dish_card_html(make_dish(allergen_data_status="verified"))


def test_the_page_footer_has_no_development_disclaimer():
    footer = components.footer_html()
    assert "حساسية" in footer and "موظف" in footer
    assert not any(word in footer + components.hero_html() for word in ("تجريبية", "خيالية", "غير مُراجَعة", "غير مكتملة"))


@pytest.mark.parametrize("level,filled", [("none", 0), ("mild", 1), ("medium", 2), ("hot", 3)])
def test_spice_indicator(level, filled):
    html = components.spice_html(level)
    assert len(re.findall(r'<i class="on">', html)) == filled
    assert len(re.findall(r"<i ", html)) == 3


def test_reasons_are_aligned_with_dishes(make_dish):
    dishes = [make_dish(id="a", name_ar="طبق أ"), make_dish(id="b", name_ar="طبق ب")]
    html = components.recommendations_html(recommendation_response(dishes, reasons=["سبب أ"]))
    assert html.count("tp-reason") == 1 and html.index("سبب أ") < html.index("طبق ب")


def test_html_has_no_blank_lines_that_would_break_markdown(make_dish):
    html = components.recommendations_html(recommendation_response([make_dish()], notes="ملاحظة"))
    assert "\n" not in html


@pytest.mark.parametrize(
    "kind,marker",
    [
        (ResponseType.CLARIFICATION, "tp-ask"),
        (ResponseType.ERROR, "tp-error"),
        (ResponseType.NO_RESULTS, "tp-callout"),
    ],
)
def test_each_response_type_has_its_own_presentation(kind, marker):
    html = components.assistant_message_html(OrchestratorResponse(kind, "رسالة"))
    assert marker in html and "رسالة" in html


def test_preference_chips_reflect_state():
    state = ConversationState(
        constraints=Constraints(
            excluded_allergens={"milk"}, excluded_ingredients={"cheese"}, vegetarian_only=True,
            max_price_sar=30, max_spice_level="mild",
        )
    )
    labels = [label for label, _ in components.preference_labels(state)]
    assert labels == ["حساسية: الحليب", "بدون جبن", "نباتي", f"حتى {format_price_sar(30)}", "حدة البهارات حتى: حار خفيف"]


def test_declined_allergy_status_is_shown_as_unknown_not_as_none():
    labels = components.preference_labels(ConversationState(allergy_status=AllergyStatus.DECLINED))
    assert labels == [("الحساسية: غير محدّدة", "muted")]
    assert components.preferences_html(ConversationState()) == ""
