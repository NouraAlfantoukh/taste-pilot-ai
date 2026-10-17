"""Pure HTML builders for the chat UI.

Everything interpolated is escaped. Dish names, prices and details are read from validated dish
objects; model-written text (the optional per-dish reason) is escaped like any other text.
HTML is emitted on a single line because Markdown treats blank or indented lines specially.
"""

from html import escape

from agent.orchestrator import OrchestratorResponse, ResponseType
from agent.state import AllergyStatus, ConversationState
from services import taxonomy
from services.auth_service import AuthFailure
from services.menu_service import SPICE_LEVELS, format_price_sar, spice_label_ar

DISCLAIMER_AR = "لأي حساسية غذائية، أبلغ موظف المطعم دائمًا قبل الطلب."
MEMORY_NOTE_AR = "لا يتم حفظ تفضيلاتك بعد إغلاق هذه الجلسة."
MEMORY_SAVED_AR = "الحساسية والمكونات المستبعدة والنظام الغذائي وحدة البهارات محفوظة في حسابك؛ أما الفئة والميزانية فلا تُحفظ."

AUTH_MESSAGES_AR = {
    AuthFailure.INVALID_INPUT: "تأكد من كتابة البريد الإلكتروني وكلمة المرور بشكل صحيح.",
    AuthFailure.INVALID_CREDENTIALS: "البريد الإلكتروني أو كلمة المرور غير صحيحة.",
    AuthFailure.EMAIL_NOT_CONFIRMED: "لم يتم تأكيد بريدك بعد. افتح رسالة التأكيد ثم سجّل الدخول.",
    AuthFailure.ALREADY_REGISTERED: "هذا البريد مسجّل مسبقًا. جرّب تسجيل الدخول.",
    AuthFailure.WEAK_PASSWORD: "كلمة المرور ضعيفة؛ استخدم ٨ أحرف على الأقل.",
    AuthFailure.RATE_LIMITED: "محاولات كثيرة. انتظر قليلًا ثم أعد المحاولة.",
    AuthFailure.SIGNUP_DISABLED: "إنشاء حساب جديد غير متاح حاليًا. جرّب تسجيل الدخول أو تابع كضيف.",
    AuthFailure.EMAIL_NOT_ALLOWED: "لا يمكن استخدام هذا البريد الإلكتروني حاليًا. جرّب بريدًا آخر.",
    AuthFailure.REJECTED: "تعذّر إكمال الطلب. تأكد من بياناتك وحاول مرة أخرى.",
    AuthFailure.SERVER_ERROR: "خدمة الحساب تواجه مشكلة مؤقتة. حاول بعد قليل.",
    AuthFailure.UNAVAILABLE: "تعذّر الاتصال بخدمة الحساب. تحقق من اتصالك وحاول لاحقًا.",
}
SIGNUP_CONFIRM_AR = "تم إنشاء الحساب. أرسلنا رسالة تأكيد إلى بريدك؛ أكّد الحساب ثم سجّل الدخول."
SESSION_EXPIRED_AR = "انتهت جلسة الدخول. سجّل الدخول من جديد لمتابعة حفظ تفضيلاتك."
CONFIG_ERROR_AR = "إعداد خدمة الحساب غير صالح. تواصل مع مسؤول التطبيق."
PROFILE_UNREADABLE_AR = (
    "تعذّرت قراءة تفضيلاتك المحفوظة، فلن أحفظ أي تغيير في هذه الجلسة حتى لا أمسح بياناتك. سأسألك عن الحساسية من جديد."
)
PROFILE_UNAVAILABLE_AR = (
    "تعذّر الوصول إلى تفضيلاتك المحفوظة الآن، فلن تُحفظ تغييرات هذه الجلسة. سأسألك عن الحساسية من جديد."
)
MEMORY_SAVE_FAILED_AR = "تعذّر حفظ آخر تغيير في تفضيلاتك؛ هو مطبّق في هذه المحادثة فقط وسأحاول حفظه مجددًا."
SAVE_FAILED_AR = "تعذّر حفظ تفضيلاتك الآن. تابع كالمعتاد وسأحاول الحفظ مجددًا."
WELCOME_BACK_AR = "أهلًا بعودتك! طبّقتُ تفضيلاتك المحفوظة (الظاهرة أعلاه). قل لي إن تغيّر شيء."
GUEST_NOTE_AR = "أنت تستخدم التطبيق كضيف؛ لن تُحفظ تفضيلاتك."

