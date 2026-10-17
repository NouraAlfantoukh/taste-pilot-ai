import json

import pytest

from services import taxonomy
from services.taxonomy import (
    AmbiguousTermError,
    TaxonomyError,
    UnknownTermError,
    implied_allergens,
    load_taxonomy,
    matches_ingredient_exclusion,
    normalize_text,
    resolve_allergen,
    resolve_ingredient,
)

REQUIRED_ALLERGENS = {"milk", "gluten", "eggs", "peanuts", "tree_nuts", "fish", "crustaceans", "sesame", "soy"}


def test_normalization_unifies_arabic_variants():
    assert normalize_text("الحَلِيب") == normalize_text("الحليب")
    assert normalize_text("الأسماك") == normalize_text("الاسماك")
    assert normalize_text("  جبن   عكاوي ") == "جبن عكاوي"
    assert normalize_text("كزبرة") == normalize_text("كزبره")


@pytest.mark.parametrize("term", ["حليب", "الحليب", "لبن", "الألبان", "منتجات الألبان", " الحَلِيب ", "milk", "MILK"])
def test_milk_aliases_resolve_to_same_id(term):
    assert resolve_allergen(term) == {"milk"}


@pytest.mark.parametrize("term", ["سمك", "الأسماك", "اسماك", "fish"])
def test_fish_aliases(term):
    assert resolve_allergen(term) == {"fish"}


def test_wheat_resolves_to_gluten_conservatively():
    assert resolve_allergen("القمح") == {"gluten"}


@pytest.mark.parametrize(
    "term,candidates",
    [
        ("مكسرات", {"peanuts", "tree_nuts"}),
        ("المكسرات", {"peanuts", "tree_nuts"}),
        ("فستق", {"peanuts", "tree_nuts"}),
        ("الفستق", {"peanuts", "tree_nuts"}),
        (" مُكَسَّرات ", {"peanuts", "tree_nuts"}),
        ("مأكولات بحرية", {"fish", "crustaceans", "molluscs"}),
    ],
)
def test_broad_or_ambiguous_terms_are_never_auto_expanded(term, candidates):
    with pytest.raises(AmbiguousTermError) as info:
        resolve_allergen(term)
    assert info.value.candidates == candidates


@pytest.mark.parametrize(
    "term,expected",
    [
        ("فستق حلبي", {"tree_nuts"}),
        ("لوز", {"tree_nuts"}),
        ("مكسرات شجرية", {"tree_nuts"}),
        ("فول سوداني", {"peanuts"}),
        ("فستق سوداني", {"peanuts"}),
        ("سمك", {"fish"}),
        ("جمبري", {"crustaceans"}),
    ],
)
def test_specific_terms_resolve_to_their_own_allergen_only(term, expected):
    assert resolve_allergen(term) == expected


def test_ambiguous_term_cannot_also_be_a_regular_alias(tmp_path):
    data = {
        "allergens": {"a": {"name_ar": "س"}, "b": {"name_ar": "ص"}},
        "ambiguous_allergen_terms": [{"terms_ar": ["س"], "candidates": ["a", "b"]}],
        "ingredients": {"x": {"name_ar": "ع"}},
    }
    with pytest.raises(TaxonomyError):
        load_taxonomy(_write(tmp_path, data))


def test_ambiguous_entry_needs_two_known_candidates(tmp_path):
    data = {
        "allergens": {"a": {"name_ar": "س"}},
        "ambiguous_allergen_terms": [{"terms_ar": ["ص"], "candidates": ["a"]}],
        "ingredients": {"x": {"name_ar": "ع"}},
    }
    with pytest.raises(TaxonomyError):
        load_taxonomy(_write(tmp_path, data))


def test_required_allergens_exist():
    assert REQUIRED_ALLERGENS <= set(taxonomy.get_taxonomy().allergen_names)


@pytest.mark.parametrize("term", ["كذا", "", "   ", "حليبية", "بيضاء", "لبنان", None, 5])
def test_unknown_allergen_terms_raise_and_never_match_by_substring(term):
    with pytest.raises(UnknownTermError):
        resolve_allergen(term)


def test_ingredient_family_terms():
    assert resolve_ingredient("جبن") == {"cheese"}
    assert resolve_ingredient("لحم") == {"meat"}
    assert resolve_ingredient("دجاج") == {"chicken"}
    with pytest.raises(UnknownTermError):
        resolve_ingredient("جبنة غريبة")


def test_cheese_family_matches_cheese_varieties_only():
    excluded = resolve_ingredient("جبن")
    for member in ("mozzarella", "white_cheese", "akkawi_cheese"):
        assert matches_ingredient_exclusion([member], excluded)
    assert not matches_ingredient_exclusion(["tomato", "fresh_milk"], excluded)


def test_meat_family_matches_meat_varieties_but_not_poultry_or_fish():
    excluded = resolve_ingredient("لحم")
    assert matches_ingredient_exclusion(["lamb"], excluded)
    assert matches_ingredient_exclusion(["minced_meat"], excluded)
    assert not matches_ingredient_exclusion(["chicken", "hammour"], excluded)


def test_dairy_family_covers_milk_cheese_and_ghee():
    excluded = resolve_ingredient("ألبان")
    assert matches_ingredient_exclusion(["ghee"], excluded)
    assert matches_ingredient_exclusion(["mozzarella"], excluded)
    assert matches_ingredient_exclusion(["qishta"], excluded)


def test_implied_allergens_follow_families():
    assert implied_allergens(["ghee"]) == {"milk"}
    assert implied_allergens(["mozzarella"]) == {"milk"}
    assert implied_allergens(["phyllo_sheets"]) == {"gluten"}
    assert implied_allergens(["kunafa", "pistachio"]) == {"gluten", "tree_nuts"}
    assert implied_allergens(["rice", "tomato"]) == frozenset()


def test_unknown_ingredient_id_raises():
    with pytest.raises(UnknownTermError):
        implied_allergens(["unobtainium"])


def test_display_names_resolve_back_to_their_ids():
    for ingredient_id, name in taxonomy.get_taxonomy().ingredient_names.items():
        assert resolve_ingredient(name) == {ingredient_id}


def _write(tmp_path, data):
    path = tmp_path / "taxonomy.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def test_alias_collision_between_ingredients_is_rejected(tmp_path):
    data = {
        "allergens": {"milk": {"name_ar": "الحليب"}},
        "ingredients": {"a": {"name_ar": "س"}, "b": {"name_ar": "ص", "aliases_ar": ["س"]}},
    }
    with pytest.raises(TaxonomyError):
        load_taxonomy(_write(tmp_path, data))


def test_unknown_allergen_reference_is_rejected(tmp_path):
    data = {
        "allergens": {"milk": {"name_ar": "الحليب"}},
        "ingredients": {"a": {"name_ar": "س", "allergens": ["nope"]}},
    }
    with pytest.raises(TaxonomyError):
        load_taxonomy(_write(tmp_path, data))


def test_family_cycle_is_rejected(tmp_path):
    data = {
        "allergens": {"milk": {"name_ar": "الحليب"}},
        "ingredients": {"a": {"name_ar": "س", "families": ["b"]}, "b": {"name_ar": "ص", "families": ["a"]}},
    }
    with pytest.raises(TaxonomyError):
        load_taxonomy(_write(tmp_path, data))


def test_missing_taxonomy_file_raises(tmp_path):
    with pytest.raises(TaxonomyError):
        load_taxonomy(tmp_path / "missing.json")
