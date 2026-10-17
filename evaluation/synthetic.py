"""A deterministic larger menu for the scale experiment (docs/DEEP_DIVE.md, "Scale").

The production menu has 15 dishes, small enough for a model to hold in its head. This module adds N synthetic dishes
(clearly marked, ids `syn_NNN`) built by recombining real taxonomy ingredients with a fixed seed, so a model has to
filter a long list instead of a short one. Allergens and the vegetarian flag are DERIVED from the ingredients through the
taxonomy, so every synthetic record is internally consistent (the same consistency the production review enforces).

Synthetic dishes that would make an "impossible" scenario satisfiable are dropped, so those scenarios stay impossible.
They exist only for the experiment and are never loaded by the app.
"""

import random

from services import menu_service, taxonomy

SEED = 20261017
CATEGORY_NAMES = ("الأطباق الرئيسية", "المقبلات", "الحلويات", "المشروبات")
POOL = (
    "basmati_rice", "rice", "chicken", "lamb", "minced_meat", "hammour", "tuna", "beef", "shrimp", "egg", "chickpeas", "lentils",
    "tomato", "onion", "garlic", "carrot", "potato", "peas", "lettuce", "cucumber", "eggplant", "zucchini", "spinach", "okra",
    "olives", "mushroom", "pepper_bell", "parsley", "coriander", "cumin", "cardamom", "cinnamon", "turmeric", "black_pepper",
    "hot_pepper", "lemon", "olive_oil", "ghee", "fresh_milk", "mozzarella", "white_cheese", "akkawi_cheese", "qishta", "tahini",
    "sesame", "almond", "pistachio", "peanut", "dough", "toasted_bread", "kunafa", "honey", "sugar", "mint", "tea",
)
_WORDS = ("طبق", "صحن", "لفّة", "مشكّل", "تشكيلة", "حلوى", "شراب")


def synthetic_dishes(count: int) -> list[dict]:
    rng = random.Random(SEED)
    dishes: list[dict] = []
    attempts = 0
    while len(dishes) < count and attempts < count * 20:
        attempts += 1
        ids = rng.sample(POOL, rng.randint(3, 6))
        allergens = sorted(taxonomy.implied_allergens(ids))
        animal = any(taxonomy.ingredient_closure(i) & {"meat", "poultry", "fish"} for i in ids)
        spicy = "hot_pepper" in ids
        number = len(dishes) + 1
        dish = {
            "id": f"syn_{number:03d}",
            "name_ar": f"{rng.choice(_WORDS)} {number}",
            "description_ar": f"طبق رقم {number} من مطبخنا يقدَّم طازجًا.",
            "category_ar": rng.choice(CATEGORY_NAMES),
            "price_sar": rng.choice((9, 12, 15, 18, 22, 26, 30, 35, 42, 55, 65)),
            "ingredients_ar": [taxonomy.ingredient_name_ar(i) for i in ids],
            "ingredient_ids": ids,
            "allergens": allergens,
            "allergen_data_status": "verified",
            "serving_temperature": rng.choice(("hot", "warm", "cold")),
            "is_vegetarian": not animal,
            "spice_level": rng.choice(("medium", "hot")) if spicy else rng.choice(("none", "none", "none", "mild")),
        }
        if menu_service.dish_problems(dish) or menu_service.consistency_problems(dish):
            continue
        dishes.append(dish)
    return dishes
