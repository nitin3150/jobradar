from typing import TypedDict


class PipelineState(TypedDict, total=False):
    """Pipeline state propagated through the LangGraph.

    Each domain node writes its own opportunity list, so the merge step
    has the full per-domain picture instead of an opaque ``res`` string.
    """
    input: str
    res: str
    funding: list[dict]
    remote: list[dict]
    ngos: list[dict]
    oss: list[dict]
