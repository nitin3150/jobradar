#!/usr/bin/env python3
"""
Extract startup funding news and job-search-relevant Reddit threads from Gmail
using Himalaya CLI. Integrates with JobRadar daily briefing pipeline.

Sources monitored:
- Superhuman AI (superhuman@mail.joinsuperhuman.ai) - daily AI startup funding
- The Neuron (theneuron@newsletter.theneurondaily.com) - AI funding & unicorns
- a16z newsletter (a16z@substack.com) - VC perspectives
- Crunchbase events (eventmarketing@email.crunchbase.com) - funding trends
- Reddit digests (noreply@redditmail.com) - job search threads from key subreddits

Outputs structured data for Notion sync and daily briefing enrichment.
"""

import json
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

# --- Configuration ---
NEWSLETTER_SOURCES = {
    "superhuman": {
        "from": "superhuman@mail.joinsuperhuman.ai",
        "category": "startup_funding",
        "priority": "high",
    },
    "the_neuron": {
        "from": "theneuron@newsletter.theneurondaily.com",
        "category": "startup_funding",
        "priority": "high",
    },
    "a16z": {
        "from": "a16z@substack.com",
        "category": "vc_insights",
        "priority": "medium",
    },
    "crunchbase": {
        "from": "eventmarketing@email.crunchbase.com",
        "category": "funding_events",
        "priority": "medium",
    },
    "wellfound_newsletter": {
        "from": "newsletters@hi.wellfound.com",
        "category": "startup_jobs",
        "priority": "high",
    },
}

REDDIT_SUBREDDITS = {
    "r/hiring": "direct_job_postings",
    "r/remotework": "remote_work_discussions",
    "r/cscareerquestions": "career_strategy",
    "r/ResumeCoverLetterTips": "resume_feedback",
    "r/forhire": "freelance_contract",
    "r/ExperiencedDevs": "senior_perspectives",
    "r/RecruitingHell": "market_signals",
}

# --- Himalaya helpers ---
def run_himalaya(args: list[str]) -> str:
    """Run himalaya command and return stdout."""
    cmd = ["himalaya"] + args
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        print(f"Warning: himalaya {' '.join(args)} failed: {result.stderr}", file=sys.stderr)
    return result.stdout

def list_emails(from_addr: str, hours_back: int = 24, limit: int = 20) -> list[dict]:
    """List emails from a specific sender within time window."""
    # Himalaya query: from <addr> and after <date>
    after_date = (datetime.now(timezone.utc) - timedelta(hours=hours_back)).strftime("%Y-%m-%d")
    query = f"from {from_addr} and after {after_date}"
    
    output = run_himalaya([
        "envelope", "list",
        "-p", "1",
        "-s", str(limit),
        "--output", "json",
        query
    ])
    
    try:
        return json.loads(output)
    except json.JSONDecodeError:
        return []


def read_email(email_id: str) -> str:
    """Read full email content by ID."""
    output = run_himalaya(["message", "read", email_id, "--output", "json"])
    try:
        data = json.loads(output)
        return data if isinstance(data, str) else json.dumps(data)
    except json.JSONDecodeError:
        return output


# --- Content extractors ---
def extract_superhuman_funding(content: str) -> list[dict]:
    """Extract funding announcements from Superhuman AI newsletter."""
    items = []
    
    # Superhuman format: **N. Title: **Body with markdown links
    # The format is: **N. Title: **Body (title ends with ': **')
    numbered_pattern = r'\*\*(\d+)\.\s+(.+?):\s*\*\*\s*(.*?)(?=\*\*\d+\.|$)'
    matches = list(re.finditer(numbered_pattern, content, re.DOTALL))
    
    funding_keywords = [
        r'\$[\d.]+[MB]\s+(?:startup|company|funding|round|valuation)',
        r'(?:raised|secured|closed)\s+\$[\d.]+[MB]',
        r'Series\s+[A-G]\s+(?:\$[\d.]+[MB]?)',
        r'(?:valuation|valued at)\s+\$[\d.]+[MB]',
        r'emerges? from stealth',
        r'unicorn',
        r'\$[\d.]+B\s+valuation',
        r'stealth',
        r'IPO',
        r'raise',
        r'funding',
        r'valuation',
        r'invest',
    ]
    
    for match in matches:
        item_num, title, body = match.groups()
        full_text = f"{title}: {body}"
        full_lower = full_text.lower()
        
        if any(re.search(p, full_lower) for p in funding_keywords):
            urls = re.findall(r'https?://[^\s\)\]]+', full_text)
            items.append({
                "source": "superhuman",
                "category": "startup_funding",
                "title": title.strip()[:200],
                "content_preview": body.strip()[:500],
                "urls": urls[:3],
                "priority": "high",
            })
    
    return items