_LOGO = (
    '<svg class="tp-logo" viewBox="0 0 48 48" role="img" aria-label="TastePilotAI">'
    '<rect width="48" height="48" rx="14" fill="#2F5D3A"/>'
    '<circle cx="24" cy="24" r="12.5" fill="none" stroke="#FAF6EF" stroke-width="2.5"/>'
    '<circle cx="24" cy="24" r="6" fill="#FAF6EF"/>'
    '<path d="M24 5.5l3.2 6.2h-6.4z" fill="#D98E04"/></svg>'
)
_AVATAR = _LOGO.replace('class="tp-logo"', 'class="tp-avatar"')

SUGGESTIONS = (
    "أبغى طبق نباتي ما يتعدى ٣٠ ريال",
    "عندي حساسية من الحليب، وش تنصحني؟",
    "رشّح لي حلى خفيف",
)


def _text(value: object) -> str:
    return escape(str(value), quote=True).replace("\n", "<br>")


def brand_header_html() -> str:
    return (
        '<div class="tp-root tp-brand">' + _LOGO + '<div><div class="tp-brand-name">TastePilotAI</div>'
        '<div class="tp-brand-tag">مساعدك الشخصي في اختيار الطبق</div></div></div>'
    )


def hero_html() -> str:
    points = (
        ("يتعرّف على ذوقك", "قل له ميزانيتك وما تحب وما لا تحب، ويرتّب لك الخيارات."),
        ("يحترم حساسيتك", "يستبعد الأطباق التي تحتوي على المسبّب، أو التي لم يُتحقَّق من بياناتها بعد."),
        ("من القائمة فقط", "لا يخترع أطباقًا ولا أسعارًا؛ كل ما يُعرض مأخوذ من القائمة."),
    )
    cards = "".join(
        f'<div class="tp-point"><div class="tp-point-title">{_text(t)}</div><div class="tp-point-text">{_text(d)}</div></div>'
        for t, d in points
    )
    return (
        '<div class="tp-root tp-hero"><div class="tp-hero-title">وش تبغى تاكل اليوم؟</div>'
        '<p class="tp-hero-lead">أنا TastePilotAI. أخبرني بذوقك وميزانيتك وأي حساسية عندك، وأرشّح لك من القائمة.</p>'
        f'<div class="tp-points">{cards}</div></div>'
    )


def typing_html() -> str:
    return (
        '<div class="tp-root tp-msg tp-assistant">' + _AVATAR +
        '<div class="tp-bubble"><span class="tp-typing" role="status" aria-label="جارٍ التحضير"><i></i><i></i><i></i></span></div></div>'
    )


def user_message_html(text: str) -> str:
    return f'<div class="tp-root tp-msg tp-user"><div class="tp-bubble">{_text(text)}</div></div>'


def _bubble(text: str, kind: str = "") -> str:
    return f'<div class="tp-root tp-msg tp-assistant {kind}">{_AVATAR}<div class="tp-bubble">{_text(text)}</div></div>'


def spice_html(level: str) -> str:
    filled = SPICE_LEVELS.index(level) if level in SPICE_LEVELS else 0
    dots = "".join(f'<i class="{"on" if i < filled else ""}"></i>' for i in range(len(SPICE_LEVELS) - 1))
    return f'<span class="tp-spice" aria-hidden="true">{dots}</span>{_text(spice_label_ar(level))}'


def _fact(label: str, value_html: str) -> str:
    return f'<div class="tp-fact"><span class="tp-fact-label">{_text(label)}</span><span>{value_html}</span></div>'


