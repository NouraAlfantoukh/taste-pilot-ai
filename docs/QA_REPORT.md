# TastePilotAI — QA report

Run date: 2026-10-17. Offline suite: **634 passed, 52 deselected** (the deselected tests are the opt-in `integration` ones).

## Reply-quality rubric

Applied automatically to every assistant reply in `tests/test_conversation_qa.py` (`check_reply`) and, for the
points a script cannot judge, reviewed by hand against the scenarios.

| # | Criterion | How it is checked |
|---|-----------|-------------------|
| 1 | Answers the actual question | memory questions are answered from the stored state (`ResponseType.INFO`), never with "what do you want to eat?" |
| 2 | Uses the available memory correctly | the answer lists exactly what is stored/in force; dislikes and allergies stay in separate lines |
| 3 | Consistent with known preferences | shown dishes are asserted against the real menu data (ingredients, spice, price, vegetarian) |
| 4 | No unsupported claims | no "safe", "free of", "does not contain" statements (guard drops them), no "saved" claim when a save failed |
| 5 | Clear, natural Arabic | reply is mostly Arabic, no Latin words, no leaked internals (ids, enum names, `None`, JSON), no repeated lines, bounded length |
| 6 | Ambiguity handled | ambiguous or unknown allergens are asked about, block recommendations and are never stored or guessed |

## Root causes of the reported failure

The reported conversation failed for **three independent reasons**:

1. **Vocabulary / matching (P1).** The taxonomy only knew the 70-odd ingredients that appear on the menu. «فطر» is not
   on the menu, so it was "unknown": the dislike was dropped (never stored) and the user got "ما عرفت المقصود".
   Also, only Arabic names resolved; English names did not.
2. **Persistence consequence (P1).** Because the term was dropped, nothing was saved. The later "no allergies" answer was
   correct and saved, but there was no dislike to recall. Unknown ingredient dislikes still cannot be stored (see limitations).
3. **Retrieval / intent (P1).** The app had **no way to answer a question about its own memory**. «وش الأكلات اللي تعرف إني
   ما أحبها؟» was treated as an empty request and answered with "وش تبغى أرشّح لك؟". Not a persistence bug: the stored
   profile loads correctly into a new conversation (verified by the tests); there was simply no feature that reads it back to the user.

Not a prompt/context problem: the generation prompt already receives the applied conditions.

## Issues found and fixed

| Priority | Issue | Fix | Regression tests |
|---|---|---|---|
| P1 | «فطر» and other common non-menu foods unknown | 13 common ingredients added to `data/taxonomy.json` (mushroom, egg, shrimp, eggplant, olives, ...) | `test_ingredient_names_in_arabic_english_and_common_variants_resolve`, the reported-conversation tests |
| P1 | English / variant names did not resolve | `aliases_en` support in `services/taxonomy.py`, English names for every ingredient and allergen | same parametrized test (28 spellings) |
| P1 | No answer to "what do you know about me?" | extraction field `memory_query`; `memory_summary_ar` renders the answer from state, no LLM, no state change, no allergy interview | `test_the_memory_answer_*`, `test_a_memory_question_*`, real-Claude test |
| P1 | A memory question while the stored profile was unreadable/unavailable would say "nothing recorded" | `ConversationState.profile_unavailable`, set by the app, answered honestly | `test_an_unreadable_stored_profile_*` (state and Streamlit levels) |
| P2 | An unknown dislike was reported as "I don't understand" even when no dish can contain it | says "no dish on the menu contains X (per its data); I cannot save it permanently" when the term appears nowhere in the menu text; still asks when it might be on the menu | `test_an_unknown_dislike_*`, `test_an_unknown_term_that_might_be_on_the_menu_*` |
| P2 | Newly stated dislikes were not acknowledged | the reply states what will be excluded ("راح أستبعد الأطباق التي تحتوي على ...") — says applied, never "saved" | `test_the_answer_names_what_was_applied_*` |
| P2 | The guard let through absence claims worded differently ("ولا تتضمن مكوناته أي فطر", seen in a real Claude reply) | negation + containing-verb pattern added to `agent/guard.py` | 5 new cases in `tests/test_guard.py` |

Each fix was verified to be load-bearing by mutation: reverting the memory short-circuit fails 11 tests, reverting the
taxonomy fails 15, ignoring `profile_unavailable` fails 1, removing the acknowledgement fails 1, and always making the
"not on the menu" claim fails 1.

## Verified against what

| Area | Verified by |
|---|---|
| Extraction → constraints → filtering → guard → rendering; persistence, recall, isolation, failure handling | offline: scripted LLM, real menu, **fake** Supabase emulating RLS (`tests/fake_supabase.py`) |
| Streamlit flows (sign-in, new chat, notices, footer) | offline `AppTest` |
| **Real Supabase: RLS isolation between users, CHECK constraints, timestamps, anon refused** | `test_supabase_rls.py`: 15 passed |
| **Real Supabase: same identity across visits, persistence across independent clients, second user gets defaults** | `test_supabase_memory.py`: 4 passed |
| **Real Supabase: the mushroom conversation across fresh sessions, updates overwrite one row, per-request budget never stored, B never sees/changes C, forged-id write refused by the DB, signed-out client refused, tampered row refused and not overwritten, account switching in the real app** | `test_real_conversation_memory.py`: 8 passed |
| Real Claude: reported conversation, plus 4 paraphrases of the memory question and 2 non-questions | `test_real_memory_flow.py`: 7 passed |

