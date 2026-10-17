import json

from agent.prompts import EXTRACTION_PROMPT_AR, SYSTEM_PROMPT_AR, TEMPERATURE_PROMPT_AR


def unverified_menu():
    """The production dishes with their allergen data treated as NOT reviewed (the fail-safe path)."""
    from services.menu_service import get_all_dishes

    return [dict(d, allergen_data_status="unverified") for d in get_all_dishes()]


def ext(**fields):
    return json.dumps(fields, ensure_ascii=False)


def assessments(*verdicts):
    """verdicts: (dish_id, temperature[, confidence[, rationale]]) tuples -> a temperature-assessor reply."""
    items = []
    for dish_id, temperature, *rest in verdicts:
        confidence = rest[0] if rest else "high"
        rationale = rest[1] if len(rest) > 1 else ""
        items.append(
            {"dish_id": dish_id, "temperature": temperature, "confidence": confidence, "rationale_ar": rationale}
        )
    return json.dumps({"assessments": items}, ensure_ascii=False)


def picks_for(*dish_ids):
    return json.dumps({"picks": [{"dish_id": d, "reason_ar": "مناسب"} for d in dish_ids]}, ensure_ascii=False)


class Scripted:
    """Fake LLM returning scripted replies in order; an unexpected extra call fails the test."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls: list[tuple[str, str]] = []

    def __call__(self, system_prompt, user_message):
        self.calls.append((system_prompt, user_message))
        assert self.replies, "unexpected LLM call"
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def extraction_calls(self):
        return [c for c in self.calls if c[0] == EXTRACTION_PROMPT_AR]

    def generation_calls(self):
        return [c for c in self.calls if c[0] == SYSTEM_PROMPT_AR]

    def assessment_calls(self):
        return [c for c in self.calls if c[0] == TEMPERATURE_PROMPT_AR]
