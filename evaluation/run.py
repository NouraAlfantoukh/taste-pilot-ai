"""Run and score the Deep Dive experiment.

    python -m evaluation.run --validate                        # 10 scenarios x 1 repeat x 2 systems (about 14 model calls)
    python -m evaluation.run --full --repeats 3                # every scenario x 3 repeats x 2 systems
    python -m evaluation.run --score evaluation/results/<file>.jsonl   # offline: re-score stored raw outputs, no API

Real model calls use the production ClaudeProvider (same model, effort and token limit as the app, no Gemini
fallback, no retries). A failed call is recorded as an error and excluded from the rates, never retried silently.
Every raw model output is stored, so the reported numbers can be recomputed without spending anything.
"""

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from evaluation.core import ARMS, Outcome, claim_stats, replay_displayed_reasons, score_run, summarize
from evaluation.scenarios import SCENARIOS, VALIDATION_IDS, Scenario, by_id

RESULTS_DIR = Path(__file__).resolve().parent / "results"


def _provider(max_tokens: Optional[int] = None):
    from services.llm_service import DEFAULT_MAX_TOKENS, ClaudeProvider

    provider = ClaudeProvider(max_tokens=max_tokens or DEFAULT_MAX_TOKENS)
    if not provider.is_configured():
        sys.exit("ANTHROPIC_API_KEY is not configured: cannot run the experiment (use --score on stored results).")
    return provider


def _one(provider, scenario: Scenario, arm: str, repeat: int, run_id: str, synthetic: int = 0) -> dict:
    record = {
        "run_id": run_id, "scenario": scenario.id, "category": scenario.category, "arm": arm, "repeat": repeat,
        "model": provider.model, "effort": provider.effort, "max_tokens": provider._max_tokens, "synthetic_dishes": synthetic, "error": None, "shown": [], "valid_output": True,
        "model_calls": 0, "raw": "",
    }
    try:
        outcome: Outcome = ARMS[arm](scenario, provider.generate, synthetic)
        record.update(shown=outcome.shown, valid_output=outcome.valid_output, model_calls=outcome.model_calls, raw=outcome.raw)
    except Exception as exc:  # noqa: BLE001 - recorded, never hidden; the run is excluded from the rates
        record["error"] = type(exc).__name__
    return record


def run_experiment(
    scenarios: list[Scenario], repeats: int, workers: int, max_calls: int, out_path: Path, synthetic: int = 0,
    max_tokens: Optional[int] = None,
) -> list[dict]:
    jobs = [(s, arm, r) for s in scenarios for r in range(repeats) for arm in ARMS]
    worst_case = len(jobs)  # pipeline runs with no eligible dish make no call, so this is an upper bound
    if worst_case > max_calls:
        sys.exit(f"{worst_case} model calls would exceed --max-calls {max_calls}; lower --repeats or raise the cap.")
    provider = _provider(max_tokens)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    print(f"run {run_id}: {len(jobs)} runs (at most {worst_case} model calls), model={provider.model}, workers={workers}")
    started = time.time()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        rows = list(pool.map(lambda j: _one(provider, j[0], j[1], j[2], run_id, synthetic), jobs))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    errors = sum(1 for r in rows if r["error"])
    calls = sum(r["model_calls"] for r in rows)
    print(f"done in {time.time() - started:.0f}s: {calls} model calls, {errors} errored runs -> {out_path}")
    return rows


