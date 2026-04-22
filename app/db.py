"""Supabase service-role client (server-side, bypasses RLS)."""
from functools import lru_cache

from supabase import Client, create_client

from app.config import get_settings


@lru_cache
def admin_client() -> Client:
    s = get_settings()
    return create_client(s.supabase_url, s.supabase_service_role_key)
