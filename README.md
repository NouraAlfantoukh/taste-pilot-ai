# TastePilotAI

An Arabic-first restaurant assistant that understands what a guest likes, **remembers it across conversations**, and
recommends dishes from the real menu, while **hard safety constraints (allergies, diet, excluded ingredients, budget,
spice) are enforced in code, never left to the language model alone.**

The interface is right-to-left Arabic (Saudi/Gulf tone), prices are in Saudi riyals (`٣٥ ر.س`). Code, file names and logs
are English.

- **Live app:** _add the Streamlit URL here after deployment_ (see [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md))
- **Deep Dive (evidence for the central design decision):** [docs/DEEP_DIVE.md](docs/DEEP_DIVE.md)
- **QA report:** [docs/QA_REPORT.md](docs/QA_REPORT.md) · **Menu review process:** [docs/MENU_REVIEW.md](docs/MENU_REVIEW.md)

## What it does

| Capability | How |
|---|---|
| Natural conversation in Arabic | An LLM interprets the message into structured fields (allergies, dislikes, diet, budget, spice, category, serving temperature, "dishes with X", memory questions). |
| Safe recommendations | Python filters the menu with the guest's hard constraints; the model only chooses and orders among the dishes that survived; a guard validates its output. |
| Allergy safety, fail closed | A declared allergy removes every dish that contains it **and** every dish whose allergen data is not reviewed. Unknown or ambiguous allergens block recommendations until clarified and are never stored or guessed. |
| Memory across conversations | Signed-in guests get a durable profile (allergies, dislikes, vegetarian, spice limits) stored in Supabase under Row Level Security. One-off constraints (budget, category, temperature) are never saved. |
| Honest explanations | Each recommendation's reason is built from menu data (e.g. "غير حار" only if the recorded spice level is `none`), never from model prose. |
| Transparent profile | A read-only "ملفي الغذائي" panel shows exactly what is saved, what is unsaved, and what failed to save. |
| Graceful degradation | Claude is primary, Gemini is a fallback for recoverable errors only; missing keys, outages and expired sessions produce clear Arabic messages. |

## Architecture

```
guest message
   │
   ▼
extraction (LLM → strict JSON, validated)          agent/extraction.py
   │  structured fields only
   ▼
state update + clarification rules (pure Python)   agent/orchestrator.py, agent/state.py
   │  allergies / dislikes / diet / budget / spice
   ▼                                   ┌─────────────── durable profile ───────────────┐
hard filtering (taxonomy, safety)      │ agent/persistence.py → services/memory_service │
services/recommendation_service.py     │ Supabase `user_preferences`, RLS, anon key     │
services/safety_service.py             └────────────────────────────────────────────────┘
   │  eligible dishes only
   ▼
LLM picks and orders among eligible dishes
   ▼
guard: ids validated, reasons rebuilt from menu data   agent/guard.py, agent/explain.py
   ▼
Arabic reply + dish cards                           ui/components.py, app.py
```

Design rules that the code and tests enforce:

1. **The model never decides safety.** It cannot add a dish the filter removed (the guard drops unknown ids) and its
   free text is never shown as a reason.
2. **Unknown is not safe.** Unreviewed allergen data, unknown ingredient ids and unresolved allergen terms all exclude
   rather than include.
3. **Only confirmed facts are saved,** and the UI never claims something was saved when the write failed.
4. **Identity comes from Supabase's response to sign-in,** never from a form field; each browser session owns its client.

## Technology

Python 3 · Streamlit · Anthropic Claude (primary) · Google Gemini (fallback) · Supabase Postgres + Auth (memory, RLS) ·
local JSON menu and taxonomy · pytest (+ `pglast` for SQL checks, optional).

## Repository layout

```
app.py                      Streamlit entry point (auth gate, chat, profile panel)
config.py                   settings from environment / Streamlit secrets
agent/                      extraction, orchestration, state, persistence bridge, guard, explanations, prompts
services/                   LLM providers, menu, taxonomy, constraints, safety, recommendation, memory, auth, menu review
ui/                         components and styles (RTL, Arabic)
data/                       menu.json, taxonomy.json, menu_review.json
db/                         Supabase client factory (per session, anon key only)
supabase/                   migration (table + RLS policies) and notes
evaluation/                 Deep Dive harness: scenarios, oracle, both systems, scoring, stored raw results
tests/                      offline suite + opt-in integration tests (real Claude / real Supabase)
docs/                       DEEP_DIVE.md, QA_REPORT.md, MENU_REVIEW.md, DEPLOYMENT.md
```

## Run locally

```bash
python -m venv .venv
# Windows: .venv\Scripts\Activate.ps1      macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # fill in your own values; .env is git-ignored
streamlit run app.py
```

