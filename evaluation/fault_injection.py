"""Fault injection: what each system shows when the MODEL misbehaves. Offline, deterministic, no API calls.

This is a simulation, not a measurement of a real model: it replaces the model with misbehaving stand-ins to show
which system's safety depends on the model and which does not.

    python -m evaluation.fault_injection

Stand-in models:
  names_every_dish  ignores every constraint and names all dishes in menu order (the worst case)
  seeded_random     names three dishes chosen at random (seeded), as if it had not read the prompt
"""

import json
import random
import zlib
from typing import Callable

from evaluation.core import ARMS, Outcome, score_run, scenario_menu, summarize
from evaluation.scenarios import SCENARIOS, Scenario

GenerateFn = Callable[[str, str], str]


def names_every_dish(_system: str, user: str) -> str:
    ids = [part.split("|")[0].replace("- dish_id:", "").strip() for part in user.splitlines() if part.startswith("- dish_id:")]
    return json.dumps({"picks": [{"dish_id": i, "reason_ar": ""} for i in ids]})


def seeded_random(seed: int) -> GenerateFn:
    rng = random.Random(seed)

    def generate(_system: str, user: str) -> str:
        ids = [part.split("|")[0].replace("- dish_id:", "").strip() for part in user.splitlines() if part.startswith("- dish_id:")]
        return json.dumps({"picks": [{"dish_id": i, "reason_ar": ""} for i in rng.sample(ids, min(3, len(ids)))]})

    return generate


MODELS = {"names_every_dish": lambda s: names_every_dish, "seeded_random": lambda s: seeded_random(zlib.crc32(s.id.encode()) % 1000)}


def run(synthetic: int = 0) -> dict[str, dict[str, dict]]:
    """{model: {arm: summary}} over every scenario."""
    out: dict[str, dict[str, dict]] = {}
    for name, factory in MODELS.items():
        out[name] = {}
        for arm, runner in ARMS.items():
            rows = []
            for scenario in SCENARIOS:
                outcome: Outcome = runner(scenario, factory(scenario), synthetic)
                rows.append(score_run(scenario, outcome, synthetic))
            out[name][arm] = summarize(rows)
    return out


def prompt_sizes(synthetic: int = 0) -> dict[str, float]:
    """Average size (characters) of the text sent to the model per scenario, and how many scenarios need no model call.

    Both systems are measured on the same scenarios; the pipeline's prompt lists only the dishes that survived filtering.
    """
    from agent.orchestrator import constraint_labels_ar
    from agent.prompts import build_menu_context
    from evaluation.core import prompt_only_message
    from services.recommendation_service import recommend

    baseline, pipeline, no_call = [], [], 0
    for scenario in SCENARIOS:
        menu = scenario_menu(scenario, synthetic)
        baseline.append(len(prompt_only_message(scenario, menu)))
        eligible = recommend(menu, scenario.constraints).eligible
        if not eligible:
            no_call += 1
            continue
        applied = constraint_labels_ar(scenario.constraints)
        pipeline.append(len(build_menu_context(eligible)) + len(scenario.request_ar) + sum(len(x) for x in applied))
    return {
        "scenarios": len(SCENARIOS),
        "prompt_only_avg_chars": sum(baseline) / len(baseline),
        "pipeline_avg_chars_when_called": sum(pipeline) / len(pipeline),
        "pipeline_scenarios_without_any_model_call": no_call,
    }


def markdown(results: dict[str, dict[str, dict]]) -> str:
    lines = ["| Stand-in model | System | Runs | Runs with a violation | Runs with an allergy violation | Dishes shown / violating |", "|---|---|---|---|---|---|"]
    for model, arms in results.items():
        for arm, s in arms.items():
            lines.append(
                f"| {model} | {arm} | {s['runs']} | {s['runs_with_any_violation']} | {s['runs_with_allergy_violation']} | "
                f"{s['dishes_shown']} / {s['dishes_violating']} |"
            )
    return "\n".join(lines)


if __name__ == "__main__":
    print(markdown(run()))
    print()
    for size in (0, 45):
        print(f"menu of {15 + size} dishes:", prompt_sizes(size))
