"""Local parser provider — runs a user script and returns JSON jobs."""

import json
import logging
import subprocess
from pathlib import Path
from typing import Dict, List

from scanner.providers.base import Job

logger = logging.getLogger(__name__)


def _run_script(script_path: str, args: List[str]) -> str:
    """Execute a script and capture its stdout."""
    result = subprocess.run(
        ["python", script_path] + args,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Script {script_path} failed: {result.stderr}")
    return result.stdout.strip()


def _normalize_output(raw: str) -> List[Dict]:
    """Parse local parser output (JSON array or object with jobs/results)."""
    data = json.loads(raw)
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        if "jobs" in data:
            return data["jobs"]
        if "results" in data:
            return data["results"]
    return []


async def fetch_local_parser(entry: Dict) -> List[Job]:
    """Run the local parser script defined in the entry and return parsed Job objects."""
    parser_cfg = entry.get("parser")
    if not parser_cfg:
        return []

    script_path = Path(parser_cfg["script"])
    args = parser_cfg.get("args", [])

    # inject company and careers_url if present
    inject_args = []
    if entry.get("company"):
        inject_args.append(f"--company={entry['company']}")
    if entry.get("careers_url"):
        inject_args.append(f"--careers_url={entry['careers_url']}")
    args = inject_args + args

    try:
        raw_output = _run_script(str(script_path), args)
        items = _normalize_output(raw_output)
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse local parser output for {entry['name']}: {e}")
        return []

    jobs = []
    for item in items:
        if not isinstance(item, dict):
            continue
        jobs.append(
            Job(
                title=item.get("title", ""),
                url=item.get("url", ""),
                company=item.get("company", entry.get("name", "")),
                location=item.get("location", ""),
                salary=item.get("salary", ""),
                description=item.get("description", ""),
                posted_at=item.get("posted_at"),
                ats_type="local_parser",
            )
        )
    return jobs
