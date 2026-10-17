"""Canonical allergen/ingredient IDs with Arabic aliases.

Resolution is exact (after Arabic normalization) against explicit names and aliases; there is no
substring matching. Unknown terms raise UnknownTermError instead of matching nothing, so a bad
allergy term can never silently disable a filter. Terms whose scope is genuinely ambiguous or broad
(e.g. "فستق", "مكسرات") are listed under ambiguous_allergen_terms and raise AmbiguousTermError with
their candidate IDs; they are never expanded automatically. The caller must ask the user.
"""

import json
import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Optional

TAXONOMY_PATH = Path(__file__).resolve().parent.parent / "data" / "taxonomy.json"

_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_DIACRITICS = re.compile("[ؐ-ًؚ-ٰٟۖ-ۭـ]")
_LETTER_MAP = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا", "ى": "ي", "ة": "ه"})


class TaxonomyError(Exception):
    """The taxonomy file itself is malformed."""


class UnknownTermError(ValueError):
    def __init__(self, kind: str, term: object) -> None:
        super().__init__(f"Unknown {kind}: {term!r}")
        self.kind = kind
        self.term = term


def normalize_text(text: str) -> str:
    """Normalize Arabic text: strip diacritics/tatweel, unify alef/ya/ta-marbuta, collapse spaces."""
    text = unicodedata.normalize("NFKC", text)
    text = _DIACRITICS.sub("", text).translate(_LETTER_MAP)
    return " ".join(text.casefold().split())


class AmbiguousTermError(ValueError):
    """A known term whose intended allergen scope is unclear; candidates lists the possibilities."""

    def __init__(self, term: str, candidates: frozenset[str]) -> None:
        super().__init__(f"Ambiguous allergen term: {term!r}")
        self.term = term
        self.candidates = candidates


@dataclass(frozen=True)
class Taxonomy:
    allergen_names: dict[str, str]
    allergen_index: dict[str, frozenset[str]]
    ambiguous_allergens: dict[str, frozenset[str]]
    category_names: dict[str, str]
    category_index: dict[str, str]
    category_group_index: dict[str, frozenset[str]]
    generic_food_terms: frozenset[str]
    new_request_terms: frozenset[str]
    ingredient_names: dict[str, str]
    ingredient_index: dict[str, str]
    ingredient_labels: dict[str, frozenset[str]]
    ingredient_ancestors: dict[str, frozenset[str]]
    ingredient_allergens: dict[str, frozenset[str]]
    compound_ingredients: frozenset[str]


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise TaxonomyError(message)


def _labels(entry_id: str, entry: dict) -> list[str]:
    aliases = entry.get("aliases_ar", [])
    _require(isinstance(aliases, list) and all(isinstance(a, str) and a.strip() for a in aliases),
             f"{entry_id}: aliases_ar must be a list of non-empty strings")
    english = entry.get("aliases_en", [])
    _require(isinstance(english, list) and all(isinstance(a, str) and a.strip() for a in english),
             f"{entry_id}: aliases_en must be a list of non-empty strings")
    _require(isinstance(entry.get("name_ar"), str) and entry["name_ar"].strip(), f"{entry_id}: name_ar required")
    return [entry["name_ar"], *aliases, *english, entry_id]


