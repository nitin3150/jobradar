from models.graph_state import PipelineState


def scan_ngos(state: PipelineState) -> PipelineState:
    return {**state, "res": "ngo scanned"}