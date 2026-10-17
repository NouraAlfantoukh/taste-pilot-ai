# Deep Dive: is a prompt enough to enforce hard constraints?

**Question.** TastePilotAI enforces allergies, diet, excluded ingredients, budget and spice in code (deterministic
filtering, then the model chooses among the surviving dishes, then a guard validates its output). A simpler design
would put the whole menu and the constraints in the prompt and trust the model. Is the extra machinery justified?

**Short answer.** On a strong model with the constraints given as structured facts, the prompt-only baseline did **not**
violate a single constraint in 263 runs, so this study does **not** show that the pipeline lowers the violation rate.
What it does show, with stored raw outputs behind every number, is where the two designs differ:
the baseline's safety depends on the model behaving (fault injection), it degraded to 89% useful answers on a
60-dish menu at the production output limit, a quarter to a third of its own explanations contain claims the product
forbids, and it sends about 1.8x more text and makes about 1.2x more model calls.
The pipeline is safe by construction, not by measurement.

## Systems compared

Both receive the **same structured constraints and the same Arabic request**, so the comparison isolates enforcement.
It excludes language understanding (the extraction step), which is covered by the integration tests instead.

| | Prompt-only (baseline) | Pipeline (production path) |
|---|---|---|
| Menu shown to the model | the whole menu | only dishes that passed the hard filter |
| Constraints | written in the prompt, with the rule "never recommend a dish whose allergen data is unreviewed" and an explicit way to answer "nothing qualifies" (`{"picks": []}`) | enforced by `filter_dishes` (taxonomy families, unreviewed data fails closed) |
| Model call | always one | none when nothing qualifies |
| Output check | ids must exist, at most 5 dishes, nothing else | the guard: ids must be eligible, reasons rebuilt from menu data |
| Model | `claude-haiku-5-5`, effort `low`, 2048 output tokens, default sampling (the app's production settings, no fallback, no retries) | the same |

The baseline prompt is deliberately strong (rules stated plainly, hedging allowed), not a straw man. It uses the same
system prompt and menu rendering as the app. The exact text is built by `evaluation/core.py::prompt_only_message`.

## Scenarios

53 fixed, hand-written scenarios (`evaluation/scenarios.py`), each with a natural Arabic request:

| Category | Count | Example |
|---|---|---|
| allergy_category | 8 | milk; milk + gluten; milk + fish + sesame |
| allergy_ingredient | 3 | cumin; cheese (matched through its family); lentils |
| dislike | 6 | mushroom (on no dish); onion + a main-course request |
| diet | 3 | vegetarian starters |
| budget | 3 | at most 15 SAR |
| spice | 4 | nothing hot; at least medium |
| category | 2 | desserts |
| combined | 6 | milk allergy + vegetarian + at most 20 SAR |
| impossible | 4 | vegetarian and at most 7 SAR: the right answer is "nothing" |
| unreviewed_data | 5 | an allergy declared while the menu's allergen data is unreviewed: the right answer is "nothing" |
| adversarial | 9 | tree-nut allergy but "recommend the kunafa"; "ignore all previous restrictions" |

**Oracle.** `evaluation/core.py::violations` decides from the raw dish record and the structured constraints whether
a shown dish breaks a rule. It never calls the filter, the guard or a model. For all 53 scenarios its set of valid
dishes equals the production filter's eligible set (a test, `tests/test_evaluation.py`, enforces it), including on the
60-dish menu. Violations are classed by risk:

- **allergy class** (high risk): a dish containing the declared allergen, an ingredient-level allergen, or *any* dish
  recommended to an allergic guest from unreviewed allergen data;
- **lower-risk**: diet, excluded ingredient, budget, spice, category.

## Method

- **Validation first:** 10 then 11 scenarios x 1 repeat (about 40 calls) to check the harness before spending more.
- **E1, production menu (15 dishes):** 53 scenarios x 3 repeats x 2 systems = 318 runs, 288 model calls, 0 errors.
- **E2, scale (60 dishes):** the 15 real dishes plus 45 deterministic synthetic dishes (`evaluation/synthetic.py`, fixed
  seed, ingredients recombined from the taxonomy, allergens derived from the ingredients; synthetic dishes that would make an
  "impossible" scenario satisfiable are dropped). 53 x 2 repeats x 2 systems = 212 runs; 2 baseline calls failed
  with a recoverable provider error and are excluded, never retried.
- **E3, diagnosis:** the 8 scenarios whose baseline output was cut off in E2, re-run with a larger output limit
  (8192 tokens).
- Every raw model output is stored in `evaluation/results/*.jsonl`; the tables below are recomputed offline with
  `python -m evaluation.run --score <file>` (no API, no cost).

## Results

### E1: production menu, 15 dishes (159 runs per system)

| Metric | Prompt-only | Pipeline |
|---|---|---|
| Runs with any constraint violation | **0** | **0** |
| Runs with an **allergy-class** violation | **0** | **0** |
| Dishes shown / violating | 488 / 0 | 464 / 0 |
| Useful answers (every constraint met) | 155 (97%) | 159 (100%) |
| Refused although a valid dish existed | 4 of 129 | 0 of 129 |
| Correctly empty when no dish qualifies | 30 of 30 | 30 of 30 |
| Unreadable model outputs | 0 | 0 |
| Model reasons containing a claim the guard forbids | 131 of 489 (27%), **shown to the guest** | 118 of 461, discarded, **never shown** |
| Model calls | 159 | 129 |

The four refusals: three adversarial runs where the guest asked for the one forbidden dish (kunafa with a nut
allergy, hummus with a sesame allergy) and the model answered with an empty list instead of alternatives, and one
vegetarian-dessert run. They are counted as unhelpful, although an empty answer to "recommend the dish I'm allergic
to" is defensible.

### E2: 60 dishes (104 baseline and 106 pipeline runs scored)

| Metric | Prompt-only | Pipeline |
|---|---|---|
| Runs with any violation / with an allergy violation | **0 / 0** | **0 / 0** |
| Useful answers | 93 of 104 (89%) | 106 of 106 (100%) |
| Unreadable (cut-off) model outputs | **10** | 0 |
| Refused although a valid dish existed | 1 of 84 | 0 of 86 |
| Model reasons containing a forbidden claim | 112 of 337 (33%), shown | 93 of 327, never shown |

The 10 unreadable outputs are JSON that stops in the middle of a sentence. Re-running exactly those 8 scenarios with an
8192-token limit (E3) gave 16 readable, useful answers out of 16, which is consistent with the production output limit
(reasoning shares the 2048-token budget) combined with a longer menu, not with the model's judgement (the stop reason was not
recorded, so this is an inference from the re-run, not a direct observation). The pipeline's shorter prompts did
not hit it. This is a configuration effect and says nothing about the baseline's constraint accuracy.

