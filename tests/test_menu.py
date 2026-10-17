import json

import pytest

from services import taxonomy
from services.menu_service import (
    consistency_problems,
    dish_problems,
    format_price_sar,
    get_all_dishes,
    get_dish_by_id,
    load_menu,
    validate_dish,
)


def test_menu_has_enough_dishes_and_categories():
    dishes = get_all_dishes()
    assert len(dishes) >= 12
    assert len({d["category_ar"] for d in dishes}) >= 3


def test_all_dishes_in_the_json_file_are_valid():
    with open("data/menu.json", encoding="utf-8") as f:
        raw = json.load(f)["dishes"]
    assert len(get_all_dishes()) == len(raw)


def test_dish_ids_are_unique():
    ids = [d["id"] for d in get_all_dishes()]
    assert len(ids) == len(set(ids))


def test_menu_has_vegetarian_and_non_vegetarian():
    assert {d["is_vegetarian"] for d in get_all_dishes()} == {True, False}


def test_get_dish_by_id():
    assert get_dish_by_id("dish_001")["name_ar"] == "كبسة دجاج"
    assert get_dish_by_id("missing") is None


def test_missing_file_returns_empty(tmp_path):
    assert load_menu(tmp_path / "nope.json") == []


def test_invalid_json_returns_empty(tmp_path):
    bad = tmp_path / "menu.json"
    bad.write_text("{not json", encoding="utf-8")
    assert load_menu(bad) == []


def test_invalid_dishes_are_skipped(tmp_path):
    path = tmp_path / "menu.json"
    path.write_text(json.dumps({"dishes": [{"id": "x"}]}), encoding="utf-8")
    assert load_menu(path) == []


def test_format_price_sar():
    assert format_price_sar(35) == "٣٥ ر.س"


def test_every_verified_production_dish_is_backed_by_a_valid_review_record():
    from services import menu_review

    review = menu_review.load_review()
    for dish in get_all_dishes():
        if dish["allergen_data_status"] == "verified":
            assert menu_review.is_review_valid(dish, review), dish["id"]


def test_menu_note_is_customer_facing_and_points_allergic_guests_to_the_staff():
    with open("data/menu.json", encoding="utf-8") as f:
        note = json.load(f)["restaurant_note_ar"]
    assert "حساسية" in note and "موظف" in note
    assert not any(word in note for word in ("تجريبية", "خيالية", "التطوير", "غير مُراجَعة"))  # no development wording


# ---- validation ----

def test_well_formed_dish_is_valid(make_dish):
    assert validate_dish(make_dish()) is True


@pytest.mark.parametrize(
    "overrides",
    [
        {"id": ""},
        {"name_ar": 5},
        {"price_sar": 0},
        {"price_sar": -1},
        {"price_sar": True},
        {"price_sar": "5"},
        {"price_sar": float("nan")},
        {"price_sar": float("inf")},
        {"spice_level": "extreme"},
        {"is_vegetarian": "yes"},
        {"allergens": [1, 2]},
        {"allergens": "milk"},
        {"allergens": ["not_an_allergen"]},
        {"allergens": ["الحليب"]},
        {"allergens": ["milk", "milk"]},
        {"allergens_ar": ["الحليب"]},
        {"allergen_data_status": "VERIFIED"},
        {"allergen_data_status": None},
        {"ingredient_ids": []},
        {"ingredient_ids": [1]},
        {"ingredient_ids": ["unobtainium"]},
        {"ingredients_ar": []},
        {"ingredients_ar": ["أرز", "طماطم"]},
        {"ingredients_ar": ["طماطم"]},
        {"ingredients_ar": [5]},
    ],
)
def test_malformed_dishes_are_rejected(make_dish, overrides):
    assert dish_problems(make_dish(**overrides))
    assert validate_dish(make_dish(**overrides)) is False


def test_missing_required_fields_are_rejected(make_dish):
    for field in ("allergens", "ingredient_ids", "ingredients_ar", "price_sar", "spice_level"):
        dish = make_dish()
        del dish[field]
        assert not validate_dish(dish), field


def test_non_dict_is_rejected():
    assert not validate_dish("dish")
    assert not validate_dish(None)


def test_missing_allergen_status_is_allowed_in_data_but_never_means_verified(make_dish):
    dish = make_dish()
    del dish["allergen_data_status"]
    assert validate_dish(dish) is True


def test_verified_dish_cannot_contain_compound_ingredients(make_dish):
    dish = make_dish(ingredients_ar=["بهارات الكبسة"], ingredient_ids=["kabsa_spice_blend"])
    assert not validate_dish(dish)
    assert validate_dish({**dish, "allergen_data_status": "unverified"})


def test_duplicate_ids_are_dropped(tmp_path, make_dish):
    path = tmp_path / "menu.json"
    dishes = [make_dish(id="dup"), make_dish(id="dup", name_ar="آخر"), make_dish(id="unique")]
    path.write_text(json.dumps({"dishes": dishes}), encoding="utf-8")
    assert [d["id"] for d in load_menu(path)] == ["unique"]


def test_legacy_schema_dish_is_rejected(tmp_path):
    legacy = {
        "id": "old", "name_ar": "قديم", "description_ar": "وصف", "category_ar": "فئة", "price_sar": 5,
        "ingredients_ar": ["أرز"], "allergens_ar": [], "is_vegetarian": True, "spice_level": "none",
    }
    path = tmp_path / "menu.json"
    path.write_text(json.dumps({"dishes": [legacy]}), encoding="utf-8")
    assert load_menu(path) == []


# ---- consistency of the production data ----

def test_production_menu_has_no_known_inconsistencies():
    for dish in get_all_dishes():
        assert consistency_problems(dish) == [], dish["id"]


def test_consistency_check_flags_missing_implied_allergen(make_dish):
    dish = make_dish(ingredients_ar=["سمن"], ingredient_ids=["ghee"], allergens=[])
    assert any("milk" in p for p in consistency_problems(dish))


def test_consistency_check_flags_vegetarian_dish_with_meat(make_dish):
    dish = make_dish(ingredients_ar=["لحم ضأن"], ingredient_ids=["lamb"], is_vegetarian=True)
    assert any("vegetarian" in p for p in consistency_problems(dish))


def test_consistency_check_cannot_see_inside_compound_ingredients(make_dish):
    dish = make_dish(
        ingredients_ar=["بهارات الكبسة"], ingredient_ids=["kabsa_spice_blend"], allergen_data_status="unverified"
    )
    assert consistency_problems(dish) == []
    assert taxonomy.is_compound("kabsa_spice_blend")


def test_production_dishes_with_compound_ingredients_are_not_verified():
    for dish in get_all_dishes():
        if any(taxonomy.is_compound(i) for i in dish["ingredient_ids"]):
            assert dish["allergen_data_status"] == "unverified", dish["id"]
