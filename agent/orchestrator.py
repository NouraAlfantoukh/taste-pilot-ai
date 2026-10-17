"""Conversation orchestration: extract -> validate/merge constraints -> filter -> respond.

The LLM interprets the user's language (extraction) and picks among dishes (generation). All
constraint validation and every safety decision happen in Python; the generation LLM only ever sees
dishes that survived filtering, is not called when none remain, and its output is checked by
agent.guard before anything is shown.
"""

import copy
import logging
from dataclasses import dataclass, field, fields, replace
from enum import Enum
from typing import Callable, Optional

from agent.extraction import Extraction, ExtractionError, extract
from agent.explain import UNVERIFIED_CLAUSE_AR, any_unverified, dish_reason_ar, intro_ar
from agent.guard import RECOMMENDATION_INTRO_AR, render_recommendations
from agent.prompts import SYSTEM_PROMPT_AR, build_menu_context
from agent.temperature import assess_temperatures
from agent.state import (
    MAX_ALLERGY_PROMPTS,
    AllergyStatus,
    ClarificationKind,
    ConversationState,
    PendingClarification,
    PendingTerm,
)
from services import taxonomy
from services.constraints import Constraints, ExclusionReason, InvalidConstraintError
from services.llm_service import (
    ProviderError,
    ProviderNotConfiguredError,
    RecoverableProviderError,
    generate_with_fallback,
)
from services.menu_service import SPICE_LEVELS, format_price_sar, get_all_dishes, spice_label_ar
from services.recommendation_service import (
    CONFIRMED,
    UNCERTAIN,
    FilterResult,
    TemperatureJudgement,
    filter_dishes,
    recommend,
)

logger = logging.getLogger(__name__)

MAX_MESSAGE_CHARS = 1000

ALLERGY_QUESTION_AR = "قبل نبدأ، هل عندك أي حساسية من الأطعمة؟ إذا إيه، وش هي؟"
ALLERGY_CLARIFY_AR = (
    "ما قدرت أفهم إجابتك. هل عندك حساسية من أي طعام؟ اكتب «ما عندي» أو حدد نوعها، "
    "وإذا تفضّل ما تجاوب اكتب «ما أبغى أجاوب»."
)
ALLERGEN_EXAMPLES_AR = "الحليب، البيض، الفول السوداني، المكسرات الشجرية، الغلوتين"
ALLERGEN_DETAIL_AR = f"ذكرت إن عندك حساسية، بس وش نوعها بالضبط؟ (مثلًا: {ALLERGEN_EXAMPLES_AR})"
ASK_REQUEST_AR = "تمام! وش تبغى أرشّح لك؟ تقدر تذكر ميزانيتك، أو إذا تفضّل أكل نباتي، أو درجة حدة البهارات المناسبة."
EMPTY_MESSAGE_AR = "اكتب رسالتك وأنا أساعدك."

NO_VERIFIED_OPTION_MESSAGE_AR = (
    "لم أجد طبقًا مناسبًا بياناته موثّقة وفق القيود التي ذكرتها، ولا أستطيع التأكد من سلامة الأطباق "
    "ذات البيانات غير المكتملة، لذلك لا أقترحها. يُرجى سؤال موظف المطعم مباشرة عن مكوّنات الأطباق "
    "وإبلاغه بحساسيتك."
)
NO_MATCHING_DISH_MESSAGE_AR = "لم أجد طبقًا يطابق طلبك في القائمة الحالية. جرّب تعديل الميزانية أو التفضيلات."
TEMPERATURE_UNAVAILABLE_AR = (
    "ما لقيت في القائمة طبقًا {wanted} ضمن بقية شروطك. تبغى أرشّح لك {what} بدون شرط الحرارة؟"
)
ALTERNATIVES_LEAD_AR = "ما لقيت طبقًا {wanted} بثقة ضمن شروطك؛ هذه أقرب البدائل (حرارة تقديمها غير مؤكدة):"
ALTERNATIVES_NOTE_AR = "ملاحظة: الأطباق الموسومة «بديل» لم أستطع تأكيد أنها {wanted}."

ALLERGEN_FOOTER_AR = "للأمان، أبلغ موظف المطعم بحساسيتك؛ هذه الترشيحات مبنية على بيانات القائمة فقط."
UNKNOWN_ALLERGY_FOOTER_AR = "ما حدّدت حساسية معينة، فإذا عندك حساسية أخبرني أو اسأل موظف المطعم."

PROFILE_UNAVAILABLE_AR = "ما قدرت أقرأ تفضيلاتك المحفوظة الآن، فما أقدر أؤكد لك ما سجّلته سابقًا. تقدر تذكرها لي من جديد."
REQUESTED_NOT_ON_MENU_AR = "ما لقيت في القائمة طبقًا فيه {names} حسب مكوّنات الأطباق المسجّلة."
REQUESTED_IS_DISLIKE_AR = " وهو أيضًا من الأطعمة اللي ذكرت إنك ما تحبها."
REQUESTED_OFFER_OTHERS_AR = " تبغى أرشّح لك أطباق ثانية؟"
REQUESTED_ALLERGY_AR = "ما أقدر أرشّح أطباقًا فيها {names} لأنك ذكرت حساسية منه."
OVERRIDE_QUESTION_AR = (
    "ذكرت قبل إنك ما تحب {names}، وفي القائمة أطباق فيه. تبغى أتجاوز هذا التفضيل لهذا الطلب فقط؟ "
    "(تفضيلك المحفوظ ما راح يتغير)"
)
NOTHING_RECORDED_AR = "لم أسجّل لك أي تفضيلات أو أطعمة تتجنبها حتى الآن."
NOT_ON_MENU_AR = (
    "ما لقيت {terms} في مكونات أي طبق بالقائمة (حسب بياناتها)، فما راح أقترح عليك طبقًا فيه. "
    "لكن ما أقدر أحفظه كتفضيل دائم لأنه خارج المكونات التي أتعرّف عليها."
)

