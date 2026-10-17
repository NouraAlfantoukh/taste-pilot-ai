import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def make_dish():
    """Factory for fictional, well-formed test dishes; override any field per test."""

    def _make(**overrides):
        dish = {
            "id": "test_dish",
            "name_ar": "طبق اختبار",
            "description_ar": "طبق وهمي للاختبار فقط.",
            "category_ar": "اختبار",
            "price_sar": 10,
            "ingredients_ar": ["أرز"],
            "ingredient_ids": ["rice"],
            "allergens": [],
            "allergen_data_status": "verified",
            "is_vegetarian": True,
            "spice_level": "none",
        }
        dish.update(overrides)
        return dish

    return _make


@pytest.fixture
def incomplete_dish():
    with open(FIXTURES / "incomplete_dish.json", encoding="utf-8") as f:
        return json.load(f)
