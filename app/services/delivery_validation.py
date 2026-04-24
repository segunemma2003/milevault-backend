"""Shared rules for delivery substance (submit + auto-release guard)."""
from __future__ import annotations

from typing import Any


def delivery_meets_auto_release_bar(m: Any) -> bool:
    """
    Auto-release only if delivery would have passed minimum submit rules:
    title ≥3 chars, note ≥10 chars, and at least one file key or valid https URL.
    """
    note = (getattr(m, "delivery_note", None) or "").strip()
    title = (getattr(m, "delivery_title", None) or "").strip()
    if len(note) < 10 or len(title) < 3:
        return False
    att = [a for a in (getattr(m, "delivery_attachments", None) or []) if isinstance(a, str) and a.strip()]
    if att:
        return True
    links = [u for u in (getattr(m, "delivery_external_links", None) or []) if isinstance(u, str) and u.strip()]
    for u in links:
        u2 = u.strip().lower()
        if u2.startswith("https://") or u2.startswith("http://"):
            return True
    return False
