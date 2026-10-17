"""Allergy clarification state: open questions close when the guest answers or settles them, and nothing unconfirmed is stored.

Covers the two conversations reported from manual testing (milk and cream; pistachio) plus the generic mechanism
behind them, including persistence across independent sessions. Mocked LLM, fake Supabase.
"""

import pytest
from helpers import ext, picks_for
from test_conversation_qa import Guest, backend, stored  # noqa: F401  (fixture re-used)

from agent.orchestrator import ALLERGEN_EXAMPLES_AR, ResponseType
from agent.state import AllergyStatus
from services import taxonomy

T = ResponseType


def asks_about(response, *terms):
    return response.type is T.CLARIFICATION and all(f"«{t}»" in response.reply_ar for t in terms)


def settled_guest(backend):
    guest = Guest(backend, "a@example.com")
    guest.onboard()
    return guest


# ---- the vocabulary: derivatives and pistachio are known, not "unknown terms" ----

@pytest.mark.parametrize("term", ["حليب ومشتقاته", "الحليب ومشتقاته", "مشتقاته", "كريمة", "الكريمة", "قشدة", "cream", "مشتقات الألبان"])
def test_milk_derivatives_and_cream_resolve_to_the_milk_allergen(term):
    assert taxonomy.resolve_allergy_term(term) == (frozenset({"milk"}), frozenset())


@pytest.mark.parametrize("term", ["بستاشيو", "البستاشيو", "فستق بستاشيو", "فستق حلبي", "pistachio"])
def test_pistachio_resolves_to_tree_nuts_not_to_the_ambiguous_peanut_or_nut_question(term):
    assert taxonomy.resolve_allergy_term(term) == (frozenset({"tree_nuts"}), frozenset())


def test_the_bare_word_fustuq_stays_ambiguous_because_it_can_mean_peanut_or_pistachio():
    with pytest.raises(taxonomy.AmbiguousTermError) as error:
        taxonomy.resolve_allergy_term("فستق")
    assert error.value.candidates == {"peanuts", "tree_nuts"}


# ---- case 1: milk and cream ----

def test_case_1_milk_and_cream_in_one_sentence_needs_no_clarification_and_is_saved(backend):
    guest = settled_guest(backend)
    reply = guest.say(
        "عندي حساسية حليب ومشتقاته، بس أغلب شيء الحليب والكريمة",
        ext(allergens=["حليب ومشتقاته", "كريمة"], wants_recommendations=True), picks_for("dish_001", "dish_002", "dish_010"),
    )
    assert reply.type is T.RECOMMENDATIONS and guest.state.pending is None
    assert guest.state.constraints.excluded_allergens == {"milk"} and not guest.state.constraints.allergy_ingredients
    assert all("milk" not in d["allergens"] for d in reply.recommendations)
    assert stored(backend, "a@example.com")["excluded_allergens"] == ["milk"]


def test_case_1_an_unrecognised_extra_term_is_closed_by_the_guests_explicit_settlement(backend):
    """The exact loop reported: the app kept asking about the extra term however the guest answered."""
    guest = settled_guest(backend)
    first = guest.say("حساسية حليب وكاسترد", ext(allergens=["حليب", "كاسترد"], wants_recommendations=True))
    assert asks_about(first, "كاسترد") and "«حليب»" not in first.reply_ar  # milk is confirmed, only the extra term is open
    assert guest.state.constraints.excluded_allergens == {"milk"}
    assert stored(backend, "a@example.com")["excluded_allergens"] == ["milk"]  # confirmed allergens are saved at once

    unrelated_answer = guest.say("حليب", ext(allergens=["حليب"]))  # answering with the part that is already known...
    assert asks_about(unrelated_answer, "كاسترد")  # ...does not close the other question by itself (fail closed)

    done = guest.say("خلاص بس حليب", ext(allergens=["حليب"], settles_pending=True), picks_for("dish_001", "dish_010"))
    assert done.type is T.RECOMMENDATIONS and guest.state.pending is None
    assert "كاسترد" not in done.reply_ar
    assert guest.state.constraints.excluded_allergens == {"milk"} and not guest.state.constraints.allergy_ingredients
    assert all("milk" not in d["allergens"] for d in done.recommendations)  # the confirmed allergy still filters

    again = guest.say("رشّح لي", ext(wants_recommendations=True), picks_for("dish_001"))
    assert again.type is T.RECOMMENDATIONS  # and it is not asked a third time


def test_case_1_the_settled_state_survives_a_fresh_session_without_the_question(backend):
    guest = settled_guest(backend)
    guest.say("حساسية حليب وكاسترد", ext(allergens=["حليب", "كاسترد"]))
    guest.say("خلاص بس حليب", ext(allergens=["حليب"], settles_pending=True))
    row = stored(backend, "a@example.com")
    assert row["excluded_allergens"] == ["milk"] and row["allergy_ingredients"] == [] and row["allergy_status"] == "reported"
    later = Guest(backend, "a@example.com")
    assert later.state.pending is None and later.state.allergy_status is AllergyStatus.REPORTED
    assert later.state.constraints.excluded_allergens == {"milk"}
    reply = later.say("رشّح لي", ext(wants_recommendations=True), picks_for("dish_001"))
    assert reply.type is T.RECOMMENDATIONS and "كاسترد" not in reply.reply_ar and ALLERGEN_EXAMPLES_AR not in reply.reply_ar


