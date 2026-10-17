# Testing guide for reviewers

A five-minute path through the deployed app, then how to run the project and its tests locally.

- **Deployed app:** <https://taste-pilot-ai.streamlit.app/>
- **Repository:** <https://github.com/NouraAlfantoukh/taste-pilot-ai>

The interface is Arabic (right-to-left). The assistant is powered by a language model, so **wording and the exact dishes can vary
between runs**; the checks below describe behaviour that is enforced by code (what may or may not be recommended), not exact text.
The menu is fictional and has 15 dishes.

## A. Open the application

1. Open the deployed URL. A free Streamlit app that has been idle may show a short wake-up screen first; wait for it.
2. You land on a welcome screen with two tabs («تسجيل الدخول» sign in, «حساب جديد» create account) and a button
   **«المتابعة كضيف (بدون حفظ)»** (continue as guest, nothing is saved). **Press the guest button**: no account is needed for sections
   B and C. Only the memory-across-conversations check needs an account (section D).

## B. Guided demo (copy and paste each message)

The first message in a fresh session makes the assistant ask about allergies before it recommends; answer it as in step 1.

| # | You send | What you should see |
|---|---|---|
| 1 | `ابي وجبة بحرية` then, when asked about allergies, `ما عندي حساسية من الأطعمة` | The grilled fish dish (the only fish dish on the menu), shown as a card with price, spice level and ingredients. The wording "وجبة بحرية" is understood as a food, not as a menu category. |
| 2 | `أبغى حلا دافي` | Warm desserts only (the menu records a serving temperature for every dish); the cold fruit salad is not offered as a match. |
| 3 | `ما أحب الفطر` | An acknowledgement that dishes containing mushroom will be excluded, and a «بدون فطر» chip under "تفضيلاتك". |
| 4 | `وش الأكلات اللي تعرف إني ما أحبها؟` | A list that includes mushroom, kept separate from allergies. It answers from stored preferences, not from the model's memory. |
| 5 | `اقترح لي أطباق فيها فطر` | An honest statement that no dish on the menu contains mushroom (it does not substitute unrelated dishes). |
| 6 | `عندي حساسية من الحليب، رشّح لي` | Only dishes without milk (for example no mandi, no karak tea, no kunafa); a note that some options were excluded because they contain milk; a reminder to tell the restaurant staff. The assistant never says a dish is "safe". |
| 7 | `أبغى طبق ما يتعدى ٢٠ ريال` | Dishes at or under 20 SAR that also respect the milk allergy and the mushroom dislike: the budget applies to this request only. |
| 8 | Press **«محادثة جديدة»** (new conversation), then send `رشّح لي شي` | The conversation is cleared. As a guest nothing was saved, so the allergy question is asked again (see D for an account, where preferences persist). |

Extra checks (optional):

- `عندي حساسية من المكسرات` should ask which kind (peanuts or tree nuts) instead of guessing; answer `المكسرات الشجرية`.
- Send several messages in a row: you must stay in the chat (a guest is not sent back to the welcome screen).
- Try `ما أبي حار` then `أبي حار`: the latest spice preference wins.

## C. Features to look for

| Feature | What to observe | Why it matters |
|---|---|---|
| Constraints enforced in code | Dishes with a declared allergen, a disliked ingredient, the wrong diet, over budget or too spicy never appear, even when the request pushes the other way | The model cannot override safety; see [DEEP_DIVE.md](DEEP_DIVE.md) |
| Honest explanations | The line under each dish ("نباتي مثل ما تفضّل", "غير حار حسب بيانات القائمة", "ضمن ميزانيتك") is built from menu data, and no reply claims a dish is "free of" an allergen | No unsupported assurances |
| Fail-closed allergy handling | Unknown or ambiguous allergen words are asked about and block recommendations until clarified | Unknown is never treated as safe |
| Data source shown | Each card lists "مسببات الحساسية (حسب بيانات المطعم)" or "(موثّقة)" | Transparency about the data |
| Preference chips and profile | «تفضيلاتك» chips; for signed-in users the «ملفي الغذائي» panel shows exactly what is saved, what is unsaved, and what failed to save | The UI never claims something was saved when it was not |
| Memory across conversations | After signing in, preferences survive «محادثة جديدة», a reload and a new sign-in; one user never sees another's | Per-user storage under database Row Level Security |

