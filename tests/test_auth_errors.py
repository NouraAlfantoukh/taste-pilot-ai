"""Sign-in / sign-up failures: classification of Supabase errors and what is (and is not) logged."""

import logging
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fake_supabase import FakeBackend
from helpers import Scripted
from streamlit.testing.v1 import AppTest
from supabase_auth import helpers as sdk_helpers
from supabase_auth.errors import AuthApiError, AuthRetryableError

from services.auth_service import AuthError, AuthFailure, sign_in, sign_up
from ui import components

F = AuthFailure
EMAIL, PASSWORD = "private.person@mail.test", "s3cret-passphrase-9"
SECRET_PARTS = [EMAIL, PASSWORD, "private.person", "eyJhbGciOiJIUzI1NiJ9", "refresh-token-value"]


def server_error(status: int, code=None, message: str = "x"):
    """What the real SDK produces for an HTTP error response, built through its own parser."""
    body = {"code": status, "msg": message}
    if code:
        body["error_code"] = code
    if code == "weak_password":
        body["weak_password"] = {"reasons": ["length"]}
    request = httpx.Request("POST", "https://project.invalid/auth/v1/signup")
    try:
        httpx.Response(status, json=body, request=request).raise_for_status()
    except httpx.HTTPStatusError as error:
        return sdk_helpers.handle_exception(error)


class Failing:
    """A client whose Supabase auth calls raise the given exception."""

    def __init__(self, exc: Exception) -> None:
        def raiser(_credentials):
            raise exc

        self.auth = SimpleNamespace(sign_up=raiser, sign_in_with_password=raiser)


def reason_of(call) -> AuthFailure:
    with pytest.raises(AuthError) as error:
        call()
    return error.value.reason


REPORTED = [
    (400, "email_address_invalid", F.INVALID_INPUT),
    (400, "validation_failed", F.INVALID_INPUT),
    (400, "email_provider_disabled", F.SIGNUP_DISABLED),
    (422, "signup_disabled", F.SIGNUP_DISABLED),
    (422, "email_address_not_authorized", F.EMAIL_NOT_ALLOWED),
    (400, "bad_json", F.REJECTED),
    (400, None, F.REJECTED),  # an older server reply without an error code
    (403, "a_code_we_do_not_know", F.REJECTED),
    (409, None, F.REJECTED),
    (429, "over_email_send_rate_limit", F.RATE_LIMITED),
    (429, None, F.RATE_LIMITED),
    (422, "user_already_exists", F.ALREADY_REGISTERED),
    (422, "weak_password", F.WEAK_PASSWORD),
    (400, "invalid_credentials", F.INVALID_CREDENTIALS),
    (500, "unexpected_failure", F.SERVER_ERROR),
    (500, None, F.SERVER_ERROR),
]


@pytest.mark.parametrize("status,code,expected", REPORTED)
def test_server_responses_are_classified_by_code_then_by_http_class(status, code, expected):
    exc = server_error(status, code)
    assert reason_of(lambda: sign_up(Failing(exc), EMAIL, PASSWORD)) is expected
    assert reason_of(lambda: sign_in(Failing(exc), EMAIL, PASSWORD)) is expected


@pytest.mark.parametrize("status", [502, 503, 504])
def test_gateway_failures_are_server_errors(status):
    exc = AuthRetryableError("upstream down", status)
    assert reason_of(lambda: sign_up(Failing(exc), EMAIL, PASSWORD)) is F.SERVER_ERROR


@pytest.mark.parametrize(
    "exc", [AuthRetryableError("no route", 0), ConnectionError("refused"), TimeoutError("slow"), RuntimeError("boom")]
)
def test_no_http_answer_at_all_is_a_connection_problem(exc):
    assert reason_of(lambda: sign_up(Failing(exc), EMAIL, PASSWORD)) is F.UNAVAILABLE


def test_a_rejection_is_never_reported_as_a_connection_problem():
    for status, code, _ in REPORTED:
        if status < 500:
            assert reason_of(lambda: sign_up(Failing(server_error(status, code)), EMAIL, PASSWORD)) is not F.UNAVAILABLE


def test_every_failure_reason_has_its_own_arabic_message():
    assert set(components.AUTH_MESSAGES_AR) == set(AuthFailure)
    assert len({components.AUTH_MESSAGES_AR[r] for r in AuthFailure}) == len(AuthFailure)
    assert "تعذّر الاتصال" in components.AUTH_MESSAGES_AR[F.UNAVAILABLE]
    assert "تعذّر الاتصال" not in components.AUTH_MESSAGES_AR[F.REJECTED]
    assert "تعذّر الاتصال" not in components.AUTH_MESSAGES_AR[F.SERVER_ERROR]


# ---- what is logged ----

def logged(caplog) -> str:
    return "\n".join(record.getMessage() for record in caplog.records)