MISUNDERSTOOD_AR = "ما قدرت أفهم رسالتك بشكل سليم. ممكن تعيد صياغتها بكلمات أبسط؟"
SERVICE_BUSY_AR = "الخدمة مشغولة حاليًا. حاول مرة ثانية بعد قليل."
NOT_CONFIGURED_AR = "خدمة الذكاء الاصطناعي غير مهيأة حاليًا."
GENERIC_ERROR_AR = "صار خلل تقني أثناء معالجة طلبك. حاول مرة ثانية."
MENU_UNAVAILABLE_AR = "القائمة غير متاحة حاليًا. حاول مرة ثانية لاحقًا."


class ResponseType(str, Enum):
    CLARIFICATION = "clarification"
    RECOMMENDATIONS = "recommendations"
    NO_RESULTS = "no_results"
    ERROR = "error"
    INFO = "info"  # a factual answer about the guest's own recorded preferences


@dataclass(frozen=True)
class OrchestratorResponse:
    type: ResponseType
    reply_ar: str  # complete plain-text reply
    recommendations: tuple[dict, ...] = ()  # validated dish objects (copies of menu data)
    reasons: tuple[str, ...] = ()  # guard-approved short reason per dish ("" if none); aligned with recommendations
    lead_ar: str = ""  # intro line for recommendations
    notes_ar: str = ""  # exclusion note and safety footer, kept separate from the dishes
    serving_notes: tuple[str, ...] = ()  # per dish: how well it fits the requested serving temperature; aligned with recommendations
    serving_levels: tuple[str, ...] = ()  # per dish: "confirmed", "likely" or "uncertain" (an alternative); aligned too


@dataclass(frozen=True)
class Composed:
    text: str
    dishes: tuple[dict, ...] = ()
    reasons: tuple[str, ...] = ()


def default_generate(system_prompt: str, user_message: str) -> str:
    """Production LLM entry point: Claude first, Gemini only on recoverable errors."""
    return generate_with_fallback(system_prompt, user_message).text


def _join_ar(names: list[str], conjunction: str = "و") -> str:
    return names[0] if len(names) == 1 else "، ".join(names[:-1]) + f" {conjunction}" + names[-1]


def exclusion_note_ar(result: FilterResult) -> str:
    """Brief, honest note about allergy-related exclusions. Names only allergens that caused one."""
    confirmed: set[str] = set()
    incomplete = False
    for exclusion in result.excluded:
        confirmed |= {taxonomy.allergen_name_ar(a) for a in exclusion.confirmed_allergens}
        confirmed |= {taxonomy.ingredient_name_ar(i) for i in exclusion.confirmed_allergy_ingredients}
        incomplete = incomplete or ExclusionReason.INCOMPLETE_ALLERGEN_DATA in exclusion.reasons
    parts = []
    if confirmed:
        names = _join_ar(sorted(confirmed))
        parts.append(f"لأنها تحتوي على {names}")
    if incomplete:
        parts.append("لأن بيانات مسببات الحساسية فيها غير مكتملة")
    return "ملاحظة: استبعدت بعض الخيارات " + " أو ".join(parts) + "." if parts else ""


def constraint_labels_ar(constraints: Constraints) -> list[str]:
    """Plain-Arabic summary of the non-allergy constraints in force (never exposes internal ids)."""
    c = constraints
    labels = []
    if c.categories:
        labels.append("الفئة: " + " أو ".join(taxonomy.category_name_ar(k) for k in sorted(c.categories)))
    if c.serving_temperature:
        labels.append("دافئ" if c.serving_temperature == "warm" else "بارد")
    if c.max_price_sar is not None:
        labels.append(f"حتى {format_price_sar(c.max_price_sar)}")
    labels += [f"بدون {taxonomy.ingredient_name_ar(i)}" for i in sorted(c.excluded_ingredients)]
    if c.vegetarian_only:
        labels.append("نباتي")
    if c.min_spice_level is not None:
        labels.append(f"حدة البهارات من {spice_label_ar(c.min_spice_level)} فأكثر")
    if c.max_spice_level is not None:
        labels.append(f"حدة البهارات حتى {spice_label_ar(c.max_spice_level)}")
    return labels


def temperature_is_the_blocker(constraints: Constraints, result: FilterResult) -> bool:
    """True when dishes fit every hard constraint and were all dropped as confidently the wrong temperature."""
    return (
        constraints.serving_temperature is not None
        and not result.eligible
        and any(e.reasons == (ExclusionReason.WRONG_TEMPERATURE,) for e in result.excluded)
    )


