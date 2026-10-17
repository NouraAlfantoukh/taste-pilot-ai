import pytest

from services.memory_service import InMemoryStore


def test_defaults_for_new_user():
    prefs = InMemoryStore().get_preferences("u1")
    assert prefs.user_id == "u1"
    assert prefs.vegetarian_only is False
    assert prefs.excluded_allergens == [] and prefs.allergy_status == "not_asked"


def test_update_preferences():
    store = InMemoryStore()
    store.update_preferences("u1", vegetarian_only=True, max_spice_level="mild")
    prefs = store.get_preferences("u1")
    assert prefs.vegetarian_only is True
    assert prefs.max_spice_level == "mild"


def test_users_are_isolated():
    store = InMemoryStore()
    store.update_preferences("u1", excluded_allergens=["milk"], allergy_status="reported")
    assert store.get_preferences("u2").excluded_allergens == []


def test_separate_stores_do_not_share_state():
    a, b = InMemoryStore(), InMemoryStore()
    a.update_preferences("u1", vegetarian_only=True)
    assert b.get_preferences("u1").vegetarian_only is False


def test_unknown_field_rejected():
    with pytest.raises(ValueError):
        InMemoryStore().update_preferences("u1", favorite_color="blue")


def test_the_user_id_cannot_be_changed_through_an_update():
    with pytest.raises((TypeError, ValueError)):  # user_id is the positional identity argument, never a change
        InMemoryStore().update_preferences("u1", user_id="u2")


@pytest.mark.parametrize(
    "bad",
    [
        {"excluded_allergens": ["not_an_allergen"]},
        {"excluded_allergens": ["حليب"]},
        {"allergy_ingredients": ["unobtainium"]},
        {"excluded_ingredients": "cheese"},
        {"min_spice_level": "volcanic"},
        {"min_spice_level": "hot", "max_spice_level": "mild"},
        {"allergy_status": "awaiting_answer"},
        {"allergy_status": "none_reported", "excluded_allergens": ["milk"]},
        {"vegetarian_only": "yes"},
    ],
)
def test_invalid_updates_are_rejected_and_leave_the_stored_record_untouched(bad):
    store = InMemoryStore()
    store.update_preferences("u1", excluded_allergens=["eggs"], allergy_status="reported")
    with pytest.raises(ValueError):
        store.update_preferences("u1", **bad)
    prefs = store.get_preferences("u1")
    assert (prefs.excluded_allergens, prefs.allergy_status) == (["eggs"], "reported")


def test_id_lists_are_stored_sorted_and_deduplicated():
    store = InMemoryStore()
    store.update_preferences("u1", excluded_allergens=["milk", "eggs", "milk"], allergy_status="reported")
    assert store.get_preferences("u1").excluded_allergens == ["eggs", "milk"]


def test_a_partial_update_keeps_the_other_fields():
    store = InMemoryStore()
    store.update_preferences("u1", excluded_allergens=["milk"], allergy_status="reported")
    store.update_preferences("u1", vegetarian_only=True)
    prefs = store.get_preferences("u1")
    assert (prefs.excluded_allergens, prefs.vegetarian_only) == (["milk"], True)
