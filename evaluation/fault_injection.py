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
    """Size of the user message sent to the model, measured on what each system really sends (characters, not tokens).

    Like for like: the reduction compares the two systems on the scenarios where the pipeline makes a model call. The
    system prompt (identical in both systems, 1,098 characters) is excluded. Scenarios where nothing qualifies send
    nothing in the pipeline; they are reported separately as avoided calls, not folded into the size reduction.
    """
    from evaluation.core import prompt_only_message, run_pipeline

    baseline_all, baseline_called, pipeline_called = [], [], []
    for scenario in SCENARIOS:
        menu = scenario_menu(scenario, synthetic)
        baseline = len(prompt_only_message(scenario, menu))
        baseline_all.append(baseline)
        sent: list[int] = []

        def capture(_system: str, user: str, sent=sent) -> str:
            sent.append(len(user))
            return '{"picks": []}'

        run_pipeline(scenario, capture, synthetic)
        if sent:
            baseline_called.append(baseline)
            pipeline_called.append(sent[0])
    called = len(pipeline_called)
    mean = lambda xs: sum(xs) / len(xs)  # noqa: E731
    return {
        "scenarios": len(SCENARIOS),
        "scenarios_where_the_pipeline_calls_the_model": called,
        "pipeline_scenarios_without_any_model_call": len(SCENARIOS) - called,
        "prompt_only_avg_chars_all_scenarios": mean(baseline_all),
        "prompt_only_avg_chars_on_called_scenarios": mean(baseline_called),
        "pipeline_avg_chars_on_called_scenarios": mean(pipeline_called),
        "reduction_on_called_scenarios": 1 - mean(pipeline_called) / mean(baseline_called),
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