def no_match_message_ar(constraints: Constraints, result: FilterResult) -> str:
    """Arabic explanation when nothing satisfies every constraint; never substitutes another dish."""
    if temperature_is_the_blocker(constraints, result):
        wanted = "دافئ" if constraints.serving_temperature == "warm" else "بارد"
        what = " أو ".join(taxonomy.category_name_ar(k) for k in sorted(constraints.categories)) or "أطباقًا"
        return TEMPERATURE_UNAVAILABLE_AR.format(wanted=wanted, what=what)
    labels = constraint_labels_ar(constraints)
    allergies = sorted(
        [taxonomy.allergen_name_ar(a) for a in constraints.excluded_allergens]
        + [taxonomy.ingredient_name_ar(i) for i in constraints.allergy_ingredients]
    )
    if allergies:  # an allergy is named as the reason and is never offered for relaxing
        text = f"ما لقيت طبقًا يناسب حساسيتك من {_join_ar(allergies)}"
        text += f" مع شروطك الثانية ({'، '.join(labels)})." if labels else "."
        text += " ما راح أقترح طبقًا يخالف حساسيتك."
        return text + (" تبغى أخفف الشروط الثانية؟" if labels else "")
    if labels:
        return f"ما لقيت طبقًا يطابق كل شروطك ({'، '.join(labels)}). تبغى أخفف أحد الشروط؟"
    return NO_MATCHING_DISH_MESSAGE_AR


def compose_reply(
    result: FilterResult,
    user_message: str,
    generate: Callable[[str, str], str],
    menu: Optional[list[dict]] = None,
    constraints: Optional[Constraints] = None,
) -> Composed:
    """Build the recommendation text from eligible dishes only; the LLM's output is validated by the guard.

    With no eligible dish this answers in Arabic without calling the LLM.
    """
    if not result.eligible:
        # "Allergen data not verified" is the reason only when that is why EVERY dish was excluded. A saved dislike,
        # a confirmed allergen, the budget or the category are named for what they are.
        if result.excluded and all(ExclusionReason.INCOMPLETE_ALLERGEN_DATA in e.reasons for e in result.excluded):
            return Composed(NO_VERIFIED_OPTION_MESSAGE_AR)
        return Composed(no_match_message_ar(constraints, result) if constraints else NO_MATCHING_DISH_MESSAGE_AR)
    applied = constraint_labels_ar(constraints) if constraints else []
    conditions = f"\nشروط الضيف المطبّقة مسبقًا على الأطباق أعلاه: {'، '.join(applied)}" if applied else ""
    context = build_menu_context(result.eligible, result.temperature_fit or None)
    raw = generate(SYSTEM_PROMPT_AR, f"{context}\n\nطلب الضيف: {user_message}{conditions}")
    explain = (lambda dish: dish_reason_ar(dish, constraints)) if constraints else None
    rendered = render_recommendations(result.eligible, raw, menu or [], result.temperature_fit or None, explain)
    return Composed(rendered.text, tuple(rendered.dishes), tuple(rendered.reasons))


# ---- state update (pure: works on a copy, committed only if the whole turn is valid) ----

@dataclass
class _Flags:
    unresolved_ingredients: list[str] = field(default_factory=list)
    unresolved_requested: list[str] = field(default_factory=list)
    unresolved_dislikes: list[str] = field(default_factory=list)  # the subset that were stated as "don't want"
    unresolved_categories: list[str] = field(default_factory=list)
    allergy_clarification: bool = False
    ask_initial_question: bool = False
    correction_unclear: bool = False
    removed_names: list[str] = field(default_factory=list)
    understood_allergy_ingredients: list[str] = field(default_factory=list)
    understood_dislikes: list[str] = field(default_factory=list)  # newly excluded ingredients, by Arabic name


def _resolve_allergy_terms(terms: tuple[str, ...]) -> tuple[set[str], set[str], list[PendingTerm]]:
    """Split allergy terms into allergen categories, recognized-ingredient allergies, and unresolved terms."""
    categories: set[str] = set()
    ingredients: set[str] = set()
    unresolved: list[PendingTerm] = []
    for term in terms:
        try:
            found_categories, found_ingredients = taxonomy.resolve_allergy_term(term)
            categories |= found_categories
            ingredients |= found_ingredients
        except taxonomy.UnknownTermError:
            unresolved.append(PendingTerm(term))
        except taxonomy.AmbiguousTermError as exc:
            unresolved.append(PendingTerm(term, tuple(sorted(exc.candidates))))
    return categories, ingredients, unresolved


def _resolve_ingredients(terms: tuple[str, ...]) -> tuple[set[str], list[str]]:
    ids: set[str] = set()
    unresolved: list[str] = []
    for term in terms:
        try:
            ids |= taxonomy.resolve_ingredient(term)
        except taxonomy.UnknownTermError:
            unresolved.append(term)
    return ids, unresolved


def _dedupe(terms: list[PendingTerm]) -> tuple[PendingTerm, ...]:
    seen: set[str] = set()
    result = []
    for term in terms:
        if term.text not in seen:
            seen.add(term.text)
            result.append(term)
    return tuple(result)


