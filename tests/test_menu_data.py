"""The production menu (data/menu.json): completeness, internal consistency, and the safety model on top of it.

Data completeness is tested separately from verification: the menu is complete and consistent, and still every
dish is "unverified" because nobody independently verified it, so allergy filtering keeps failing safe.
"""

import itertools
import json
from pathlib import Path

import pytest
from helpers import Scripted, ext, picks_for, unverified_menu

from agent.orchestrator import NO_VERIFIED_OPTION_MESSAGE_AR, ResponseType, handle_message
from agent.state import AllergyStatus, ConversationState
from services import menu_review, taxonomy
from services.constraints import Constraints, ExclusionReason
from services.menu_service import SPICE_LEVELS, consistency_problems, dish_problems, get_all_dishes, load_menu
from services.recommendation_service import filter_dishes
from services.safety_service import allergen_data_complete, assess_dish

DISHES = get_all_dishes()
BY_ID = {d["id"]: d for d in DISHES}
FIELDS = {
    "id", "name_ar", "description_ar", "category_ar", "price_sar", "ingredients_ar", "ingredient_ids", "allergens",
    "allergen_data_status", "serving_temperature", "is_vegetarian", "spice_level",
}
ANIMAL = {"meat", "poultry", "fish"}


def ids_of(result):
    return {d["id"] for d in result.eligible}


# ---- completeness and schema ----

def test_the_menu_is_loaded_whole_and_every_record_is_structurally_valid():
    assert len(DISHES) == 15
    assert all(dish_problems(d) == [] for d in DISHES)


@pytest.mark.parametrize("dish", DISHES, ids=lambda d: d["id"])
def test_every_dish_has_exactly_the_expected_fields_filled_in(dish):
    assert set(dish) == FIELDS
    assert all(dish[f] not in (None, "", []) for f in FIELDS - {"allergens", "is_vegetarian"})
    assert dish["serving_temperature"] in ("hot", "warm", "cold") and dish["spice_level"] in SPICE_LEVELS


def test_names_and_ids_are_unique_and_prices_are_sensible():
    assert len({d["id"] for d in DISHES}) == len({d["name_ar"] for d in DISHES}) == len(DISHES)
    assert all(isinstance(d["price_sar"], (int, float)) and 5 <= d["price_sar"] <= 200 for d in DISHES)


def test_every_category_is_known_and_the_menu_covers_each_of_them():
    known = set(taxonomy.get_taxonomy().category_names.values())
    assert {d["category_ar"] for d in DISHES} == known


# ---- ingredient and allergen mapping ----

@pytest.mark.parametrize("dish", DISHES, ids=lambda d: d["id"])
def test_ingredient_names_and_ids_correspond_one_to_one_and_are_all_itemized(dish):
    assert len(dish["ingredient_ids"]) == len(set(dish["ingredient_ids"])) == len(dish["ingredients_ar"])
    for ingredient_id, name in zip(dish["ingredient_ids"], dish["ingredients_ar"]):
        assert taxonomy.ingredient_label_matches(ingredient_id, name)
    assert not any(taxonomy.is_compound(i) for i in dish["ingredient_ids"])  # no blend with hidden contents


@pytest.mark.parametrize("dish", DISHES, ids=lambda d: d["id"])
def test_recorded_allergens_are_exactly_what_the_ingredients_imply(dish):
    assert consistency_problems(dish) == []
    assert set(dish["allergens"]) == set(taxonomy.implied_allergens(dish["ingredient_ids"]))  # nothing missing, nothing extra


@pytest.mark.parametrize("dish", DISHES, ids=lambda d: d["id"])
def test_vegetarian_flag_matches_the_ingredients(dish):
    has_animal = any(taxonomy.ingredient_closure(i) & ANIMAL for i in dish["ingredient_ids"])
    assert dish["is_vegetarian"] is (not has_animal)


@pytest.mark.parametrize("dish", DISHES, ids=lambda d: d["id"])
def test_spice_level_is_coherent_with_chili_ingredients(dish):
    has_chili = "hot_pepper" in dish["ingredient_ids"]
    index = SPICE_LEVELS.index(dish["spice_level"])
    assert (index >= SPICE_LEVELS.index("medium")) if has_chili else True
    assert (dish["spice_level"] != "none") if has_chili else True
    if dish["spice_level"] == "none":
        assert not has_chili


def test_serving_temperatures_are_plausible_for_the_kind_of_dish():
    assert BY_ID["dish_015"]["serving_temperature"] == "cold" and BY_ID["dish_013"]["serving_temperature"] == "cold"
    assert BY_ID["dish_014"]["serving_temperature"] == "hot" and BY_ID["dish_010"]["serving_temperature"] == "hot"
    assert {d["serving_temperature"] for d in DISHES if d["category_ar"] == "المشروبات"} <= {"hot", "cold"}