def load_results(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def score_results(rows: list[dict]) -> dict:
    """{arm: {"overall": summary, "by_category": {category: summary}}} over the runs that produced an answer."""
    scenarios = by_id()
    scored: dict[str, list[dict]] = {}
    errored = {}
    for row in rows:
        if row.get("error"):
            errored[row["arm"]] = errored.get(row["arm"], 0) + 1
            continue
        scenario = scenarios[row["scenario"]]
        outcome = Outcome(row["shown"], row["valid_output"], row["model_calls"], row.get("raw", ""))
        scored.setdefault(row["arm"], []).append({**score_run(scenario, outcome, row.get("synthetic_dishes", 0)), "category": scenario.category, "scenario": scenario.id})
    report = {}
    raw_by_arm: dict[str, list[str]] = {}
    for row in rows:
        if not row.get("error"):
            raw_by_arm.setdefault(row["arm"], []).append(row.get("raw", ""))
    for arm, items in scored.items():
        categories = sorted({i["category"] for i in items})
        report[arm] = {
            "overall": summarize(items),
            "by_category": {c: summarize([i for i in items if i["category"] == c]) for c in categories},
            "errored_runs": errored.get(arm, 0),
            "model_reason_claims": claim_stats(raw_by_arm.get(arm, [])),
            "displayed_replay": replay_displayed_reasons([r for r in rows if r["arm"] == arm]) if arm == "pipeline" else None,
            "scenarios_with_violation": sorted({i["scenario"] for i in items if i["any_violation"]}),
        }
    return report


def _pct(part: int, whole: int) -> str:
    return f"{100 * part / whole:.0f}%" if whole else "n/a"


def markdown(report: dict, rows: list[dict]) -> str:
    arms = [a for a in ("prompt_only", "pipeline") if a in report]
    meta = rows[0] if rows else {}
    lines = [
        f"Run `{meta.get('run_id', '?')}`, model `{meta.get('model', '?')}` (effort `{meta.get('effort')}`), "
        f"{15 + meta.get('synthetic_dishes', 0)} dishes on the menu, "
        f"{len({r['scenario'] for r in rows})} scenarios x {max((r['repeat'] for r in rows), default=0) + 1} repeats.",
        "",
        "| Metric | " + " | ".join(arms) + " |",
        "|---|" + "---|" * len(arms),
    ]

    def row(label, fn):
        lines.append(f"| {label} | " + " | ".join(fn(report[a]["overall"]) for a in arms) + " |")

    row("Runs scored", lambda s: str(s["runs"]))
    row("Runs with any constraint violation", lambda s: f"{s['runs_with_any_violation']} ({_pct(s['runs_with_any_violation'], s['runs'])})")
    row("Runs with an **allergy** violation", lambda s: f"{s['runs_with_allergy_violation']} ({_pct(s['runs_with_allergy_violation'], s['runs'])})")
    row("Runs with only lower-risk violations", lambda s: str(s["runs_with_lower_risk_violation_only"]))
    row("Dishes shown / violating", lambda s: f"{s['dishes_shown']} / {s['dishes_violating']} ({_pct(s['dishes_violating'], s['dishes_shown'])})")
    row("Useful answers (all constraints met)", lambda s: f"{s['useful_runs']} ({_pct(s['useful_runs'], s['runs'])})")
    row("Refused although a valid dish existed", lambda s: f"{s['refused_although_possible']} of {s['possible_runs']}")
    row("Correctly empty when no dish qualifies", lambda s: f"{s['correctly_empty_when_impossible']} of {s['impossible_runs']}")
    row("Unreadable model outputs", lambda s: str(s["unreadable_outputs"]))
    lines.append(
        "| Model reasons containing a claim the guard forbids (\"does not contain...\", \"safe\") | "
        + " | ".join(
            f"{report[a]['model_reason_claims']['with_unsupported_claim']} of {report[a]['model_reason_claims']['reasons']}"
            + (" (shown to the guest)" if a == "prompt_only" else " (model text discarded, see replay below)")
            for a in arms
        )
        + " |"
    )
    replay = report.get("pipeline", {}).get("displayed_replay")
    if replay:
        lines.append(
            f"| Reasons actually displayed by the pipeline (offline replay of the stored outputs) | n/a | {replay['displayed_reasons']} displayed, "
            f"{replay['displayed_with_forbidden_claim']} with a forbidden claim; {replay['dish_list_mismatches']} dish-list mismatches in {replay['replayed_runs']} replays |"
        )
    lines += ["", "**Per category** (runs with a violation / runs; allergy violations in brackets):", "",
              "| Category | " + " | ".join(arms) + " |", "|---|" + "---|" * len(arms)]
    categories = sorted({c for a in arms for c in report[a]["by_category"]})
    for category in categories:
        cells = []
        for a in arms:
            s = report[a]["by_category"].get(category)
            cells.append("-" if not s else f"{s['runs_with_any_violation']}/{s['runs']} ({s['runs_with_allergy_violation']})")
        lines.append(f"| {category} | " + " | ".join(cells) + " |")
    for a in arms:
        if report[a]["errored_runs"]:
            lines.append(f"\n`{a}`: {report[a]['errored_runs']} runs errored and are excluded.")
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--validate", action="store_true")
    mode.add_argument("--full", action="store_true")
    mode.add_argument("--score", type=Path)
    parser.add_argument("--repeats", type=int, default=None)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--max-calls", type=int, default=400)
    parser.add_argument("--only", default=None, help="comma-separated scenario ids (with --full)")
    parser.add_argument("--max-tokens", type=int, default=None, help="override the output token limit (default: the app's)")
    parser.add_argument("--synthetic", type=int, default=0, help="add N deterministic synthetic dishes (scale experiment)")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    if args.score:
        rows = load_results(args.score)
        report = score_results(rows)
        print(markdown(report, rows))
        out = args.score.with_suffix(".summary.json")
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nsummary written to {out}")
        return

    if args.validate:
        scenarios, repeats, name = [by_id()[i] for i in VALIDATION_IDS], args.repeats or 1, "validation"
    else:
        scenarios, repeats, name = list(SCENARIOS), args.repeats or 3, "full"
        if args.only:
            scenarios = [by_id()[i] for i in args.only.split(",")]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = args.out or RESULTS_DIR / f"{name}-{stamp}.jsonl"
    rows = run_experiment(scenarios, repeats, args.workers, args.max_calls, out_path, args.synthetic, args.max_tokens)
    print(markdown(score_results(rows), rows))


if __name__ == "__main__":
    main()
