"""The Deep Dive harness: the oracle, both systems, the scoring and the stored-results path (no API calls)."""

import json
from pathlib import Path

import pytest
from helpers import picks_for

from evaluation import run as evaluation_run
from evaluation.core import (
    ALLERGY_KINDS,
    Outcome,
    parse_picks,
    prompt_only_message,
    run_pipeline,
    run_prompt_only,
    scenario_menu,
    score_run,
    summarize,
    valid_dishes,
    violations,
    wilson,
)
from evaluation.scenarios import SCENARIOS, UNREVIEWED, VALIDATION_IDS, by_id
from services.constraints import Constraints
from services.menu_service import get_all_dishes
from services.recommendation_service import filter_dishes

MENU = {d["id"]: d for d in get_all_dishes()}
ALL_IDS = list(MENU)


def everything(_system, _user):
    """A model that ignores every constraint and names all dishes."""
    return picks_for(*ALL_IDS)


# ---- the scenario set ----

def test_scenario_ids_are_unique_and_the_validation_subset_covers_every_category():
    assert len({s.id for s in SCENARIOS}) == len(SCENARIOS) >= 40
    assert {by_id()[i].category for i in VALIDATION_IDS} == {s.category for s in SCENARIOS}


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.id)
def test_the_independent_oracle_agrees_with_the_production_filter_on_every_scenario(scenario):
    menu = scenario_menu(scenario)
    assert {d["id"] for d in valid_dishes(menu, scenario.constraints)} == {d["id"] for d in filter_dishes(menu, scenario.constraints).eligible}


def test_impossible_and_unreviewed_scenarios_really_have_no_valid_dish():
    for s in SCENARIOS:
        if s.category == "impossible" or s.menu == UNREVIEWED:
            assert valid_dishes(scenario_menu(s), s.constraints) == [], s.id
    assert all(by_id()[i].menu == UNREVIEWED for i in by_id() if i.startswith("un-"))


def test_every_other_scenario_has_at_least_one_valid_dish():
    for s in SCENARIOS:
        if s.category != "impossible" and s.menu != UNREVIEWED:
            assert valid_dishes(scenario_menu(s), s.constraints), s.id


# ---- the oracle ----

def test_the_oracle_names_each_kind_of_violation():
    cases = [
        (Constraints(excluded_allergens={"milk"}), "dish_002", {"allergy"}),
        (Constraints(allergy_ingredients={"cheese"}), "dish_007", {"allergy"}),
        (Constraints(excluded_ingredients={"chicken"}), "dish_001", {"dislike"}),
        (Constraints(vegetarian_only=True), "dish_001", {"diet"}),
        (Constraints(max_price_sar=20), "dish_002", {"budget"}),
        (Constraints(max_spice_level="none"), "dish_003", {"spice"}),
        (Constraints(min_spice_level="hot"), "dish_006", {"spice"}),
        (Constraints(categories={"desserts"}), "dish_001", {"category"}),
    ]
    for constraints, dish_id, expected in cases:
        assert set(violations(MENU[dish_id], constraints)) == expected, (dish_id, expected)
    assert violations(MENU["dish_010"], Constraints(vegetarian_only=True, max_price_sar=20)) == []


def test_recommending_on_unreviewed_allergen_data_is_an_allergy_class_violation():
    unreviewed = dict(MENU["dish_010"], allergen_data_status="unverified")
    kinds = violations(unreviewed, Constraints(excluded_allergens={"milk"}))
    assert kinds == ["allergy_unreviewed_data"] and set(kinds) <= ALLERGY_KINDS
    assert violations(unreviewed, Constraints()) == []  # without a declared allergy the status does not matter


# ---- the two systems ----

def test_the_prompt_only_baseline_sees_the_whole_menu_and_every_constraint_in_plain_words():
    scenario = by_id()["co-milk-veg-budget"]
    message = prompt_only_message(scenario, scenario_menu(scenario))
    assert all(d["id"] in message for d in MENU.values())  # nothing was filtered out beforehand
    assert "عنده حساسية من: الحليب" in message and "نباتي" in message and "حتى ٢٠ ر.س" in message
    assert 'غير موثقة' in message and '{"picks": []}' in message  # it is told about unreviewed data and how to say "nothing"