def test_descriptions_are_short_natural_and_customer_facing():
    for d in DISHES:
        text = d["description_ar"]
        assert 15 <= len(text) <= 120 and text.endswith("."), d["id"]
        assert not any(word in text for word in ("تجريبي", "وهمي", "اختبار", "خيالي", "غير مُراجَعة", "dish_"))


# ---- completeness is not verification ----

def test_every_dish_is_complete_and_its_verified_status_is_backed_by_a_review():
    assert all(allergen_data_complete(d) for d in DISHES)
    review = menu_review.load_review()
    assert all(d["allergen_data_status"] == "verified" and menu_review.is_review_valid(d, review) for d in DISHES)


def test_completeness_requires_itemized_ingredients_and_every_implied_allergen(make_dish):
    assert allergen_data_complete(make_dish(ingredient_ids=["fresh_milk"], ingredients_ar=["حليب"], allergens=["milk"]))
    missing = make_dish(ingredient_ids=["fresh_milk"], ingredients_ar=["حليب"], allergens=[])
    assert not allergen_data_complete(missing)
    blend = make_dish(ingredient_ids=["kabsa_spice_blend"], ingredients_ar=["بهارات الكبسة"], allergens=[])
    assert not allergen_data_complete(blend)
    assert not allergen_data_complete({"ingredient_ids": [], "allergens": []}) and not allergen_data_complete({})
    assert not allergen_data_complete(make_dish(ingredient_ids=["not_an_ingredient"]))


def test_a_complete_but_unreviewed_dish_is_still_not_allergy_safe(make_dish):
    complete = make_dish(allergen_data_status="unverified")
    assert allergen_data_complete(complete)
    milk = Constraints(excluded_allergens={"milk"})
    assert assess_dish(complete, milk).reasons == (ExclusionReason.INCOMPLETE_ALLERGEN_DATA,)
    assert assess_dish(dict(complete, allergen_data_status="verified"), milk).eligible


def test_missing_allergen_metadata_is_never_read_as_allergen_free(make_dish):
    milky = make_dish(ingredient_ids=["fresh_milk"], ingredients_ar=["حليب"], allergens=[], allergen_data_status="verified")
    assert ExclusionReason.CONFIRMED_ALLERGEN in assess_dish(milky, Constraints(excluded_allergens={"milk"})).reasons


# ---- allergy filtering on the real, reviewed menu ----

def contains(dish, allergen):
    return allergen in dish["allergens"] or allergen in taxonomy.implied_allergens(dish["ingredient_ids"])


ALLERGENS = sorted({a for d in DISHES for a in d["allergens"]})


@pytest.mark.parametrize("allergen", ALLERGENS)
def test_a_dish_with_the_declared_allergen_is_never_eligible_and_the_rest_are(allergen):
    result = filter_dishes(DISHES, Constraints(excluded_allergens={allergen}))
    holders = {d["id"] for d in DISHES if contains(d, allergen)}
    assert holders and ids_of(result) == {d["id"] for d in DISHES} - holders
    confirmed = {e.dish_id for e in result.excluded if ExclusionReason.CONFIRMED_ALLERGEN in e.reasons}
    assert confirmed == holders


@pytest.mark.parametrize("allergen", ALLERGENS)
def test_with_unreviewed_data_the_same_allergy_leaves_nothing_to_recommend(allergen):
    result = filter_dishes(unverified_menu(), Constraints(excluded_allergens={allergen}))
    assert result.eligible == []
    assert all(ExclusionReason.INCOMPLETE_ALLERGEN_DATA in e.reasons for e in result.excluded)


@pytest.mark.parametrize("allergen", ALLERGENS)
def test_the_model_cannot_slip_a_conflicting_dish_in_through_its_picks(allergen):
    state = ConversationState(constraints=Constraints(excluded_allergens={allergen}), allergy_status=AllergyStatus.REPORTED)
    llm = Scripted(ext(wants_recommendations=True), picks_for(*[d["id"] for d in DISHES]))  # it names every dish
    response = handle_message(state, "رشّح لي", llm)
    assert response.type is ResponseType.RECOMMENDATIONS and response.recommendations
    assert not any(contains(d, allergen) for d in response.recommendations)
    shown_names = {d["name_ar"] for d in response.recommendations}
    assert not shown_names & {d["name_ar"] for d in DISHES if contains(d, allergen)}