def _update_state(old: ConversationState, ex: Extraction, text: str = "") -> tuple[ConversationState, _Flags]:
    """Merge an extraction into a copy of the state. Raises InvalidConstraintError on bad values."""
    s = copy.deepcopy(old)
    c = old.constraints
    flags = _Flags()

    add_a, add_ai, unresolved = _resolve_allergy_terms(ex.allergens)
    rem_a, rem_ai, unresolved_removals = _resolve_allergy_terms(ex.remove_allergens)
    add_i, bad_i = _resolve_ingredients(ex.ingredients)
    rem_i, bad_ri = _resolve_ingredients(ex.remove_ingredients)
    flags.unresolved_ingredients = bad_i + bad_ri
    flags.unresolved_dislikes = list(bad_i)
    add_req, bad_req = _resolve_ingredients(ex.requested_ingredients)
    flags.unresolved_requested = bad_req
    lifting = old.pending is not None and old.pending.kind is ClarificationKind.OVERRIDE_DISLIKE
    if add_req:
        s.requested_ingredients, s.override_ingredients = frozenset(add_req), frozenset()
    elif lifting and ex.confirm_override:
        s.requested_ingredients, s.override_ingredients = old.requested_ingredients, old.requested_ingredients
    elif old.requested_ingredients and (
        old.allergy_status is AllergyStatus.AWAITING_ANSWER or (old.pending is not None and old.pending.blocking)
    ):
        # The first message asked for a food ("وجبة بحرية") and the assistant had to ask about allergies first: the
        # request must still be applied when the guest answers, exactly as a requested category already is.
        s.requested_ingredients, s.override_ingredients = old.requested_ingredients, frozenset()
    else:  # per-request: anything else (including declining the override) drops it
        s.requested_ingredients, s.override_ingredients = frozenset(), frozenset()
    # (foods named through the category field are added to s.requested_ingredients further below)
    flags.understood_dislikes = sorted(taxonomy.ingredient_name_ar(i) for i in add_i - c.excluded_ingredients)
    new_categories: set[str] = set()
    generic_seen = False
    for term in ex.categories:
        if taxonomy.is_generic_food_term(term):
            generic_seen = True  # "أكل" / "وجبة": broad intent, never a literal category
            continue
        try:
            new_categories |= taxonomy.resolve_categories(term)
        except taxonomy.UnknownTermError:
            try:  # not a menu category, but a food ("وجبة بحرية"): the guest is asking for that food
                foods = taxonomy.resolve_ingredient(term)
            except taxonomy.UnknownTermError:
                flags.unresolved_categories.append(term)
            else:
                s.requested_ingredients = frozenset(s.requested_ingredients | foods)
    # A fresh request for food in general ("أبي أكل ...") must not inherit the previous request's category.
    # Deterministic on purpose: it does not depend on the model remembering to emit clear=["category"].
    allergy_turn = old.allergy_status is AllergyStatus.AWAITING_ANSWER or ex.allergy_answer is not None or bool(add_a or add_ai)
    broad_request = ex.wants_recommendations and not allergy_turn and (generic_seen or taxonomy.has_new_request_intent(text))
    if new_categories:
        categories = frozenset(new_categories)
    elif flags.unresolved_categories or broad_request or "category" in ex.clear:
        categories = frozenset()  # an unknown term must not leave the old category silently active either
    else:
        categories = c.categories

    # Allergies are only ever removed by an explicit, resolvable correction that does not contradict
    # an allergy reported in the same message.
    conflicting = (add_a & rem_a) | (add_ai & rem_ai)
    removal_a, removal_ai = rem_a - add_a, rem_ai - add_ai
    removed_a, removed_ai = removal_a & c.excluded_allergens, removal_ai & c.allergy_ingredients
    flags.removed_names = sorted(taxonomy.allergen_name_ar(a) for a in removed_a) + sorted(
        taxonomy.ingredient_name_ar(i) for i in removed_ai
    )
    flags.understood_allergy_ingredients = sorted(
        taxonomy.ingredient_name_ar(i) for i in add_ai - c.allergy_ingredients
    )

    price = ex.max_price_sar
    if price is None and "budget" not in ex.clear:
        price = c.max_price_sar
    keep_spice = "spice" not in ex.clear
    min_spice = ex.min_spice_level or (c.min_spice_level if keep_spice else None)
    max_spice = ex.max_spice_level or (c.max_spice_level if keep_spice else None)
    # A newly stated bound replaces an older bound that it contradicts (e.g. "no spicy" then "I want spicy").
    if ex.min_spice_level and not ex.max_spice_level and max_spice and SPICE_LEVELS.index(max_spice) < SPICE_LEVELS.index(min_spice):
        max_spice = None
    if ex.max_spice_level and not ex.min_spice_level and min_spice and SPICE_LEVELS.index(min_spice) > SPICE_LEVELS.index(max_spice):
        min_spice = None
    s.constraints = replace(
        c,
        excluded_allergens=(c.excluded_allergens | add_a) - removal_a,
        excluded_ingredients=(c.excluded_ingredients | add_i) - rem_i,
        allergy_ingredients=(c.allergy_ingredients | add_ai) - removal_ai,
        vegetarian_only=c.vegetarian_only if ex.vegetarian is None else ex.vegetarian,
        max_price_sar=price,
        max_spice_level=max_spice,
        min_spice_level=min_spice,
        # A newly requested category or temperature replaces the old one; it is not cumulative.
        categories=categories,
        serving_temperature=ex.serving_temperature or (
            None if "temperature" in ex.clear else c.serving_temperature
        ),
    )
    recorded = bool(s.constraints.excluded_allergens or s.constraints.allergy_ingredients)
    new_allergy = bool(add_a or add_ai)

    awaiting = old.allergy_status is AllergyStatus.AWAITING_ANSWER
    prev_blocking = old.pending is not None and old.pending.blocking
    answer = ex.allergy_answer
    reported = new_allergy or bool(unresolved) or ex.unclear_allergy or (awaiting and answer == "has")
    detail_needed = bool(unresolved) or ex.unclear_allergy or (awaiting and answer == "has" and not recorded)
    retracted = (awaiting or prev_blocking) and answer == "none"

    # A bare "I have no allergies" cannot silently delete recorded allergies; a conflicting or
    # unresolvable correction cannot either. Keep them and ask.
    general_none = answer == "none" and not awaiting and not prev_blocking and not ex.remove_allergens and not new_allergy
    flags.correction_unclear = recorded and (general_none or bool(conflicting) or bool(unresolved_removals))

    if reported:
        s.allergy_status = AllergyStatus.REPORTED
    elif retracted:
        if not recorded:
            s.allergy_status = AllergyStatus.NONE_REPORTED
    elif awaiting and answer == "declined":
        s.allergy_status = AllergyStatus.DECLINED
    elif awaiting:
        if old.allergy_prompts < MAX_ALLERGY_PROMPTS:
            flags.allergy_clarification = True
            s.allergy_prompts = old.allergy_prompts + 1
        else:
            s.allergy_status = AllergyStatus.DECLINED
    elif general_none and not recorded and old.allergy_status is AllergyStatus.DECLINED:
        s.allergy_status = AllergyStatus.NONE_REPORTED
    if (removed_a or removed_ai) and not recorded and s.allergy_status is AllergyStatus.REPORTED:
        s.allergy_status = AllergyStatus.NONE_REPORTED

    # Pending terms survive until the user actually answers them. An ambiguous term is answered by
    # choosing one of its candidates; an unknown term only when the model reports the user addressed it
    # and something was resolved. Resolving one term never discards the others.
    carried: list[PendingTerm] = []
    generic_carried = False
    # "That is all / only milk": once the guest states an allergy and says it closes every open question, the
    # unresolved terms are dropped. They were never confirmed allergies, so nothing is stored for them, and the
    # confirmed allergy stays in force (fail closed). Without that explicit statement they stay open.
    settled = ex.settles_pending and (new_allergy or recorded)
    if prev_blocking and not retracted and not settled:
        if not old.pending.terms:
            generic_carried = not new_allergy
        else:
            addressed = {taxonomy.normalize_text(t) for t in ex.answered_pending}

            def answered(term: PendingTerm) -> bool:
                if term.candidates:
                    return bool(add_a & set(term.candidates))
                return new_allergy and taxonomy.normalize_text(term.text) in addressed

            carried = [t for t in old.pending.terms if not answered(t)]
    pending_terms = _dedupe(carried + unresolved)

    if pending_terms or generic_carried or detail_needed:
        s.pending = PendingClarification(ClarificationKind.ALLERGEN_TERM, pending_terms)
    elif flags.correction_unclear:
        s.pending = PendingClarification(ClarificationKind.ALLERGY_CORRECTION)
    elif s.allergy_status is AllergyStatus.AWAITING_ANSWER:
        s.pending = PendingClarification(ClarificationKind.ALLERGY_ANSWER)
    elif flags.unresolved_categories:
        s.pending = PendingClarification(ClarificationKind.CATEGORY_TERM)
    elif flags.unresolved_ingredients:
        s.pending = PendingClarification(ClarificationKind.INGREDIENT_TERM)
    else:
        s.pending = None

    if s.allergy_status is AllergyStatus.NOT_ASKED:
        s.allergy_status = AllergyStatus.AWAITING_ANSWER
        s.allergy_prompts = 1
        s.pending = PendingClarification(ClarificationKind.ALLERGY_ANSWER)
        flags.ask_initial_question = True

    s.recommendation_requested = (
        old.recommendation_requested or ex.wants_recommendations or s.constraints != c or bool(s.requested_ingredients)
    )
    if ex.wants_recommendations or s.constraints != c or s.requested_ingredients:
        s.last_request = text or old.last_request
    return s, flags


