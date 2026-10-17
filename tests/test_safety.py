from services.constraints import Constraints, ExclusionReason
from services.safety_service import allergen_data_status, assess_dish

MILK = Constraints(excluded_allergens={"milk"})
C = ExclusionReason


def test_confirmed_allergen_excludes_verified_dish(make_dish):
    dish = make_dish(allergens=["milk"], ingredients_ar=["حليب"], ingredient_ids=["fresh_milk"])
    assessment = assess_dish(dish, MILK)
    assert not assessment.eligible
    assert assessment.reasons == (C.CONFIRMED_ALLERGEN,)


def test_confirmed_allergen_excludes_regardless_of_data_status(make_dish):
    dish = make_dish(allergens=["milk"], allergen_data_status="unverified")
    assessment = assess_dish(dish, MILK)
    assert assessment.reasons[0] == C.CONFIRMED_ALLERGEN
    assert C.INCOMPLETE_ALLERGEN_DATA in assessment.reasons


def test_allergen_implied_by_ingredients_is_confirmed_even_if_not_recorded(make_dish):
    dish = make_dish(ingredients_ar=["سمن"], ingredient_ids=["ghee"], allergens=[])
    assert C.CONFIRMED_ALLERGEN in assess_dish(dish, MILK).reasons


def test_unverified_dish_with_empty_allergens_is_excluded_for_declared_allergy(make_dish):
    dish = make_dish(allergens=[], allergen_data_status="unverified")
    assert assess_dish(dish, MILK).reasons == (C.INCOMPLETE_ALLERGEN_DATA,)


def test_incomplete_fixture_is_excluded_for_milk_allergy(incomplete_dish):
    assert assess_dish(incomplete_dish, MILK).reasons == (C.INCOMPLETE_ALLERGEN_DATA,)


def test_verified_dish_without_target_allergen_is_only_eligible_for_further_evaluation(make_dish):
    assessment = assess_dish(make_dish(allergens=["gluten"]), MILK)
    assert assessment.eligible and assessment.reasons == ()


def test_missing_or_invalid_status_is_treated_as_unverified(make_dish):
    missing = make_dish()
    del missing["allergen_data_status"]
    assert allergen_data_status(missing) == "unverified"
    for bad in ("VERIFIED", "yes", None, True, ""):
        assert allergen_data_status(make_dish(allergen_data_status=bad)) == "unverified"
    assert assess_dish(missing, MILK).reasons == (C.INCOMPLETE_ALLERGEN_DATA,)


def test_unverified_dish_stays_eligible_when_no_allergy_is_declared(make_dish):
    assert assess_dish(make_dish(allergen_data_status="unverified"), Constraints()).eligible


def test_other_declared_allergen_not_present_in_verified_dish(make_dish):
    assert assess_dish(make_dish(), Constraints(excluded_allergens={"peanuts"})).eligible


def test_unknown_ingredient_id_fails_closed_for_declared_allergy(make_dish):
    dish = make_dish(ingredient_ids=["unobtainium"])
    assert C.INCOMPLETE_ALLERGEN_DATA in assess_dish(dish, MILK).reasons


def test_ingredient_family_exclusion(make_dish):
    dish = make_dish(ingredients_ar=["جبن موزاريلا"], ingredient_ids=["mozzarella"])
    assert assess_dish(dish, Constraints(excluded_ingredients={"cheese"})).reasons == (C.EXCLUDED_INGREDIENT,)
    assert assess_dish(make_dish(), Constraints(excluded_ingredients={"cheese"})).eligible


def test_unknown_ingredient_id_fails_closed_for_ingredient_exclusion(make_dish):
    dish = make_dish(ingredient_ids=["unobtainium"])
    assert C.EXCLUDED_INGREDIENT in assess_dish(dish, Constraints(excluded_ingredients={"cheese"})).reasons
