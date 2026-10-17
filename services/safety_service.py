"""Allergy/ingredient eligibility. Pure functions, no I/O, no ranking.

"Eligible" means: no declared allergen is recorded for the dish in data that is marked verified.
It does NOT mean the dish is guaranteed safe (cross-contact and recipe changes are not modelled).
"""

from dataclasses import dataclass

from services import taxonomy
from services.constraints import Constraints, ExclusionReason

VERIFIED = "verified"
UNVERIFIED = "unverified"


@dataclass(frozen=True)
class Assessment:
    reasons: tuple[ExclusionReason, ...] = ()
    confirmed_allergens: frozenset[str] = frozenset()
    confirmed_allergy_ingredients: frozenset[str] = frozenset()

    @property
    def eligible(self) -> bool:
        return not self.reasons


def allergen_data_status(dish: dict) -> str:
    """Return 'verified' only for an explicit 'verified'; anything else (missing, invalid) is 'unverified'."""
    return VERIFIED if dish.get("allergen_data_status") == VERIFIED else UNVERIFIED


def allergen_data_complete(dish: dict) -> bool:
    """True when the record itself is complete and consistent: every ingredient known and itemized (no blend with
    hidden contents) and every allergen implied by those ingredients is recorded.

    This is DATA COMPLETENESS, deliberately separate from `allergen_data_status` (independent verification).
    A complete record is never treated as verified, and filtering for allergies still requires verification.
    """
    ids = dish.get("ingredient_ids")
    if not isinstance(ids, list) or not ids or not isinstance(dish.get("allergens"), list):
        return False
    try:
        if any(taxonomy.is_compound(i) for i in ids):
            return False
        return taxonomy.implied_allergens(ids) <= set(dish["allergens"])
    except taxonomy.UnknownTermError:
        return False


def assess_dish(dish: dict, constraints: Constraints) -> Assessment:
    """Assess a dish against allergies (categories and ingredients) and ingredient exclusions.

    Every applicable reason is collected. With any allergy declared, a dish whose allergen data is not
    verified, whose ingredients are not fully understood, or (for ingredient allergies) that contains a
    compound ingredient with unitemized contents is excluded: absence of evidence is not safety.
    """
    reasons: list[ExclusionReason] = []
    matched: frozenset[str] = frozenset()
    matched_ingredients: frozenset[str] = frozenset()
    ingredient_ids = dish.get("ingredient_ids") or []

    if constraints.excluded_allergens or constraints.allergy_ingredients:
        present = set(dish.get("allergens") or [])
        contained: set[str] = set()
        try:
            present |= taxonomy.implied_allergens(ingredient_ids)
            for ingredient_id in ingredient_ids:
                contained |= taxonomy.ingredient_closure(ingredient_id)
            ingredients_understood = True
        except taxonomy.UnknownTermError:
            ingredients_understood = False
        matched = frozenset(constraints.excluded_allergens & present)
        matched_ingredients = frozenset(constraints.allergy_ingredients & contained)
        if matched or matched_ingredients:
            reasons.append(ExclusionReason.CONFIRMED_ALLERGEN)
        has_unitemized_content = bool(constraints.allergy_ingredients) and ingredients_understood and any(
            taxonomy.is_compound(i) for i in ingredient_ids
        )
        if allergen_data_status(dish) != VERIFIED or not ingredients_understood or has_unitemized_content:
            reasons.append(ExclusionReason.INCOMPLETE_ALLERGEN_DATA)

    if constraints.excluded_ingredients:
        try:
            if taxonomy.matches_ingredient_exclusion(ingredient_ids, constraints.excluded_ingredients):
                reasons.append(ExclusionReason.EXCLUDED_INGREDIENT)
        except taxonomy.UnknownTermError:
            reasons.append(ExclusionReason.EXCLUDED_INGREDIENT)  # cannot rule it out: fail closed

    return Assessment(tuple(reasons), matched, matched_ingredients)
