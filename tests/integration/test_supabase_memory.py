"""Persistent identity and memory against a REAL Supabase project (opt-in; not run by default).

Uses the application's own code path (create_supabase_client, auth_service, SupabaseStore) with two
pre-created, confirmed email/password test users on a DEVELOPMENT project where supabase/migrations has been
applied. Never run it against a project holding real customer data. See supabase/README.md.

    python -m pytest -m integration tests/integration/test_supabase_memory.py -v -p no:cacheprovider
"""

import pytest

from config import get_setting
from db.database import create_supabase_client, is_privileged_key
from services import auth_service
from services.memory_service import SupabaseStore, UserPreferences

pytestmark = pytest.mark.integration
REQUIRED = (
    "SUPABASE_URL",
    "SUPABASE_KEY",
    "SUPABASE_TEST_USER_A_EMAIL",
    "SUPABASE_TEST_USER_A_PASSWORD",
    "SUPABASE_TEST_USER_B_EMAIL",
    "SUPABASE_TEST_USER_B_PASSWORD",
)


@pytest.fixture(scope="module")
def settings():
    missing = [name for name in REQUIRED if not get_setting(name)]
    if missing:
        pytest.skip("Real Supabase test settings are not configured: " + ", ".join(missing))
    if is_privileged_key(get_setting("SUPABASE_KEY")):
        pytest.fail("SUPABASE_KEY is a privileged key; it would bypass RLS and make this test meaningless")
    return {name: get_setting(name) for name in REQUIRED}


def visit(settings, who):
    """A new, independent visit: a brand-new client signed in with the account's credentials."""
    client = create_supabase_client()
    user = auth_service.sign_in(client, settings[f"SUPABASE_TEST_USER_{who}_EMAIL"], settings[f"SUPABASE_TEST_USER_{who}_PASSWORD"])
    return client, SupabaseStore(client, user.user_id), user.user_id


@pytest.fixture(autouse=True)
def clean_rows(settings):
    def wipe():
        for who in "AB":
            client, _, user_id = visit(settings, who)
            client.table("user_preferences").delete().eq("user_id", user_id).execute()

    wipe()
    yield
    wipe()


def test_the_same_account_has_the_same_identity_on_every_visit_and_users_differ(settings):
    _, _, first = visit(settings, "A")
    _, _, second = visit(settings, "A")
    _, _, other = visit(settings, "B")
    assert first == second and first != other


def test_preferences_persist_across_independent_visits(settings):
    _, store, user_id = visit(settings, "A")
    store.update_preferences(
        user_id, excluded_allergens=["milk"], allergy_ingredients=["lentils"], vegetarian_only=True,
        min_spice_level="mild", allergy_status="reported",
    )
    _, later_store, same_id = visit(settings, "A")  # a new client, as after closing and reopening the app
    prefs = later_store.get_preferences(same_id)
    assert same_id == user_id
    assert (prefs.excluded_allergens, prefs.allergy_ingredients, prefs.vegetarian_only, prefs.min_spice_level,
            prefs.allergy_status) == (["milk"], ["lentils"], True, "mild", "reported")


def test_a_second_user_gets_defaults_and_cannot_reach_the_first_users_record(settings):
    _, store_a, id_a = visit(settings, "A")
    store_a.update_preferences(id_a, excluded_allergens=["eggs"], allergy_status="reported")

    client_b, store_b, id_b = visit(settings, "B")
    assert store_b.get_preferences(id_b) == UserPreferences(user_id=id_b)
    with pytest.raises(PermissionError):
        store_b.get_preferences(id_a)  # the application layer refuses another user's id...
    assert client_b.table("user_preferences").select("*").eq("user_id", id_a).execute().data == []  # ...and so does RLS


def test_an_unauthenticated_client_gets_nothing(settings):
    anonymous = create_supabase_client()  # never signed in: role anon
    try:
        assert anonymous.table("user_preferences").select("*").execute().data == []
    except Exception as exc:  # permission denied is an equally good outcome
        assert getattr(exc, "code", "") == "42501"
