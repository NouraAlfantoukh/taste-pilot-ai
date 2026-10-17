# Menu review process (fictional test data)

`allergen_data_status: "verified"` in `data/menu.json` means **this dish passed the review described here**, at the
exact ingredient and allergen data it has now. It does **not** mean the dish is safe for a real allergic person.

## How `verified` is enforced

`verified` cannot be earned by editing `menu.json` alone. When the production menu is loaded
(`services/menu_service.py`), a dish keeps `verified` only if **all** of these hold, otherwise it silently becomes
`unverified` (allergy filtering then excludes it for anyone with a declared allergy):

1. `data/menu_review.json` exists and has the expected `process` (`fictional-menu-review-v1`);
2. it holds a record for the dish whose **fingerprint** (SHA-256 of the dish id, `ingredient_ids`, `ingredients_ar`
   and `allergens`) equals the dish's current data, so any later edit to those fields voids the review;
3. the record lists every criterion as passed and carries written reviewer notes;
4. the dish still passes every automated criterion (R1-R5) today.

Prices, descriptions, spice level, temperature and category are not part of the fingerprint: changing them does not
affect allergy safety. Code: `services/menu_review.py`. Tests: `tests/test_menu_review.py`.

## Criteria

| Id | Criterion | Checked by |
|---|---|---|
| R1 | every ingredient id is known to the taxonomy and itemized (no spice blend or mix with hidden contents) | automated |
| R2 | the Arabic ingredient names correspond one-to-one to the ingredient ids | automated |
| R3 | the recorded allergens are exactly the allergens implied by the ingredients through the taxonomy (none missing, none extra) | automated |
| R4 | recorded allergen ids are known and not duplicated | automated |
| R5 | the description does not mention a food or allergen that the record does not declare (e.g. "الأجبان" requires milk, "مكسرات" requires a nut allergen) | automated |
| R6 | the dish was read against its description by the reviewer and judged coherent; the reasoning is written in the record | reviewer, recorded in `reviewer_notes` |

## The review that was performed

- **Date:** 2026-10-17. **Reviewer:** Claude (an AI assistant), at the project owner's request. This is a review of
  *fictional* data against the taxonomy and the dish descriptions, **not a kitchen audit** and not a person with
  food-safety training. Every one of the 15 dishes passed R1-R5 and was judged coherent for R6; the per-dish
  reasoning is in `data/menu_review.json`.
- Making the menu pass required itemizing the blends and mixes the dishes used before (kabsa, mandi and biryani
  spices, "herbs", "grilled vegetables") into plain ingredients, so R1 holds.

## Limitations (read before relying on it)

- **Scope of allergens:** only the 14 categories in `data/taxonomy.json` (milk, gluten, eggs, peanuts, tree nuts, fish,
  crustaceans, molluscs, sesame, soy, mustard, celery, lupin, sulphites) are tracked. Other allergies (kiwi, lentils,
  strawberries...) are matched only when the guest names the ingredient (ingredient-level allergy), never as a category.
- **Assumed recipes:** the dough in the samosas is treated as plain wheat dough (gluten only, no egg or milk); syrup
  is plain sugar syrup; spices are single spices. A real kitchen could differ.
- **No cross-contact, no recipe changes, no supplier variation** are modelled. The assistant never says a dish is
  "safe"; it says the data comes from the restaurant's menu and asks the guest to tell the staff.
- **A person did not verify this.** For a real restaurant, replace `reviewer` and the notes with a real review and
  re-run the process; do not reuse this record.
- Ingredient lists are what the fictional menu states; the review checks they are coherent, not that they are true.

## Re-reviewing after an edit

1. Edit the dish in `data/menu.json`. Its `verified` status now silently drops to `unverified` (fingerprint changed).
2. `python -m services.menu_review` prints, per dish, the automated result, whether the review record is valid, and the
   current fingerprint.
3. Fix any failed criterion (R1-R5).
4. Read the dish against its description, then update that dish's entry in `data/menu_review.json`: new `fingerprint`,
   `criteria_passed` listing all of R1-R6, and honest `reviewer_notes`. Only then does it count as verified again.
5. Run `pytest`; `tests/test_menu_data.py` and `tests/test_menu_review.py` fail if a verified dish lacks a valid review.