def extract_neuron_funding(content: str) -> list[dict]:
    """Extract funding from The Neuron newsletter."""
    items = []
    
    funding_patterns = [
        r'\$[\d.]+[MB]\s+in\s+(?:AI\s+)?funding',
        r'(?:raised|secured)\s+\$[\d.]+[MB]',
        r'(?:valuation|valued at)\s+\$[\d.]+[MB]',
        r'unicorn',
        r'Series\s+[A-G]',
        r'IPO',
        r'stealth',
        r'\$[\d.]+B',
    ]
    
    # The Neuron uses emoji headers (## 😼 Title) and bullet points
    # Split by markdown headers
    sections = re.split(r'#{2,}\s+', content)
    
    for section in sections:
        section_lower = section.lower()
        if any(re.search(p, section_lower) for p in funding_patterns):
            lines = section.strip().split('\n')
            title = lines[0][:200] if lines else "AI Funding News"
            urls = re.findall(r'https?://[^\s\)\]]+', section)
            
            items.append({
                "source": "the_neuron",
                "category": "startup_funding",
                "title": title,
                "content_preview": section[:500],
                "urls": urls[:5],
                "priority": "high",
            })
    
    return items


def extract_a16z_insights(content: str) -> list[dict]:
    """Extract VC insights from a16z newsletter."""
    items = []
    
    keywords = [
        'late stage', 'early stage', 'series a', 'series b', 'series c',
        'venture', 'portfolio', 'founder', 'scaling', 'hiring',
        'ai agent', 'agentic', 'infrastructure', 'compute',
        'funding', 'valuation', 'unicorn', 'stealth',
    ]
    
    sections = re.split(r'#{2,}\s+', content)
    
    for section in sections:
        section_lower = section.lower()
        if any(kw in section_lower for kw in keywords):
            lines = section.strip().split('\n')
            title = lines[0][:200] if lines else "a16z Insight"
            urls = re.findall(r'https?://[^\s\)\]]+', section)
            
            items.append({
                "source": "a16z",
                "category": "vc_insights",
                "title": title,
                "content_preview": section[:500],
                "urls": urls[:5],
                "priority": "medium",
            })
    
    return items


def extract_crunchbase_events(content: str) -> list[dict]:
    """Extract event/funding info from Crunchbase emails."""
    items = []
    
    keywords = [
        'funding', 'series', 'raised', 'venture', 'startup', 'trends',
        'predict', '2026', 'ai agent', 'agentic', 'webinar', 'event',
        'ipo', 'unicorn', 'valuation',
    ]
    
    sections = re.split(r'#{2,}\s+', content)
    
    for section in sections:
        section_lower = section.lower()
        if any(kw in section_lower for kw in keywords):
            lines = section.strip().split('\n')
            title = lines[0][:200] if lines else "Crunchbase Event"
            urls = re.findall(r'https?://[^\s\)\]]+', section)
            
            items.append({
                "source": "crunchbase",
                "category": "funding_events",
                "title": title,
                "content_preview": section[:500],
                "urls": urls[:5],
                "priority": "medium",
            })
    
    return items


def extract_reddit_threads(content: str) -> list[dict]:
    """Extract job-search-relevant Reddit threads from digest emails."""
    items = []
    
    # Reddit digests contain multiple posts with subreddit markers
    # Pattern: r/subreddit followed by title and metadata
    
    # Find all subreddit references
    subreddit_matches = list(re.finditer(r'r/(\w+)', content))
    
    for i, match in enumerate(subreddit_matches):
        subreddit = f"r/{match.group(1)}"
        
        if subreddit not in REDDIT_SUBREDDITS:
            continue
        
        # Extract content around this match (next ~3000 chars)
        start = match.start()
        end = min(start + 3000, len(content))
        section = content[start:end]
        
        # Extract title (near the subreddit marker)
        title_match = re.search(r'[>"]\s*([^<\n]{20,200}?)\s*(?:\(|read more|$)', section)
        title = title_match.group(1).strip() if title_match else "Reddit Thread"
        
        # Extract upvotes/comments
        upvotes = re.search(r'(\d+(?:,\d+)?)\s+upvotes?', section)
        comments = re.search(r'(\d+(?:,\d+)?)\s+comments?', section)
        
        # Extract Reddit URL
        url_match = re.search(r'https?://www\.reddit\.com/r/\w+/comments/[^\s\)\]]+', section)
        url = url_match.group(0) if url_match else None
        
        # Extract preview text
        body_match = re.search(r'read more\s*\([^)]+\)\s*([^<]{50,500})', section, re.DOTALL)
        preview = body_match.group(1).strip()[:500] if body_match else section[:500]
        
        upvote_count = int(upvotes.group(1).replace(',', '')) if upvotes else 0
        comment_count = int(comments.group(1).replace(',', '')) if comments else 0
        
        items.append({
            "source": "reddit",
            "subreddit": subreddit,
            "category": REDDIT_SUBREDDITS[subreddit],
            "title": title,
            "content_preview": preview,
            "url": url,
            "upvotes": upvote_count,
            "comments": comment_count,
            "priority": "high" if upvote_count > 100 else "medium",
        })
    
    return items


