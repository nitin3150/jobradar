#!/usr/bin/env -S /Users/nitingoyal/Developer/jobradar/backend/.venv/bin/python3
# -*- coding: utf-8 -*-
import sys
# Force use of the jobradar backend venv site-packages
sys.path.insert(0, '/Users/nitingoyal/Developer/jobradar/backend/.venv/lib/python3.12/site-packages')

"""
Notion Sync for JobRadar — Supabase jobs → Notion Application Tracker

Reads jobs from Supabase `jobs` table and upserts into Notion's
"Application Tracker" database (data_source_id: f39784ee-3b36-83a8-b926-07d16814445e).

Run via cron (daily) or manually after a boards-scan run.
"""

import os
import sys
from datetime import datetime, timezone
from typing import Optional

import requests
from supabase import create_client, Client

# ============================================================================
# HTTP SESSION WITH TIMEOUT
# ============================================================================

_session: Optional[requests.Session] = None

def _get_session() -> requests.Session:
    global _session
    if _session is None:
        _session = requests.Session()
    return _session

def _http(method: str, url: str, **kwargs) -> requests.Response:
    """HTTP request with timeout."""
    kwargs.setdefault("timeout", 30)
    return _get_session().request(method, url, **kwargs)

# ============================================================================
# CONFIGURATION
# ============================================================================

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
NOTION_API_KEY = os.environ.get("NOTION_API_KEY")

# Notion data_source ID (new API uses data_source, not database_id)
NOTION_DATA_SOURCE_ID = "f39784ee-3b36-83a8-b926-07d16814445e"

NOTION_API_VERSION = "2025-09-03"
NOTION_BASE_URL = "https://api.notion.com/v1"

# Status mapping: Supabase job_status → Notion status options
STATUS_MAP = {
    "in_review": "Not started",
    "approved": "Not started",    # AI approved for queue — not yet applied
    "applied": "Applied",          # Actually submitted
    "interview": "Interviewing",
    "offered": "Offered",
    "rejected": "Rejected",
    "paused": "Not started",
    "flagged": "Not started",
}

FLEXIBILITY_MAP = {
    "remote": "Remote",
    "hybrid": "Hybrid",
    "on-site": "On-site",
    "onsite": "On-site",
}

# ============================================================================
# SUPABASE CLIENT
# ============================================================================

def get_supabase() -> Client:
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        raise RuntimeError("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY required")
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


# ============================================================================
# NOTION HELPERS
# ============================================================================

def notion_headers() -> dict:
    return {
        "Authorization": f"Bearer {NOTION_API_KEY}",
        "Notion-Version": NOTION_API_VERSION,
        "Content-Type": "application/json",
    }


def _req(method: str, url: str, **kwargs) -> requests.Response:
    """Wrapper with timeout for all Notion API requests."""
    kwargs.setdefault("timeout", 30)
    kwargs.setdefault("headers", notion_headers())
    resp = _get_session().request(method, url, **kwargs)
    resp.raise_for_status()
    return resp


def query_notion_pages(company: str, title: str) -> list[dict]:
    """Find existing Notion pages matching company + title."""
    url = f"{NOTION_BASE_URL}/data_sources/{NOTION_DATA_SOURCE_ID}/query"
    payload = {
        "filter": {
            "and": [
                {"property": "Company", "rich_text": {"equals": company}},
                {"property": "Job Role", "title": {"equals": title}},
            ]
        }
    }
    resp = _req("post", url, json=payload)
    return resp.json().get("results", [])


