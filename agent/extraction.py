"""LLM-based extraction of structured intent from a natural Arabic message.

The LLM only interprets language. Its output is parsed strictly here, and terms are validated and
normalized afterwards by the taxonomy and Constraints. Malformed output raises ExtractionError;
it is never turned into empty constraints.
"""

import json
import math
import re
from dataclasses import dataclass
from typing import Callable, Optional

from agent.prompts import EXTRACTION_PROMPT_AR
from agent.state import ConversationState
from services import taxonomy
from services.menu_service import SPICE_LEVELS

ALLERGY_ANSWERS = ("none", "has", "declined", "unclear")
CLEARABLE = ("budget", "spice", "category", "temperature")
SERVING_TEMPERATURES = ("warm", "cold")

_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)


class ExtractionError(ValueError):
    """The LLM output is not a valid extraction."""


@dataclass(frozen=True)
class Extraction:
    allergy_answer: Optional[str] = None
    allergens: tuple[str, ...] = ()
    remove_allergens: tuple[str, ...] = ()
    unclear_allergy: bool = False
    ingredients: tuple[str, ...] = ()
    remove_ingredients: tuple[str, ...] = ()
    vegetarian: Optional[bool] = None
    max_price_sar: Optional[float] = None
    max_spice_level: Optional[str] = None
    min_spice_level: Optional[str] = None
    clear: tuple[str, ...] = ()
    wants_recommendations: bool = False
    answered_pending: tuple[str, ...] = ()
    categories: tuple[str, ...] = ()
    serving_temperature: Optional[str] = None
    settles_pending: bool = False  # the guest says this answer closes ALL open allergy questions ("خلاص بس حليب")
    requested_ingredients: tuple[str, ...] = ()  # foods the guest WANTS in the dish (the opposite of ingredients)
    confirm_override: bool = False
    memory_query: bool = False  # the guest asks what the assistant already knows about them


def _terms(data: dict, key: str) -> tuple[str, ...]:
    value = data.get(key)
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise ExtractionError(f"{key} must be a list of non-empty strings")
    return tuple(v.strip() for v in value)


def _bool(data: dict, key: str, allow_none: bool = False) -> Optional[bool]:
    value = data.get(key)
    if value is None and allow_none:
        return None
    if value is None:
        return False
    if not isinstance(value, bool):
        raise ExtractionError(f"{key} must be a boolean")
    return value


def parse_extraction(raw: str) -> Extraction:
    """Parse and strictly validate the LLM's JSON. Unknown keys are ignored; wrong types are errors."""
    if not isinstance(raw, str):
        raise ExtractionError("output is not text")
    text = raw.strip()
    fenced = _FENCE.match(text)
    if fenced:
        text = fenced.group(1)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ExtractionError("output is not valid JSON") from exc
    if not isinstance(data, dict):
        raise ExtractionError("output is not a JSON object")

    answer = data.get("allergy_answer")
    if answer is not None and answer not in ALLERGY_ANSWERS:
        raise ExtractionError("invalid allergy_answer")

    price = data.get("max_price_sar")
    if price is not None and (isinstance(price, bool) or not isinstance(price, (int, float)) or not math.isfinite(price)):
        raise ExtractionError("max_price_sar must be a finite number")

    spice = data.get("max_spice_level")
    if spice is not None and spice not in SPICE_LEVELS:
        raise ExtractionError("invalid max_spice_level")

    min_spice = data.get("min_spice_level")
    if min_spice == "none":
        min_spice = None
    if min_spice is not None and min_spice not in SPICE_LEVELS:
        raise ExtractionError("invalid min_spice_level")

    temperature = data.get("serving_temperature")
    if temperature is not None and temperature not in SERVING_TEMPERATURES:
        raise ExtractionError("invalid serving_temperature")

    clear = _terms(data, "clear")
    if any(c not in CLEARABLE for c in clear):
        raise ExtractionError("invalid clear value")

    return Extraction(
        allergy_answer=answer,
        allergens=_terms(data, "allergens"),
        remove_allergens=_terms(data, "remove_allergens"),
        unclear_allergy=_bool(data, "unclear_allergy"),
        ingredients=_terms(data, "ingredients"),
        remove_ingredients=_terms(data, "remove_ingredients"),
        vegetarian=_bool(data, "vegetarian", allow_none=True),
        max_price_sar=price,
        max_spice_level=spice,
        min_spice_level=min_spice,
        clear=clear,
        wants_recommendations=_bool(data, "wants_recommendations"),
        answered_pending=_terms(data, "answered_pending"),
        categories=_terms(data, "categories"),
        serving_temperature=temperature,
        requested_ingredients=_terms(data, "requested_ingredients"),
        confirm_override=_bool(data, "confirm_override"),
        settles_pending=_bool(data, "settles_pending"),
        memory_query=_bool(data, "memory_query"),
    )


def build_extraction_input(message: str, state: ConversationState) -> str:
    """Give the LLM the context it needs to interpret a short reply (pending question, current constraints)."""
    c = state.constraints
    context = {
        "pending_question": state.pending.kind.value if state.pending else None,
        "pending_terms": (
            [
                {"text": t.text, "options": [taxonomy.allergen_name_ar(c) for c in t.candidates]}
                for t in state.pending.terms
            ]
            if state.pending
            else []
        ),
        "pending_requested": sorted(taxonomy.ingredient_name_ar(i) for i in state.requested_ingredients),
        "allergy_status": state.allergy_status.value,
        "current_allergens": sorted(
            [taxonomy.allergen_name_ar(a) for a in c.excluded_allergens]
            + [taxonomy.ingredient_name_ar(i) for i in c.allergy_ingredients]
        ),
        "current_excluded_ingredients": sorted(taxonomy.ingredient_name_ar(i) for i in c.excluded_ingredients),
        "current_categories": sorted(taxonomy.category_name_ar(k) for k in c.categories),
        "current_serving_temperature": c.serving_temperature,
        "vegetarian_only": c.vegetarian_only,
        "max_price_sar": c.max_price_sar,
        "max_spice_level": c.max_spice_level,
        "min_spice_level": c.min_spice_level,
        "recent_messages": [{"role": t.role, "text": t.text} for t in state.history],
        "message": message,
    }
    return json.dumps(context, ensure_ascii=False)


def extract(message: str, state: ConversationState, generate: Callable[[str, str], str]) -> Extraction:
    """One LLM call, no retries. Provider errors propagate; malformed output raises ExtractionError."""
    return parse_extraction(generate(EXTRACTION_PROMPT_AR, build_extraction_input(message, state)))
