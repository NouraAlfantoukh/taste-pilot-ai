"""Supabase client creation. Nothing connects at import time.

Application code must only ever hold the anon/publishable key; row access is limited by RLS policies.

A client carries the signed-in user's session, so it must NEVER be shared between users or cached at module
level: every browser session creates its own client and keeps it in its own session state.
"""

import base64
import json
import logging
from typing import Optional

from config import get_setting, is_supabase_configured

logger = logging.getLogger(__name__)


class PrivilegedKeyError(ValueError):
    """SUPABASE_KEY is a privileged key (service_role / secret), which would bypass Row Level Security."""


def is_privileged_key(key: str) -> bool:
    """True for a service_role JWT or a new-style secret key. The key is inspected, never logged."""
    if key.startswith("sb_secret_"):
        return True
    parts = key.split(".")
    if len(parts) != 3:
        return False
    try:
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
    except ValueError:
        return False
    return isinstance(payload, dict) and payload.get("role") == "service_role"


def create_supabase_client() -> Optional[object]:
    """Create a NEW Supabase client (one per user session). Returns None if Supabase is not configured.

    Raises PrivilegedKeyError if SUPABASE_KEY is a service_role/secret key.
    """
    if not is_supabase_configured():
        logger.info("Supabase is not configured; persistent memory disabled.")
        return None

    key = get_setting("SUPABASE_KEY")
    if is_privileged_key(key):
        raise PrivilegedKeyError("SUPABASE_KEY must be the anon/publishable key, never a service_role or secret key.")

    from supabase import create_client

    return create_client(get_setting("SUPABASE_URL"), key)