def test_a_settlement_without_any_allergy_does_not_close_the_question(backend):
    guest = Guest(backend, "a@example.com")
    guest.onboard()
    guest.say("حساسية كاسترد", ext(allergens=["كاسترد"], wants_recommendations=True))
    assert guest.state.pending is not None and guest.state.constraints.excluded_allergens == frozenset()
    reply = guest.say("خلاص", ext(settles_pending=True))  # settles nothing: no allergy was stated or recorded
    assert asks_about(reply, "كاسترد") and guest.state.pending is not None
    assert stored(backend, "a@example.com")["excluded_allergens"] == []  # an unconfirmed term is never stored


def test_a_settlement_never_removes_an_allergy_that_was_already_confirmed(backend):
    guest = settled_guest(backend)
    guest.say("حساسية بيض وكاسترد", ext(allergens=["بيض", "كاسترد"]))
    guest.say("خلاص هذا كل شي", ext(settles_pending=True))  # the confirmed egg allergy counts as the stated allergy
    assert guest.state.pending is None and guest.state.constraints.excluded_allergens == {"eggs"}
    assert stored(backend, "a@example.com")["excluded_allergens"] == ["eggs"]


def test_naming_the_open_term_resolves_it_without_the_settlement_flag(backend):
    guest = settled_guest(backend)
    guest.say("حساسية كاسترد", ext(allergens=["كاسترد"]))
    done = guest.say("قصدي الحليب", ext(allergens=["الحليب"], answered_pending=["كاسترد"]))
    assert guest.state.pending is None and guest.state.constraints.excluded_allergens == {"milk"}
    assert not asks_about(done, "كاسترد")


# ---- case 2: pistachio ----

def test_case_2_pistachio_in_one_phrase_resolves_directly_to_tree_nuts(backend):
    guest = settled_guest(backend)
    reply = guest.say("فستق بستاشيو", ext(allergens=["فستق بستاشيو"], wants_recommendations=True), picks_for("dish_001", "dish_011", "dish_012"))
    assert reply.type is T.RECOMMENDATIONS and guest.state.pending is None
    assert guest.state.constraints.excluded_allergens == {"tree_nuts"}
    assert all("tree_nuts" not in d["allergens"] for d in reply.recommendations)
    assert stored(backend, "a@example.com")["excluded_allergens"] == ["tree_nuts"]


def test_case_2_the_reported_loop_picking_tree_nuts_closes_both_the_ambiguous_word_and_the_pistachio(backend):
    guest = settled_guest(backend)
    first = guest.say("فستق بستاشيو", ext(allergens=["فستق", "بستاشيو"], wants_recommendations=True))
    assert asks_about(first, "فستق") and "بستاشيو" not in first.reply_ar.split("\n")[0].replace("«فستق»", "")  # one open question
    assert guest.state.constraints.excluded_allergens == {"tree_nuts"}  # the pistachio part is already confirmed

    done = guest.say("المكسرات الشجرية", ext(allergens=["المكسرات الشجرية"]), picks_for("dish_001", "dish_010"))
    assert done.type is T.RECOMMENDATIONS and guest.state.pending is None
    assert guest.state.constraints.excluded_allergens == {"tree_nuts"}  # not peanuts: the guest picked tree nuts
    assert "فستق" not in done.reply_ar.split("رشّحت")[0]
    row = stored(backend, "a@example.com")
    assert row["excluded_allergens"] == ["tree_nuts"] and row["allergy_status"] == "reported"


def test_case_2_survives_a_fresh_session_and_still_filters_nut_dishes(backend):
    guest = settled_guest(backend)
    guest.say("فستق بستاشيو", ext(allergens=["فستق", "بستاشيو"]))
    guest.say("المكسرات الشجرية", ext(allergens=["المكسرات الشجرية"]))
    later = Guest(backend, "a@example.com")
    assert later.state.pending is None and later.state.constraints.excluded_allergens == {"tree_nuts"}
    reply = later.say("رشّح لي", ext(wants_recommendations=True), picks_for("dish_011", "dish_012", "dish_001", "dish_013"))
    shown = {d["id"] for d in reply.recommendations}
    assert reply.type is T.RECOMMENDATIONS and not shown & {"dish_011", "dish_012"} and "dish_001" in shown


def test_choosing_peanuts_for_the_ambiguous_word_stores_peanuts_only(backend):
    guest = settled_guest(backend)
    guest.say("حساسية فستق", ext(allergens=["فستق"]))
    guest.say("الفول السوداني", ext(allergens=["الفول السوداني"]))
    assert guest.state.pending is None and guest.state.constraints.excluded_allergens == {"peanuts"}
    assert stored(backend, "a@example.com")["excluded_allergens"] == ["peanuts"]


def test_an_unanswered_ambiguous_word_keeps_blocking_recommendations(backend):
    guest = settled_guest(backend)
    guest.say("حساسية فستق", ext(allergens=["فستق"]))
    reply = guest.say("رشّح لي", ext(wants_recommendations=True))
    assert reply.type is T.CLARIFICATION and not reply.recommendations
    assert stored(backend, "a@example.com")["excluded_allergens"] == []  # nothing unconfirmed is stored
