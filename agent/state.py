"""Per-session conversation state. Independent of Streamlit and of any persistence layer."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from services.constraints import Constraints

MAX_HISTORY_MESSAGES = 6
MAX_ALLERGY_PROMPTS = 2


class AllergyStatus(str, Enum):
    NOT_ASKED = "not_asked"
    AWAITING_ANSWER = "awaiting_answer"
    NONE_REPORTED = "none_reported"  # the user clearly said they have no food allergies
    REPORTED = "reported"
    DECLINED = "declined"  # unknown: declined or never answered; never treated as "no allergies"


class ClarificationKind(str, Enum):
    ALLERGY_ANSWER = "allergy_answer"
    ALLERGEN_TERM = "allergen_term"
    ALLERGY_CORRECTION = "allergy_correction"  # unclear whether to remove recorded allergies; they stay meanwhile
    INGREDIENT_TERM = "ingredient_term"
    CATEGORY_TERM = "category_term"
    OVERRIDE_DISLIKE = "override_dislike"  # asked whether a saved dislike may be lifted for ONE request
    TEMPERATURE_RELAX = "temperature_relax"  # offered to drop a temperature wish the menu cannot verify


@dataclass(frozen=True)
class PendingTerm:
    text: str
    candidates: tuple[str, ...] = ()  # canonical allergen ids when the term is ambiguous; empty if unknown


@dataclass(frozen=True)
class PendingClarification:
    kind: ClarificationKind
    terms: tuple[PendingTerm, ...] = ()

    @property
    def blocking(self) -> bool:
        """Unresolved allergen information must block recommendations."""
        return self.kind is ClarificationKind.ALLERGEN_TERM


@dataclass(frozen=True)
class Turn:
    role: str  # "user" or "assistant"
    text: str


@dataclass
class ConversationState:
    constraints: Constraints = field(default_factory=Constraints)
    allergy_status: AllergyStatus = AllergyStatus.NOT_ASKED
    allergy_prompts: int = 0
    pending: Optional[PendingClarification] = None
    recommendation_requested: bool = False
    last_request: str = ""  # latest message that asked for dishes; survives clarification turns
    history: list[Turn] = field(default_factory=list)
    requested_ingredients: frozenset[str] = frozenset()  # "dishes with X": per request, never saved
    override_ingredients: frozenset[str] = frozenset()  # dislikes lifted for this request only, never saved
    profile_unavailable: bool = False  # the saved profile could not be read, so nothing can be claimed about it

    def add_turn(self, role: str, text: str) -> None:
        self.history.append(Turn(role, text))
        del self.history[:-MAX_HISTORY_MESSAGES]