Real-service total: 27 Supabase tests (172 s) + 7 Claude tests, all passed. Tests wrote and deleted only the
`user_preferences` rows of the dedicated test accounts, signed in with the normal anon key and their own sessions
(no `service_role`); auth users were not touched.

## Remaining limitations (ranked)

1. ~~P1 — real RLS unproven~~ — now verified against the real project (see above), for the accounts used.
2. **P2 — Dislikes outside the known vocabulary cannot be persisted.** The stored profile only holds known ingredient ids;
   a free-text column would need a new migration (not applied automatically). The app says so honestly instead of pretending.
3. **P2 — A preference statement alone ends in recommendations** (existing design: "stating a condition counts as asking").
   It is now acknowledged, but a user who only wants to record a preference still gets dishes.
4. **P2 — Extraction quality depends on the model.** The deterministic parts are covered; the model's choice of `memory_query`
   was confirmed only for the reported sentences on real Claude, not for every paraphrase.
5. ~~P3 — footer claimed saving after a failed save~~ — fixed: the footer now says the change is applied to this conversation only (`test_the_footer_claims_saving_only_when_saving_works` and two app-level tests).
6. **P3 — All dishes are "unverified"**, so any real allergy yields the "ask the staff" answer (intended, honest).

## Manual action still required

- Optional: set up a second Supabase project for the demo so test accounts never share a project with real users.
- Open question from the earlier investigation: why Supabase rejects `a@gmail.com` as invalid (the app sends it unchanged).

## Sprint 3 addendum (exploratory QA, profile, "dishes with X")

Offline: **669 passed, 56 deselected**. Real services: 34 Supabase/Claude tests passed after the changes
(`test_supabase_rls` 15, `test_supabase_memory` 4, `test_real_conversation_memory` 8, `test_real_memory_flow` 7) plus
4 new in `test_real_sprint_scenario` (the manual conversation on real Supabase, three sentences on real Claude).

| Priority | Issue found | Fix |
|---|---|---|
| P0 | "I have an allergy" (type not yet given) left a stale `none_reported` in the database; abandoning the chat meant the next session believed "no allergies" | an unresolved allergy disclosure now saves `not_asked` (never an unconfirmed allergen), so the next session asks again |
| P1 | "اقترح لي أطباق فيها فطر" returned unrelated dishes and ignored the clash with the saved dislike | new per-request `requested_ingredients`: no dish contains it → say so (and mention the dislike); dish exists but clashes with a dislike → ask before lifting it for that request only (never saved); allergy clash → refuse, never overridable |
| P2 | memory answer ignored an open allergy disclosure | says the allergy type is still unspecified |
| UX | no view of saved data | read-only "ملفي الغذائي" expander built from the store-confirmed profile only; unspecified fields shown as such; unsaved/failed-save/unavailable states shown; favourites and usual budget shown as "not supported" |

Profile fields supported by the schema: excluded (disliked) ingredients, allergen categories and ingredient-level
allergies (+ allergy status), vegetarian flag, minimum/maximum spice. Not stored: favourites, usual budget.

## Sprint 4 addendum (reviewed menu and allergy recommendations)

Offline: **890 passed, 56 deselected**. The 15 dishes now carry `verified` backed by `data/menu_review.json`
(process in [MENU_REVIEW.md](MENU_REVIEW.md)); allergic guests receive dishes that do not contain their allergen,
and unreviewed or tampered data still fails safe. Real services: `test_real_sprint_scenario` and
`test_real_extraction` (10 tests) passed against real Supabase and Claude after the change.

## Sprint 5 addendum (allergy clarification, Deep Dive, delivery)

- **Allergy clarification loops fixed** (milk and cream; pistachio): milk derivatives and pistachio are now recognised
  vocabulary, and a guest's explicit "that's all / only milk" closes every open allergy question (never removes a confirmed
  allergy, never stores an unconfirmed term). 25 focused tests plus real Claude and real Supabase checks.
- **Deep Dive added** (`docs/DEEP_DIVE.md`, `evaluation/`): 53 scenarios, two systems, real-model runs, offline fault
  injection; the harness itself is tested (`tests/test_evaluation.py`).
- **Delivery hygiene:** dependencies pinned, `.gitignore` covers every virtual environment and local assistant settings,
  `docs/DEPLOYMENT.md` documents the Streamlit Community Cloud deployment.
- Offline suite at this point: **996 passed, 59 deselected** (the deselected tests are the opt-in real-service ones).
