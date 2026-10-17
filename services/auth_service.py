"""Email/password authentication through Supabase Auth.

Identity comes only from Supabase's response to the sign-in (a server-validated session); no user id is
ever accepted from the UI. Every function takes the per-session client created by
db.database.create_supabase_client() and never keeps module-level state.

Passwords are passed straight to Supabase and are never logged or stored. A failed attempt logs exactly three
facts: the operation, the HTTP status and Supabase's error code (validated to be a short snake_case token).
Never the email, the password, tokens or the server's message text.
"""

import logging
import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)

MIN_PASSWORD_LENGTH = 8
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_CODE_SHAPE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class AuthFailure(str, Enum):
    INVALID_INPUT = "invalid_input"  # the server rejected the email/format (or we did, locally)
    INVALID_CREDENTIALS = "invalid_credentials"
    EMAIL_NOT_CONFIRMED = "email_not_confirmed"
    ALREADY_REGISTERED = "already_registered"
    WEAK_PASSWORD = "weak_password"
    RATE_LIMITED = "rate_limited"
    SIGNUP_DISABLED = "signup_disabled"
    EMAIL_NOT_ALLOWED = "email_not_allowed"
    REJECTED = "rejected"  # an HTTP 4xx we have no specific message for: the service answered and refused
    SERVER_ERROR = "server_error"  # an HTTP 5xx: the service itself failed
    UNAVAILABLE = "unavailable"  # no HTTP answer at all: network or unexpected failure


class AuthError(Exception):
    """Sign-in or sign-up failed. `reason` is safe to show; the message never contains credentials."""

    def __init__(self, reason: AuthFailure) -> None:
        super().__init__(reason.value)
        self.reason = reason


@dataclass(frozen=True)
class AuthenticatedUser:
    user_id: str
    email: str


@dataclass(frozen=True)
class SignUpResult:
    user: Optional[AuthenticatedUser]  # set only when Supabase also opened a session
    needs_confirmation: bool  # True: the user must confirm the email, then sign in


# Documented Supabase Auth error codes.
_CODES = {
    "invalid_credentials": AuthFailure.INVALID_CREDENTIALS,
    "email_not_confirmed": AuthFailure.EMAIL_NOT_CONFIRMED,
    "user_already_exists": AuthFailure.ALREADY_REGISTERED,
    "email_exists": AuthFailure.ALREADY_REGISTERED,
    "weak_password": AuthFailure.WEAK_PASSWORD,
    "over_request_rate_limit": AuthFailure.RATE_LIMITED,
    "over_email_send_rate_limit": AuthFailure.RATE_LIMITED,
    "email_address_invalid": AuthFailure.INVALID_INPUT,
    "validation_failed": AuthFailure.INVALID_INPUT,
    "signup_disabled": AuthFailure.SIGNUP_DISABLED,
    "email_provider_disabled": AuthFailure.SIGNUP_DISABLED,
    "email_address_not_authorized": AuthFailure.EMAIL_NOT_ALLOWED,
}


def _diagnostics(exc: Exception) -> tuple[Optional[int], Optional[str]]:
    """The HTTP status and Supabase error code of a failure, only if they have the expected harmless shape."""
    status = getattr(exc, "status", None)
    if not isinstance(status, int) or isinstance(status, bool) or not 0 <= status <= 599:
        status = None
    code = getattr(exc, "code", None)
    if not isinstance(code, str) or not _CODE_SHAPE.match(code):
        code = None
    return status, code


def _classify(status: Optional[int], code: Optional[str], exc: Exception) -> AuthFailure:
    """Specific known code first, then the kind of failure: 4xx rejection, 5xx server error, or no answer."""
    if code in _CODES:
        return _CODES[code]
    name = type(exc).__name__
    if name == "AuthInvalidCredentialsError":
        return AuthFailure.INVALID_CREDENTIALS
    if name == "AuthWeakPasswordError":
        return AuthFailure.WEAK_PASSWORD
    if status == 429:
        return AuthFailure.RATE_LIMITED
    if status is not None and 400 <= status < 500:
        return AuthFailure.REJECTED
    if status is not None and 500 <= status < 600:
        return AuthFailure.SERVER_ERROR
    return AuthFailure.UNAVAILABLE


def _failure(operation: str, exc: Exception) -> AuthError:
    """Log the operation, status and code (nothing else) and return the error to raise."""
    status, code = _diagnostics(exc)
    reason = _classify(status, code, exc)
    logger.warning("%s failed: reason=%s status=%s code=%s", operation, reason.value, status, code)
    return AuthError(reason)


def _normalize(email: str, password: str) -> tuple[str, str]:
    email = (email or "").strip().lower()
    if not _EMAIL.match(email) or not password:
        raise AuthError(AuthFailure.INVALID_INPUT)
    return email, password


def _user(response_user) -> AuthenticatedUser:
    return AuthenticatedUser(user_id=str(response_user.id), email=str(response_user.email or ""))


def sign_in(client, email: str, password: str) -> AuthenticatedUser:
    """Sign in and return the identity Supabase reports for the new session."""
    email, password = _normalize(email, password)
    try:
        response = client.auth.sign_in_with_password({"email": email, "password": password})
    except Exception as exc:  # noqa: BLE001 - classified, never re-raised with its message
        raise _failure("Sign-in", exc) from None
    if response is None or response.user is None or response.session is None:
        logger.warning("Sign-in failed: reason=%s status=None code=no_session", AuthFailure.UNAVAILABLE.value)
        raise AuthError(AuthFailure.UNAVAILABLE)
    return _user(response.user)


def sign_up(client, email: str, password: str) -> SignUpResult:
    """Create an account. If email confirmation is enabled no session exists yet, so no identity is returned."""
    email, password = _normalize(email, password)
    if len(password) < MIN_PASSWORD_LENGTH:
        raise AuthError(AuthFailure.WEAK_PASSWORD)
    try:
        response = client.auth.sign_up({"email": email, "password": password})
    except Exception as exc:  # noqa: BLE001
        raise _failure("Sign-up", exc) from None
    if response is not None and response.user is not None and response.session is not None:
        return SignUpResult(_user(response.user), needs_confirmation=False)
    return SignUpResult(None, needs_confirmation=True)


def sign_out(client) -> None:
    """Best-effort server-side sign-out; local state is cleared by the caller either way."""
    try:
        client.auth.sign_out()
    except Exception as exc:  # noqa: BLE001
        logger.info("Sign-out request failed: %s", type(exc).__name__)
