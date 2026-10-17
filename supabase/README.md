# Supabase: accounts and persistent preferences

- **Phase 1** (done): the `public.user_preferences` table, constraints and Row Level Security
  (`supabase/migrations/`).
- **Phase 2** (this document, below): email/password accounts in the Streamlit app, loading the user's durable
  preferences when a session starts, and saving confirmed changes.

Supabase is optional. With `SUPABASE_URL` / `SUPABASE_KEY` empty the app behaves exactly as before and saves nothing.

## What is stored

One row per authenticated user in `public.user_preferences`, holding only the **durable** profile as taxonomy ids
(`data/taxonomy.json`), never Arabic text:

| Column | Meaning |
|---|---|
| `user_id` | Primary key, `auth.users(id)`, `on delete cascade` (deleting the user deletes the row). Defaults to `auth.uid()`. |
| `excluded_allergens` | Allergen category ids, e.g. `milk`, `tree_nuts` |
| `allergy_ingredients` | Ingredient-level allergies, e.g. `lentils` |
| `excluded_ingredients` | Plain dislikes (never treated as allergies) |
| `vegetarian_only` | Diet |
| `min_spice_level` / `max_spice_level` | Spice floor and ceiling (`none`, `mild`, `medium`, `hot`) |
| `allergy_status` | `not_asked`, `none_reported`, `reported`, `declined` (`awaiting_answer` is transient and never stored) |
| `created_at` / `updated_at` | Maintained by the database |

Deliberately **not** stored: conversation text, and per-request constraints (category, budget, serving temperature,
last request). `services/memory_service.py::UserPreferences` mirrors these columns one to one (its older Arabic-text
fields, a budget and liked dishes were retired because the database does not store them).

## Phase 1 security design

- **Row Level Security is enabled and forced.** Four policies, all `to authenticated`, all `(select auth.uid()) = user_id`:
  select (using), insert (with check), update (using **and** with check), delete (using).
- Privileges are reset (`revoke all` from `public`, `anon`, `authenticated`), then `select, insert, update, delete` is
  granted to `authenticated` only. `anon` has no access.
- A trigger keeps `user_id` and `created_at` immutable and maintains `updated_at`.
- Check constraints reject malformed ids, invalid spice levels and statuses, a floor above the ceiling, and a record
  that claims `none_reported` while listing allergies. Whether an id exists in the taxonomy is validated by the
  application, which owns the taxonomy.
- **The `service_role` / secret key bypasses RLS and must never be in application code.** `SUPABASE_KEY` must be the
  anon/publishable key; `db/database.py` refuses to create a client with a privileged key.

## Phase 2: how the app uses it

### Identity and persistence across visits

- Sign-in is **email + password** (Supabase Auth). The identity is the account: signing in again, from any browser
  or after a restart, yields the same `auth.users.id`, so the same `user_preferences` row.
- Anonymous sign-in is deliberately **not** used: an anonymous user is identified only by a token kept in the
  browser, and Streamlit cannot reliably keep it across a refresh or a browser restart, so every visit would be a new
  user. A real account does not depend on browser storage.
- The identity comes only from Supabase's response to the sign-in. No form field, query parameter or message is ever
  treated as a user id.