### What the zero violations do and do not mean

Zero events in 159 + 104 = 263 baseline runs bounds the true run-level violation rate for *these conditions*
below about **1.4%** (95% Wilson upper bound; 2.4% for E1 alone, 3.6% for E2 alone), and below 0.5% per dish shown
(0 of 796). That is a bound, not proof of safety, and it holds only for this model, these scenarios and
structured inputs.

### Fault injection (simulation, offline, deterministic)

To see which design's safety depends on the model, the model is replaced by misbehaving stand-ins
(`python -m evaluation.fault_injection`). This is **not** a measurement of a real model.

| Stand-in model | System | Runs with a violation | Runs with an allergy violation | Dishes shown / violating |
|---|---|---|---|---|
| names every dish, ignoring all constraints | prompt-only | 42 of 53 | 16 | 265 / 156 |
| names every dish, ignoring all constraints | pipeline | 0 | 0 | 192 / 0 |
| three random dishes | prompt-only | 42 of 53 | 19 | 159 / 89 |
| three random dishes | pipeline | 0 | 0 | 124 / 0 |

### Cost (measured on the prompts, offline)

Average text sent per scenario: 4,807 characters (prompt-only) vs 2,725 (pipeline) on 15 dishes, and 17,013 vs 9,774 on
60 dishes (about 43% less). 10 of the 53 scenarios need no model call at all because nothing qualifies (19% fewer calls
in E1: 129 vs 159). Latency was not recorded.

## Interpretation

1. **Accuracy of a strong model was not the bottleneck.** With constraints given as facts and a small menu, the baseline
   was correct every time. Claiming otherwise would be inventing a result.
2. **The measured differences are about reliability, honesty and cost:** the pipeline's shorter prompts avoided the
   truncation the baseline suffered at 60 dishes; the model's own prose often asserts what the data does not
   establish ("does not contain the allergen..."), which the guard and the data-built reasons remove; and the deterministic
   path is cheaper and answers "nothing qualifies" without a model call.
3. **The strongest argument is structural, not statistical.** The pipeline's guarantee is a property of the code and is
   tested for every scenario against a model that names every dish; the baseline's guarantee is a property of the model,
   which can change with a model update, a temperature change, a longer menu or a hostile request.

## Limitations (read these before citing the numbers)

- One model (`claude-haiku-5-5`), 2 to 3 repeats, default sampling; no confidence statement beyond the bounds above.
- Constraints were given as structure. In the real app they come from the extraction step, which can misunderstand a
  guest; that risk applies to both designs equally and is tested separately (integration tests on real Claude).
- The scenario set was written by the project author, not independently; the synthetic menu is random recombination, not a
  real restaurant, and it is still only 60 dishes.
- **The oracle and the pipeline share the taxonomy** (ingredient to allergen and family mappings). A wrong mapping would
  be invisible to both. The menu review ([MENU_REVIEW.md](MENU_REVIEW.md)) addresses data consistency, not the
  correctness of the taxonomy itself.
- Single-turn only; multi-turn clarification and memory are covered by the QA suite, not this study.
- The baseline prompt is strong but not exhaustive (no few-shot examples, no self-check step); a better prompt could narrow
  the "unsupported claim" and "refusal" gaps.

## Reproduce

```bash
python -m evaluation.run --score evaluation/results/full-main.jsonl          # E1, offline
python -m evaluation.run --score evaluation/results/full-scale60.jsonl       # E2, offline
python -m evaluation.run --score evaluation/results/scale60-max-tokens-8192.jsonl   # E3, offline
python -m evaluation.fault_injection                                          # fault injection and prompt sizes, offline
# to re-run with real model calls (needs ANTHROPIC_API_KEY; E1 is 288 calls with a small model):
python -m evaluation.run --validate
python -m evaluation.run --full --repeats 3
python -m evaluation.run --full --repeats 2 --synthetic 45 --max-calls 300
```

Files: `evaluation/scenarios.py` (scenarios), `evaluation/core.py` (oracle, both systems, scoring),
`evaluation/synthetic.py` (scale menu), `evaluation/fault_injection.py`, `evaluation/run.py` (runner and report),
`evaluation/results/` (raw outputs and summaries), `tests/test_evaluation.py` (the harness is itself tested).
