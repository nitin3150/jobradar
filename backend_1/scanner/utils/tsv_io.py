"""TSV I/O utilities for scan history."""

import csv
from pathlib import Path
from typing import Dict, List

TSV_PATH = Path(__file__).parent.parent / "data" / "scan-history.tsv"
PIPELINE_PATH = Path(__file__).parent.parent / "data" / "pipeline.md"


def load_history() -> List[Dict[str, str]]:
    """Read the TSV history file. Returns a list of dicts (one per row)."""
    if not TSV_PATH.exists():
        return []
    with TSV_PATH.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f, delimiter="\t")
        return list(reader)


def append_to_history(rows: List[Dict[str, str]], status: str = "added"):
    """Append rows to the TSV, adding the `status` column."""
    file_exists = TSV_PATH.exists()
    header = [
        "url",
        "first_seen",
        "portal",
        "title",
        "company",
        "status",
        "location",
    ]
    with TSV_PATH.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=header, delimiter="\t")
        if not file_exists:
            writer.writeheader()
        for r in rows:
            r["status"] = status
            writer.writerow(r)


def append_to_pipeline(jobs: List[Dict]):
    """Append new jobs to the markdown pipeline file (Pending section)."""
    if not PIPELINE_PATH.exists():
        ensure_pipeline_section()

    with PIPELINE_PATH.open("a", encoding="utf-8") as f:
        for job in jobs:
            line = f"- [ ] {job['url']} | {job['company']} | {job['title']}\n"
            f.write(line)


def ensure_pipeline_section():
    """Make sure the pipeline markdown file has a '## Pending' header."""
    path = Path(PIPELINE_PATH)
    if not path.exists():
        path.write_text(
            "# Pipeline – Pending\n\n"
            "Paste job URLs below as `- [ ] {url} | {company} | {title}` then run `/career-ops pipeline`\n\n"
            "## Pending\n\n"
            "## Processed\n\n",
            encoding="utf-8",
        )
