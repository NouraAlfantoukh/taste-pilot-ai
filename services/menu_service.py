"""Menu loading, validation, and lookup."""

import json
import logging
import math
from collections import Counter
from pathlib import Path
from typing import Optional

from services import menu_review, taxonomy

logger = logging.getLogger(__name__)

MENU_PATH = Path(__file__).resolve().parent.parent / "data" / "menu.json"

SPICE_LEVELS = ("none", "mild", "medium", "hot")

SPICE_LABELS_AR = {
    "none": "غير حار",
    "mild": "حار خفيف",
    "medium": "حار متوسط",
    "hot": "حار",
}

ALLERGEN_STATUSES = ("verified", "unverified")
SERVING_TEMPERATURE_VALUES = ("hot", "warm", "cold")

_ARABIC_DIGITS = str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩")


def format_price_sar(price: float) -> str:
    """Format a numeric price as Arabic text, e.g. 35 -> '٣٥ ر.س'."""
    text = f"{price:g}".translate(_ARABIC_DIGITS).replace(".", "٫")
    return f"{text} ر.س"


def spice_label_ar(spice_level: str) -> str:
    return SPICE_LABELS_AR.get(spice_level, "غير محدد")


def _non_empty_str(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _str_list(value: object) -> bool:
    return isinstance(value, list) and all(_non_empty_str(v) for v in value)


def dish_problems(dish: object) -> list[str]:
    """Return every structural problem found in a dish (empty list means well-formed)."""
    if not isinstance(dish, dict):
        return ["dish is not an object"]
    problems: list[str] = []

    for field in ("id", "name_ar", "description_ar", "category_ar"):
        if not _non_empty_str(dish.get(field)):
            problems.append(f"{field} must be a non-empty string")

    price = dish.get("price_sar")
    if isinstance(price, bool) or not isinstance(price, (int, float)) or not math.isfinite(price) or price <= 0:
        problems.append("price_sar must be a positive finite number")

    if not isinstance(dish.get("is_vegetarian"), bool):
        problems.append("is_vegetarian must be a bool")
    if dish.get("spice_level") not in SPICE_LEVELS:
        problems.append("spice_level is invalid")

    if "allergens_ar" in dish:
        problems.append("legacy field allergens_ar is not allowed; use allergens (canonical ids)")

    allergens = dish.get("allergens")
    if not _str_list(allergens):
        problems.append("allergens must be a list of strings (required; use [] for none recorded)")
    else:
        if any(not taxonomy.is_known_allergen(a) for a in allergens):
            problems.append("allergens contains an unknown allergen id")
        if len(set(allergens)) != len(allergens):
            problems.append("allergens contains duplicates")

    if "serving_temperature" in dish and dish["serving_temperature"] not in SERVING_TEMPERATURE_VALUES:
        problems.append("serving_temperature must be 'hot', 'warm' or 'cold' when present")

    if "allergen_data_status" in dish and dish["allergen_data_status"] not in ALLERGEN_STATUSES:
        problems.append("allergen_data_status must be 'verified' or 'unverified'")

    ids, names = dish.get("ingredient_ids"), dish.get("ingredients_ar")
    if not _str_list(ids) or not ids:
        problems.append("ingredient_ids must be a non-empty list of strings")
    if not _str_list(names) or not names:
        problems.append("ingredients_ar must be a non-empty list of strings")
    if _str_list(ids) and _str_list(names) and ids and names:
        if any(not taxonomy.is_known_ingredient(i) for i in ids):
            problems.append("ingredient_ids contains an unknown ingredient id")
        elif len(ids) != len(names):
            problems.append("ingredients_ar and ingredient_ids differ in length")
        elif not all(taxonomy.ingredient_label_matches(i, n) for i, n in zip(ids, names)):
            problems.append("ingredients_ar does not match ingredient_ids")
        elif dish.get("allergen_data_status") == "verified" and any(taxonomy.is_compound(i) for i in ids):
            problems.append("a verified dish cannot contain compound ingredients with unitemized contents")

    return problems


def validate_dish(dish: object) -> bool:
    return not dish_problems(dish)


def consistency_problems(dish: dict) -> list[str]:
    """Flag known data inconsistencies in a structurally valid dish.

    Only checks what the taxonomy knows; it cannot see hidden ingredients inside compound products.
    """
    problems: list[str] = []
    missing = taxonomy.implied_allergens(dish["ingredient_ids"]) - set(dish["allergens"])
    if missing:
        problems.append(f"ingredients imply allergens not recorded: {sorted(missing)}")
    if dish["is_vegetarian"]:
        animal = {"meat", "poultry", "fish"}
        if any(taxonomy.ingredient_closure(i) & animal for i in dish["ingredient_ids"]):
            problems.append("vegetarian dish contains meat, poultry or fish")
    return problems


def load_menu(path: Optional[Path] = None, review_path: Optional[Path] = None) -> list[dict]:
    """Load valid dishes from the menu file. Returns an empty list on any failure.

    Invalid dishes are skipped. If an id appears more than once, every entry with that id is dropped.
    For the production menu (or when `review_path` is given) a dish keeps `verified` only if data/menu_review.json
    holds a valid review record for its current data (see services/menu_review.py); otherwise it is unverified.
    """
    menu_path = path or MENU_PATH
    try:
        with open(menu_path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        logger.error("Failed to load menu from %s: %s", menu_path, exc)
        return []

    raw_dishes = data.get("dishes") if isinstance(data, dict) else None
    if not isinstance(raw_dishes, list):
        logger.error("Menu file has no 'dishes' list: %s", menu_path)
        return []

    id_counts = Counter(d["id"] for d in raw_dishes if isinstance(d, dict) and isinstance(d.get("id"), str))
    dishes: list[dict] = []
    for raw in raw_dishes:
        problems = dish_problems(raw)
        if problems:
            logger.warning("Skipping invalid dish %r: %s", raw.get("id") if isinstance(raw, dict) else None, problems)
            continue
        if id_counts[raw["id"]] > 1:
            logger.error("Dropping dish with duplicate id: %s", raw["id"])
            continue
        dishes.append(raw)
    if path is None or review_path is not None:
        dishes = menu_review.enforce_review(dishes, menu_review.load_review(review_path))
    return dishes


def get_all_dishes(path: Optional[Path] = None) -> list[dict]:
    """Return all valid dishes."""
    return load_menu(path)


def get_dish_by_id(dish_id: str, path: Optional[Path] = None) -> Optional[dict]:
    """Return the dish with the given ID, or None if not found."""
    for dish in load_menu(path):
        if dish["id"] == dish_id:
            return dish
    return None
