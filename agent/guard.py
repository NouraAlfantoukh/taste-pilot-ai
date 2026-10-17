"""Deterministic guard for generated recommendations.

The generation LLM returns JSON picks ({"picks": [{"dish_id", "reason_ar"}]}). Nothing it writes is
trusted as a dish reference: a pick is shown only if its dish_id is an eligible dish of the current
result, and names, prices and spice levels are rendered from that dish object. The optional short
reason is dropped if it states numbers/prices, absence-of-allergen claims ("لا يحتوي", "لا تتضمن"...), or mentions a menu dish
that is not eligible. Malformed output falls back to the top eligible dishes without any LLM prose.
"""

import json
import logging
import re
from typing import Callable, Mapping, NamedTuple, Optional

from agent.temperature import mentions_temperature
from services.menu_service import format_price_sar, spice_label_ar
from services.recommendation_service import FIT_ORDER, TemperatureFit
from services.taxonomy import normalize_text

logger = logging.getLogger(__name__)

MAX_RECOMMENDATIONS = 5
FALLBACK_RECOMMENDATIONS = 3
MAX_REASON_CHARS = 120
RECOMMENDATION_INTRO_AR = "رشّحت لك من القائمة:"

_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)
_DIGITS = re.compile(r"[0-9٠-٩۰-۹]")
_CURRENCY = re.compile(r"ريال|ر\.\s?س|sar|riyal", re.IGNORECASE)
_CLAIM_PHRASES = ("لا يحتوي", "لا تحتوي", "خالي من", "خاليه من", "بدون حساسيه")
_CLAIM_WORDS = {"امن", "امنه", "امنا", "خال", "خالي", "خاليه"}
# "does not contain/include/have X": a negation followed (within one word) by a verb of containing.
_ABSENCE = re.compile(r"(?:^|\s)(?:و?لا|و?ما)\s+(?:\S+\s+)?[يت](?:حتوي|تضمن|شمل|حوي|ضم)")


class Rendered(NamedTuple):
    text: str
    dishes: list[dict]
    reasons: list[str]  # aligned with dishes; "" when the model's reason was dropped


def _load_picks(raw: object) -> Optional[list[tuple[str, object]]]:
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    fenced = _FENCE.match(text)
    if fenced:
        text = fenced.group(1)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    picks = data.get("picks") if isinstance(data, dict) else None
    if not isinstance(picks, list):
        return None
    result = []
    for pick in picks:
        if isinstance(pick, dict) and isinstance(pick.get("dish_id"), str):
            result.append((pick["dish_id"].strip(), pick.get("reason_ar")))
    return result


def _strip_article(token: str) -> str:
    return token[2:] if token.startswith("ال") and len(token) > 4 else token


def has_unsupported_claim(text: str) -> bool:
    """True if the text claims a dish is free of / does not contain something, or calls it safe (never allowed)."""
    normalized = normalize_text(text)
    if any(phrase in normalized for phrase in _CLAIM_PHRASES) or _ABSENCE.search(normalized):
        return True
    return any(_strip_article(token) in _CLAIM_WORDS for token in normalized.split())


def _clean_reason(reason: object, ineligible: list[dict]) -> str:
    """Return the reason if it is safe to show next to a validated dish, else ''."""
    if not isinstance(reason, str):
        return ""
    text = " ".join(reason.split())
    if not text or len(text) > MAX_REASON_CHARS or _DIGITS.search(text) or _CURRENCY.search(text):
        return ""
    normalized = normalize_text(text)
    if has_unsupported_claim(text):
        return ""
    lowered = text.lower()
    for dish in ineligible:
        if dish["id"].lower() in lowered or normalize_text(dish["name_ar"]) in normalized:
            return ""
    return text


def _arrange_by_temperature(
    chosen: list[tuple[dict, str]], eligible: list[dict], fits: Mapping[str, TemperatureFit]
) -> list[tuple[dict, str]]:
    """Matches first (never skipped in favour of alternatives); alternatives only fill the remaining slots.

    An alternative's reason may not talk about temperature, so it cannot sound like a confirmed match.
    """
    picked = {dish["id"] for dish, _ in chosen}
    matches = [(d, r) for d, r in chosen if fits[d["id"]].is_match]
    matches += [(d, "") for d in eligible if fits[d["id"]].is_match and d["id"] not in picked]
    matches.sort(key=lambda pair: FIT_ORDER[fits[pair[0]["id"]].level])
    matches = matches[:MAX_RECOMMENDATIONS]
    alternatives = [(d, r) for d, r in chosen if not fits[d["id"]].is_match]
    alternatives += [(d, "") for d in eligible if not fits[d["id"]].is_match and d["id"] not in picked]
    needed = max(0, FALLBACK_RECOMMENDATIONS - len(matches))
    return matches + [(d, "" if mentions_temperature(r) else r) for d, r in alternatives[:needed]]


def render_recommendations(
    eligible: list[dict],
    raw: object,
    menu: list[dict],
    fits: Optional[Mapping[str, TemperatureFit]] = None,
    explain: Optional[Callable[[dict], str]] = None,
) -> Rendered:
    """Return the Arabic text plus the dishes shown. `eligible` must already be filtered and ranked.

    With `explain`, the reason under each dish comes from that function (built from menu data) and the model's
    own reason text is discarded; the model then only chooses and orders dishes.
    """
    by_id = {d["id"]: d for d in eligible}
    ineligible = [d for d in menu if d["id"] not in by_id]

    chosen: list[tuple[dict, str]] = []
    picks = _load_picks(raw)
    if picks is None:
        logger.warning("Generated recommendation was not valid JSON; using deterministic fallback")
    else:
        seen: set[str] = set()
        for dish_id, reason in picks:
            dish = by_id.get(dish_id)
            if dish is None or dish_id in seen:
                logger.warning("Dropped an unsupported or duplicate generated pick")
                continue
            seen.add(dish_id)
            chosen.append((dish, _clean_reason(reason, ineligible)))
    if not chosen:
        chosen = [(d, "") for d in eligible[:FALLBACK_RECOMMENDATIONS]]
    if fits:
        chosen = _arrange_by_temperature(chosen, eligible, fits)
    chosen = chosen[:MAX_RECOMMENDATIONS]

    if explain is not None:
        chosen = [(dish, explain(dish)) for dish, _ in chosen]
    lines = [RECOMMENDATION_INTRO_AR]
    for index, (dish, reason) in enumerate(chosen, start=1):
        line = f"{index}. {dish['name_ar']} — {format_price_sar(dish['price_sar'])} — {spice_label_ar(dish['spice_level'])}"
        lines.append(f"{line}\n   {reason}" if reason else line)
    return Rendered("\n".join(lines), [dish for dish, _ in chosen], [reason for _, reason in chosen])
