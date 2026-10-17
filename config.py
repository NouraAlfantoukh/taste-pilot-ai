"""Central configuration access. Reads environment variables first, then Streamlit secrets."""

import os

from dotenv import load_dotenv

load_dotenv()


def get_setting(name: str, default: str = "") -> str:
    """Return a setting from the environment or Streamlit secrets; never raises if missing."""
    value = os.environ.get(name)
    if value:
        return value
    try:
        import streamlit as st

        secret = st.secrets.get(name)
        if secret:
            return str(secret)
    except Exception:
        pass
    return default


def is_anthropic_configured() -> bool:
    return bool(get_setting("ANTHROPIC_API_KEY"))


def is_gemini_configured() -> bool:
    return bool(get_setting("GEMINI_API_KEY"))


def is_supabase_configured() -> bool:
    return bool(get_setting("SUPABASE_URL") and get_setting("SUPABASE_KEY"))