def _build(raw: object) -> Taxonomy:
    _require(isinstance(raw, dict), "taxonomy root must be an object")
    raw_allergens, raw_ingredients = raw.get("allergens"), raw.get("ingredients")
    _require(isinstance(raw_allergens, dict) and raw_allergens, "'allergens' must be a non-empty object")
    _require(isinstance(raw_ingredients, dict) and raw_ingredients, "'ingredients' must be a non-empty object")

    allergen_names: dict[str, str] = {}
    allergen_index: dict[str, set[str]] = {}
    for allergen_id, entry in raw_allergens.items():
        _require(_ID_RE.match(allergen_id) is not None, f"invalid allergen id: {allergen_id!r}")
        _require(isinstance(entry, dict), f"{allergen_id}: entry must be an object")
        labels = _labels(allergen_id, entry)
        allergen_names[allergen_id] = entry["name_ar"]
        for label in labels:
            allergen_index.setdefault(normalize_text(label), set()).add(allergen_id)

    ambiguous: dict[str, frozenset[str]] = {}
    raw_ambiguous = raw.get("ambiguous_allergen_terms", [])
    _require(isinstance(raw_ambiguous, list), "'ambiguous_allergen_terms' must be a list")
    for entry in raw_ambiguous:
        _require(isinstance(entry, dict), "ambiguous entry must be an object")
        terms, candidates = entry.get("terms_ar"), entry.get("candidates")
        _require(isinstance(terms, list) and terms and all(isinstance(t, str) and t.strip() for t in terms),
                 "ambiguous entry needs terms_ar")
        _require(isinstance(candidates, list) and len(candidates) >= 2 and all(c in allergen_names for c in candidates),
                 "ambiguous entry needs at least two known candidate allergen ids")
        for term in terms:
            key = normalize_text(term)
            _require(key not in allergen_index, f"ambiguous term {term!r} is also a regular alias")
            _require(key not in ambiguous, f"duplicate ambiguous term {term!r}")
            ambiguous[key] = frozenset(candidates)

    category_names: dict[str, str] = {}
    category_index: dict[str, str] = {}
    raw_categories = raw.get("categories", {})
    _require(isinstance(raw_categories, dict), "'categories' must be an object")
    for category_id, entry in raw_categories.items():
        _require(_ID_RE.match(category_id) is not None, f"invalid category id: {category_id!r}")
        _require(isinstance(entry, dict), f"{category_id}: entry must be an object")
        labels = _labels(category_id, entry)
        category_names[category_id] = entry["name_ar"]
        for label in labels:
            owner = category_index.setdefault(normalize_text(label), category_id)
            _require(owner == category_id, f"category alias {label!r} maps to both {owner} and {category_id}")

    group_index: dict[str, frozenset[str]] = {}
    raw_groups = raw.get("category_groups", {})
    _require(isinstance(raw_groups, dict), "'category_groups' must be an object")
    for group_id, entry in raw_groups.items():
        _require(_ID_RE.match(group_id) is not None, f"invalid category group id: {group_id!r}")
        members = entry.get("members") if isinstance(entry, dict) else None
        _require(
            isinstance(members, list) and members and all(m in category_names for m in members),
            f"{group_id}: members must be known categories",
        )
        for label in _labels(group_id, entry):
            key = normalize_text(label)
            _require(key not in category_index and key not in group_index, f"group alias {label!r} is not unique")
            group_index[key] = frozenset(members)

    def term_set(key: str) -> frozenset[str]:
        values = raw.get(key, [])
        _require(isinstance(values, list) and all(isinstance(v, str) and v.strip() for v in values), f"{key!r} must be a list of strings")
        return frozenset(normalize_text(v) for v in values)

    ingredient_names: dict[str, str] = {}
    ingredient_index: dict[str, str] = {}
    ingredient_labels: dict[str, frozenset[str]] = {}
    own_allergens: dict[str, frozenset[str]] = {}
    parents: dict[str, list[str]] = {}
    compound: set[str] = set()
    for ingredient_id, entry in raw_ingredients.items():
        _require(_ID_RE.match(ingredient_id) is not None, f"invalid ingredient id: {ingredient_id!r}")
        _require(isinstance(entry, dict), f"{ingredient_id}: entry must be an object")
        labels = {normalize_text(label) for label in _labels(ingredient_id, entry)}
        for label in sorted(labels):
            owner = ingredient_index.setdefault(label, ingredient_id)
            _require(owner == ingredient_id, f"alias {label!r} maps to both {owner} and {ingredient_id}")
        ingredient_names[ingredient_id] = entry["name_ar"]
        ingredient_labels[ingredient_id] = frozenset(labels)
        allergens = entry.get("allergens", [])
        _require(isinstance(allergens, list) and all(a in allergen_names for a in allergens),
                 f"{ingredient_id}: allergens must reference known allergen ids")
        own_allergens[ingredient_id] = frozenset(allergens)
        families = entry.get("families", [])
        _require(isinstance(families, list), f"{ingredient_id}: families must be a list")
        parents[ingredient_id] = families
        if entry.get("compound") is True:
            compound.add(ingredient_id)

    for ingredient_id, families in parents.items():
        for family in families:
            _require(family in ingredient_names, f"{ingredient_id}: unknown family {family!r}")

    ancestors: dict[str, frozenset[str]] = {}
    for ingredient_id in ingredient_names:
        seen: set[str] = set()
        stack = list(parents[ingredient_id])
        while stack:
            current = stack.pop()
            _require(current != ingredient_id, f"{ingredient_id}: family cycle")
            if current not in seen:
                seen.add(current)
                stack.extend(parents[current])
        ancestors[ingredient_id] = frozenset(seen)

    return Taxonomy(
        allergen_names=allergen_names,
        allergen_index={k: frozenset(v) for k, v in allergen_index.items()},
        ambiguous_allergens=ambiguous,
        category_names=category_names,
        category_index=category_index,
        category_group_index=group_index,
        generic_food_terms=term_set("generic_food_terms"),
        new_request_terms=term_set("new_request_terms"),
        ingredient_names=ingredient_names,
        ingredient_index=ingredient_index,
        ingredient_labels=ingredient_labels,
        ingredient_ancestors=ancestors,
        ingredient_allergens=own_allergens,
        compound_ingredients=frozenset(compound),
    )