def test_several_allergies_together_exclude_the_union_of_their_dishes():
    wanted = {"milk", "gluten", "fish"}
    result = filter_dishes(DISHES, Constraints(excluded_allergens=wanted))
    assert ids_of(result) == {d["id"] for d in DISHES if not any(contains(d, a) for a in wanted)}
    assert ids_of(result) >= {"dish_001", "dish_010"}


def test_an_ingredient_level_allergy_is_matched_through_families_on_the_itemized_menu():
    result = filter_dishes(DISHES, Constraints(allergy_ingredients={"cumin"}))
    holders = {d["id"] for d in DISHES if "cumin" in d["ingredient_ids"]}
    assert holders >= {"dish_001", "dish_010"} and ids_of(result) == {d["id"] for d in DISHES} - holders
    cheese = filter_dishes(DISHES, Constraints(allergy_ingredients={"cheese"}))
    assert not ids_of(cheese) & {"dish_007", "dish_011"} and "dish_001" in ids_of(cheese)  # families: mozzarella, akkawi


def test_a_tampered_dish_loses_its_verification_and_is_excluded_for_allergic_guests(tmp_path):
    menu = json.loads(Path("data/menu.json").read_text(encoding="utf-8"))
    for dish in menu["dishes"]:
        if dish["id"] == "dish_006":
            dish["ingredient_ids"] = ["chickpeas", "lemon"]  # edited after the review
            dish["ingredients_ar"] = ["حمص", "ليمون"]
            dish["allergens"] = []
    path = tmp_path / "menu.json"
    path.write_text(json.dumps(menu, ensure_ascii=False), encoding="utf-8")
    loaded = {d["id"]: d for d in load_menu(path, review_path=menu_review.REVIEW_PATH)}
    assert loaded["dish_006"]["allergen_data_status"] == "unverified" and loaded["dish_001"]["allergen_data_status"] == "verified"
    result = filter_dishes(list(loaded.values()), Constraints(excluded_allergens={"milk"}))
    assert "dish_006" not in ids_of(result) and "dish_001" in ids_of(result)


def test_the_user_hears_that_nothing_can_be_confirmed_when_the_data_is_unreviewed():
    state = ConversationState(constraints=Constraints(excluded_allergens={"milk"}), allergy_status=AllergyStatus.REPORTED)
    llm = Scripted(ext(wants_recommendations=True))
    response = handle_message(state, "رشّح لي", llm, unverified_menu())
    assert response.type is ResponseType.NO_RESULTS and response.reply_ar == NO_VERIFIED_OPTION_MESSAGE_AR
    assert not llm.generation_calls()


# ---- recommendation behaviour: the engine agrees with an independent oracle ----

def oracle(vegetarian, max_spice, min_spice, budget, excluded):
    selected = set()
    for d in DISHES:
        if vegetarian and not d["is_vegetarian"]:
            continue
        index = SPICE_LEVELS.index(d["spice_level"])
        if max_spice and index > SPICE_LEVELS.index(max_spice):
            continue
        if min_spice and index < SPICE_LEVELS.index(min_spice):
            continue
        if budget is not None and d["price_sar"] > budget:
            continue
        if any(taxonomy.ingredient_closure(i) & excluded for i in d["ingredient_ids"]):
            continue
        selected.add(d["id"])
    return selected


GRID = itertools.product(
    (False, True), (None, "none", "mild"), (None, "mild"), (None, 20, 40), (frozenset(), frozenset({"cheese"}), frozenset({"meat", "onion"}))
)


@pytest.mark.parametrize("vegetarian,max_spice,min_spice,budget,excluded", [g for g in GRID if not (g[1] == "none" and g[2])])
def test_the_filter_matches_an_independent_oracle_on_the_real_menu(vegetarian, max_spice, min_spice, budget, excluded):
    c = Constraints(
        vegetarian_only=vegetarian, max_spice_level=max_spice, min_spice_level=min_spice, max_price_sar=budget,
        excluded_ingredients=excluded,
    )
    assert ids_of(filter_dishes(DISHES, c)) == oracle(vegetarian, max_spice, min_spice, budget, set(excluded))


def test_a_vegetarian_with_a_mushroom_dislike_and_a_budget_gets_only_matching_dishes():
    state = ConversationState(
        constraints=Constraints(vegetarian_only=True, excluded_ingredients={"mushroom"}, max_price_sar=20),
        allergy_status=AllergyStatus.NONE_REPORTED,
    )
    llm = Scripted(ext(wants_recommendations=True), picks_for("dish_001", "dish_006", "dish_009", "dish_010", "dish_013"))
    response = handle_message(state, "رشّح لي", llm)
    assert {d["id"] for d in response.recommendations} == {"dish_006", "dish_010", "dish_013"}
    assert all(d["is_vegetarian"] and d["price_sar"] <= 20 for d in response.recommendations)
