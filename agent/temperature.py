"""LLM inference of how each dish is probably served (warm / cold / room / unknown) from its name and description.

This is a soft signal. Each verdict carries a confidence and a short rationale; Python only sanitizes the
reply and never lets it override a hard constraint (the filter has already run). Spiciness is never part of
the input. Missing or malformed output means "unknown", never an error and never a guess.
"""

import json
import logging
import re
from typing import Callable

from agent.prompts import TEMPERATURE_PROMPT_AR
from services.recommendation_service import TemperatureJudgement
from services.taxonomy import normalize_text

logger = logging.getLogger(__name__)

TEMPERATURES = ("warm", "cold", "room", "unknown")
CONFIDENCES = ("high", "medium", "low")
MAX_RATIONALE_CHARS = 140

_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)
_NUMBERS_OR_MONEY = re.compile(r"[0-9٠-٩۰-۹]|ريال|ر\.\s?س|sar", re.IGNORECASE)
# Words that state or imply a temperature; used to stop uncertain alternatives from claiming one.
_TEMPERATURE_WORDS = ("ساخن", "سخن", "دافئ", "دافي", "حار", "بارد", "مثلج", "ثلج", "مجمد", "مبرد", "منعش")


def mentions_temperature(text: str) -> bool:
    """True if the text talks about how hot or cold something is served."""
    normalized = normalize_text(text)
    return any(normalize_text(word) in normalized for word in _TEMPERATURE_WORDS)


def _clean_rationale(value: object) -> str:
    if not isinstance(value, str):
        return ""
    text = " ".join(value.split())
    if not text or len(text) > MAX_RATIONALE_CHARS or _NUMBERS_OR_MONEY.search(text):
        return ""
    return text


def _load(raw: object) -> list[dict]:
    if not isinstance(raw, str):
        return []
    text = raw.strip()
    fenced = _FENCE.match(text)
    if fenced:
        text = fenced.group(1)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    items = data.get("assessments") if isinstance(data, dict) else None
    return [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []


def assess_temperatures(
    candidates: list[dict], generate: Callable[[str, str], str]
) -> dict[str, TemperatureJudgement]:
    """One LLM call for all candidates. Every candidate gets a judgement ("unknown" if not credibly given).

    Provider errors propagate to the caller. Malformed output yields all-"unknown".
    """
    verdicts = {d["id"]: TemperatureJudgement() for d in candidates}
    if not candidates:
        return verdicts
    payload = {
        "dishes": [
            {"dish_id": d["id"], "name_ar": d["name_ar"], "category_ar": d["category_ar"], "description_ar": d["description_ar"]}
            for d in candidates
        ]
    }
    for item in _load(generate(TEMPERATURE_PROMPT_AR, json.dumps(payload, ensure_ascii=False))):
        dish_id, label = item.get("dish_id"), item.get("temperature")
        if dish_id not in verdicts or label not in TEMPERATURES:
            continue
        confidence = item.get("confidence")
        verdicts[dish_id] = TemperatureJudgement(
            label,
            confidence if confidence in CONFIDENCES else "low",
            _clean_rationale(item.get("rationale_ar")),
        )
    return verdicts