## D. Accounts and credentials

- **Guest mode** needs no credentials and is the supported way to try the app. It saves nothing.
- **Creating an account:** open «حساب جديد», enter an email you control and a password of at least 8 characters. The project may require
  the confirmation link sent by email before the first sign-in (the app says so). Use a real mailbox: some addresses are rejected by
  the authentication service.
- **Shared demo account: not provisioned in this repository, and none is published here.** If reviewers should test persistence without
  creating an account, the project owner must create a disposable account in the Supabase dashboard (Authentication -> Users -> Add user,
  *Auto Confirm User* on) and give the credentials to reviewers privately, outside the repository. **Status: pending manual action.**
  Note that signing out ends that account's sessions on every device, so give each reviewer their own account if several will test at once.
- The test accounts used by the repository's own real-service tests live only in a git-ignored `.env`; they are never in the repository.

## E. Run and test locally

Requirements: Python 3.12 or newer (the deployment runs 3.12; the offline suite was also run on 3.14).

**Windows (PowerShell)**

```powershell
git clone https://github.com/NouraAlfantoukh/taste-pilot-ai.git
cd taste-pilot-ai
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env        # then fill in your own values (see below)
streamlit run app.py
```

**macOS / Linux:** the same, with `source .venv/bin/activate` and `cp .env.example .env`.

Minimum `.env` for a working chat: `ANTHROPIC_API_KEY` or `GEMINI_API_KEY`. Add `SUPABASE_URL` and `SUPABASE_KEY` (anon/publishable key only)
to enable accounts and memory; without them the app runs as guest-only. Variable reference: [README](../README.md#configuration).

**Tests**

```powershell
pip install -r requirements-dev.txt   # adds pglast, used by the optional SQL checks
python -m pytest                      # offline suite: scripted model + fake Supabase, no network, no keys, no cost
python -m pytest -m integration -s    # OPT-IN: real Claude calls (a few cents) and real Supabase test accounts
python -m evaluation.run --score evaluation/results/full-main.jsonl   # recompute the Deep Dive numbers, no API
```

The offline suite is the one to run; the integration tests need your own API keys and dedicated Supabase test users
(see `.env.example`) and are skipped when those are missing. Latest verified offline result: see the README "Testing" section.

## F. Troubleshooting

| Symptom | Likely cause and action |
|---|---|
| A notice says the AI service is not configured and the chat input is disabled | No model key is set. Add `ANTHROPIC_API_KEY` or `GEMINI_API_KEY` to `.env` (local) or the Streamlit secrets (deployment), then restart. |
| «الخدمة مشغولة حاليًا» | The model provider returned a rate-limit or temporary error. Claude is tried once, then Gemini if configured. Wait a moment and resend. |
| The welcome screen does not appear and nothing is saved | `SUPABASE_URL` or `SUPABASE_KEY` is empty, so the app runs as guest-only by design. |
| «إعداد خدمة الحساب غير صالح» | `SUPABASE_KEY` is a service-role or secret key; the app refuses to use it. Use the anon/publishable key. |
| Sign-up says to confirm the email, or sign-in fails with "email not confirmed" | Open the confirmation email, or confirm the user in the Supabase dashboard. |
| «انتهت جلسة الدخول» | The sign-in session expired; sign in again. |
| «تعذّر حفظ تفضيلاتك الآن» | The database was unreachable. The preference still applies in this conversation, is not saved, and the profile panel says so; it is retried on the next message. |
| The deployed page shows an error right after a code update | Streamlit reloads modules while updating; reload the page, or reboot the app from Manage app. |
| Offline tests fail to import `streamlit` or `pglast` | The virtual environment is not active or dependencies are not installed; run the commands in section E. Tests that need `pglast` are skipped without it. |