def _commit(dst: ConversationState, src: ConversationState) -> None:
    for f in fields(ConversationState):
        setattr(dst, f.name, getattr(src, f.name))


def _quote(terms: list[str] | tuple[str, ...]) -> str:
    return "، ".join(f"«{t[:40]}»" for t in terms)


def _allergen_question(terms: tuple[PendingTerm, ...]) -> str:
    if not terms:
        return ALLERGEN_DETAIL_AR
    lines = []
    for term in terms:
        if term.candidates:
            options = " أو ".join(taxonomy.allergen_name_ar(c) for c in term.candidates)
            lines.append(f"ذكرت «{term.text[:40]}»، وهذا قد يشمل أكثر من نوع: {options}. أي منها تقصد؟ (واحد أو أكثر)")
        else:
            lines.append(
                f"ما قدرت أتأكد من الحساسية «{term.text[:40]}». وش نوع الطعام بالضبط؟ (مثلًا: {ALLERGEN_EXAMPLES_AR})"
            )
    return "\n".join(lines)


def _mentioned_in_menu(term: str, menu: list[dict]) -> bool:
    """True if the (normalized) term appears anywhere in a dish's name, description or ingredient list.

    Deliberately a loose substring test: it is only used to say "no dish mentions this", so a false positive
    just means we ask the guest instead of claiming nothing on the menu contains it.
    """
    needle = taxonomy.normalize_text(term)
    if needle.startswith("ال") and len(needle) > 3:
        needle = needle[2:]
    return any(
        needle in taxonomy.normalize_text(" ".join([d.get("name_ar", ""), d.get("description_ar", ""), *d.get("ingredients_ar", [])]))
        for d in menu
    )