- **Each browser session has its own Supabase client**, held in its own `st.session_state` (never a module-level
  object, which would share one user's session with everyone). Signing out, expiry or signing in again replaces the
  whole session state, so nothing from a previous visitor survives.
- A page refresh starts a new Streamlit session, so the user signs in again (the data is already in their account).
  Keeping the login across refreshes would need a cookie component and is intentionally out of scope.
- The user can also continue **as a guest**: nothing is read or saved.

### Loading, updating and saving

1. **Sign-in:** `PreferenceSync.load()` reads the user's row through `SupabaseStore` (their own session, RLS applied).
   No row means the defaults. The profile is applied to a new `ConversationState`: allergies, ingredient exclusions,
   diet and spice bounds, and the allergy status (so a returning user is not asked the initial allergy question
   again). Category, budget and temperature always start empty.
2. **During the conversation** the orchestrator works exactly as before, on the in-memory state.
3. **After every answer** `PreferenceSync.save()` stores the durable fields only if they changed, using
   `SupabaseStore.update_preferences`, which validates against the taxonomy before any write and upserts the
   row with the session's user id. Only *confirmed* allergies are saved; a term still awaiting clarification is never
   stored or guessed.
4. **"New conversation"** keeps the loaded profile and clears the per-request constraints.

Fail-closed rules: a stored profile that cannot be read faithfully (for example an id the taxonomy no longer knows)
is not partially applied and saving is disabled for the session, so it is never overwritten, and the allergy
question is asked again; "reported" with no recorded allergy is not trusted (asked again); a saved "declined" is
asked once more in a new session; a failed save shows a notice and is retried, and never interrupts the chat; an
expired session sends the user back to sign-in.

## Manual setup (required once per Supabase project)

1. Create a Supabase project (use a separate **development** project for tests).
2. Apply the migration, either:
   - SQL editor: paste `supabase/migrations/20261017120000_create_user_preferences.sql` and run it; or
   - CLI: `supabase init` (once), `supabase link --project-ref <ref>`, `supabase db push`.
3. **Authentication > Sign In / Providers:** keep the *Email* provider enabled. Decide on **Confirm email**:
   on (recommended for production): a new user must click the link in the email before signing in; the app explains
   this. Off (convenient for development): sign-up signs the user in immediately.
4. Put the project URL and the **anon/publishable** key in `.env` (never commit it, never use `service_role`):
   `SUPABASE_URL=...` and `SUPABASE_KEY=...`. Restart the app.
5. Confirm in Table Editor that `user_preferences` shows "RLS enabled".

Rollback (development only): `drop table public.user_preferences; drop function public.tp_touch_user_preferences();
drop function public.tp_valid_id_array(text[]);`

## Verification

| Level | Command | Needs |
|---|---|---|
| Offline (default suite) | `python -m pytest -q` | nothing. Includes the SQL structure checks, the store, sync and auth logic, and the Streamlit account flows against an in-memory Supabase stand-in (`tests/fake_supabase.py`) |
| Offline, with SQL parsing | `pip install -r requirements-dev.txt`, same command | `pglast` (skipped when absent) validates the SQL as PostgreSQL |
| **Real project: database isolation** | `python -m pytest -m integration tests/integration/test_supabase_rls.py -v` | a development project with the migration applied and two confirmed test users |
| **Real project: app code path, identity, memory** | `python -m pytest -m integration tests/integration/test_supabase_memory.py -v` | the same |

The offline tests prove what the code and SQL declare; the fake emulates RLS but is not Postgres. Only the two real
tests show that a live project enforces it. Create two users in Authentication > Users (**Add user**, with *Auto
Confirm User* ticked) and put them in `.env` (not committed):

```
SUPABASE_TEST_USER_A_EMAIL=...   SUPABASE_TEST_USER_A_PASSWORD=...
SUPABASE_TEST_USER_B_EMAIL=...   SUPABASE_TEST_USER_B_PASSWORD=...
```

### Manual two-user and persistent-identity check in the real app

1. `streamlit run app.py`. You should see the sign-in screen.
2. In a normal window, create/sign in as **user A**. Tell the assistant, for example: *عندي حساسية من الحليب ونباتي*.
   In Table Editor, `user_preferences` now has one row with `excluded_allergens = {milk}`, `vegetarian_only = true`,
   `allergy_status = reported` and **no** category, budget or conversation text.
3. **Persistent identity:** close the tab (or the browser), open the app again, sign in as A. The chip shows
   "حساسية: الحليب", the "welcome back" notice appears, and the initial allergy question is **not** asked.
4. **Isolation:** in a private window, sign in as **user B**. B sees no allergy chip, is asked the allergy question,
   and B's row (after B says something durable) is separate from A's. In the SQL editor, `select user_id from
   user_preferences` as the project owner shows two different ids.
5. Sign A out and B in in the same window: nothing of A remains on screen.
6. In A's account, say *ما عندي حساسية من الحليب*; the row's `excluded_allergens` becomes empty on the next save.

## Security assumptions and limitations

- Isolation rests on RLS (checked by the real-project tests) plus the application layer (the store refuses any other
  user id). The anon key is public by design; it is safe only because of RLS. Never ship `service_role`.
- Passwords are handled by Supabase Auth and are never stored or logged by the app; the sign-in form clears itself.
- Allergies are health data: use a project and region that fit your obligations. There is no in-app "delete my data"
  button yet (deleting the account in Supabase deletes the row by cascade); a correction in chat updates the row.
- Unresolved allergy terms are not stored: a user who leaves mid-clarification is asked again next time.
- No password reset, social login or "keep me signed in after refresh" yet.
- Email confirmation, rate limits and password rules are configured in the Supabase project, not in this app.
