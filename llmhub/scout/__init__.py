"""Scheduled promo hunting on the hub's own free models.

collect (no LLM) -> extract (alias `fast`) -> curate (alias `auto`) -> apply to Promos.
"""

from __future__ import annotations

from .runner import ScoutService, next_run_at, run

__all__ = ["ScoutService", "next_run_at", "run"]
