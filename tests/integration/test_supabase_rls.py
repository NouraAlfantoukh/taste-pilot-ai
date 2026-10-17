"""Two-user Row Level Security checks against a REAL Supabase project (opt-in; not run by default).

Needs a development project where supabase/migrations has been applied, and two pre-created, confirmed
email/password test users. Never run it against a project holding real customer data. See supabase/README.md.

    python -m pytest -m integration tests/integration/test_supabase_rls.py -v -p no:cacheprovider
"""

import pytest
from postgrest.exceptions import APIError
from supabase import create_client

from config import get_setting
from db.database import is_privileged_key

pytestmark = pytest.mark.integration
TABLE = "user_preferences"
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


def signed_in(settings, who):
    client = create_client(settings["SUPABASE_URL"], settings["SUPABASE_KEY"])
    session = client.auth.sign_in_with_password(
        {"email": settings[f"SUPABASE_TEST_USER_{who}_EMAIL"], "password": settings[f"SUPABASE_TEST_USER_{who}_PASSWORD"]}
    )
    return client, session.user.id


@pytest.fixture(scope="module")
def user_a(settings):
    return signed_in(settings, "A")


@pytest.fixture(scope="module")
def user_b(settings):
    return signed_in(settings, "B")


@pytest.fixture(scope="module")
def anonymous(settings):
    return create_client(settings["SUPABASE_URL"], settings["SUPABASE_KEY"])  # no session: role anon


@pytest.fixture(autouse=True)
def clean_rows(user_a, user_b):
    for client, user_id in (user_a, user_b):
        client.table(TABLE).delete().eq("user_id", user_id).execute()
    yield
    for client, user_id in (user_a, user_b):
        client.table(TABLE).delete().eq("user_id", user_id).execute()


def rows(client):
    return client.table(TABLE).select("*").execute().data


def sql_state(error: pytest.ExceptionInfo) -> str:
    return str(getattr(error.value, "code", ""))


def test_a_user_creates_and_reads_only_their_own_record(user_a, user_b):
    client_a, id_a = user_a
    client_b, _ = user_b
    created = client_a.table(TABLE).insert({"excluded_allergens": ["milk"], "allergy_status": "reported"}).execute().data
    assert [r["user_id"] for r in created] == [id_a]  # user_id defaulted from the session, not from the client
    assert [r["user_id"] for r in rows(client_a)] == [id_a]
    assert rows(client_b) == []


def test_only_one_record_per_user(user_a):
    client_a, _ = user_a
    client_a.table(TABLE).insert({}).execute()
    with pytest.raises(APIError) as error:
        client_a.table(TABLE).insert({}).execute()
    assert sql_state(error) == "23505"


def test_a_user_cannot_create_a_record_for_someone_else(user_a, user_b):
    client_a, id_a = user_a
    client_b, _ = user_b
    with pytest.raises(APIError) as error:
        client_b.table(TABLE).insert({"user_id": id_a, "excluded_allergens": ["eggs"]}).execute()
    assert sql_state(error) == "42501"
    assert rows(client_a) == []


def test_a_user_cannot_update_or_delete_someone_elses_record(user_a, user_b):
    client_a, id_a = user_a
    client_b, _ = user_b
    client_a.table(TABLE).insert({"excluded_allergens": ["milk"], "allergy_status": "reported"}).execute()

    assert client_b.table(TABLE).update({"excluded_allergens": []}).eq("user_id", id_a).execute().data == []
    assert client_b.table(TABLE).delete().eq("user_id", id_a).execute().data == []

    assert [(r["excluded_allergens"], r["allergy_status"]) for r in rows(client_a)] == [(["milk"], "reported")]


def test_a_record_cannot_be_reassigned_to_another_user(user_a, user_b):
    client_a, id_a = user_a
    client_b, id_b = user_b
    client_a.table(TABLE).insert({}).execute()
    try:
        client_a.table(TABLE).update({"user_id": id_b, "vegetarian_only": True}).eq("user_id", id_a).execute()
    except APIError:
        pass  # rejecting the attempt is just as good as ignoring the user_id
    assert [r["user_id"] for r in rows(client_a)] == [id_a]
    assert rows(client_b) == []


def test_an_unauthenticated_client_gets_nothing(anonymous):
    try:
        assert rows(anonymous) == []
    except APIError as error:
        assert error.code == "42501"
    with pytest.raises(APIError):
        anonymous.table(TABLE).insert({}).execute()


@pytest.mark.parametrize(
    "bad",
    [
        {"min_spice_level": "volcanic"},
        {"min_spice_level": "hot", "max_spice_level": "mild"},
        {"excluded_allergens": ["Milk"]},
        {"excluded_allergens": ["حليب"]},
        {"excluded_ingredients": ["a" * 70]},
        {"allergy_status": "awaiting_answer"},
        {"allergy_status": "none_reported", "excluded_allergens": ["milk"]},
        {"allergy_status": "none_reported", "allergy_ingredients": ["lentils"]},
    ],
)
def test_the_database_rejects_invalid_values(user_a, bad):
    client_a, _ = user_a
    with pytest.raises(APIError) as error:
        client_a.table(TABLE).insert(bad).execute()
    assert sql_state(error) == "23514"


def test_timestamps_are_maintained_by_the_database(user_a):
    client_a, id_a = user_a
    created = client_a.table(TABLE).insert({}).execute().data[0]
    updated = (
        client_a.table(TABLE)
        .update({"vegetarian_only": True, "created_at": "2000-01-01T00:00:00Z"})
        .eq("user_id", id_a)
        .execute()
        .data[0]
    )
    assert updated["vegetarian_only"] is True
    assert updated["created_at"] == created["created_at"]
    assert updated["updated_at"] >= created["updated_at"]