def _clarification(state: ConversationState, flags: _Flags, menu: Optional[list[dict]] = None) -> str:
    """Return the question(s) to ask before recommending, or '' if we can proceed."""
    questions: list[str] = []
    if state.pending is not None and state.pending.blocking:
        questions.append(_allergen_question(state.pending.terms))
    elif flags.ask_initial_question:
        questions.append(ALLERGY_QUESTION_AR)
    elif flags.allergy_clarification:
        questions.append(ALLERGY_CLARIFY_AR)
    if flags.correction_unclear:
        c = state.constraints
        names = _join_ar(
            sorted([taxonomy.allergen_name_ar(a) for a in c.excluded_allergens]
                   + [taxonomy.ingredient_name_ar(i) for i in c.allergy_ingredients])
        )
        questions.append(
            f"سجّلت سابقًا حساسية من {names}. هل تقصد أنها لم تعد موجودة؟ إذا إيه، حدّد أي حساسية تبغى أشيلها."
        )
    if flags.unresolved_categories:
        available = "، ".join(taxonomy.get_taxonomy().category_names.values())
        questions.append(
            f"ما عندي فئة {_quote(flags.unresolved_categories)} في القائمة. الفئات المتاحة: {available}، أو «مالح» (الرئيسية والمقبلات). أي واحدة تبغى؟"
        )
    if flags.unresolved_requested:
        menu = menu if menu is not None else get_all_dishes()
        absent = [t for t in flags.unresolved_requested if menu and not _mentioned_in_menu(t, menu)]
        if absent:
            questions.append(f"ما لقيت {_quote(absent)} في مكونات أي طبق بالقائمة. تبغى أرشّح لك شي ثاني؟")
        unclear = [t for t in flags.unresolved_requested if t not in absent]
        if unclear:
            questions.append(f"ما عرفت المقصود بـ {_quote(unclear)} ضمن مكونات القائمة. ممكن توضّح؟")
    if flags.unresolved_ingredients:
        menu = menu if menu is not None else get_all_dishes()
        absent = [t for t in flags.unresolved_dislikes if menu and not _mentioned_in_menu(t, menu)]
        unclear = [t for t in flags.unresolved_ingredients if t not in absent]
        if absent:
            questions.append(NOT_ON_MENU_AR.format(terms=_quote(absent)))
        if unclear:
            questions.append(f"ما عرفت المقصود بـ {_quote(unclear)} ضمن مكونات القائمة. ممكن توضّح؟")
    return "\n\n".join(questions)


def memory_summary_ar(state: ConversationState) -> str:
    """What is recorded about this guest, rendered from the state itself (never generated by the LLM)."""
    if state.profile_unavailable:
        return PROFILE_UNAVAILABLE_AR
    c = state.constraints
    allergies = sorted(
        [taxonomy.allergen_name_ar(a) for a in c.excluded_allergens]
        + [taxonomy.ingredient_name_ar(i) for i in c.allergy_ingredients]
    )
    dislikes = sorted(taxonomy.ingredient_name_ar(i) for i in c.excluded_ingredients)
    lines = []
    if dislikes:
        lines.append("أطعمة تتجنبها (تفضيل، وليس حساسية): " + _join_ar(dislikes))
    if c.vegetarian_only:
        lines.append("تفضّل الأكل النباتي")
    if allergies:
        lines.append("حساسية مسجّلة: " + _join_ar(allergies))
    elif state.allergy_status is AllergyStatus.NONE_REPORTED:
        lines.append("ذكرت إن ما عندك حساسية من الأطعمة")
    elif state.allergy_status is AllergyStatus.REPORTED:
        lines.append("ذكرت إن عندك حساسية، بس لسا ما حدّدت نوعها")
    if c.min_spice_level is not None:
        lines.append(f"تفضّل حدة البهارات من {spice_label_ar(c.min_spice_level)} فأكثر")
    if c.max_spice_level is not None:
        lines.append(f"ولا تبغى حدة البهارات أعلى من {spice_label_ar(c.max_spice_level)}")
    current = constraint_labels_ar(
        replace(c, excluded_ingredients=frozenset(), vegetarian_only=False, min_spice_level=None, max_spice_level=None)
    )
    parts = ["هذا اللي أعرفه عنك:\n" + "\n".join(f"• {line}" for line in lines)] if lines else [NOTHING_RECORDED_AR]
    if current:
        parts.append("وفي طلبك الحالي فقط (مؤقت): " + "، ".join(current))
    return "\n\n".join(parts)


