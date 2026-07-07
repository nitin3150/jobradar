"""Domain-aware merge node.

Takes the per-domain opportunity lists attached by each LangGraph node
and produces a single ordered list (newest-first within each domain),
without changing the input shape so back-compat code still works.
"""
from datetime import datetime, timezone
from typing import Any

from models.graph_state import PipelineState

DOMAIN_KEYS = ("funding", "remote", "ngos", "oss")


def _sort_key(opportunity: dict[str, Any]) -> str:
    return str(opportunity.get("published") or "")


def merge_results(state: PipelineState) -> PipelineState:
    merged: list[dict[str, Any]] = []
    for domain in DOMAIN_KEYS:
        bucket = state.get(domain) or []
        if isinstance(bucket, list):
            merged.extend(bucket)

    # Stable, newest-first within the merged list.
    merged.sort(key=lambda opp: _sort_key(opp) or "0", reverse=True)

    return {**state, "res": merged, "merged_at": datetime.now(timezone.utc).isoformat()}
