"""An in-memory stand-in for Supabase Auth + PostgREST, with Row Level Security emulated per client session.

It reproduces what the application relies on, not Postgres: a client only sees and writes rows of the user it
signed in as (like `auth.uid() = user_id`), unauthenticated clients are refused, sessions can expire, and the
service can be down. The real database behaviour is verified separately by tests/integration/test_supabase_rls.py.
"""

import uuid
from types import SimpleNamespace

from postgrest.exceptions import APIError
from supabase_auth.errors import AuthApiError

from services.memory_service import UserPreferences


def _api_error(message: str, code: str) -> APIError:
    return APIError({"message": message, "code": code, "hint": None, "details": None})


class FakeBackend:
    """The 'server': accounts and the user_preferences table, shared by every client."""

    def __init__(self, confirm_email: bool = False) -> None:
        self.confirm_email = confirm_email
        self.users: dict[str, dict] = {}  # email -> {id, password, confirmed}
        self.rows: dict[str, dict] = {}  # user_id -> stored row
        self.writes: list[tuple[str, dict]] = []  # (session user, payload) for every upsert
        self.reads: list[str] = []
        self.sign_outs: list[str] = []
        self.outage = False
        self.signup_error = None  # an exception sign_up should raise, to simulate a server rejection
        self.clients: list["FakeClient"] = []

    def add_user(self, email: str, password: str, confirmed: bool = True) -> str:
        user_id = str(uuid.uuid4())
        self.users[email] = {"id": user_id, "password": password, "confirmed": confirmed}
        return user_id

    def client(self) -> "FakeClient":
        client = FakeClient(self)
        self.clients.append(client)
        return client


class FakeClient:
    def __init__(self, backend: FakeBackend) -> None:
        self.backend = backend
        self.uid = None  # the identity of this client's session
        self.expired = False
        self.auth = FakeAuth(self)

    def table(self, name: str) -> "FakeTable":
        assert name == "user_preferences"
        return FakeTable(self)


class FakeAuth:
    def __init__(self, client: FakeClient) -> None:
        self._client = client

    def _open(self, email: str):
        user = self._client.backend.users[email]
        self._client.uid = user["id"]
        return SimpleNamespace(
            user=SimpleNamespace(id=user["id"], email=email),
            session=SimpleNamespace(access_token="fake-token"),
        )

    def sign_in_with_password(self, credentials: dict):
        backend = self._client.backend
        user = backend.users.get(credentials["email"])
        if user is None or user["password"] != credentials["password"]:
            raise AuthApiError("Invalid login credentials", 400, "invalid_credentials")
        if not user["confirmed"]:
            raise AuthApiError("Email not confirmed", 400, "email_not_confirmed")
        return self._open(credentials["email"])

    def sign_up(self, credentials: dict):
        backend = self._client.backend
        email = credentials["email"]
        if backend.signup_error is not None:
            raise backend.signup_error
        if email in backend.users:
            raise AuthApiError("User already registered", 422, "user_already_exists")
        backend.add_user(email, credentials["password"], confirmed=not backend.confirm_email)
        if backend.confirm_email:
            user = backend.users[email]
            return SimpleNamespace(user=SimpleNamespace(id=user["id"], email=email), session=None)
        return self._open(email)

    def sign_out(self):
        self._client.backend.sign_outs.append(str(self._client.uid))
        self._client.uid = None


class FakeTable:
    def __init__(self, client: FakeClient) -> None:
        self._client = client
        self._filters: list[tuple[str, object]] = []
        self._columns = None
        self._limit = None
        self._upsert = None

    def select(self, columns="*"):
        self._columns = None if columns == "*" else columns.split(",")
        return self

    def eq(self, column, value):
        self._filters.append((column, value))
        return self

    def limit(self, count):
        self._limit = count
        return self

    def upsert(self, payload: dict):
        self._upsert = payload
        return self

    def execute(self):
        client, backend = self._client, self._client.backend
        if backend.outage:
            raise ConnectionError("service down")
        if client.expired:
            raise _api_error("JWT expired", "PGRST303")
        if client.uid is None:
            raise _api_error("permission denied for table user_preferences", "42501")
        if self._upsert is not None:
            if self._upsert.get("user_id") != client.uid:  # RLS: with check (auth.uid() = user_id)
                raise _api_error("new row violates row-level security policy", "42501")
            backend.writes.append((client.uid, dict(self._upsert)))
            defaults = UserPreferences(user_id=client.uid).to_row()
            backend.rows[client.uid] = {**defaults, **backend.rows.get(client.uid, {}), **self._upsert}
            return SimpleNamespace(data=[backend.rows[client.uid]])
        backend.reads.append(client.uid)
        visible = [row for row in backend.rows.values() if row["user_id"] == client.uid]  # RLS: using
        for column, value in self._filters:
            visible = [row for row in visible if row.get(column) == value]
        if self._columns:
            visible = [{c: row[c] for c in self._columns if c in row} for row in visible]
        return SimpleNamespace(data=visible[: self._limit] if self._limit else visible)