def _only_asks_memory(ex: Extraction) -> bool:
    """A pure question about recorded preferences: it must not change, ask or save anything.

    Fields a model tends to fill spuriously on a question (a recommendation flag, pending-term echoes) are ignored;
    anything that would change a preference or answer the allergy question makes it a normal turn.
    """
    inert = replace(ex, memory_query=False, wants_recommendations=False, answered_pending=(), unclear_allergy=False, settles_pending=False)
    return ex.memory_query and inert == Extraction()


def _error(reply: str) -> OrchestratorResponse:
    return OrchestratorResponse(ResponseType.ERROR, reply)


def _provider_error_reply(exc: ProviderError) -> str:
    if isinstance(exc, ProviderNotConfiguredError):
        return NOT_CONFIGURED_AR
    if isinstance(exc, RecoverableProviderError):
        return SERVICE_BUSY_AR
    return GENERIC_ERROR_AR


def _safety_footer(state: ConversationState, shown: tuple[dict, ...] = ()) -> str:
    """The allergy caveat; says so plainly when a shown dish has unverified allergen data."""
    if state.constraints.excluded_allergens or state.constraints.allergy_ingredients:
        footer = ALLERGEN_FOOTER_AR
    elif state.allergy_status is AllergyStatus.DECLINED:
        footer = UNKNOWN_ALLERGY_FOOTER_AR
    else:
        return ""
    return f"{footer} {UNVERIFIED_CLAUSE_AR}" if any_unverified(shown) else footer


def _serving_judgements(
    menu: list[dict], constraints: Constraints, generate: Callable[[str, str], str]
) -> dict[str, TemperatureJudgement]:
    """One LLM call about serving temperature, only for dishes that already pass every hard constraint.

    Dishes with a structured serving_temperature need no inference.
    """
    if constraints.serving_temperature is None:
        return {}
    candidates = filter_dishes(menu, constraints).eligible
    undecided = [d for d in candidates if d.get("serving_temperature") is None]
    return assess_temperatures(undecided, generate) if undecided else {}


def _serving_view(
    constraints: Constraints, result: FilterResult, dishes: tuple[dict, ...]
) -> tuple[tuple[str, ...], tuple[str, ...], str]:
    """Per-dish labels and levels for the shown dishes, plus an extra note when alternatives appear."""
    if constraints.serving_temperature is None or not result.temperature_fit:
        return (), (), ""
    wanted = "دافئ" if constraints.serving_temperature == "warm" else "بارد"
    notes, levels = [], []
    for dish in dishes:
        fit = result.temperature_fit[dish["id"]]
        levels.append(fit.level)
        if fit.level == CONFIRMED:
            notes.append(wanted)
        elif fit.is_match:
            notes.append(f"غالبًا {wanted} (استنتاج: {fit.rationale})" if fit.rationale else f"غالبًا {wanted} (استنتاج من وصف الطبق)")
        else:
            notes.append("بديل: حرارة التقديم غير مؤكدة")
    alternatives = UNCERTAIN in levels
    return tuple(notes), tuple(levels), ALTERNATIVES_NOTE_AR.format(wanted=wanted) if alternatives else ""


def _requested_ingredients(
    state: ConversationState, menu: list[dict]
) -> tuple[Optional[OrchestratorResponse], Constraints, Optional[set[str]]]:
    """Handle "dishes with X": explain a clash with a saved dislike, say when no dish has it, never override safety.

    Returns (an early response or None, the constraints to filter with, the ids of dishes that contain X).
    A dislike can be lifted for this one request after the guest agrees; allergies never can. The saved
    profile is untouched: the lifted dislike lives only in this request's constraints.
    """
    c, wanted = state.constraints, state.requested_ingredients
    names = _join_ar(sorted(taxonomy.ingredient_name_ar(i) for i in wanted))
    allergic = {
        i for i in wanted
        if taxonomy.matches_ingredient_exclusion([i], c.allergy_ingredients)
        or taxonomy.implied_allergens([i]) & c.excluded_allergens
    }
    if allergic:
        state.requested_ingredients = state.override_ingredients = frozenset()
        text = REQUESTED_ALLERGY_AR.format(names=_join_ar(sorted(taxonomy.ingredient_name_ar(i) for i in allergic)))
        return OrchestratorResponse(ResponseType.NO_RESULTS, text), c, None
    clashing = {e for e in c.excluded_ingredients if any(e in taxonomy.ingredient_closure(i) for i in wanted)}
    matching = {
        d["id"] for d in menu
        if taxonomy.matches_ingredient_exclusion(d.get("ingredient_ids", []), wanted)
    }
    if not matching:
        state.requested_ingredients = state.override_ingredients = frozenset()
        text = REQUESTED_NOT_ON_MENU_AR.format(names=names) + (REQUESTED_IS_DISLIKE_AR if clashing else "") + REQUESTED_OFFER_OTHERS_AR
        return OrchestratorResponse(ResponseType.NO_RESULTS, text), c, None
    if clashing and not state.override_ingredients:
        state.pending = PendingClarification(ClarificationKind.OVERRIDE_DISLIKE)
        return OrchestratorResponse(ResponseType.CLARIFICATION, OVERRIDE_QUESTION_AR.format(names=names)), c, None
    return None, replace(c, excluded_ingredients=c.excluded_ingredients - clashing), matching


