# Deployment (Streamlit Community Cloud)

TastePilotAI is a single Streamlit app (`app.py`). It reads every setting from environment variables first, then from
Streamlit secrets (`config.py`), so no code change is needed to deploy and **no secret ever lives in the repository**.

## What you need

| Item | Where it comes from |
|---|---|
| A GitHub repository with this code (public, or private with Streamlit granted access) | this repo |
| A Streamlit Community Cloud account | <https://share.streamlit.io>, sign in with GitHub |
| `ANTHROPIC_API_KEY` | Anthropic console |
| `GEMINI_API_KEY` (fallback model, optional but recommended) | Google AI Studio |
| `SUPABASE_URL` and `SUPABASE_KEY` | Supabase project, *Project Settings -> API* |

`SUPABASE_KEY` must be the **anon / publishable** key. The app refuses to start persistence with a `service_role` or
`sb_secret_...` key.

## Steps

1. **Push the repository** (done by the project owner; `.env`, virtual environments and `.claude/` are git-ignored).
2. **Create the app:** share.streamlit.io -> *Create app* -> *Deploy a public app from GitHub* ->
   repository `NouraAlfantoukh/taste-pilot-ai`, branch `main`, main file `app.py`.
3. **Advanced settings -> Python version:** choose **3.12** or **3.13**. The code was developed and fully tested only on
   Python 3.14.2; if the platform fails to install the pinned packages on your chosen version, switch to the other one
   (check the build log) rather than loosening the pins.
4. **Advanced settings -> Secrets:** paste this TOML, with your own values (type them in the Streamlit dashboard, never
   in chat or in a file you commit):

   ```toml
   ANTHROPIC_API_KEY = "..."
   GEMINI_API_KEY = "..."
   ANTHROPIC_MODEL = "claude-haiku-5-5"
   GEMINI_MODEL = "gemini-3.8-flash"
   SUPABASE_URL = "https://<project-ref>.supabase.co"
   SUPABASE_KEY = "<anon-or-publishable-key>"
   ```

5. **Deploy**, then copy the public `https://<name>.streamlit.app` URL.
6. **Supabase -> Authentication -> URL Configuration:** set *Site URL* to that URL (and add it under *Redirect URLs*),
   so the confirmation email links back to the live app. If you do not want to wait for emails during a demo, create the
   demo user in *Authentication -> Users -> Add user* with *Auto Confirm User* enabled.
7. **Supabase -> SQL:** confirm `supabase/migrations/20261017120000_create_user_preferences.sql` has been applied
   (table `public.user_preferences` with RLS enabled and forced, four policies for `authenticated`).

## What the app does when something is missing

| Missing | Behaviour |
|---|---|
| Both Supabase values | the app runs as a guest-only assistant and saves nothing |
| `ANTHROPIC_API_KEY` | Gemini is used if configured; otherwise a clear Arabic notice and the chat is disabled |
| both model keys | the same notice; no crash |

## Security notes

- Secrets exist only in the Streamlit dashboard (or a local, git-ignored `.env`).
- Each browser session uses its own Supabase client signed in with the user's own session and the anon key; the
  database enforces `auth.uid() = user_id` (RLS), so one user can never read or write another user's row.
- The repository contains no credentials; `.env.example` lists the variable names only.
