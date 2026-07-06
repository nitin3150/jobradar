from langgraph.graph import StateGraph, START, END

from models.graph_state import PipelineState
from .nodes.ngo import scan_ngos
from .nodes.funding import scan_startups
from .nodes.jobs import scan_jobs
from .nodes.merge import merge_results

graph = StateGraph(PipelineState)

graph.add_node("NGOs", scan_ngos)
graph.add_node("jobs", scan_jobs)
graph.add_node("startups", scan_startups)

graph.add_node("merge", merge_results)

graph.add_edge(START, "NGOs")
graph.add_edge("NGOs", "jobs")
graph.add_edge("jobs", "startups")
graph.add_edge("startups", "merge")
graph.add_edge("merge", END)

scan_pipeline = graph.compile()