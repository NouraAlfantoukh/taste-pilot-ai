import pytest
from fake_supabase import FakeBackend

from services.auth_service import AuthError, AuthFailure, sign_in, sign_out, sign_up


@pytest.fixture
def backend():
    server = FakeBackend()
    server.add_user("ali@example.com", "correct-horse")
    return server


def reason_of(call):
    with pytest.raises(AuthError) as error:
        call()
    return error.value.reason


def test_sign_in_returns_the_identity_supabase_reports(backend):
    client = backend.client()
    user = sign_in(client, "  Ali@Example.com ", "correct-horse")
    assert user.user_id == backend.users["ali@example.com"]["id"] and user.email == "ali@example.com"
    assert client.uid == user.user_id


def test_wrong_password_and_unknown_email_look_identical(backend):
    assert reason_of(lambda: sign_in(backend.client(), "ali@example.com", "nope")) is AuthFailure.INVALID_CREDENTIALS
    assert reason_of(lambda: sign_in(backend.client(), "who@example.com", "nope")) is AuthFailure.INVALID_CREDENTIALS


def test_unconfirmed_email_is_reported_as_such(backend):
    backend.add_user("new@example.com", "long-enough-pass", confirmed=False)
    assert reason_of(lambda: sign_in(backend.client(), "new@example.com", "long-enough-pass")) is AuthFailure.EMAIL_NOT_CONFIRMED


@pytest.mark.parametrize("email,password", [("", "x"), ("not-an-email", "x"), ("a@b.co", ""), ("a b@c.co", "x")])
def test_malformed_input_is_rejected_before_any_request(backend, email, password):
    client = backend.client()
    assert reason_of(lambda: sign_in(client, email, password)) is AuthFailure.INVALID_INPUT
    assert client.uid is None


def test_sign_up_opens_a_session_when_confirmation_is_off(backend):
    client = backend.client()
    result = sign_up(client, "fresh@example.com", "long-enough-pass")
    assert not result.needs_confirmation and result.user.user_id == client.uid


def test_sign_up_without_a_session_when_email_confirmation_is_required():
    server = FakeBackend(confirm_email=True)
    client = server.client()
    result = sign_up(client, "fresh@example.com", "long-enough-pass")
    assert result.user is None and result.needs_confirmation and client.uid is None


def test_sign_up_validation_and_duplicates(backend):
    assert reason_of(lambda: sign_up(backend.client(), "fresh@example.com", "short")) is AuthFailure.WEAK_PASSWORD
    assert reason_of(lambda: sign_up(backend.client(), "ali@example.com", "long-enough-pass")) is AuthFailure.ALREADY_REGISTERED


def test_service_failures_become_a_safe_reason_without_leaking_details(backend):
    class Broken:
        class auth:
            @staticmethod
            def sign_in_with_password(_):
                raise RuntimeError("password=hunter2 at https://secret.internal")

    with pytest.raises(AuthError) as error:
        sign_in(Broken(), "ali@example.com", "hunter2")
    assert error.value.reason is AuthFailure.UNAVAILABLE
    assert "hunter2" not in str(error.value) and "secret.internal" not in repr(error.value)


def test_sign_out_clears_the_session_and_never_raises(backend):
    client = backend.client()
    sign_in(client, "ali@example.com", "correct-horse")
    sign_out(client)
    assert client.uid is None and backend.sign_outs == [backend.users["ali@example.com"]["id"]]

    class Broken:
        class auth:
            @staticmethod
            def sign_out():
                raise RuntimeError("down")

    sign_out(Broken())
