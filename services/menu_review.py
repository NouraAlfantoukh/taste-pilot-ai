"""The test-data review that earns a dish the status `verified` (see docs/MENU_REVIEW.md).

`allergen_data_status: "verified"` in data/menu.json is NOT a self-declaration. At load time a dish keeps that
status only if data/menu_review.json holds a review record whose fingerprint matches the dish's current safety
data AND the dish still passes every automated criterion below. Anything else (no record, edited ingredients or
allergens, a failing criterion) silently becomes `unverified`, which makes allergy filtering fail safe.

The criteria check the record against the taxonomy and its own description. They cannot know how a real
kitchen prepares a dish, so for this fictional menu "verified" means "passed this documented review", never
"guaranteed safe" (cross-contact and recipe changes are out of scope).
"""

import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Optional

from services import taxonomy

logger = logging.getLogger(__name__)

REVIEW_PATH = Path(__file__).resolve().parent.parent / "data" / "menu_review.json"
PROCESS = "fictional-menu-review-v1"

CRITERIA = {
    "R1": "every ingredient id is known to the taxonomy and itemized (no blend or mix with hidden contents)",
    "R2": "the Arabic ingredient names correspond one-to-one to the ingredient ids",
    "R3": "the recorded allergens are exactly the allergens implied by the ingredients (none missing, none extra)",
    "R4": "recorded allergen ids are known and not duplicated",
    "R5": "the description does not mention a food, or an allergen, that the record does not declare",
    "R6": "the dish was read against the description by the reviewer and judged coherent (recorded in the review file)",
}
AUTOMATED = ("R1", "R2", "R3", "R4", "R5")


def fingerprint(dish: dict) -> str:
    """Hash of everything the allergy decision depends on. Any edit to it invalidates the review."""
    payload = {
        "id": dish.get("id"),
        "ingredient_ids": list(dish.get("ingredient_ids") or []),
        "ingredients_ar": list(dish.get("ingredients_ar") or []),
        "allergens": sorted(dish.get("allergens") or []),
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _description_mentions(dish: dict) -> set[frozenset[str]]:
    """Allergen groups (a set of alternatives) that the description's wording implies.

    A term that names one allergen yields a one-element group; a broad term such as "مكسرات" yields all of its
    possible meanings, any one of which satisfies the check.
    """
    tax = taxonomy.get_taxonomy()
    text = " " + " ".join(re.sub(r"[^\w\s]", " ", taxonomy.normalize_text(dish.get("description_ar", ""))).split()) + " "

    def forms(word: str) -> set[str]:
        """The word with and without the conjunction "و" and the article "ال" (e.g. والحليب -> حليب)."""
        variants = {word}
        for prefix in ("و", "ب", "ل", "ف"):
            if word.startswith(prefix) and len(word) > 3:
                variants.add(word[1:])
        for form in list(variants):
            if form.startswith("ال") and len(form) > 4:
                variants.add(form[2:])
        return variants

    words: set[str] = set()
    for word in text.split():
        words |= forms(word)

    def mentioned(label: str) -> bool:
        key = taxonomy.normalize_text(label)
        if " " in key:
            return f" {key} " in text
        return bool(forms(key) & words)

    groups: set[frozenset[str]] = set()
    for label, ids in tax.allergen_index.items():
        if mentioned(label):
            groups.add(frozenset(ids))
    for label, candidates in tax.ambiguous_allergens.items():
        if mentioned(label):
            groups.add(frozenset(candidates))
    for ingredient_id, labels in tax.ingredient_labels.items():
        implied = taxonomy.implied_allergens([ingredient_id])
        if implied and any(mentioned(label) for label in labels if not label.isascii()):
            groups.add(frozenset(implied))
    return groups


def automated_failures(dish: dict) -> list[str]:
    """The ids of the automated criteria (R1-R5) that this dish does not meet."""
    failed: list[str] = []
    ids, names = dish.get("ingredient_ids"), dish.get("ingredients_ar")
    allergens = dish.get("allergens")
    if not isinstance(ids, list) or not ids or not all(taxonomy.is_known_ingredient(i) for i in ids) or any(
        taxonomy.is_compound(i) for i in ids
    ):
        failed.append("R1")
    if not (
        isinstance(ids, list) and isinstance(names, list) and len(ids) == len(names)
        and all(taxonomy.ingredient_label_matches(i, n) for i, n in zip(ids, names))
    ):
        failed.append("R2")
    if "R1" not in failed and isinstance(allergens, list):
        if set(allergens) != set(taxonomy.implied_allergens(ids)):
            failed.append("R3")
    else:
        failed.append("R3")
    if not isinstance(allergens, list) or len(set(allergens)) != len(allergens) or not all(
        taxonomy.is_known_allergen(a) for a in allergens
    ):
        failed.append("R4")
    elif any(not (group & set(allergens)) for group in _description_mentions(dish)):
        failed.append("R5")
    return failed


def load_review(path: Optional[Path] = None) -> dict:
    try:
        with open(path or REVIEW_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) and data.get("process") == PROCESS else {}


def is_review_valid(dish: dict, review: dict) -> bool:
    """True only if a record exists for this dish, matches its current data, and every criterion holds."""
    if review.get("process") != PROCESS:
        return False
    record = (review.get("dishes") or {}).get(dish.get("id"))
    if not isinstance(record, dict) or record.get("fingerprint") != fingerprint(dish):
        return False
    if record.get("criteria_passed") != sorted(CRITERIA) or not str(record.get("reviewer_notes", "")).strip():
        return False
    return not automated_failures(dish)


def enforce_review(dishes: list[dict], review: dict) -> list[dict]:
    """Return the dishes with `verified` kept only where a valid review backs it (others become unverified)."""
    result = []
    for dish in dishes:
        if dish.get("allergen_data_status") == "verified" and not is_review_valid(dish, review):
            logger.warning("Dish %s is marked verified without a valid review; treating it as unverified", dish.get("id"))
            dish = {**dish, "allergen_data_status": "unverified"}
        result.append(dish)
    return result


def report() -> list[str]:
    """One line per dish: automated criteria, whether a current review record backs it, and its fingerprint."""
    from services.menu_service import MENU_PATH, load_menu

    review = load_review()
    lines = []
    for dish in load_menu(MENU_PATH):
        failed = automated_failures(dish)
        backed = is_review_valid(dish, review)
        lines.append(
            f"{dish['id']}: automated={'ok' if not failed else 'FAILED ' + ','.join(failed)} "
            f"review_record={'valid' if backed else 'missing/stale'} fingerprint={fingerprint(dish)[:12]}"
        )
    return lines


if __name__ == "__main__":  # python -m services.menu_review
    print("\n".join(report()))
