-- TastePilotAI persistent memory, phase 1: one durable preference record per authenticated user.
--
-- Stores only DURABLE preferences, as canonical taxonomy ids (data/taxonomy.json): allergies, ingredient
-- exclusions, diet and spice bounds. It deliberately does NOT store conversation text, nor per-request
-- constraints (category, budget, serving temperature, last request), nor any credential.
--
-- Isolation is enforced by Postgres itself: Row Level Security with auth.uid() = user_id. Application
-- code must use the user's own session with the anon/publishable key, never the service_role key.

-- Shape check for arrays of taxonomy ids: <= 50 items, no NULLs, ids look like "milk" or "tree_nuts".
-- Whether an id exists in the taxonomy is validated by the application (it owns the taxonomy).
create or replace function public.tp_valid_id_array(ids text[])
returns boolean
language sql
immutable
parallel safe
set search_path = ''
as $$
  select ids is not null
     and cardinality(ids) <= 50
     and not exists (
       select 1 from unnest(ids) as id where id is null or id !~ '^[a-z][a-z0-9_]{0,63}$'
     );
$$;

create table public.user_preferences (
  user_id uuid not null default auth.uid() references auth.users (id) on delete cascade,

  excluded_allergens   text[] not null default '{}',  -- allergen category ids, e.g. milk, tree_nuts
  allergy_ingredients  text[] not null default '{}',  -- ingredient-level allergies, e.g. lentils
  excluded_ingredients text[] not null default '{}',  -- plain dislikes; never treated as allergies
  vegetarian_only      boolean not null default false,
  min_spice_level      text,                          -- "at least this spicy"
  max_spice_level      text,                          -- "nothing hotter than this"
  allergy_status       text not null default 'not_asked',

  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),

  constraint user_preferences_pkey primary key (user_id),
  constraint user_preferences_allergens_valid check (public.tp_valid_id_array(excluded_allergens)),
  constraint user_preferences_allergy_ingredients_valid check (public.tp_valid_id_array(allergy_ingredients)),
  constraint user_preferences_excluded_ingredients_valid check (public.tp_valid_id_array(excluded_ingredients)),
  constraint user_preferences_min_spice_valid
    check (min_spice_level is null or min_spice_level in ('none', 'mild', 'medium', 'hot')),
  constraint user_preferences_max_spice_valid
    check (max_spice_level is null or max_spice_level in ('none', 'mild', 'medium', 'hot')),
  constraint user_preferences_spice_order
    check (
      min_spice_level is null
      or max_spice_level is null
      or array_position(array['none', 'mild', 'medium', 'hot'], min_spice_level)
         <= array_position(array['none', 'mild', 'medium', 'hot'], max_spice_level)
    ),
  -- "awaiting_answer" is a transient conversation state and is never persisted.
  constraint user_preferences_allergy_status_valid
    check (allergy_status in ('not_asked', 'none_reported', 'reported', 'declined')),
  -- Safety: a record can never claim "no allergies" while listing allergies.
  constraint user_preferences_none_means_none
    check (
      allergy_status <> 'none_reported'
      or (cardinality(excluded_allergens) = 0 and cardinality(allergy_ingredients) = 0)
    )
);

comment on table public.user_preferences is
  'One durable preference record per user (taxonomy ids only). Protected by RLS: auth.uid() = user_id.';

-- The primary key index on user_id serves every query and every RLS filter; no other index is needed
-- for a one-row-per-user table.

-- Keep updated_at honest and make created_at / user_id immutable, whatever the client sends.
create or replace function public.tp_touch_user_preferences()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  new.updated_at := now();
  new.created_at := old.created_at;
  new.user_id := old.user_id;
  return new;
end;
$$;

create trigger user_preferences_touch
  before update on public.user_preferences
  for each row execute function public.tp_touch_user_preferences();

-- Row Level Security: nobody but the owner, enforced by the database.
alter table public.user_preferences enable row level security;
alter table public.user_preferences force row level security;

-- Supabase grants new public tables to anon/authenticated by default. Start from nothing, then give
-- authenticated users exactly the four operations that the policies below constrain.
revoke all on table public.user_preferences from public;
revoke all on table public.user_preferences from anon;
revoke all on table public.user_preferences from authenticated;
grant select, insert, update, delete on table public.user_preferences to authenticated;

create policy user_preferences_select_own
  on public.user_preferences
  for select
  to authenticated
  using ((select auth.uid()) = user_id);

create policy user_preferences_insert_own
  on public.user_preferences
  for insert
  to authenticated
  with check ((select auth.uid()) = user_id);

create policy user_preferences_update_own
  on public.user_preferences
  for update
  to authenticated
  using ((select auth.uid()) = user_id)
  with check ((select auth.uid()) = user_id);

create policy user_preferences_delete_own
  on public.user_preferences
  for delete
  to authenticated
  using ((select auth.uid()) = user_id);