def _recommend(
    state: ConversationState, message: str, generate: Callable[[str, str], str], dishes: Optional[list[dict]]
) -> OrchestratorResponse:
    menu = dishes if dishes is not None else get_all_dishes()
    if not menu:
        return _error(MENU_UNAVAILABLE_AR)
    constraints = state.constraints
    requested = state.requested_ingredients
    match_ids: Optional[set[str]] = None
    if requested:
        early, constraints, match_ids = _requested_ingredients(state, menu)
        if early is not None:
            state.recommendation_requested = False  # answered (or waiting for a yes/no): not a pending request
            return early
    try:
        judgements = _serving_judgements(menu, constraints, generate)
        result = recommend(menu, constraints, temperature_judgements=judgements)
        if match_ids is not None:  # "dishes with X": only those that also passed every hard filter
            result = replace(result, eligible=[d for d in result.eligible if d["id"] in match_ids])
        composed = compose_reply(result, state.last_request or message, generate, menu, constraints)
    except ProviderError as exc:
        logger.warning("Response generation failed: %s", type(exc).__name__)
        return _error(_provider_error_reply(exc))
    state.recommendation_requested = False
    state.requested_ingredients = state.override_ingredients = frozenset()
    if not result.eligible:
        if temperature_is_the_blocker(constraints, result):
            state.pending = PendingClarification(ClarificationKind.TEMPERATURE_RELAX)
        return OrchestratorResponse(ResponseType.NO_RESULTS, composed.text)
    serving_notes, serving_levels, alternatives_note = _serving_view(constraints, result, composed.dishes)
    lead = intro_ar(constraints, sorted(taxonomy.ingredient_name_ar(i) for i in requested))
    if serving_levels and all(level == UNCERTAIN for level in serving_levels):
        lead = ALTERNATIVES_LEAD_AR.format(wanted="دافئًا" if constraints.serving_temperature == "warm" else "باردًا")
    notes = "\n\n".join(
        e for e in (exclusion_note_ar(result), alternatives_note, _safety_footer(state, composed.dishes)) if e
    )
    text = composed.text.replace(RECOMMENDATION_INTRO_AR, lead, 1)
    return OrchestratorResponse(
        ResponseType.RECOMMENDATIONS,
        "\n\n".join(p for p in (text, notes) if p),
        composed.dishes,
        composed.reasons,
        lead,
        notes,
        serving_notes,
        serving_levels,
    )


def _finish(
    state: ConversationState, message: str, response: OrchestratorResponse, flags: _Flags
) -> OrchestratorResponse:
    acks = []
    if flags.removed_names:
        acks.append(f"تمام، أزلت حساسية {_join_ar(flags.removed_names)} من قيودك.")
    if flags.understood_allergy_ingredients:
        names = _join_ar(flags.understood_allergy_ingredients)
        acks.append(f"فهمت إن عندك حساسية من {names}، وسأستبعد الأطباق التي تحتوي على {names}. إذا قصدت شيئًا آخر، قل لي.")
    if flags.understood_dislikes:
        acks.append(f"تمام، راح أستبعد الأطباق التي تحتوي على {_join_ar(flags.understood_dislikes)}.")
    if acks:
        ack = "\n\n".join(acks) + "\n\n"
        lead = ack + response.lead_ar if response.type is ResponseType.RECOMMENDATIONS else response.lead_ar
        response = replace(response, reply_ar=ack + response.reply_ar, lead_ar=lead)
    state.add_turn("user", message)
    state.add_turn("assistant", response.reply_ar)
    return response


def handle_message(
    state: ConversationState,
    message: str,
    generate: Callable[[str, str], str] = default_generate,
    dishes: Optional[list[dict]] = None,
) -> OrchestratorResponse:
    """Process one user message and update `state` in place. `generate(system, user) -> text` is the LLM."""
    text = (message or "").strip()[:MAX_MESSAGE_CHARS]
    if not text:
        return OrchestratorResponse(ResponseType.CLARIFICATION, EMPTY_MESSAGE_AR)

    try:
        extraction = extract(text, state, generate)
        if _only_asks_memory(extraction):
            reply = OrchestratorResponse(ResponseType.INFO, memory_summary_ar(state))
            state.add_turn("user", text)
            state.add_turn("assistant", reply.reply_ar)
            return reply
        new_state, flags = _update_state(state, extraction, text)
    except ProviderError as exc:
        logger.warning("Extraction provider failure: %s", type(exc).__name__)
        return _error(_provider_error_reply(exc))
    except (ExtractionError, InvalidConstraintError) as exc:
        logger.warning("Extraction rejected: %s", type(exc).__name__)
        return _error(MISUNDERSTOOD_AR)

    _commit(state, new_state)

    question = _clarification(state, flags, dishes)
    if question:
        return _finish(state, text, OrchestratorResponse(ResponseType.CLARIFICATION, question), flags)
    if not state.recommendation_requested:
        return _finish(state, text, OrchestratorResponse(ResponseType.CLARIFICATION, ASK_REQUEST_AR), flags)

    response = _recommend(state, text, generate, dishes)
    return response if response.type is ResponseType.ERROR else _finish(state, text, response, flags)