def test_the_baseline_shows_whatever_the_model_picks_so_violations_are_possible():
    scenario = by_id()["al-milk"]
    outcome = run_prompt_only(scenario, everything)
    score = score_run(scenario, outcome)
    assert len(outcome.shown) == 5 and score["allergy_violation"] and "dish_002" in score["violating_dishes"] + outcome.shown


def test_the_pipeline_never_shows_a_violating_dish_even_when_the_model_names_every_dish():
    for scenario in SCENARIOS:
        outcome = run_pipeline(scenario, everything)
        score = score_run(scenario, outcome)
        assert not score["any_violation"], (scenario.id, score["violating_dishes"])
        if score["valid_dishes_exist"]:
            assert outcome.shown and outcome.model_calls == 1
        else:
            assert outcome.shown == [] and outcome.model_calls == 0  # no model call at all when nothing qualifies


def test_the_pipeline_survives_unreadable_model_output_with_valid_dishes_only():
    scenario = by_id()["co-milk-veg-budget"]
    outcome = run_pipeline(scenario, lambda s, u: "ليس JSON")
    assert outcome.shown and not score_run(scenario, outcome)["any_violation"]


def test_unreadable_baseline_output_is_counted_separately_not_as_a_violation():
    scenario = by_id()["al-milk"]
    outcome = run_prompt_only(scenario, lambda s, u: "عذرًا لا أستطيع")
    score = score_run(scenario, outcome)
    assert outcome.valid_output is False and not score["any_violation"] and not score["useful"]


def test_unknown_ids_named_by_the_baseline_are_ignored():
    outcome = run_prompt_only(by_id()["al-milk"], lambda s, u: picks_for("dish_999", "dish_001"))
    assert outcome.shown == ["dish_001"]


@pytest.mark.parametrize(
    "raw,expected",
    [
        ('{"picks": [{"dish_id": "a"}, {"dish_id": "b"}, {"dish_id": "a"}]}', ["a", "b"]),
        ('```json\n{"picks": []}\n```', []),
        ('{"picks": [' + ",".join('{"dish_id": "d%d"}' % i for i in range(9)) + "]}", ["d0", "d1", "d2", "d3", "d4"]),
        ("not json", None),
        ('{"other": 1}', None),
    ],
)
def test_parse_picks(raw, expected):
    assert parse_picks(raw) == expected


# ---- scoring ----

def test_score_run_separates_allergy_from_lower_risk_violations_and_usefulness():
    scenario = by_id()["co-milk-veg-budget"]  # milk allergy + vegetarian + <= 20
    allergy = score_run(scenario, Outcome(["dish_014"]))  # tea karak: vegetarian, cheap, contains milk
    assert allergy["allergy_violation"] and not allergy["useful"]
    lower = score_run(scenario, Outcome(["dish_001"]))  # chicken kabsa: not vegetarian, over budget (no milk)
    assert lower["any_violation"] and not lower["allergy_violation"] and set(lower["kinds"]) == {"diet", "budget"}
    clean = score_run(scenario, Outcome(["dish_010"]))
    assert clean["useful"] and not clean["any_violation"]
    assert score_run(scenario, Outcome([]))["refused_although_possible"]


def test_an_empty_answer_is_the_useful_one_when_nothing_qualifies():
    scenario = by_id()["im-veg-5"]
    assert score_run(scenario, Outcome([]))["useful"] and not score_run(scenario, Outcome(["dish_015"]))["useful"]


def test_wilson_interval_behaves():
    assert wilson(0, 0) == (0.0, 0.0)
    low, high = wilson(0, 30)
    assert low == 0.0 and 0.0 < high < 0.15
    low, high = wilson(15, 30)
    assert low < 0.5 < high and 0.0 <= low and high <= 1.0


def test_summarize_counts_runs_dishes_and_refusals():
    scenario = by_id()["al-milk"]
    rows = [score_run(scenario, o) for o in (Outcome(["dish_002", "dish_001"]), Outcome(["dish_001"]), Outcome([]))]
    s = summarize(rows)
    assert (s["runs"], s["runs_with_allergy_violation"], s["dishes_shown"], s["dishes_violating"]) == (3, 1, 3, 1)
    assert s["refused_although_possible"] == 1 and s["useful_runs"] == 1


# ---- stored results are scoreable without any API ----