def load_taxonomy(path: Optional[Path] = None) -> Taxonomy:
    try:
        with open(path or TAXONOMY_PATH, encoding="utf-8") as f:
            return _build(json.load(f))
    except (OSError, json.JSONDecodeError) as exc:
        raise TaxonomyError(f"cannot read taxonomy: {exc}") from exc


@lru_cache(maxsize=1)
def get_taxonomy() -> Taxonomy:
    return load_taxonomy()


def resolve_allergen(term: str) -> frozenset[str]:
    """Map an unambiguous Arabic allergen term to canonical IDs.

    Raises AmbiguousTermError for broad/ambiguous terms and UnknownTermError for unknown ones.
    """
    if isinstance(term, str):
        key = normalize_text(term)
        taxonomy = get_taxonomy()
        if key in taxonomy.ambiguous_allergens:
            raise AmbiguousTermError(term, taxonomy.ambiguous_allergens[key])
        ids = taxonomy.allergen_index.get(key)
        if ids:
            return ids
    raise UnknownTermError("allergen", term)


def resolve_ingredient(term: str) -> frozenset[str]:
    """Map a term to a canonical ingredient or family ID; raises UnknownTermError if unknown."""
    if isinstance(term, str):
        ingredient_id = get_taxonomy().ingredient_index.get(normalize_text(term))
        if ingredient_id:
            return frozenset({ingredient_id})
    raise UnknownTermError("ingredient", term)


def is_known_allergen(allergen_id: object) -> bool:
    return isinstance(allergen_id, str) and allergen_id in get_taxonomy().allergen_names


def is_known_ingredient(ingredient_id: object) -> bool:
    return isinstance(ingredient_id, str) and ingredient_id in get_taxonomy().ingredient_names


def allergen_name_ar(allergen_id: str) -> str:
    if not is_known_allergen(allergen_id):
        raise UnknownTermError("allergen id", allergen_id)
    return get_taxonomy().allergen_names[allergen_id]


def ingredient_name_ar(ingredient_id: str) -> str:
    if not is_known_ingredient(ingredient_id):
        raise UnknownTermError("ingredient id", ingredient_id)
    return get_taxonomy().ingredient_names[ingredient_id]