def upsert_notion_page(job: dict) -> tuple[str, bool]:
    """
    Upsert a job into Notion Application Tracker.
    Returns (page_id, created_new).
    """
    existing = query_notion_pages(job["company_name"], job["title"])
    
    # Build properties
    properties = {
        "Job Role": {
            "title": [{"text": {"content": job["title"][:2000]}}]
        },
        "Company": {
            "rich_text": [{"text": {"content": job["company_name"][:2000]}}]
        },
        "URL": {"url": job["url"][:2000] if job.get("url") else None},
        "Status": {"status": {"name": STATUS_MAP.get(job.get("status", "in_review"), "Not started")}},
        "Flexibility": {"select": {"name": FLEXIBILITY_MAP.get(job.get("flexibility", "").lower(), "Remote")}},
    }
    
    # Application Date — use posted_at if available, else created_at, else today
    app_date = job.get("posted_at") or job.get("created_at") or datetime.now(timezone.utc).isoformat()
    if isinstance(app_date, str):
        try:
            dt = datetime.fromisoformat(app_date.replace("Z", "+00:00"))
            properties["Application Date"] = {"date": {"start": dt.date().isoformat()}}
        except Exception:
            properties["Application Date"] = {"date": {"start": datetime.now(timezone.utc).date().isoformat()}}
    
    # Location
    if job.get("location"):
        properties["Location"] = {"rich_text": [{"text": {"content": job["location"][:2000]}}]}
    
    # Email (optional — could extract from description later)
    if job.get("email"):
        properties["Email"] = {"email": job["email"]}
    
    # Build description content as page body
    description = job.get("description") or ""
    ai_reasoning = job.get("ai_fit_reasoning") or ""
    body_blocks = []
    
    if description:
        body_blocks.append({
            "object": "block",
            "type": "heading_2",
            "heading_2": {"rich_text": [{"type": "text", "text": {"content": "Job Description"}}]}
        })
        # Split into chunks (Notion blocks have 2000 char limit)
        for chunk in [description[i:i+1800] for i in range(0, len(description), 1800)]:
            body_blocks.append({
                "object": "block",
                "type": "paragraph",
                "paragraph": {"rich_text": [{"type": "text", "text": {"content": chunk}}]}
            })
    
    if ai_reasoning:
        body_blocks.append({
            "object": "block",
            "type": "heading_2",
            "heading_2": {"rich_text": [{"type": "text", "text": {"content": "AI Fit Analysis"}}]}
        })
        for chunk in [ai_reasoning[i:i+1800] for i in range(0, len(ai_reasoning), 1800)]:
            body_blocks.append({
                "object": "block",
                "type": "callout",
                "callout": {"rich_text": [{"type": "text", "text": {"content": chunk}}], "icon": {"emoji": "🤖"}}
            })
    
    # Add metadata block
    body_blocks.append({"object": "block", "type": "divider", "divider": {}})
    body_blocks.append({
        "object": "block",
        "type": "paragraph",
        "paragraph": {
            "rich_text": [{
                "type": "text",
                "text": {"content": f"Source: {job.get('ats_type', 'unknown').title()} | AI Fit Score: {job.get('ai_fit_score', 'N/A')} | Synced: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"}
            }]
        }
    })
    
    if existing:
        # UPDATE existing page
        page_id = existing[0]["id"]
        url = f"{NOTION_BASE_URL}/pages/{page_id}"
        payload = {"properties": properties}
        _req("patch", url, json=payload)
        
        # Replace page content (clear + append)
        # Get existing blocks
        blocks_url = f"{NOTION_BASE_URL}/blocks/{page_id}/children"
        resp = _req("get", blocks_url)
        existing_blocks = resp.json().get("results", [])
        
        # Delete existing blocks
        for block in existing_blocks:
            del_url = f"{NOTION_BASE_URL}/blocks/{block['id']}"
            _req("delete", del_url)
        
        # Append new blocks
        append_url = f"{NOTION_BASE_URL}/blocks/{page_id}/children"
        for i in range(0, len(body_blocks), 100):
            chunk = body_blocks[i:i+100]
            _req("patch", append_url, json={"children": chunk})
        
        return page_id, False
    else:
        # CREATE new page
        url = f"{NOTION_BASE_URL}/pages"
        payload = {
            "parent": {"data_source_id": NOTION_DATA_SOURCE_ID},
            "properties": properties,
            "children": body_blocks[:100],  # Initial create limit
        }
        resp = _http("post", url, headers=notion_headers(), json=payload)
        resp.raise_for_status()
        page = resp.json()
        page_id = page["id"]
        
        # Append remaining blocks if any
        if len(body_blocks) > 100:
            append_url = f"{NOTION_BASE_URL}/blocks/{page_id}/children"
            for i in range(100, len(body_blocks), 100):
                chunk = body_blocks[i:i+100]
                _req("patch", append_url, json={"children": chunk})
        
        return page_id, True


# ============================================================================
# MAIN SYNC LOGIC
# ============================================================================

def fetch_jobs_from_supabase(sb: Client, since: Optional[datetime] = None, status_filter: Optional[str] = None, limit: int = 500) -> list[dict]:
    """Fetch jobs from Supabase, optionally filtered by updated_at and status."""
    query = sb.table("jobs").select("*").order("updated_at", desc=True).limit(limit)
    
    if since:
        query = query.gte("updated_at", since.isoformat())
    if status_filter:
        query = query.eq("status", status_filter)
    
    resp = query.execute()
    return resp.data or []


def sync_jobs(since: Optional[datetime] = None, status_filter: Optional[str] = None, limit: int = 500, dry_run: bool = False) -> dict:
    """Main sync function."""
    sb = get_supabase()
    jobs = fetch_jobs_from_supabase(sb, since, status_filter, limit)
    
    print(f"Fetched {len(jobs)} jobs from Supabase")
    
    created = 0
    updated = 0
    errors = 0
    
    for job in jobs:
        try:
            if dry_run:
                print(f"  [DRY RUN] Would upsert: {job['title']} @ {job['company_name']} ({job.get('status')})")
                continue
            
            page_id, is_new = upsert_notion_page(job)
            if is_new:
                created += 1
                print(f"  ✅ Created: {job['title']} @ {job['company_name']}")
            else:
                updated += 1
                print(f"  🔄 Updated: {job['title']} @ {job['company_name']}")
        except Exception as e:
            errors += 1
            print(f"  ❌ Error syncing {job.get('title', 'unknown')} @ {job.get('company_name', 'unknown')}: {e}")
    
    return {"created": created, "updated": updated, "errors": errors, "total": len(jobs)}


def main():
    import argparse
    from datetime import timedelta
    
    parser = argparse.ArgumentParser(description="Sync Supabase jobs → Notion Application Tracker")
    parser.add_argument("--since-hours", type=int, default=24, help="Only sync jobs updated in last N hours")
    parser.add_argument("--status", type=str, help="Filter by job status (e.g., approved, applied)")
    parser.add_argument("--limit", type=int, default=500, help="Max jobs to fetch")
    parser.add_argument("--dry-run", action="store_true", help="Print what would be done without writing")
    args = parser.parse_args()
    
    # Validate env
    missing = []
    for var in ["SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY", "NOTION_API_KEY"]:
        if not os.environ.get(var):
            missing.append(var)
    if missing:
        print(f"ERROR: Missing required env vars: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)
    
    since = datetime.now(timezone.utc) - timedelta(hours=args.since_hours)
    
    print(f"🔄 Syncing jobs updated since {since.isoformat()}...")
    if args.status:
        print(f"   Filter: status = {args.status}")
    
    result = sync_jobs(since=since, status_filter=args.status, limit=args.limit, dry_run=args.dry_run)
    
    print(f"\n📊 Summary: {result['created']} created, {result['updated']} updated, {result['errors']} errors, {result['total']} total")
    
    if result["errors"] > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()