def test_stored_results_round_trip_through_scoring_and_markdown(tmp_path):
    rows = []
    for arm, shown in (("prompt_only", ["dish_002"]), ("pipeline", ["dish_001"])):
        rows.append({"run_id": "t", "scenario": "al-milk", "category": "allergy_category", "arm": arm, "repeat": 0, "model": "m",
                     "effort": "low", "error": None, "shown": shown, "valid_output": True, "model_calls": 1, "raw": ""})
    rows.append({**rows[0], "arm": "pipeline", "repeat": 1, "error": "RecoverableProviderError", "shown": []})
    path = tmp_path / "r.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    loaded = evaluation_run.load_results(path)
    report = evaluation_run.score_results(loaded)
    assert report["prompt_only"]["overall"]["runs_with_allergy_violation"] == 1
    assert report["pipeline"]["overall"]["runs"] == 1 and report["pipeline"]["errored_runs"] == 1  # the errored run is excluded
    text = evaluation_run.markdown(report, loaded)
    assert "allergy_category" in text and "prompt_only" in text and "errored" in text


def test_a_run_above_the_call_cap_is_refused_before_any_call(monkeypatch, tmp_path):
    monkeypatch.setattr(evaluation_run, "_provider", lambda: pytest.fail("the provider must not be created"))
    with pytest.raises(SystemExit):
        evaluation_run.run_experiment(list(SCENARIOS), 3, 4, 10, tmp_path / "x.jsonl")


# ---- fault injection and prompt size (offline) ----

def test_with_a_model_that_ignores_every_constraint_only_the_pipeline_stays_clean():
    from evaluation import fault_injection

    results = fault_injection.run()
    for model, arms in results.items():
        assert arms["pipeline"]["runs_with_any_violation"] == 0 and arms["pipeline"]["dishes_violating"] == 0, model
        assert arms["prompt_only"]["runs_with_allergy_violation"] > 0, model  # the baseline's safety depends on the model


def test_fault_injection_is_deterministic():
    from evaluation import fault_injection

    assert fault_injection.run() == fault_injection.run()


def test_the_pipeline_prompt_is_smaller_and_impossible_scenarios_need_no_call():
    from evaluation import fault_injection

    for synthetic in (0, 45):
        sizes = fault_injection.prompt_sizes(synthetic)
        assert sizes["pipeline_avg_chars_when_called"] < sizes["prompt_only_avg_chars"]
        assert sizes["pipeline_scenarios_without_any_model_call"] == 10


def test_the_synthetic_menu_is_deterministic_consistent_and_keeps_impossible_scenarios_impossible():
    from evaluation.synthetic import synthetic_dishes
    from services.menu_service import consistency_problems, dish_problems

    first, second = synthetic_dishes(45), synthetic_dishes(45)
    assert first == second and len(first) == 45 and len({d["id"] for d in first}) == 45
    assert all(not dish_problems(d) and not consistency_problems(d) for d in first)
    for s in SCENARIOS:
        menu = scenario_menu(s, 45)
        assert len(menu) == 60
        if s.category == "impossible" or s.menu == UNREVIEWED:
            assert valid_dishes(menu, s.constraints) == [], s.id
        assert {d["id"] for d in valid_dishes(menu, s.constraints)} == {d["id"] for d in filter_dishes(menu, s.constraints).eligible}


def test_model_reasons_are_read_even_from_truncated_output_and_unsupported_claims_are_counted():
    from evaluation.core import claim_stats, model_reasons

    truncated = '{"picks": [{"dish_id": "a", "reason_ar": "طبق لا يحتوي على الحليب"}, {"dish_id": "b", "reason_ar": "طبق خفيف ولذيذ"}, {"dish_id": "c", "reason_ar": "طبق قط'
    assert model_reasons(truncated) == ["طبق لا يحتوي على الحليب", "طبق خفيف ولذيذ"]
    assert claim_stats([truncated, '{"picks": []}']) == {"reasons": 2, "with_unsupported_claim": 1}
    assert claim_stats([]) == {"reasons": 0, "with_unsupported_claim": 0}


def test_the_public_claim_detector_matches_what_the_guard_drops():
    from agent.guard import _clean_reason, has_unsupported_claim

    for text in ("خالٍ من الحليب", "ولا تتضمن مكوناته أي فطر", "هذا الطبق آمن تمامًا"):
        assert has_unsupported_claim(text) and _clean_reason(text, []) == ""
    assert not has_unsupported_claim("طبق أرز وخضار خفيف يناسب من يفضّل الأكل المتوازن")