def dish_card_html(dish: dict, reason: str = "", serving: str = "", level: str = "") -> str:
    facts = [_fact("حدة البهارات", spice_html(dish["spice_level"]))]
    if serving:
        facts.insert(0, _fact("حرارة التقديم", _text(serving)))
    shown = dish["ingredients_ar"][:7]
    more = "…" if len(dish["ingredients_ar"]) > len(shown) else ""
    facts.append(_fact("المكونات", _text("، ".join(shown) + more)))
    allergens = [taxonomy.allergen_name_ar(a) for a in dish["allergens"]]
    source = "موثّقة" if dish.get("allergen_data_status") == "verified" else "حسب بيانات المطعم"
    facts.append(_fact("مسببات الحساسية", _text("، ".join(allergens) if allergens else "لا شيء معلن") + f" ({_text(source)})"))
    badge = " 🌱" if dish["is_vegetarian"] else ""
    alternative = '<span class="tp-chip tp-chip-warn">بديل</span> ' if level == "uncertain" else ""
    reason_html = f'<div class="tp-reason">{_text(reason)}</div>' if reason else ""
    return (
        f'<article class="tp-card{" tp-card-alt" if level == "uncertain" else ""}"><div class="tp-card-head"><div>'
        f'<div class="tp-cat">{alternative}{_text(dish["category_ar"])}{"· نباتي" if dish["is_vegetarian"] else ""}</div>'
        f'<div class="tp-card-title">{_text(dish["name_ar"])}{badge}</div></div>'
        f'<div class="tp-price">{_text(format_price_sar(dish["price_sar"]))}</div></div>'
        f'<p class="tp-desc">{_text(dish["description_ar"])}</p>{reason_html}'
        f'<div class="tp-facts">{"".join(facts)}</div></article>'
    )


def notice_html(text: str, warn: bool = False) -> str:
    return f'<div class="tp-root tp-notice{" tp-notice-warn" if warn else ""}">{_text(text)}</div>'


def recommendations_html(response: OrchestratorResponse) -> str:
    padding = [""] * len(response.recommendations)
    reasons = list(response.reasons) + padding
    serving = list(response.serving_notes) + padding
    levels = list(response.serving_levels) + padding
    cards = "".join(
        dish_card_html(d, reasons[i], serving[i], levels[i]) for i, d in enumerate(response.recommendations)
    )
    html = (
        f'<div class="tp-root tp-msg tp-assistant">{_AVATAR}<div class="tp-bubble tp-lead">{_text(response.lead_ar)}</div></div>'
        f'<div class="tp-root tp-cards">{cards}</div>'
    )
    return html + (notice_html(response.notes_ar) if response.notes_ar else "")


def no_results_html(text: str) -> str:
    return (
        '<div class="tp-root tp-msg tp-assistant">' + _AVATAR +
        f'<div class="tp-callout"><span class="tp-callout-icon">🍽️</span><div>{_text(text)}</div></div></div>'
    )


def assistant_message_html(response: OrchestratorResponse) -> str:
    if response.type is ResponseType.RECOMMENDATIONS:
        return recommendations_html(response)
    if response.type is ResponseType.NO_RESULTS:
        return no_results_html(response.reply_ar)
    if response.type is ResponseType.ERROR:
        return _bubble(response.reply_ar, "tp-error")
    return _bubble(response.reply_ar, "tp-ask")


def preference_labels(state: ConversationState) -> list[tuple[str, str]]:
    """(label, tone) pairs describing what the assistant currently knows; tone is '', 'warn' or 'muted'."""
    c = state.constraints
    labels: list[tuple[str, str]] = []
    if c.categories:
        labels.append(("الفئة: " + " أو ".join(sorted(taxonomy.category_name_ar(k) for k in c.categories)), ""))
    if c.serving_temperature:
        labels.append(("دافئ" if c.serving_temperature == "warm" else "بارد", ""))
    for allergen in sorted(taxonomy.allergen_name_ar(a) for a in c.excluded_allergens):
        labels.append((f"حساسية: {allergen}", "warn"))
    for ingredient in sorted(taxonomy.ingredient_name_ar(i) for i in c.allergy_ingredients):
        labels.append((f"حساسية: {ingredient}", "warn"))
    if state.allergy_status is AllergyStatus.DECLINED and not (c.excluded_allergens or c.allergy_ingredients):
        labels.append(("الحساسية: غير محدّدة", "muted"))
    for ingredient in sorted(taxonomy.ingredient_name_ar(i) for i in c.excluded_ingredients):
        labels.append((f"بدون {ingredient}", ""))
    if c.vegetarian_only:
        labels.append(("نباتي", ""))
    if c.max_price_sar is not None:
        labels.append((f"حتى {format_price_sar(c.max_price_sar)}", ""))
    if c.min_spice_level is not None:
        labels.append((f"حدة البهارات من: {spice_label_ar(c.min_spice_level)}", ""))
    if c.max_spice_level is not None:
        labels.append((f"حدة البهارات حتى: {spice_label_ar(c.max_spice_level)}", ""))
    return labels


def preferences_html(state: ConversationState) -> str:
    labels = preference_labels(state)
    if not labels:
        return ""
    tone = {"warn": " tp-chip-warn", "muted": " tp-chip-muted", "": ""}
    chips = "".join(f'<span class="tp-chip{tone[t]}">{_text(label)}</span>' for label, t in labels)
    return f'<div class="tp-root tp-prefs"><span class="tp-prefs-label">تفضيلاتك:</span>{chips}</div>'