def ingredient_label_matches(ingredient_id: str, display_name: str) -> bool:
    """True if display_name is the name or an alias of the given ingredient."""
    if not is_known_ingredient(ingredient_id) or not isinstance(display_name, str):
        return False
    return normalize_text(display_name) in get_taxonomy().ingredient_labels[ingredient_id]


def is_compound(ingredient_id: str) -> bool:
    return ingredient_id in get_taxonomy().compound_ingredients


def ingredient_closure(ingredient_id: str) -> frozenset[str]:
    """The ingredient plus all of its families (transitively)."""
    if not is_known_ingredient(ingredient_id):
        raise UnknownTermError("ingredient id", ingredient_id)
    return frozenset({ingredient_id}) | get_taxonomy().ingredient_ancestors[ingredient_id]


def implied_allergens(ingredient_ids: Iterable[str]) -> frozenset[str]:
    """Allergens implied by the ingredients' taxonomy entries (and their families).

    Covers only what the taxonomy knows; contents of compound ingredients are not inferred.
    """
    taxonomy = get_taxonomy()
    implied: set[str] = set()
    for ingredient_id in ingredient_ids:
        for member in ingredient_closure(ingredient_id):
            implied |= taxonomy.ingredient_allergens[member]
    return frozenset(implied)


def matches_ingredient_exclusion(ingredient_ids: Iterable[str], excluded_ids: Iterable[str]) -> bool:
    """True if any ingredient (or one of its families) is in the excluded set."""
    excluded = set(excluded_ids)
    return any(ingredient_closure(i) & excluded for i in ingredient_ids)


def resolve_allergy_term(term: str) -> tuple[frozenset[str], frozenset[str]]:
    """Resolve a term the user gave as an allergy into (allergen category ids, ingredient ids).

    A known allergen category wins. Otherwise a recognized ingredient (or ingredient family) is accepted
    as an ingredient-level allergy, so it need not be a top-level allergen. Ambiguous category terms
    raise AmbiguousTermError; unrecognized terms raise UnknownTermError.
    """
    try:
        return resolve_allergen(term), frozenset()
    except UnknownTermError:
        return frozenset(), resolve_ingredient(term)


def resolve_category(term: str) -> str:
    """Map a user term (e.g. حلا, تحلية) to a canonical menu category id; raises UnknownTermError if unknown."""
    if isinstance(term, str):
        category_id = get_taxonomy().category_index.get(normalize_text(term))
        if category_id:
            return category_id
    raise UnknownTermError("category", term)


def is_known_category(category_id: object) -> bool:
    return isinstance(category_id, str) and category_id in get_taxonomy().category_names


def category_name_ar(category_id: str) -> str:
    if not is_known_category(category_id):
        raise UnknownTermError("category id", category_id)
    return get_taxonomy().category_names[category_id]


def dish_category_matches(dish: dict, category_ids: Iterable[str]) -> bool:
    """True if the dish's category_ar is one of the given canonical categories."""
    dish_category = dish.get("category_ar")
    if not isinstance(dish_category, str):
        return False
    wanted = {normalize_text(category_name_ar(c)) for c in category_ids}
    return normalize_text(dish_category) in wanted


def resolve_categories(term: str) -> frozenset[str]:
    """Resolve a category word, or a group word such as "مالح" (main dishes and appetizers), to category ids."""
    if isinstance(term, str):
        key = normalize_text(term)
        taxonomy = get_taxonomy()
        if key in taxonomy.category_group_index:
            return taxonomy.category_group_index[key]
        if key in taxonomy.category_index:
            return frozenset({taxonomy.category_index[key]})
    raise UnknownTermError("category", term)


def is_generic_food_term(term: object) -> bool:
    """True for broad words like "أكل" or "وجبة": a request for food in general, not a menu category."""
    return isinstance(term, str) and normalize_text(term) in get_taxonomy().generic_food_terms


def has_new_request_intent(text: str) -> bool:
    """True if the message asks for food in general ("أبي أكل ..."), which starts a fresh request."""
    return bool(set(normalize_text(text).split()) & get_taxonomy().new_request_terms)
