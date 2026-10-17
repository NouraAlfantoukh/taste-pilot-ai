"""Deterministic, personalized explanations for recommendations.

Everything here is built from the guest's constraints and from the recorded menu data of dishes the
recommendation engine already approved. No model text is involved, so nothing can be invented: a claim such as
"not spicy" is made only when the dish's recorded spice level supports it, and nothing is ever said about
safety or about an allergen being absent. The wording is Gulf Arabic and deliberately short; ingredients are not
repeated because the dish card already lists them.
"""

from services import taxonomy
from services.constraints import Constraints
from services.menu_service import format_price_sar, spice_label_ar

DEFAULT_INTRO_AR = "رشّحت لك من القائمة:"
UNVERIFIED_CLAUSE_AR = "وبيانات الحساسية لبعض هذه الأطباق غير موثّقة، فلا أقدر أؤكد سلامتها."


def _join(parts: list[str]) -> str:
    return parts[0] if len(parts) == 1 else "، ".join(parts[:-1]) + " و" + parts[-1]


def intro_ar(constraints: Constraints, requested: list[str] | None = None) -> str:
    """Why these dishes: the saved-style preferences and the current request, as one short sentence."""
    c = constraints
    preferences: list[str] = []
    dislikes = sorted(taxonomy.ingredient_name_ar(i) for i in c.excluded_ingredients)
    if dislikes:
        preferences.append(f"ما تحب {_join(dislikes)}")
    if c.vegetarian_only:
        preferences.append("تفضّل الأكل النباتي")
    allergies = sorted(
        [taxonomy.allergen_name_ar(a) for a in c.excluded_allergens]
        + [taxonomy.ingredient_name_ar(i) for i in c.allergy_ingredients]
    )
    if allergies:
        preferences.append(f"عندك حساسية من {_join(allergies)}")
    if c.min_spice_level is not None:
        preferences.append(f"تبغى شي {spice_label_ar(c.min_spice_level)} على الأقل")
    if c.max_spice_level is not None:
        preferences.append("ما تبغى حار" if c.max_spice_level == "none" else f"ما تبغى أحر من ({spice_label_ar(c.max_spice_level)})")
    request: list[str] = []
    if c.categories:
        request.append(" أو ".join(taxonomy.category_name_ar(k) for k in sorted(c.categories)))
    if c.max_price_sar is not None:
        request.append(f"حتى {format_price_sar(c.max_price_sar)}")
    if c.serving_temperature:
        request.append("دافئ" if c.serving_temperature == "warm" else "بارد")
    if requested:
        request.append(f"فيها {_join(requested)}")
    if not preferences and not request:
        return DEFAULT_INTRO_AR
    text = "رشّحت لك هذي الأطباق"
    if preferences:
        text += "، وراعيت إنك " + preferences[0] + "".join(f"، و{p}" for p in preferences[1:])
    if request:
        text += f"، وبحسب طلبك الحالي ({'، '.join(request)})"
    return text + ":"


def dish_reason_ar(dish: dict, constraints: Constraints) -> str:
    """Why this dish fits, from its recorded metadata and the constraints in force; '' when nothing specific applies."""
    c = constraints
    reasons: list[str] = []
    if c.vegetarian_only and dish.get("is_vegetarian") is True:
        reasons.append("نباتي مثل ما تفضّل")
    level = dish.get("spice_level")
    if level in ("none", "mild", "medium", "hot"):
        if c.max_spice_level is not None:
            reasons.append("غير حار حسب بيانات القائمة" if level == "none" else f"حدّته ({spice_label_ar(level)}) ضمن اللي تحبه")
        elif c.min_spice_level is not None:
            reasons.append(f"{spice_label_ar(level)} مثل ما تبغى")
    price = dish.get("price_sar")
    if c.max_price_sar is not None and isinstance(price, (int, float)) and price <= c.max_price_sar:
        reasons.append("ضمن ميزانيتك")
    if c.categories and dish.get("category_ar"):
        reasons.append(f"من فئة {dish['category_ar']}")
    return "، ".join(reasons)


def any_unverified(dishes) -> bool:
    return any(d.get("allergen_data_status") != "verified" for d in dishes)