PROFILE_TITLE_AR = "ملفي الغذائي"
UNSPECIFIED_AR = "غير محدّد"
PROFILE_UNSAVED_AR = "في هذه المحادثة تغييرات لم تُحفظ بعد؛ الملف أدناه يعرض ما تم حفظه فقط."
PROFILE_SAVE_FAILED_AR = "تعذّر حفظ آخر تغيير، لذلك لا يظهر أدناه. الملف يعرض ما تم حفظه فقط."
_ALLERGY_STATUS_AR = {
    "not_asked": UNSPECIFIED_AR,
    "declined": "فضّلت عدم الإجابة",
    "none_reported": "ذكرت أنه ما عندك حساسية من الأطعمة",
    "reported": UNSPECIFIED_AR,
}


def _profile_row(label: str, value: str, muted: bool = False) -> str:
    style = ' style="color:var(--tp-muted)"' if muted else ""
    return (
        f'<div class="tp-fact" style="grid-template-columns: 11rem 1fr"><span class="tp-fact-label">{_text(label)}</span>'
        f'<span{style}>{_text(value)}</span></div>'
    )


def profile_html(prefs, unsaved: bool = False, save_failed: bool = False, unavailable: bool = False) -> str:
    """The saved profile only (what the database holds), with every unspecified field shown as such.

    `prefs` is the store-confirmed UserPreferences; it is never built from the conversation, so nothing appears
    here before it was really saved. `unavailable` means the stored profile could not be read at all.
    """
    if unavailable:
        return f'<div class="tp-root tp-notice tp-notice-warn">{_text(PROFILE_UNAVAILABLE_AR)}</div>'
    banner = ""
    if save_failed:
        banner = notice_html(PROFILE_SAVE_FAILED_AR, warn=True)
    elif unsaved:
        banner = notice_html(PROFILE_UNSAVED_AR)
    dislikes = sorted(taxonomy.ingredient_name_ar(i) for i in prefs.excluded_ingredients)
    allergies = sorted(
        [taxonomy.allergen_name_ar(a) for a in prefs.excluded_allergens]
        + [taxonomy.ingredient_name_ar(i) for i in prefs.allergy_ingredients]
    )
    spice = []
    if prefs.min_spice_level:
        spice.append(f"{spice_label_ar(prefs.min_spice_level)} على الأقل")
    if prefs.max_spice_level:
        spice.append("بدون حدة" if prefs.max_spice_level == "none" else f"حتى {spice_label_ar(prefs.max_spice_level)}")
    rows = [
        _profile_row("مكونات لا تحبها أو تستبعدها", "، ".join(dislikes) if dislikes else UNSPECIFIED_AR, not dislikes),
        _profile_row(
            "الحساسية المسجّلة",
            "، ".join(allergies) if allergies else _ALLERGY_STATUS_AR.get(prefs.allergy_status, UNSPECIFIED_AR),
            not allergies,
        ),
        _profile_row("النظام الغذائي", "نباتي" if prefs.vegetarian_only else UNSPECIFIED_AR, not prefs.vegetarian_only),
        _profile_row("حدة البهارات", "، ".join(spice) if spice else UNSPECIFIED_AR, not spice),
    ]
    return banner + f'<div class="tp-root tp-facts">{"".join(rows)}</div>'


def footer_html(persistent: bool = False, save_failed: bool = False) -> str:
    """The memory line never says preferences are saved while the last save failed."""
    memory = MEMORY_NOTE_AR if not persistent else MEMORY_SAVE_FAILED_AR if save_failed else MEMORY_SAVED_AR
    return f'<div class="tp-root tp-foot">{_text(DISCLAIMER_AR)}<br>{_text(memory)}</div>'


def auth_intro_html() -> str:
    return (
        '<div class="tp-root tp-hero"><div class="tp-hero-title">أهلًا بك</div>'
        '<p class="tp-hero-lead">سجّل الدخول لأتذكّر حساسيتك وتفضيلاتك في زياراتك القادمة، '
        'أو تابع كضيف بدون حفظ.</p></div>'
    )


def account_html(email: str) -> str:
    return f'<div class="tp-root tp-brand-tag" dir="ltr" style="text-align:right">{_text(email)}</div>'