| Variable | Purpose |
|---|---|
| `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL` | Claude (primary) |
| `GEMINI_API_KEY`, `GEMINI_MODEL` | Gemini (fallback, recoverable errors only) |
| `SUPABASE_URL`, `SUPABASE_KEY` | Accounts and memory. **Anon/publishable key only**: the app refuses a `service_role` key. Leave empty to run as a guest-only app that saves nothing. |

Secrets live in `.env` (local) or in the hosting platform's secrets, never in the repository.
Apply `supabase/migrations/*.sql` to your Supabase project for persistence (details in [supabase/README.md](supabase/README.md)).

## Deep Dive: constraining an LLM with deterministic code

Full write-up (method, denominators, tables, limitations, claims and what is *not* claimed): [docs/DEEP_DIVE.md](docs/DEEP_DIVE.md).
53 fixed scenarios, a model-independent oracle, about 550 real calls to `claude-haiku-5-5`, every raw output stored and re-scored offline.

| Evidence | Prompt-only | Pipeline |
|---|---|---|
| Constraint violations, 15 dishes (159 runs) **[measured]** | 0 | 0 |
| Constraint violations, 60 dishes (104 / 106 runs) **[measured]** | 0 | 0 |
| Useful answers, 15 / 60 dishes **[measured]** | 97.5% / 89.4% | 100% / 100% |
| Model reasons containing an absence or safety claim the product forbids **[measured]** | 27% / 33%, shown to the guest | 0 of 440 displayed (replay) |
| User-message size on the same scenarios **[measured, characters]** | 4,795 / 16,980 | 2,768 / 9,817 (-42%) |
| Scenarios needing no recommendation call **[measured]** | 0 of 53 | 10 of 53 |
| Scenarios with a violation if the model ignores every constraint **[simulated]** | 42 of 53 | 0 of 53 |

Honest reading: a strong model with structured constraints did **not** violate any rule in 263 runs, so the study does not claim
the pipeline lowered the real violation rate. It shows a structural guarantee that does not depend on model behaviour, shorter
prompts, avoided calls, no truncation at 60 dishes (the baseline lost 10 of 104 answers; the cause is inferred, not proven) and
explanations that cannot carry the model's unverified assurances. Characters are not tokens and no monetary cost was measured.

## Tests

```bash
pytest                         # offline: scripted LLM + a fake Supabase that emulates RLS; no network, no cost
pytest -m integration -s       # opt-in: real Claude (a few cents) and, with test accounts, real Supabase
python -m evaluation.run --score evaluation/results/full-main.jsonl   # recompute the Deep Dive numbers offline
```

The offline suite covers extraction parsing, the taxonomy, allergy and ingredient filtering, the guard, menu data
consistency and review, multi-turn Arabic conversations (including the allergy clarification flows), persistence,
authentication errors, user isolation, failure handling, the UI components and the Deep Dive harness. Real-service
tests exercise Row Level Security with two authenticated users, persistence across independent sessions and the key
conversations on real Claude. Results are in [docs/QA_REPORT.md](docs/QA_REPORT.md).

## Demo script (about 10 minutes)

1. Sign in (or create a user), then say «ما أحب الفطر أبدًا، ولا تقترح علي أي طبق يحتوي عليه.» and answer the allergy
   question with «ما عندي حساسية».
2. Press «محادثة جديدة» and ask «وش الأكلات اللي تعرف إني ما أحبها؟» (memory, kept apart from allergies), then
   «اقترح لي أطباق فيها فطر» (honest "not on the menu" answer, no unrelated dishes).
3. Say «معليش عندي حساسية» then «الحليب»: the allergy is stored separately and milk dishes never appear.
4. Open «ملفي الغذائي»; sign out, sign in as another user (empty profile), back again (profile restored).

## Known limitations

- **The menu is fictional** (15 dishes). `verified` allergen status is earned through a documented, fingerprinted review
  ([docs/MENU_REVIEW.md](docs/MENU_REVIEW.md)); the current record is an AI-assisted review of test data, **not a kitchen
  audit**. Only 14 allergen categories are tracked; cross-contact and recipe variation are not modelled, and the
  assistant never calls a dish "safe".
- A dislike for a food outside the known ingredient vocabulary is acknowledged but cannot be saved (the stored profile
  only holds known ingredient ids; supporting free text would need a schema change).
- Favourite ingredients and a usual budget are not stored (no schema support), so the profile does not show them.
- Model behaviour (interpretation of unusual phrasings) is probabilistic. Safety does not depend on it, but the quality of
  the clarification questions does; the Deep Dive reports where the model alone is and is not reliable.
- Developed and fully tested on Python 3.14; see [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) for the platform's Python setting.
