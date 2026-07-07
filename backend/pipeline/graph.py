"""LangGraph pipeline orchestration.

The graph composes the four non-board scrape domains in parallel-feed
fashion. Job Boards are dispatched separately (run on an hourly schedule
with a much larger delta_hours window) so they have their own runner.

Each node still uses the same ``{domain: opportunities}`` shape and the
merged state now includes a per-domain ``opportunities`` list rather
than a mixed bag.
"""
from langgraph.graph import StateGraph, START, END

from models.graph_state import PipelineState
from .nodes.funding.runner import scan_funding
from .nodes.ngos.runner import scan_ngos
from .nodes.remote.runner import scan_remote
from .nodes.oss.runner import scan_oss
from .nodes.merge import merge_results


def _funding_node(state: PipelineState) -> PipelineState:
    return {**state, "funding": scan_funding(delta_hours=24)}


def _remote_node(state: PipelineState) -> PipelineState:
    return {**state, "remote": scan_remote(delta_hours=24)}


def _ngos_node(state: PipelineState) -> PipelineState:
    return {**state, "ngos": scan_ngos(delta_hours=72)}


def _oss_node(state: PipelineState) -> PipelineState:
    return {**state, "oss": scan_oss(delta_hours=168)}


graph = StateGraph(PipelineState)
graph.add_node("funding", _funding_node)
graph.add_node("remote", _remote_node)
graph.add_node("ngos", _ngos_node)
graph.add_node("oss", _oss_node)
graph.add_node("merge", merge_results)

graph.add_edge(START, "funding")
graph.add_edge(START, "remote")
graph.add_edge(START, "ngos")
graph.add_edge(START, "oss")
graph.add_edge("funding", "merge")
graph.add_edge("remote", "merge")
graph.add_edge("ngos", "merge")
graph.add_edge("oss", "merge")
graph.add_edge("merge", END)

scan_pipeline = graph.compile()
