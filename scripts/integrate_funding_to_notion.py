#!/usr/bin/env python3
"""
Integrate Newsletter Funding Intelligence into Life Game Mode Dashboard
Adds a "Funding Intel" section to the Game OS with startup funding news,
VC insights, and job-search-relevant Reddit threads.
"""

import json
import os
import subprocess
from datetime import datetime, timezone

NOTION_API_KEY = os.environ.get("NOTION_API_KEY")
BASE_URL = "https://api.notion.com/v1"
HEADERS = {
    "Authorization": f"Bearer {NOTION_API_KEY}",
    "Notion-Version": "2025-09-03",
    "Content-Type": "application/json"
}

# Life Game Mode Dashboard ID
GAME_OS_DASHBOARD_ID = "f43784ee-3b36-8314-82fc-01f6e0aec636"

# Database IDs from your workspace
DATABASES = {
    "tasks": "866784ee-3b36-831d-9ee6-07f208ab7213",  # Tasks - Game Mode
    "missions": "371784ee-3b36-8344-b103-879904b118f3",  # Missions - Game Mode
    "character": "770784ee-3b36-8391-96b7-8750a76c7aee",  # Character - Game Mode
    "application_tracker": "f39784ee-3b36-83a8-b926-07d16814445e",  # Application Tracker
    "goals": "857784ee-3b36-823c-ad30-873e47ebfe94",  # Goals - Game Mode
    "rewards": "e81784ee-3b36-8206-ab4f-871981a3d572",  # Rewards - Game Mode
    "habits_good": "27d784ee-3b36-821c-a97b-870643c3774a",  # Good Habits
    "habits_bad": "2c3784ee-3b36-83f1-94fe-87dadd8aab54",  # Bad Habits
    "journal": "8a3784ee-3b36-8276-9782-07fd01640e9a",  # Journal
    "tasks_points": "9f5784ee-3b36-8227-bb27-0786bf7e43a4",  # Tasks Points
}


def run_extraction():
    """Run the newsletter funding extraction script."""
    result = subprocess.run([
        "python3", 
        "/Users/nitingoyal/Developer/jobradar/scripts/extract_newsletter_funding.py",
        "--hours", "24",
        "--output", "notion"
    ], capture_output=True, text=True, cwd="/Users/nitingoyal/Developer/jobradar")
    
    if result.returncode != 0:
        print(f"Extraction failed: {result.stderr}")
        return []
    
    return json.loads(result.stdout)


def create_funding_intel_page(items):
    """Create a markdown page for Funding Intel section."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    
    md = f"""# 💰 Funding Intel — {today}

*Auto-extracted from Superhuman AI, The Neuron, a16z, Crunchbase, and Reddit digests*

---

"""
    
    # Startup Funding
    funding_items = [i for i in items if i.get("Category") == "startup_funding"]
    if funding_items:
        md += "## 🚀 Startup Funding & Stealth Exits\n\n"
        for item in funding_items[:5]:
            title = item.get("Title", "Funding News")
            url = item.get("URL", "")
            source = item.get("Source", "newsletter").replace("_", " ").title()
            preview = item.get("Content Preview", "")[:200]
            
            if url:
                md += f"- **{title}** — *{source}* — [Read]({url})\n"
            else:
                md += f"- **{title}** — *{source}*\n"
            if preview:
                md += f"  > {preview}...\n"
        md += "\n"
    
    # VC Insights
    vc_items = [i for i in items if i.get("Category") == "vc_insights"]
    if vc_items:
        md += "## 🏛 VC Perspectives (a16z, etc.)\n\n"
        for item in vc_items[:3]:
            title = item.get("Title", "VC Insight")
            url = item.get("URL", "")
            preview = item.get("Content Preview", "")[:200]
            
            if url:
                md += f"- **{title}** — [Read]({url})\n"
            else:
                md += f"- **{title}**\n"
            if preview:
                md += f"  > {preview}...\n"
        md += "\n"
    
    # Reddit Job Threads
    reddit_items = [i for i in items if i.get("Category") == "reddit_job_threads"]
    if reddit_items:
        # Sort by upvotes
        reddit_items.sort(key=lambda x: x.get("Upvotes", 0), reverse=True)
        md += "## 🧵 Reddit Job Search Threads (High Signal)\n\n"
        for item in reddit_items[:5]:
            title = item.get("Title", "Reddit Thread")
            subreddit = item.get("Subcategory", "")
            upvotes = item.get("Upvotes", 0)
            url = item.get("URL", "")
            
            if url:
                md += f"- **{title}** — *{subreddit}* — {upvotes}↑ — [Thread]({url})\n"
            else:
                md += f"- **{title}** — *{subreddit}* — {upvotes}↑\n"
        md += "\n"
    
    # Action Items for Game Mode
    md += """## ⚡ Suggested Game Mode Actions

