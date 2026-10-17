"""Bridge between the durable user profile and a conversation. Independent of Streamlit and of Supabase.

Only the DURABLE profile crosses this bridge: allergies, ingredient exclusions, diet and spice bounds, and the
allergy-question status. Per-request constraints (category, budget, serving temperature, last request) and
the conversation itself are never saved.

Safety rules, all fail-closed:
- a stored profile that cannot be read faithfully (e.g. an id the taxonomy no longer knows) is NOT silently
  partially applied; loading fails and saving is disabled so the stored data is never overwritten;
- "reported" with no recorded allergy cannot be trusted, so the allergy question is asked again;
- a saved "declined" means the question is asked once more in a new session;
- only CONFIRMED allergies are saved: a term still awaiting clarification is never stored (nor guessed), but
  confirmed ones are saved at once so a user who leaves mid-clarification does not lose them.
"""

from dataclasses import replace
from typing import Optional

from agent.state import AllergyStatus, ConversationState
from services.constraints import InvalidConstraintError
from services.memory_service import LIST_FIELDS, MemoryStore, UserPreferences, validate_preferences

_STATUS_FROM_STORED = {
    "not_asked": AllergyStatus.NOT_ASKED,
    "none_reported": AllergyStatus.NONE_REPORTED,
    "reported": AllergyStatus.REPORTED,
    "declined": AllergyStatus.NOT_ASKED,
}
_STATUS_TO_STORED = {
    AllergyStatus.NOT_ASKED: "not_asked",
    AllergyStatus.AWAITING_ANSWER: "not_asked",  # transient: the answer has not been given yet
    AllergyStatus.NONE_REPORTED: "none_reported",
    AllergyStatus.REPORTED: "reported",
    AllergyStatus.DECLINED: "declined",
}


class PreferencesLoadError(Exception):
    """The stored profile cannot be applied faithfully."""


def _normalized(row: dict) -> dict:
    return {k: sorted(v) if k in LIST_FIELDS else v for k, v in row.items()}


def apply_preferences(state: ConversationState, prefs: UserPreferences) -> None:
    """Load the durable profile into a conversation state (replacing its durable fields)."""
    try:
        validate_preferences(prefs)
        constraints = replace(
            state.constraints,
            excluded_allergens=frozenset(prefs.excluded_allergens),
            allergy_ingredients=frozenset(prefs.allergy_ingredients),
            excluded_ingredients=frozenset(prefs.excluded_ingredients),
            vegetarian_only=prefs.vegetarian_only,
            min_spice_level=prefs.min_spice_level,
            max_spice_level=prefs.max_spice_level,
        )
    except (ValueError, InvalidConstraintError):
        raise PreferencesLoadError("stored preferences are not valid") from None
    has_allergies = bool(prefs.excluded_allergens or prefs.allergy_ingredients)
    status = _STATUS_FROM_STORED[prefs.allergy_status]
    if has_allergies:
        status = AllergyStatus.REPORTED
    elif status is AllergyStatus.REPORTED:
        status = AllergyStatus.NOT_ASKED  # "reported" with nothing recorded cannot be trusted: ask again
    state.constraints = constraints
    state.allergy_status = status


def durable_snapshot(state: ConversationState) -> dict:
    """The durable fields of a conversation as stored columns."""
    c = state.constraints
    has_allergies = bool(c.excluded_allergens or c.allergy_ingredients)
    status = _STATUS_TO_STORED[state.allergy_status]
    if has_allergies:
        status = "reported"
    elif status == "reported":
        # An allergy was disclosed but its type is not resolved yet. Never keep a stale "no allergies" on file,
        # and never store an unconfirmed allergen: "not_asked" makes the next session ask the question again.
        status = "not_asked"
    return {
        "excluded_allergens": sorted(c.excluded_allergens),
        "allergy_ingredients": sorted(c.allergy_ingredients),
        "excluded_ingredients": sorted(c.excluded_ingredients),
        "vegetarian_only": c.vegetarian_only,
        "min_spice_level": c.min_spice_level,
        "max_spice_level": c.max_spice_level,
        "allergy_status": status,
    }


class PreferenceSync:
    """Loads one user's durable profile into conversations and saves confirmed changes back.

    The user id is the identity of the signed-in session, supplied by the caller from Supabase's response.
    """

    def __init__(self, store: MemoryStore, user_id: str) -> None:
        self._store = store
        self._user_id = user_id
        self._prefs = UserPreferences(user_id=user_id)
        self._saved = _normalized(self._prefs.to_row())
        self.enabled = True
        self.had_saved_preferences = False

    def load(self) -> None:
        """Fetch the stored profile. Raises MemoryStoreError (store) or PreferencesLoadError (unusable data)."""
        prefs = self._store.get_preferences(self._user_id)
        apply_preferences(ConversationState(), prefs)  # proves it can be applied before it is trusted
        self._prefs = prefs
        self._saved = _normalized(prefs.to_row())
        self.had_saved_preferences = self._saved != _normalized(UserPreferences(user_id=self._user_id).to_row())

    @property
    def saved(self) -> UserPreferences:
        """The profile as last confirmed by the store (loaded or successfully written), never the conversation."""
        return self._prefs

    def has_unsaved_changes(self, state: ConversationState) -> bool:
        """True when the conversation holds durable preferences that the store has not confirmed."""
        return _normalized(durable_snapshot(state)) != self._saved

    def disable(self) -> None:
        """Stop saving (used when the stored profile could not be read, so it is never overwritten)."""
        self.enabled = False

    def new_state(self) -> ConversationState:
        """A fresh conversation with the durable profile applied and every per-request constraint cleared."""
        state = ConversationState()
        apply_preferences(state, self._prefs)
        return state

    def save(self, state: ConversationState) -> bool:
        """Persist the durable fields if they changed and are confirmed. Returns True when a write happened."""
        if not self.enabled:
            return False
        snapshot = durable_snapshot(state)
        if snapshot is None or _normalized(snapshot) == self._saved:
            return False
        updated = self._store.update_preferences(self._user_id, **snapshot)
        self._prefs = updated
        self._saved = _normalized(updated.to_row())
        return True
