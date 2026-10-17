import pytest
from fake_supabase import FakeBackend

from services.auth_service import sign_in
from services.memory_service import MemoryStoreError, SupabaseStore, UserPreferences


@pytest.fixture
def backend():
    server = FakeBackend()
    server.add_user("a@example.com", "password-a")
    server.add_user("b@example.com", "password-b")
    return server


def store_for(backend, email):
    client = backend.client()
    user = sign_in(client, email, "password-" + email[0])
    return SupabaseStore(client, user.user_id), user.user_id


def test_a_user_without_a_record_gets_defaults_and_nothing_is_written(backend):
    store, uid = store_for(backend, "a@example.com")
    assert store.get_preferences(uid) == UserPreferences(user_id=uid)
    assert backend.writes == []


def test_updates_round_trip_through_a_new_session(backend):
    store, uid = store_for(backend, "a@example.com")
    store.update_preferences(uid, excluded_allergens=["milk", "eggs"], allergy_ingredients=["lentils"],
                             vegetarian_only=True, min_spice_level="mild", allergy_status="reported")
    again, same_uid = store_for(backend, "a@example.com")  # a brand-new client, as after a restart
    prefs = again.get_preferences(same_uid)
    assert same_uid == uid
    assert (prefs.excluded_allergens, prefs.allergy_ingredients, prefs.vegetarian_only, prefs.min_spice_level,
            prefs.allergy_status) == (["eggs", "milk"], ["lentils"], True, "mild", "reported")


def test_partial_updates_keep_other_stored_fields(backend):
    store, uid = store_for(backend, "a@example.com")
    store.update_preferences(uid, excluded_allergens=["milk"], allergy_status="reported")
    store.update_preferences(uid, vegetarian_only=True)
    prefs = store.get_preferences(uid)
    assert (prefs.excluded_allergens, prefs.vegetarian_only, prefs.allergy_status) == (["milk"], True, "reported")


def test_users_never_see_each_others_preferences(backend):
    store_a, uid_a = store_for(backend, "a@example.com")
    store_b, uid_b = store_for(backend, "b@example.com")
    store_a.update_preferences(uid_a, excluded_allergens=["milk"], allergy_status="reported")
    assert store_b.get_preferences(uid_b) == UserPreferences(user_id=uid_b)
    store_b.update_preferences(uid_b, vegetarian_only=True)
    assert store_a.get_preferences(uid_a).vegetarian_only is False
    assert store_b.get_preferences(uid_b).excluded_allergens == []


def test_the_store_refuses_to_act_for_another_user_id_and_sends_nothing(backend):
    store_a, uid_a = store_for(backend, "a@example.com")
    _, uid_b = store_for(backend, "b@example.com")
    for call in (lambda: store_a.get_preferences(uid_b), lambda: store_a.update_preferences(uid_b, vegetarian_only=True)):
        with pytest.raises(PermissionError):
            call()
    assert backend.writes == [] and backend.reads == []


def test_the_written_user_id_is_always_the_session_identity(backend):
    store, uid = store_for(backend, "a@example.com")
    store.update_preferences(uid, vegetarian_only=True)
    assert [writer for writer, _ in backend.writes] == [uid]
    assert backend.writes[0][1]["user_id"] == uid


def test_a_store_needs_an_authenticated_identity(backend):
    with pytest.raises(ValueError):
        SupabaseStore(backend.client(), "")


def test_the_database_still_refuses_a_forged_user_id_if_the_store_were_bypassed(backend):
    _, uid_b = store_for(backend, "b@example.com")
    client_a = backend.client()
    sign_in(client_a, "a@example.com", "password-a")
    with pytest.raises(Exception) as error:
        client_a.table("user_preferences").upsert({"user_id": uid_b, "vegetarian_only": True}).execute()
    assert getattr(error.value, "code", "") == "42501"
    assert uid_b not in backend.rows


def test_invalid_data_is_rejected_before_any_write(backend):
    store, uid = store_for(backend, "a@example.com")
    for bad in ({"excluded_allergens": ["not_real"]}, {"min_spice_level": "volcanic"}, {"favorite": 1}):
        with pytest.raises(ValueError):
            store.update_preferences(uid, **bad)
    assert backend.writes == []


def test_an_unauthenticated_client_cannot_read_or_write(backend):
    store = SupabaseStore(backend.client(), "someone")  # never signed in
    with pytest.raises(MemoryStoreError) as error:
        store.get_preferences("someone")
    assert error.value.kind == "forbidden"


def test_an_expired_session_is_reported_distinctly(backend):
    store, uid = store_for(backend, "a@example.com")
    backend.clients[0].expired = True
    for call in (lambda: store.get_preferences(uid), lambda: store.update_preferences(uid, vegetarian_only=True)):
        with pytest.raises(MemoryStoreError) as error:
            call()
        assert error.value.kind == "session_expired"


def test_an_outage_is_unavailable_and_leaks_nothing(backend):
    store, uid = store_for(backend, "a@example.com")
    backend.outage = True
    with pytest.raises(MemoryStoreError) as error:
        store.get_preferences(uid)
    assert error.value.kind == "unavailable" and "service down" not in str(error.value)