### New Missions to Consider
"""
    
    # Generate mission suggestions based on funding news
    if funding_items:
        md += "- [ ] **Research funded startups** — Add 3-5 companies from today's funding news to Application Tracker\n"
        md += "- [ ] **Tailor outreach** — Reference specific funding rounds in cold emails to show you've done homework\n"
    
    if vc_items:
        md += "- [ ] **Study VC theses** — Note which sectors a16z/Sequoia/etc. are betting on for interview talking points\n"
    
    if reddit_items:
        top_thread = reddit_items[0] if reddit_items else None
        if top_thread and top_thread.get("Upvotes", 0) > 100:
            md += f"- [ ] **Deep-dive thread**: \"{top_thread.get('Title', '')[:60]}...\" — High-signal career discussion\n"
    
    md += f"""

---
*Last updated: {datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}*
*Source: Gmail → Superhuman AI, The Neuron, a16z, Crunchbase, Reddit digests*
"""
    
    return md


def append_to_dashboard(markdown_content):
    """Append funding intel as a new block section to the Game OS dashboard."""
    import requests
    
    # Notion's append blocks endpoint
    url = f"{BASE_URL}/blocks/{GAME_OS_DASHBOARD_ID}/children"
    
    # Convert markdown to Notion blocks (simplified - using paragraph blocks)
    # For production, you'd want proper markdown parsing
    blocks = []
    
    lines = markdown_content.split("\n")
    for line in lines:
        if line.startswith("# "):
            blocks.append({
                "object": "block",
                "type": "heading_1",
                "heading_1": {"rich_text": [{"type": "text", "text": {"content": line[2:]}}]}
            })
        elif line.startswith("## "):
            blocks.append({
                "object": "block",
                "type": "heading_2",
                "heading_2": {"rich_text": [{"type": "text", "text": {"content": line[3:]}}]}
            })
        elif line.startswith("- [ ] "):
            blocks.append({
                "object": "block",
                "type": "to_do",
                "to_do": {"rich_text": [{"type": "text", "text": {"content": line[6:]}}], "checked": False}
            })
        elif line.startswith("- "):
            blocks.append({
                "object": "block",
                "type": "bulleted_list_item",
                "bulleted_list_item": {"rich_text": [{"type": "text", "text": {"content": line[2:]}}]}
            })
        elif line.startswith("> "):
            blocks.append({
                "object": "block",
                "type": "quote",
                "quote": {"rich_text": [{"type": "text", "text": {"content": line[2:]}}]}
            })
        elif line.strip() == "---":
            blocks.append({"object": "block", "type": "divider", "divider": {}})
        elif line.strip():
            blocks.append({
                "object": "block",
                "type": "paragraph",
                "paragraph": {"rich_text": [{"type": "text", "text": {"content": line}}]}
            })
    
    # Append in chunks of 100 (Notion limit)
    for i in range(0, len(blocks), 100):
        chunk = blocks[i:i+100]
        payload = {"children": chunk}
        response = requests.patch(url, headers=HEADERS, json=payload)
        if response.status_code >= 400:
            print(f"Error appending blocks: {response.text}")
            return False
    
    return True


def create_funding_database_entry(item):
    """Create an entry in a relevant database (e.g., Missions or Application Tracker)."""
    import requests
    
    # Determine which database based on category
    if item.get("Category") == "startup_funding":
        # Create a Mission to research this company
        url = f"{BASE_URL}/pages"
        payload = {
            "parent": {"database_id": DATABASES["missions"]},
            "properties": {
                "Name": {"title": [{"text": {"content": f"Research: {item.get('Title', 'Startup')[:100]}"}}]},
                "Status": {"status": {"name": "Not started"}},
                "Start Date": {"date": {"start": datetime.now(timezone.utc).date().isoformat()}},
            }
        }
        if item.get("URL"):
            payload["properties"]["Goal"] = {"relation": []}  # Could link to a Goal
        response = requests.post(url, headers=HEADERS, json=payload)
        return response.status_code < 400
    
    return False


def main():
    print("🔄 Extracting newsletter funding intelligence...")
    items = run_extraction()
    
    print(f"📊 Found: {len(items)} items")
    for cat in set(i.get("Category") for i in items):
        count = sum(1 for i in items if i.get("Category") == cat)
        print(f"  {cat}: {count}")
    
    # Create markdown for dashboard
    markdown = create_funding_intel_page(items)
    
    print("📝 Appending to Life Game Mode dashboard...")
    success = append_to_dashboard(markdown)
    
    if success:
        print("✅ Funding Intel added to Game OS dashboard!")
    else:
        print("❌ Failed to append to dashboard")
        # Save markdown for manual copy
        with open("/tmp/funding_intel.md", "w") as f:
            f.write(markdown)
        print("📄 Saved to /tmp/funding_intel.md for manual paste")
    
    # Optionally create Mission entries for funded startups
    funding_items = [i for i in items if i.get("Category") == "startup_funding"]
    for item in funding_items[:3]:
        if create_funding_database_entry(item):
            print(f"  ✅ Created Mission: {item.get('Title', '')[:50]}")


if __name__ == "__main__":
    main()