def test_a_sign_up_rejection_logs_only_the_operation_status_and_code(caplog):
    caplog.set_level(logging.DEBUG)
    exc = server_error(400, "email_address_invalid", message=f'Email address "{EMAIL}" is invalid (password {PASSWORD})')
    reason_of(lambda: sign_up(Failing(exc), EMAIL, PASSWORD))
    text = logged(caplog)
    assert "Sign-up failed: reason=invalid_input status=400 code=email_address_invalid" in text
    for secret in (*SECRET_PARTS, "is invalid", "Email address"):
        assert secret not in text


def test_a_sign_in_failure_logs_the_same_safe_facts(caplog):
    caplog.set_level(logging.DEBUG)
    reason_of(lambda: sign_in(Failing(server_error(400, "invalid_credentials", message=f"bad login for {EMAIL}")), EMAIL, PASSWORD))
    text = logged(caplog)
    assert "Sign-in failed: reason=invalid_credentials status=400 code=invalid_credentials" in text
    assert not any(secret in text for secret in SECRET_PARTS)


@pytest.mark.parametrize(
    "hostile_code", [f"Email {EMAIL} invalid", f"eyJhbGciOiJIUzI1NiJ9.refresh-token-value", "UPPER_case", "has space", "x" * 200, 400, "", None]
)
def test_an_error_code_with_an_unexpected_shape_is_not_logged(caplog, hostile_code):
    caplog.set_level(logging.DEBUG)
    exc = AuthApiError(f"token eyJhbGciOiJIUzI1NiJ9 for {EMAIL} {PASSWORD}", 400, hostile_code)
    reason_of(lambda: sign_up(Failing(exc), EMAIL, PASSWORD))
    text = logged(caplog)
    assert "status=400 code=None" in text
    assert not any(secret in text for secret in SECRET_PARTS)


def test_a_non_supabase_exception_logs_no_message_text(caplog):
    caplog.set_level(logging.DEBUG)
    reason_of(lambda: sign_in(Failing(RuntimeError(f"password={PASSWORD} for {EMAIL} refresh-token-value")), EMAIL, PASSWORD))
    text = logged(caplog)
    assert "reason=unavailable status=None code=None" in text
    assert not any(secret in text for secret in SECRET_PARTS)


def test_a_status_that_is_not_an_http_status_is_not_logged(caplog):
    caplog.set_level(logging.DEBUG)
    exc = AuthApiError("m", 99999, "bad_json")
    reason_of(lambda: sign_up(Failing(exc), EMAIL, PASSWORD))
    assert "status=None code=bad_json" in logged(caplog)


def test_local_validation_failures_log_nothing_sensitive(caplog):
    caplog.set_level(logging.DEBUG)
    reason_of(lambda: sign_up(Failing(RuntimeError("never called")), "not-an-email", PASSWORD))
    reason_of(lambda: sign_up(Failing(RuntimeError("never called")), EMAIL, "short"))
    assert not any(secret in logged(caplog) for secret in SECRET_PARTS)


def test_a_successful_sign_in_logs_no_credentials(caplog):
    caplog.set_level(logging.DEBUG)
    backend = FakeBackend()
    backend.add_user(EMAIL, PASSWORD)
    sign_in(backend.client(), EMAIL, PASSWORD)
    assert not any(secret in logged(caplog) for secret in SECRET_PARTS)


# ---- through the Streamlit sign-up form ----

APP = str(Path(__file__).resolve().parent.parent / "app.py")


@pytest.fixture
def backend():
    return FakeBackend()


@pytest.fixture
def signup_page(monkeypatch, backend):
    monkeypatch.setattr("config.is_supabase_configured", lambda: True)
    monkeypatch.setattr("db.database.create_supabase_client", backend.client)
    monkeypatch.setattr("services.llm_service.any_provider_configured", lambda: True)
    monkeypatch.setattr("agent.orchestrator.default_generate", Scripted())
    return AppTest.from_file(APP, default_timeout=30).run()


def submit_sign_up(at):
    at.text_input(key="sign_up_email").input("someone@mail.test")
    at.text_input(key="sign_up_password").input("long-enough-pass")
    next(b for b in at.button if b.label == "إنشاء حساب").click().run()
    return "".join(m.value for m in at.markdown)


@pytest.mark.parametrize(
    "status,code,reason",
    [
        (400, "email_address_invalid", F.INVALID_INPUT),
        (400, "email_provider_disabled", F.SIGNUP_DISABLED),
        (400, "bad_json", F.REJECTED),
        (500, "unexpected_failure", F.SERVER_ERROR),
    ],
)
def test_the_sign_up_form_shows_the_matching_arabic_message(backend, signup_page, status, code, reason):
    backend.signup_error = server_error(status, code)
    page = submit_sign_up(signup_page)
    assert components.AUTH_MESSAGES_AR[reason] in page
    assert components.AUTH_MESSAGES_AR[F.UNAVAILABLE] not in page
    assert "auth" not in signup_page.session_state


def test_a_connection_failure_still_says_so(backend, signup_page):
    backend.signup_error = ConnectionError("refused")
    assert components.AUTH_MESSAGES_AR[F.UNAVAILABLE] in submit_sign_up(signup_page)
