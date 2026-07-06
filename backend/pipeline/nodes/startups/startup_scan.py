from models.graph_state import PipelineState
from startups.hackernews import HN_scan
from startups.startupgallary import SG_scan
from startups.producthunt import PH_scan

def scan_startups(state: PipelineState) -> PipelineState:
    hn_news = HN_scan()
    sg_news = SG_scan()

    summary = hn_news.get("res", "") or sg_news.get("res", "") or "no funding data"
    return {**state, "res": summary}