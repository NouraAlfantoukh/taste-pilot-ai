"""Hard-constraint filtering, then ranking of the dishes that remain eligible.

Hard constraints (allergies, ingredient exclusions, budget, category, diet, spice) are enforced in code,
never delegated to an LLM, and are never relaxed. Serving temperature is only a soft preference: it
orders the already-eligible dishes and can never make an ineligible dish eligible.
"""

from dataclasses import dataclass, field
from typing import Mapping, Optional

from services import taxonomy
from services.constraints import Constraints, ExclusionReason
from services.menu_service import SPICE_LEVELS
from services.safety_service import assess_dish

CONFIRMED, LIKELY, UNCERTAIN = "confirmed", "likely", "uncertain"
FIT_ORDER = {CONFIRMED: 0, LIKELY: 1, UNCERTAIN: 2}


@dataclass(frozen=True)
class Exclusion:
    dish_id: str
    reasons: tuple[ExclusionReason, ...]
    confirmed_allergens: frozenset[str] = frozenset()
    confirmed_allergy_ingredients: frozenset[str] = frozenset()


@dataclass(frozen=True)
class TemperatureJudgement:
    """An LLM inference about how a dish is probably served: warm, cold, room or unknown."""

    temperature: str = "unknown"
    confidence: str = "low"  # "high", "medium" or "low"
    rationale: str = ""


@dataclass(frozen=True)
class TemperatureFit:
    """How well an eligible dish matches the requested serving temperature."""

    level: str  # CONFIRMED (menu data), LIKELY (confident inference) or UNCERTAIN (alternative only)
    basis: str  # "menu" or "inferred"
    rationale: str = ""

    @property
    def is_match(self) -> bool:
        return self.level != UNCERTAIN


@dataclass(frozen=True)
class FilterResult:
    eligible: list[dict]
    excluded: list[Exclusion]
    temperature_fit: Mapping[str, TemperatureFit] = field(default_factory=dict)


_WARM_VALUES = {"warm": {"hot", "warm"}, "cold": {"cold"}}


def temperature_fit(
    dish: dict, wanted: str, judgement: Optional[TemperatureJudgement]
) -> Optional[TemperatureFit]:
    """Classify one eligible dish against the wish. None means a confident mismatch (never shown).

    The structured serving_temperature is authoritative. Otherwise only the inference counts; low
    confidence or "unknown" can only ever yield an UNCERTAIN alternative, never a match.
    """
    actual = dish.get("serving_temperature")
    if actual is not None:
        return TemperatureFit(CONFIRMED, "menu") if actual in _WARM_VALUES[wanted] else None
    j = judgement or TemperatureJudgement()
    if j.temperature == "unknown":
        return TemperatureFit(UNCERTAIN, "inferred", j.rationale)
    confident = j.confidence in ("high", "medium")
    if j.temperature == wanted:
        return TemperatureFit(LIKELY if confident else UNCERTAIN, "inferred", j.rationale)
    return None if confident else TemperatureFit(UNCERTAIN, "inferred", j.rationale)


def filter_dishes(dishes: list[dict], constraints: Constraints) -> FilterResult:
    """Split dishes into eligible and excluded by every HARD constraint (with structured reasons)."""
    eligible: list[dict] = []
    excluded: list[Exclusion] = []
    for dish in dishes:
        assessment = assess_dish(dish, constraints)
        reasons = list(assessment.reasons)
        if constraints.vegetarian_only and not dish["is_vegetarian"]:
            reasons.append(ExclusionReason.NOT_VEGETARIAN)
        if constraints.max_price_sar is not None and dish["price_sar"] > constraints.max_price_sar:
            reasons.append(ExclusionReason.OVER_BUDGET)
        if constraints.max_spice_level is not None and (
            SPICE_LEVELS.index(dish["spice_level"]) > SPICE_LEVELS.index(constraints.max_spice_level)
        ):
            reasons.append(ExclusionReason.TOO_SPICY)
        if constraints.min_spice_level is not None and (
            SPICE_LEVELS.index(dish["spice_level"]) < SPICE_LEVELS.index(constraints.min_spice_level)
        ):
            reasons.append(ExclusionReason.TOO_MILD)
        if constraints.categories and not taxonomy.dish_category_matches(dish, constraints.categories):
            reasons.append(ExclusionReason.WRONG_CATEGORY)
        if reasons:
            excluded.append(
                Exclusion(
                    dish["id"], tuple(reasons), assessment.confirmed_allergens, assessment.confirmed_allergy_ingredients
                )
            )
        else:
            eligible.append(dish)
    return FilterResult(eligible, excluded)


def rank_dishes(dishes: list[dict], preferences: Optional[dict] = None) -> list[dict]:
    """Placeholder ranking: orders by price ascending. Preference scoring comes later."""
    return sorted(dishes, key=lambda d: d["price_sar"])


def recommend(
    dishes: list[dict],
    constraints: Constraints,
    preferences: Optional[dict] = None,
    temperature_judgements: Optional[Mapping[str, TemperatureJudgement]] = None,
) -> FilterResult:
    """Filter by hard constraints, rank, then apply the soft temperature preference to what remains.

    With a temperature wish, eligible dishes are ordered confirmed, likely, then uncertain alternatives
    (price order inside each group). Confidently contrary dishes are dropped. The eligible set can only
    shrink here; it never grows.
    """
    result = filter_dishes(dishes, constraints)
    ranked = rank_dishes(result.eligible, preferences)
    wanted = constraints.serving_temperature
    if wanted is None:
        return FilterResult(ranked, result.excluded)

    fits: dict[str, TemperatureFit] = {}
    contrary: list[Exclusion] = []
    for dish in ranked:
        fit = temperature_fit(dish, wanted, (temperature_judgements or {}).get(dish["id"]))
        if fit is None:
            contrary.append(Exclusion(dish["id"], (ExclusionReason.WRONG_TEMPERATURE,)))
        else:
            fits[dish["id"]] = fit
    kept = sorted((d for d in ranked if d["id"] in fits), key=lambda d: FIT_ORDER[fits[d["id"]].level])
    return FilterResult(kept, result.excluded + contrary, fits)