# --- Main extraction pipeline ---
def extract_all(hours_back: int = 24) -> dict[str, list]:
    """Run extraction for all configured sources."""
    results = {
        "startup_funding": [],
        "vc_insights": [],
        "funding_events": [],
        "reddit_job_threads": [],
        "startup_jobs": [],
    }
    
    extractors = {
        "superhuman": extract_superhuman_funding,
        "the_neuron": extract_neuron_funding,
        "a16z": extract_a16z_insights,
        "crunchbase": extract_crunchbase_events,
        "wellfound_newsletter": lambda c: [],  # placeholder
    }
    
    # Process newsletter sources
    for source_name, config in NEWSLETTER_SOURCES.items():
        emails = list_emails(config["from"], hours_back=hours_back, limit=10)
        
        for email in emails:
            content = read_email(email["id"])
            extractor = extractors.get(source_name)
            if extractor:
                extracted = extractor(content)
                for item in extracted:
                    item["email_date"] = email["date"]
                    item["email_subject"] = email["subject"]
                    results[config["category"]].append(item)
    
    # Process Reddit digests
    reddit_emails = list_emails("noreply@redditmail.com", hours_back=hours_back, limit=5)
    for email in reddit_emails:
        content = read_email(email["id"])
        extracted = extract_reddit_threads(content)
        for item in extracted:
            item["email_date"] = email["date"]
            item["email_subject"] = email["subject"]
            results["reddit_job_threads"].append(item)
    
    return results


def format_for_notion(results: dict) -> list[dict]:
    """Format extracted items for Notion database insertion."""
    notion_items = []
    
    for category, items in results.items():
        for item in items:
            notion_item = {
                "Title": item.get("title", "Untitled")[:100],
                "Source": item.get("source", "unknown"),
                "Category": category,
                "Subcategory": item.get("subreddit") or item.get("category", ""),
                "URL": item.get("url") or (item.get("urls", [None])[0] if item.get("urls") else ""),
                "Content Preview": item.get("content_preview", "")[:2000],
                "Priority": item.get("priority", "medium"),
                "Email Date": item.get("email_date", ""),
                "Email Subject": item.get("email_subject", "")[:100],
                "Upvotes": item.get("upvotes", 0),
                "Comments": item.get("comments", 0),
                "Extracted At": datetime.now(timezone.utc).isoformat(),
            }
            notion_items.append(notion_item)
    
    return notion_items


def format_for_briefing(results: dict) -> str:
    """Format extracted items as markdown section for daily briefing."""
    sections = []
    
    if results.get("startup_funding"):
        sections.append("## 💰 Startup Funding & Stealth Exits (from Newsletters)")
        for item in results["startup_funding"][:5]:
            title = item.get("title", "Funding News")
            url = item.get("url") or (item.get("urls", [None])[0] if item.get("urls") else "")
            source = item.get("source", "newsletter").replace("_", " ").title()
            if url:
                sections.append(f"- **{title}** — *{source}* — [Read]({url})")
            else:
                sections.append(f"- **{title}** — *{source}*")
        sections.append("")
    
    if results.get("vc_insights"):
        sections.append("## 🏛 VC Perspectives (a16z, etc.)")
        for item in results["vc_insights"][:3]:
            title = item.get("title", "VC Insight")
            url = item.get("url") or (item.get("urls", [None])[0] if item.get("urls") else "")
            if url:
                sections.append(f"- **{title}** — [Read]({url})")
            else:
                sections.append(f"- **{title}**")
        sections.append("")
    
    if results.get("reddit_job_threads"):
        sections.append("## 🧵 Reddit Job Search Threads (High Signal)")
        # Sort by upvotes
        sorted_threads = sorted(
            results["reddit_job_threads"],
            key=lambda x: x.get("upvotes", 0),
            reverse=True
        )
        for item in sorted_threads[:5]:
            title = item.get("title", "Reddit Thread")
            subreddit = item.get("subreddit", "")
            upvotes = item.get("upvotes", 0)
            url = item.get("url", "")
            if url:
                sections.append(f"- **{title}** — *{subreddit}* — {upvotes}↑ — [Thread]({url})")
            else:
                sections.append(f"- **{title}** — *{subreddit}* — {upvotes}↑")
        sections.append("")
    
    return "\n".join(sections)


# --- CLI ---
def main():
    import argparse
    
    parser = argparse.ArgumentParser(description="Extract newsletter funding + Reddit job threads from Gmail")
    parser.add_argument("--hours", type=int, default=24, help="Hours back to search")
    parser.add_argument("--output", choices=["json", "notion", "briefing"], default="json")
    parser.add_argument("--save", type=str, help="Save output to file")
    
    args = parser.parse_args()
    
    print(f"Extracting from last {args.hours} hours...", file=sys.stderr)
    results = extract_all(hours_back=args.hours)
    
    # Print summary
    for cat, items in results.items():
        print(f"  {cat}: {len(items)} items", file=sys.stderr)
    
    output = ""
    if args.output == "json":
        output = json.dumps(results, indent=2)
    elif args.output == "notion":
        output = json.dumps(format_for_notion(results), indent=2)
    elif args.output == "briefing":
        output = format_for_briefing(results)
    
    if args.save:
        with open(args.save, "w") as f:
            f.write(output)
        print(f"Saved to {args.save}", file=sys.stderr)
    else:
        print(output)


if __name__ == "__main__":
    main()