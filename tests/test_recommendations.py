from helpers import unverified_menu
import pytest

from services.constraints import (
    Constraints,
    ExclusionReason,
    InvalidConstraintError,
    UnresolvedConstraintError,
    build_constraints,
)
from services.menu_service import get_all_dishes
from services.recommendation_service import filter_dishes, rank_dishes, recommend

C = ExclusionReason


def ids(dishes):
    return [d["id"] for d in dishes]


def reasons_by_id(result):
    return {e.dish_id: e.reasons for e in result.excluded}


# ---- Constraints validation ----

@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_spice_level": "extreme"},
        {"max_price_sar": -5},
        {"max_price_sar": True},
        {"max_price_sar": float("nan")},
        {"max_price_sar": "10"},
        {"excluded_allergens": {"not_an_allergen"}},
        {"excluded_allergens": {"الحليب"}},
        {"excluded_ingredients": {"unobtainium"}},
        {"vegetarian_only": "yes"},
    ],
)
def test_invalid_constraints_are_rejected(kwargs):
    with pytest.raises(InvalidConstraintError):
        Constraints(**kwargs)


def test_valid_constraints_are_accepted():
    constraints = Constraints(excluded_allergens=["milk"], max_price_sar=0, max_spice_level="mild")
    assert constraints.excluded_allergens == {"milk"}


def test_build_constraints_resolves_arabic_terms():
    constraints = build_constraints(allergens=["حليب", "الأسماك"], ingredients=["جبن"])
    assert constraints.excluded_allergens == {"milk", "fish"}
    assert constraints.excluded_ingredients == {"cheese"}


def test_build_constraints_does_not_expand_ambiguous_allergen_terms():
    with pytest.raises(UnresolvedConstraintError) as info:
        build_constraints(allergens=["فستق", "حليب"])
    (term,) = info.value.unresolved
    assert (term.kind, term.term, term.candidates) == ("allergen", "فستق", {"peanuts", "tree_nuts"})


def test_unknown_terms_raise_unresolved_error_listing_all_of_them():
    with pytest.raises(UnresolvedConstraintError) as info:
        build_constraints(allergens=["حليب", "كذا"], ingredients=["شيء"])
    assert {(u.kind, u.term) for u in info.value.unresolved} == {("allergen", "كذا"), ("ingredient", "شيء")}


def test_build_constraints_rejects_invalid_values():
    with pytest.raises(InvalidConstraintError):
        build_constraints(max_price_sar=-1)
    with pytest.raises(InvalidConstraintError):
        build_constraints(max_spice_level="extreme")


# ---- Filtering on the real (all unverified) menu ----

def test_no_constraints_returns_all_dishes():
    dishes = get_all_dishes()
    result = filter_dishes(dishes, Constraints())
    assert len(result.eligible) == len(dishes) and result.excluded == []


def test_vegetarian_only():
    result = filter_dishes(get_all_dishes(), Constraints(vegetarian_only=True))
    assert result.eligible and all(d["is_vegetarian"] for d in result.eligible)
    assert all(C.NOT_VEGETARIAN in r for r in reasons_by_id(result).values())


def test_budget_limit():
    result = filter_dishes(get_all_dishes(), Constraints(max_price_sar=20))
    assert result.eligible and all(d["price_sar"] <= 20 for d in result.eligible)
    assert all(C.OVER_BUDGET in r for r in reasons_by_id(result).values())


def test_max_spice_level():
    result = filter_dishes(get_all_dishes(), Constraints(max_spice_level="mild"))
    assert result.eligible and all(d["spice_level"] in ("none", "mild") for d in result.eligible)


def test_ingredient_family_exclusions_on_real_menu():
    cheese = filter_dishes(get_all_dishes(), build_constraints(ingredients=["جبن"]))
    assert {"dish_007", "dish_011"} <= set(reasons_by_id(cheese))
    meat = filter_dishes(get_all_dishes(), build_constraints(ingredients=["لحم"]))
    assert {"dish_002", "dish_008"} <= set(reasons_by_id(meat))
    assert "dish_001" not in reasons_by_id(meat)


def test_every_dish_is_excluded_for_a_declared_allergy_while_it_is_unreviewed():
    for term in ("حليب", "بيض", "الفول السوداني"):
        result = filter_dishes(unverified_menu(), build_constraints(allergens=[term]))
        assert result.eligible == [], term
        assert all(C.INCOMPLETE_ALLERGEN_DATA in r for r in reasons_by_id(result).values())


# ---- Filtering with verified fictional dishes ----

def test_milk_allergy_keeps_only_verified_dishes_without_milk(make_dish, incomplete_dish):
    dishes = [
        make_dish(id="clean"),
        make_dish(id="milky", allergens=["milk"], ingredients_ar=["حليب"], ingredient_ids=["fresh_milk"]),
        incomplete_dish,
    ]
    result = filter_dishes(dishes, build_constraints(allergens=["حليب"]))
    assert ids(result.eligible) == ["clean"]
    reasons = reasons_by_id(result)
    assert reasons["milky"] == (C.CONFIRMED_ALLERGEN,)
    assert reasons[incomplete_dish["id"]] == (C.INCOMPLETE_ALLERGEN_DATA,)


def test_verified_dish_still_goes_through_the_other_filters(make_dish):
    dishes = [make_dish(id="pricey", price_sar=99), make_dish(id="spicy", spice_level="hot"), make_dish(id="ok")]
    constraints = Constraints(excluded_allergens={"milk"}, max_price_sar=50, max_spice_level="mild")
    result = filter_dishes(dishes, constraints)
    assert ids(result.eligible) == ["ok"]
    assert reasons_by_id(result) == {"pricey": (C.OVER_BUDGET,), "spicy": (C.TOO_SPICY,)}


def test_ranking_orders_eligible_dishes_by_price(make_dish):
    dishes = [make_dish(id="b", price_sar=30), make_dish(id="a", price_sar=10), make_dish(id="c", price_sar=20)]
    assert ids(recommend(dishes, Constraints()).eligible) == ["a", "c", "b"]


def test_ranking_never_restores_excluded_dishes(make_dish, incomplete_dish):
    cheap_unverified = {**incomplete_dish, "price_sar": 1}
    dishes = [cheap_unverified, make_dish(id="clean", price_sar=50)]
    result = recommend(dishes, Constraints(excluded_allergens={"milk"}))
    assert ids(result.eligible) == ["clean"]
    assert cheap_unverified["id"] in reasons_by_id(result)
    assert set(ids(rank_dishes(result.eligible))) <= set(ids(result.eligible))
