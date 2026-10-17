"""User constraints: validated model, typed errors, and exclusion reasons."""

import math
from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Optional

from services import taxonomy
from services.menu_service import SPICE_LEVELS


class ExclusionReason(str, Enum):
    CONFIRMED_ALLERGEN = "confirmed_allergen"
    INCOMPLETE_ALLERGEN_DATA = "incomplete_allergen_data"
    EXCLUDED_INGREDIENT = "excluded_ingredient"
    NOT_VEGETARIAN = "not_vegetarian"
    OVER_BUDGET = "over_budget"
    TOO_SPICY = "too_spicy"
    TOO_MILD = "too_mild"
    WRONG_CATEGORY = "wrong_category"
    WRONG_TEMPERATURE = "wrong_temperature"


SERVING_TEMPERATURES = ("warm", "cold")

SAFETY_REASONS = frozenset(
    {
        ExclusionReason.CONFIRMED_ALLERGEN,
        ExclusionReason.INCOMPLETE_ALLERGEN_DATA,
        ExclusionReason.EXCLUDED_INGREDIENT,
    }
)


class InvalidConstraintError(ValueError):
    """A constraint value is malformed (unknown ID, bad spice level, negative budget...)."""


@dataclass(frozen=True)
class UnresolvedTerm:
    kind: str  # "allergen" or "ingredient"
    term: str
    candidates: frozenset[str] = frozenset()  # set when the term is ambiguous rather than unknown


class UnresolvedConstraintError(ValueError):
    """User terms that could not be mapped to canonical IDs; a conversational layer should ask about them."""

    def __init__(self, unresolved: Iterable[UnresolvedTerm]) -> None:
        self.unresolved = tuple(unresolved)
        super().__init__("Unresolved constraint terms: " + ", ".join(f"{u.kind}:{u.term!r}" for u in self.unresolved))


@dataclass(frozen=True)
class Constraints:
    """Hard constraints expressed with canonical IDs. Validated on construction."""

    excluded_allergens: frozenset[str] = frozenset()
    excluded_ingredients: frozenset[str] = frozenset()
    allergy_ingredients: frozenset[str] = frozenset()  # ingredient-level allergies (ingredient or family ids)
    vegetarian_only: bool = False
    max_price_sar: Optional[float] = None
    max_spice_level: Optional[str] = None  # ceiling: "nothing hotter than this"
    min_spice_level: Optional[str] = None  # floor: "I want it at least this spicy"
    categories: frozenset[str] = frozenset()  # canonical menu category ids; empty means any category
    serving_temperature: Optional[str] = None  # "warm" or "cold": a soft preference, never a hard exclusion

    def __post_init__(self) -> None:
        allergens = frozenset(self.excluded_allergens)
        ingredients = frozenset(self.excluded_ingredients)
        allergy_ingredients = frozenset(self.allergy_ingredients)
        unknown_allergens = [a for a in allergens if not taxonomy.is_known_allergen(a)]
        unknown_ingredients = [i for i in ingredients if not taxonomy.is_known_ingredient(i)]
        if unknown_allergens:
            raise InvalidConstraintError(f"Unknown allergen ids: {unknown_allergens}")
        if unknown_ingredients:
            raise InvalidConstraintError(f"Unknown ingredient ids: {unknown_ingredients}")
        unknown_allergy_ingredients = [i for i in allergy_ingredients if not taxonomy.is_known_ingredient(i)]
        if unknown_allergy_ingredients:
            raise InvalidConstraintError(f"Unknown allergy ingredient ids: {unknown_allergy_ingredients}")
        categories = frozenset(self.categories)
        unknown_categories = [k for k in categories if not taxonomy.is_known_category(k)]
        if unknown_categories:
            raise InvalidConstraintError(f"Unknown category ids: {unknown_categories}")
        if self.serving_temperature is not None and self.serving_temperature not in SERVING_TEMPERATURES:
            raise InvalidConstraintError(f"Invalid serving temperature: {self.serving_temperature!r}")
        if not isinstance(self.vegetarian_only, bool):
            raise InvalidConstraintError("vegetarian_only must be a bool")
        if self.max_spice_level is not None and self.max_spice_level not in SPICE_LEVELS:
            raise InvalidConstraintError(f"Invalid spice level: {self.max_spice_level!r}")
        if self.min_spice_level is not None and self.min_spice_level not in SPICE_LEVELS:
            raise InvalidConstraintError(f"Invalid minimum spice level: {self.min_spice_level!r}")
        if (
            self.min_spice_level is not None
            and self.max_spice_level is not None
            and SPICE_LEVELS.index(self.min_spice_level) > SPICE_LEVELS.index(self.max_spice_level)
        ):
            raise InvalidConstraintError("Minimum spice level is above the maximum")
        price = self.max_price_sar
        if price is not None and (
            isinstance(price, bool) or not isinstance(price, (int, float)) or not math.isfinite(price) or price < 0
        ):
            raise InvalidConstraintError(f"Invalid budget: {price!r}")
        object.__setattr__(self, "excluded_allergens", allergens)
        object.__setattr__(self, "excluded_ingredients", ingredients)
        object.__setattr__(self, "allergy_ingredients", allergy_ingredients)
        object.__setattr__(self, "categories", categories)


def build_constraints(
    allergens: Iterable[str] = (),
    ingredients: Iterable[str] = (),
    vegetarian_only: bool = False,
    max_price_sar: Optional[float] = None,
    max_spice_level: Optional[str] = None,
    min_spice_level: Optional[str] = None,
    categories: Iterable[str] = (),
    serving_temperature: Optional[str] = None,
) -> Constraints:
    """Build Constraints from Arabic user terms.

    Unknown or ambiguous terms raise UnresolvedConstraintError (never ignored, never auto-expanded).
    """
    allergen_ids: set[str] = set()
    allergy_ingredient_ids: set[str] = set()
    ingredient_ids: set[str] = set()
    category_ids: set[str] = set()
    unresolved: list[UnresolvedTerm] = []
    for term in categories:
        if taxonomy.is_generic_food_term(term):
            continue  # broad intent, not a category
        try:
            category_ids |= taxonomy.resolve_categories(term)
        except taxonomy.UnknownTermError:
            unresolved.append(UnresolvedTerm("category", str(term)))
    for term in allergens:
        try:
            categories, ingredients_allergic_to = taxonomy.resolve_allergy_term(term)
            allergen_ids |= categories
            allergy_ingredient_ids |= ingredients_allergic_to
        except taxonomy.UnknownTermError:
            unresolved.append(UnresolvedTerm("allergen", str(term)))
        except taxonomy.AmbiguousTermError as exc:
            unresolved.append(UnresolvedTerm("allergen", str(term), exc.candidates))
    for term in ingredients:
        try:
            ingredient_ids |= taxonomy.resolve_ingredient(term)
        except taxonomy.UnknownTermError:
            unresolved.append(UnresolvedTerm("ingredient", str(term)))
    if unresolved:
        raise UnresolvedConstraintError(unresolved)
    return Constraints(
        excluded_allergens=frozenset(allergen_ids),
        excluded_ingredients=frozenset(ingredient_ids),
        allergy_ingredients=frozenset(allergy_ingredient_ids),
        vegetarian_only=vegetarian_only,
        max_price_sar=max_price_sar,
        max_spice_level=max_spice_level,
        min_spice_level=min_spice_level,
        categories=frozenset(category_ids),
        serving_temperature=serving_temperature,
    )
