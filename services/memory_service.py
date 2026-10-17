"""Durable user preference memory.

UserPreferences mirrors the durable columns of public.user_preferences (taxonomy ids, never Arabic text).
Per-request constraints (category, budget, serving temperature, last request) are deliberately not part of it.

Two stores implement the same interface: InMemoryStore (development and tests) and SupabaseStore. The
Supabase store is bound to ONE authenticated user: the id comes from the Supabase session, and a call that
names another user is refused instead of trusted. Row Level Security in the database enforces the same rule.
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field, fields, replace
from typing import Callable, Optional, TypeVar

from services import taxonomy
from services.menu_service import SPICE_LEVELS

logger = logging.getLogger(__name__)

TABLE = "user_preferences"
PERSISTED_ALLERGY_STATUSES = ("not_asked", "none_reported", "reported", "declined")
LIST_FIELDS = ("excluded_allergens", "allergy_ingredients", "excluded_ingredients")
T = TypeVar("T")


@dataclass
class UserPreferences:
    user_id: str
    excluded_allergens: list[str] = field(default_factory=list)  # allergen category ids
    allergy_ingredients: list[str] = field(default_factory=list)  # ingredient-level allergies
    excluded_ingredients: list[str] = field(default_factory=list)  # plain dislikes, never allergies
    vegetarian_only: bool = False
    min_spice_level: Optional[str] = None
    max_spice_level: Optional[str] = None
    allergy_status: str = "not_asked"

    def to_dict(self) -> dict:
        return asdict(self)

    def to_row(self) -> dict:
        """The durable columns exactly as stored (user_id and timestamps are handled by the database)."""
        row = asdict(self)
        del row["user_id"]
        return row

    @classmethod
    def from_row(cls, user_id: str, row: dict) -> "UserPreferences":
        prefs = cls(user_id=user_id)
        for name in (f.name for f in fields(cls) if f.name != "user_id"):
            if name in row and row[name] is not None:
                setattr(prefs, name, list(row[name]) if name in LIST_FIELDS else row[name])
        return prefs


DURABLE_FIELDS = tuple(f.name for f in fields(UserPreferences) if f.name != "user_id")


def validate_preferences(prefs: UserPreferences) -> None:
    """Raise ValueError (never including stored values) if the record is not safe to keep."""
    for name in LIST_FIELDS:
        values = getattr(prefs, name)
        if not isinstance(values, list) or not all(isinstance(v, str) for v in values) or len(values) > 50:
            raise ValueError(f"{name} must be a list of at most 50 ids")
    if not all(taxonomy.is_known_allergen(a) for a in prefs.excluded_allergens):
        raise ValueError("excluded_allergens contains an unknown allergen id")
    if not all(taxonomy.is_known_ingredient(i) for i in (*prefs.allergy_ingredients, *prefs.excluded_ingredients)):
        raise ValueError("an ingredient list contains an unknown ingredient id")
    if not isinstance(prefs.vegetarian_only, bool):
        raise ValueError("vegetarian_only must be a bool")
    for name in ("min_spice_level", "max_spice_level"):
        value = getattr(prefs, name)
        if value is not None and value not in SPICE_LEVELS:
            raise ValueError(f"{name} is invalid")
    low, high = prefs.min_spice_level, prefs.max_spice_level
    if low is not None and high is not None and SPICE_LEVELS.index(low) > SPICE_LEVELS.index(high):
        raise ValueError("min_spice_level is above max_spice_level")
    if prefs.allergy_status not in PERSISTED_ALLERGY_STATUSES:
        raise ValueError("allergy_status is invalid")
    if prefs.allergy_status == "none_reported" and (prefs.excluded_allergens or prefs.allergy_ingredients):
        raise ValueError("none_reported cannot be combined with recorded allergies")


def apply_changes(prefs: UserPreferences, changes: dict) -> UserPreferences:
    """Return a validated copy with the changes applied. Unknown fields (including user_id) are rejected."""
    for key in changes:
        if key not in DURABLE_FIELDS:
            raise ValueError(f"Unknown preference field: {key}")
    normalized = {
        key: sorted(set(value)) if key in LIST_FIELDS and isinstance(value, (list, tuple, set, frozenset)) else value
        for key, value in changes.items()
    }
    updated = replace(prefs, **normalized)
    validate_preferences(updated)
    return updated


class MemoryStoreError(Exception):
    """The store could not complete the operation. `kind` is one of: session_expired, forbidden, unavailable."""

    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


class MemoryStore(ABC):
    @abstractmethod
    def get_preferences(self, user_id: str) -> UserPreferences:
        """Return the user's preferences (defaults if none are stored)."""

    @abstractmethod
    def update_preferences(self, user_id: str, **changes) -> UserPreferences:
        """Apply changes to the user's preferences and return the updated object."""


class InMemoryStore(MemoryStore):
    """Non-persistent store for development and tests."""

    def __init__(self) -> None:
        self._data: dict[str, UserPreferences] = {}

    def get_preferences(self, user_id: str) -> UserPreferences:
        if user_id not in self._data:
            self._data[user_id] = UserPreferences(user_id=user_id)
        return self._data[user_id]

    def update_preferences(self, user_id: str, **changes) -> UserPreferences:
        self._data[user_id] = apply_changes(self.get_preferences(user_id), changes)
        return self._data[user_id]


class SupabaseStore(MemoryStore):
    """Preferences of one authenticated user, stored in public.user_preferences.

    `client` is that user's own signed-in Supabase client (anon key + user session) and `user_id` is the
    identity Supabase reported for the session. A different user_id is refused. The database applies RLS on
    top, so even a bug here cannot reach another user's row.
    """

    def __init__(self, client, user_id: str) -> None:
        if not user_id:
            raise ValueError("SupabaseStore needs the authenticated user's id")
        self._client = client
        self._user_id = user_id

    def _own(self, user_id: str) -> None:
        if user_id != self._user_id:
            raise PermissionError("This store only serves the signed-in user.")

    def _run(self, operation: Callable[[], T]) -> T:
        try:
            return operation()
        except Exception as exc:  # noqa: BLE001 - mapped to a safe kind; the message may contain data
            code = str(getattr(exc, "code", "") or "")
            message = str(getattr(exc, "message", "") or "").lower()
            logger.warning("Preference store failure: %s %s", type(exc).__name__, code)
            if code in ("PGRST301", "PGRST303") or "jwt" in message:
                raise MemoryStoreError("session_expired") from None
            if code == "42501":
                raise MemoryStoreError("forbidden") from None
            raise MemoryStoreError("unavailable") from None

    def get_preferences(self, user_id: str) -> UserPreferences:
        self._own(user_id)
        rows = self._run(
            lambda: self._client.table(TABLE)
            .select(",".join(DURABLE_FIELDS))
            .eq("user_id", self._user_id)
            .limit(1)
            .execute()
            .data
        )
        return UserPreferences.from_row(self._user_id, rows[0]) if rows else UserPreferences(user_id=self._user_id)

    def update_preferences(self, user_id: str, **changes) -> UserPreferences:
        self._own(user_id)
        updated = apply_changes(self.get_preferences(user_id), changes)  # validated before anything is written
        self._run(
            lambda: self._client.table(TABLE).upsert({"user_id": self._user_id, **updated.to_row()}).execute()
        )
        return updated

