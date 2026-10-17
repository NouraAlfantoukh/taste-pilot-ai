"""The test-data review process: `verified` must be earned (docs/MENU_REVIEW.md), never just typed into the menu."""

import json
from pathlib import Path

import pytest

from services import menu_review as review
from services.menu_service import get_all_dishes, load_menu

REVIEW = review.load_review()
DISHES = get_all_dishes()


def record_for(dish, **overrides):
    record = {"fingerprint": review.fingerprint(dish), "criteria_passed": sorted(review.CRITERIA), "reviewer_notes": "read against the description"}
    record.update(overrides)
    return {"process": review.PROCESS, "dishes": {dish["id"]: record}}


# ---- the review record on disk ----

def test_the_review_record_covers_exactly_the_menu_and_states_what_it_is():
    assert REVIEW["process"] == review.PROCESS and set(REVIEW["dishes"]) == {d["id"] for d in load_menu(Path("data/menu.json"))}
    assert set(REVIEW["criteria"]) == set(review.CRITERIA)
    assert "fictional" in REVIEW["reviewer"].lower() and "not a kitchen audit" in REVIEW["reviewer"].lower()
    for record in REVIEW["dishes"].values():
        assert record["criteria_passed"] == sorted(review.CRITERIA) and len(record["reviewer_notes"]) > 30


def test_every_dish_passes_every_automated_criterion_and_has_a_matching_fingerprint():
    for dish in DISHES:
        assert review.automated_failures(dish) == [], dish["id"]
        assert review.is_review_valid(dish, REVIEW), dish["id"]


# ---- automated criteria catch real defects ----

def test_r1_rejects_blends_with_hidden_contents_and_unknown_ids(make_dish):
    assert review.automated_failures(make_dish(ingredient_ids=["kabsa_spice_blend"], ingredients_ar=["بهارات الكبسة"])) == ["R1"] or "R1" in review.automated_failures(
        make_dish(ingredient_ids=["kabsa_spice_blend"], ingredients_ar=["بهارات الكبسة"])
    )
    assert "R1" in review.automated_failures(make_dish(ingredient_ids=["no_such_thing"], ingredients_ar=["x"]))


def test_r2_rejects_names_that_do_not_match_the_ids(make_dish):
    assert "R2" in review.automated_failures(make_dish(ingredient_ids=["rice"], ingredients_ar=["دجاج"]))
    assert "R2" in review.automated_failures(make_dish(ingredient_ids=["rice"], ingredients_ar=["أرز", "ملح"]))


def test_r3_rejects_missing_and_extra_allergens(make_dish):
    milk = dict(ingredient_ids=["fresh_milk"], ingredients_ar=["حليب"])
    assert review.automated_failures(make_dish(allergens=["milk"], **milk)) == []
    assert "R3" in review.automated_failures(make_dish(allergens=[], **milk))  # an implied allergen is missing
    assert "R3" in review.automated_failures(make_dish(allergens=["milk", "eggs"], **milk))  # an allergen nothing implies


def test_r4_rejects_unknown_or_duplicated_allergen_ids(make_dish):
    assert "R4" in review.automated_failures(make_dish(allergens=["unobtainium"]))
    assert "R4" in review.automated_failures(make_dish(ingredient_ids=["fresh_milk"], ingredients_ar=["حليب"], allergens=["milk", "milk"]))


@pytest.mark.parametrize(
    "description,fails",
    [
        ("أرز مع الجبن المذاب.", True),  # cheese is in the text, not in the ingredients
        ("طبق مع المكسرات المحمصة.", True),  # broad "nuts" term, no nut allergen recorded
        ("حلوى بالحليب والقشطة.", True),
        ("أرز بسيط مطبوخ بعناية.", False),
    ],
)
def test_r5_rejects_descriptions_that_mention_what_the_record_does_not_declare(make_dish, description, fails):
    assert ("R5" in review.automated_failures(make_dish(description_ar=description))) is fails


def test_r5_accepts_a_description_whose_foods_are_all_declared(make_dish):
    dish = make_dish(
        description_ar="كنافة ناعمة محشوة بالجبن والفستق.", ingredient_ids=["kunafa", "akkawi_cheese", "pistachio"],
        ingredients_ar=["كنافة", "جبن عكاوي", "فستق"], allergens=["gluten", "milk", "tree_nuts"],
    )
    assert review.automated_failures(dish) == []


# ---- verified is only honoured with a valid review ----

def verified(make_dish, **fields):
    return make_dish(allergen_data_status="verified", **fields)


def test_a_verified_dish_with_a_matching_record_stays_verified(make_dish):
    dish = verified(make_dish)
    assert review.enforce_review([dish], record_for(dish))[0]["allergen_data_status"] == "verified"


@pytest.mark.parametrize(
    "tamper",
    [
        lambda d, r: {},  # no review file at all
        lambda d, r: {"process": "something-else", "dishes": r["dishes"]},  # a different process
        lambda d, r: {"process": r["process"], "dishes": {}},  # no record for this dish
        lambda d, r: record_for(d, fingerprint="0" * 64),  # reviewed data was changed afterwards
        lambda d, r: record_for(d, criteria_passed=["R1", "R2"]),  # not every criterion was recorded as passed
        lambda d, r: record_for(d, reviewer_notes="  "),  # the human-readable review is missing
    ],
)
def test_without_a_valid_record_verified_silently_becomes_unverified(make_dish, tamper):
    dish = verified(make_dish)
    result = review.enforce_review([dish], tamper(dish, record_for(dish)))
    assert result[0]["allergen_data_status"] == "unverified" and dish["allergen_data_status"] == "verified"  # copy, not edit


def test_a_verified_dish_that_fails_a_criterion_is_downgraded_even_with_a_record(make_dish):
    dish = verified(make_dish, ingredient_ids=["fresh_milk"], ingredients_ar=["حليب"], allergens=[])  # allergen missing
    assert review.enforce_review([dish], record_for(dish))[0]["allergen_data_status"] == "unverified"


def test_editing_any_safety_field_changes_the_fingerprint(make_dish):
    base = make_dish()
    assert review.fingerprint(base) == review.fingerprint(dict(base))
    for change in ({"ingredient_ids": ["rice", "lemon"]}, {"ingredients_ar": ["رز"]}, {"allergens": ["milk"]}, {"id": "other"}):
        assert review.fingerprint(dict(base, **change)) != review.fingerprint(base)
    assert review.fingerprint(dict(base, price_sar=99, description_ar="وصف جديد")) == review.fingerprint(base)  # not safety data


def test_loading_the_production_menu_without_its_review_file_makes_every_dish_unverified(monkeypatch, tmp_path):
    monkeypatch.setattr(review, "REVIEW_PATH", tmp_path / "missing.json")
    assert {d["allergen_data_status"] for d in load_menu()} == {"unverified"}


def test_a_hand_edited_menu_cannot_promote_a_dish_by_typing_verified(tmp_path):
    menu = json.loads(Path("data/menu.json").read_text(encoding="utf-8"))
    menu["dishes"].append({**menu["dishes"][0], "id": "dish_900", "name_ar": "طبق جديد", "allergen_data_status": "verified"})
    path = tmp_path / "menu.json"
    path.write_text(json.dumps(menu, ensure_ascii=False), encoding="utf-8")
    loaded = {d["id"]: d for d in load_menu(path, review_path=review.REVIEW_PATH)}
    assert loaded["dish_900"]["allergen_data_status"] == "unverified"  # no record for it
    assert loaded["dish_001"]["allergen_data_status"] == "verified"
