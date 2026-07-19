#!/usr/bin/env python3
"""
Comprehensive Life Game Mode Dashboard Enhancement

Adds:
1. Navigation links (Job Tracker, Learning/LeetCode, Newsletter Hub)
2. Newsletter Hub with daily dated pages
3. Job Tracker sync from AI Job Daily Briefing
4. Daily Quest tasks for job applications
"""

import json
import os
import subprocess
from datetime import datetime, timezone, timedelta
import requests

NOTION_API_KEY = os.environ.get("NOTION_API_KEY")
BASE_URL = "https://api.notion.com/v1"
HEADERS = {
    "Authorization": f"Bearer {NOTION_API_KEY}",
    "Notion-Version": "2025-09-03",
    "Content-Type": "application/json"
}

# Page IDs from your workspace
PAGES = {
    "life_game_mode": "f43784ee-3b36-8314-82fc-01f6e0aec636",      # Life Game Mode (dashboard)
    "job_application_tracker": "bde784ee-3b36-82a9-8380-012e2046ac86",  # Job Application Tracker
    "daily_quests": "bc7784ee-3b36-82a9-a4f0-81f937f6eb9b",          # Daily Quests
    "leetcode_hub": "0ce784ee-3b36-823d-b19c-817f3be3d93f",          # Leetcode Study Hub
    "objectives": "02a784ee-3b36-822a-bf70-810a82b740fe",            # Objectives (Goals)
    "missions": "9f0784ee-3b36-8368-abf0-01870b9002b2",              # Missions
}

# Database IDs
DATABASES = {
    "tasks": "866784ee-3b36-831d-9ee6-07f208ab7213",      # Tasks - Game Mode
    "application_tracker": "92f784ee-3b36-831e-9895-017285bc6914",  # Application Tracker (parent database_id for writes)
    "application_tracker_ds": "f39784ee-3b36-83a8-b926-07d16814445e",  # Application Tracker data source (for queries)
    "missions_db": "371784ee-3b36-8344-b103-879904b118f3",  # Missions - Game Mode
    "goals_db": "857784ee-3b36-823c-ad30-873e47ebfe94",    # Goals - Game Mode
    "character": "770784ee-3b36-8391-96b7-8750a76c7aee",   # Character - Game Mode
    "tasks_points": "9f5784ee-3b36-8227-bb27-0786bf7e43a4", # Tasks Points
}


def run_himalaya(args):
    """Run himalaya command and return stdout."""
    cmd = ["himalaya"] + args
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        print(f"Warning: himalaya {' '.join(args)} failed: {result.stderr}")
    return result.stdout


def list_emails(from_addr, hours_back=24, limit=20):
    """List emails from a specific sender within time window."""
    after_date = (datetime.now(timezone.utc) - timedelta(hours=hours_back)).strftime("%Y-%m-%d")
    query = f"from {from_addr} and after {after_date}"
    
    output = run_himalaya([
        "envelope", "list", "-p", "1", "-s", str(limit), "--output", "json", query
    ])
    try:
        return json.loads(output)
    except json.JSONDecodeError:
        return []


def read_email(email_id):
    """Read full email content by ID."""
    output = run_himalaya(["message", "read", email_id, "--output", "json"])
    try:
        data = json.loads(output)
        return data if isinstance(data, str) else json.dumps(data)
    except json.JSONDecodeError:
        return output


def create_notion_page(parent_page_id, title, markdown_content, icon_emoji="📄"):
    """Create a new Notion page with markdown content."""
    url = f"{BASE_URL}/pages"
    payload = {
        "parent": {"page_id": parent_page_id},
        "icon": {"emoji": icon_emoji},
        "properties": {
            "title": {"title": [{"text": {"content": title}}]}
        },
        "children": []
    }
    
    # Convert markdown to Notion blocks
    blocks = markdown_to_blocks(markdown_content)
    payload["children"] = blocks[:100]  # Notion limit
    
    response = requests.post(url, headers=HEADERS, json=payload)
    if response.status_code >= 400:
        print(f"Error creating page: {response.text}")
        return None
    
    result = response.json()
    page_id = result["id"]
    
    # Add remaining blocks if any
    if len(blocks) > 100:
        append_blocks(page_id, blocks[100:])
    
    return page_id


def append_blocks(page_id, blocks):
    """Append blocks to an existing page."""
    url = f"{BASE_URL}/blocks/{page_id}/children"
    for i in range(0, len(blocks), 100):
        chunk = blocks[i:i+100]
        payload = {"children": chunk}
        response = requests.patch(url, headers=HEADERS, json=payload)
        if response.status_code >= 400:
            print(f"Error appending blocks: {response.text}")


def markdown_to_blocks(markdown):
    """Convert markdown to Notion blocks (simplified)."""
    blocks = []
    lines = markdown.split("\n")
    
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
        elif line.startswith("### "):
            blocks.append({
                "object": "block",
                "type": "heading_3",
                "heading_3": {"rich_text": [{"type": "text", "text": {"content": line[4:]}}]}
            })
        elif line.startswith("- [ ] "):
            blocks.append({
                "object": "block",
                "type": "to_do",
                "to_do": {"rich_text": [{"type": "text", "text": {"content": line[6:]}}], "checked": False}
            })
        elif line.startswith("- [x] "):
            blocks.append({
                "object": "block",
                "type": "to_do",
                "to_do": {"rich_text": [{"type": "text", "text": {"content": line[6:]}}], "checked": True}
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
    
    return blocks


def add_navigation_section():
    """Add navigation links section to Life Game Mode dashboard."""
    markdown = """## 🧭 Quick Navigation
---

<columns>
\t<column>
\t\t### 📋 Job Search
\t\t- [📋 Job Application Tracker](https://app.notion.com/p/Job-Application-Tracker-bde784ee3b3682a98380012e2046ac86)
\t\t- [🎯 Daily Quests](https://app.notion.com/p/Daily-Quests-bc7784ee3b3682a9a4f081f937f6eb9b)
\t\t- [🎯 Missions](https://app.notion.com/p/Missions-9f0784ee3b368368abf001870b9002b2)
</column>
\t<column>
\t\t### 🧠 Learning
\t\t- [💻 LeetCode Study Hub](https://app.notion.com/p/Leetcode-Study-Hub-0ce784ee3b36823db19c817f3be3d93f)
\t\t- [📚 Objectives/Goals](https://app.notion.com/p/Objectives-02a784ee3b36822abf70810a82b740fe)
</column>
\t<column>
\t\t### 📰 Newsletter Hub
\t\t- [💰 Funding Intel](https://app.notion.com/p/Life-Game-Mode-f43784ee3b36831482fc01f6e0aec636#funding)
\t\t- [🤖 AI Daily Briefs](https://app.notion.com/p/Life-Game-Mode-f43784ee3b36831482fc01f6e0aec636#ai-briefs)
\t\t- [📅 Today's Brief](https://app.notion.com/p/Life-Game-Mode-f43784ee3b36831482fc01f6e0aec636#today)
</column>
</columns>

---
"""
    # Append to dashboard
    url = f"{BASE_URL}/blocks/{PAGES['life_game_mode']}/children"
    blocks = markdown_to_blocks(markdown)
    payload = {"children": blocks}
    response = requests.patch(url, headers=HEADERS, json=payload)
    if response.status_code < 400:
        print("✅ Navigation section added to Life Game Mode dashboard")
    else:
        print(f"❌ Error adding navigation: {response.text}")


def create_newsletter_hub():
    """Create a Newsletter Hub sub-page under Life Game Mode."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    
    markdown = f"""# 📰 Newsletter Hub
*Central hub for AI Daily Briefs, Funding Intel, and Job Briefings*

---

## 📅 Today's Briefing ({today})
*Auto-generated from daily cron jobs*

### 🤖 AI Daily Brief — {today}
*Technical AI papers, lab blogs, GitHub/HF trends*
[View Full Brief →](#ai-brief-{today})

### 💰 Funding Intel — {today}
*Startup funding, VC insights, stealth exits*
[View Full Intel →](#funding-{today})

### 🎯 AI Job Daily Brief — {today}
*New job matches from 8 sources*
[View Full Brief →](#jobs-{today})

---

## 📚 Archive
*Previous daily pages*

"""
    
    # Create as sub-page of Life Game Mode
    page_id = create_notion_page(
        PAGES["life_game_mode"],
        f"📰 Newsletter Hub",
        markdown,
        "📰"
    )
    
    if page_id:
        print(f"✅ Newsletter Hub created: {page_id}")
        return page_id
    return None


def create_daily_newsletter_page(hub_page_id, date_str=None):
    """Create a dated daily newsletter page under the Newsletter Hub."""
    if date_str is None:
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    
    title = f"📰 Daily Brief — {date_str}"
    
    markdown = f"""# 📰 Daily Brief — {date_str}
*Generated: {datetime.now(timezone.utc).strftime("%H:%M UTC")}*

---

## 🤖 AI Daily Brief — {date_str}
*10 AM ET cron • arXiv, Lab Blogs, GitHub Trending, HF Trending*

### 📄 Papers & Preprints
*Loading from 10 AM cron...*

### 🛠 Tools & Libraries
*Loading...*

### 🏭 Industry Updates
*Loading...*

---

## 💰 Funding Intel — {date_str}
*11 AM ET cron • Superhuman AI, The Neuron, a16z, Crunchbase, Reddit*

### 🚀 Startup Funding & Stealth Exits
*Loading from 11 AM cron...*

### 🏛 VC Perspectives
*Loading...*

### 🧵 Reddit Job Threads
*Loading...*

---

## 🎯 AI Job Daily Brief — {date_str}
*12 PM ET cron • 8 sources → Notion sync*

### ✅ New Matches Added to Tracker
*Loading from 12 PM cron...*

### 📊 Source Breakdown
*Loading...*

---

## ⚡ Suggested Actions
- [ ] Review new job matches in **Application Tracker**
- [ ] Apply to top 3 matches today
- [ ] Research funded startups for outreach
- [ ] Complete LeetCode daily problem

---
*Auto-generated by JobRadar pipeline*
"""
    
    page_id = create_notion_page(hub_page_id, title, markdown, "📅")
    if page_id:
        print(f"✅ Daily newsletter page created: {title} ({page_id})")
    return page_id


def sync_ai_job_briefing_to_tracker():
    """Parse AI Job Daily Briefing email and add new jobs to Application Tracker."""
    # Search for the AI Job Daily Briefing email
    emails = list_emails("nitingoyal3150@gmail.com", hours_back=48, limit=20)
    
    job_brief_email = None
    for email in emails:
        subject = email.get("subject", "")
        if "AI Job Daily Brief" in subject or "🎯 AI Job Daily Brief" in subject:
            job_brief_email = email
            break
    
    if not job_brief_email:
        print("ℹ️ No AI Job Daily Briefing found in last 48 hours")
        return
    
    print(f"📧 Found job briefing: {job_brief_email['subject']}")
    content = read_email(job_brief_email["id"])
    
    # Parse the markdown content for job entries
    # The email has HTML-encoded markdown with <#part type=text/markdown> wrapper
    import re
    import html
    
    # Extract the markdown content from the email
    markdown_match = re.search(r'<#part type=text/markdown>\n(.*?)\n<#/part>', content, re.DOTALL)
    if markdown_match:
        markdown_content = markdown_match.group(1)
        # Decode HTML entities
        markdown_content = html.unescape(markdown_content)
    else:
        # Fallback: try to find markdown content after the part marker
        markdown_content = content
    
    # Parse job entries: ### N. Job Title at Company [Tier • Score]
    job_pattern = r'###\s+\d+\.\s+([^@]+?)\s+at\s+([^\n]+?)\s*\[([^\]]+)\]'
    jobs = re.findall(job_pattern, markdown_content)
    
    print(f"📊 Found {len(jobs)} job entries in briefing")
    
    added_count = 0
    for job_title, company, tier_score in jobs:
        job_title = job_title.strip()
        company = company.strip()
        tier_score = tier_score.strip()
        
        print(f"  Processing: {job_title} at {company} [{tier_score}]")
        
        # Check if already exists in tracker
        if check_job_exists(company, job_title):
            print(f"  ⏭️ Already exists: {job_title} at {company}")
            continue
        
        # Add to Application Tracker
        job_id = add_job_to_tracker(job_title, company, tier_score)
        if job_id:
            added_count += 1
            print(f"  ✅ Added: {job_title} at {company}")
            
            # Create a Daily Quest task for applying
            create_apply_task(job_title, company, job_id)
    
    print(f"📊 Synced {added_count} new jobs to tracker")
    return added_count


def check_job_exists(company, title):
    """Check if job already exists in Application Tracker."""
    url = f"{BASE_URL}/data_sources/{DATABASES['application_tracker_ds']}/query"
    payload = {
        "filter": {
            "and": [
                {"property": "Company", "rich_text": {"equals": company}},
                {"property": "Job Role", "title": {"equals": title}}
            ]
        },
        "page_size": 1
    }
    response = requests.post(url, headers=HEADERS, json=payload)
    if response.status_code < 400:
        result = response.json()
        return len(result.get("results", [])) > 0
    else:
        print(f"  ⚠️ Check failed: {response.text}")
        return False


def add_job_to_tracker(title, company, tier_score=""):
    """Add a new job to the Application Tracker database."""
    url = f"{BASE_URL}/pages"
    payload = {
        "parent": {"database_id": DATABASES["application_tracker"]},
        "properties": {
            "Job Role": {"title": [{"text": {"content": title}}]},
            "Company": {"rich_text": [{"text": {"content": company}}]},
            "Status": {"status": {"name": "Not started"}},
            "Application Date": {"date": {"start": datetime.now(timezone.utc).date().isoformat()}},
            "Flexibility": {"select": {"name": "Remote"}},  # Default
        }
    }
    
    response = requests.post(url, headers=HEADERS, json=payload)
    if response.status_code == 404:
        print(f"  ❌ Database not shared with integration. Please share it in Notion:")
        print(f"     Go to: https://app.notion.com/p/92f784ee3b36831e9895017285bc6914")
        print(f"     Click ⋮⋮ → Connect to → hermes")
        return None
    elif response.status_code < 400:
        return response.json()["id"]
    else:
        print(f"  ❌ Failed to add job: {response.text}")
        return None


def create_apply_task(job_title, company, job_id=None):
    """Create a Daily Quest task for applying to this job."""
    url = f"{BASE_URL}/pages"
    task_name = f"Apply: {job_title} at {company}"
    
    properties = {
        "Name": {"title": [{"text": {"content": task_name}}]},
        "Status": {"status": {"name": "Not started"}},
        "Priority ": {"select": {"name": "1st Priority"}},
        "Date": {"date": {"start": datetime.now(timezone.utc).date().isoformat()}},
    }
    
    if job_id:
        properties["Job Applications"] = {"relation": [{"id": job_id}]}
    else:
        properties["Job Applications"] = {"relation": []}
    
    payload = {
        "parent": {"database_id": DATABASES["tasks"]},
        "properties": properties
    }
    
    response = requests.post(url, headers=HEADERS, json=payload)
    if response.status_code < 400:
        print(f"  ✅ Created apply task: {task_name}")
    else:
        print(f"  ❌ Failed to create task: {response.text}")


def main():
    print("🚀 Enhancing Life Game Mode Dashboard...")
    
    # 1. Add navigation section
    print("\n1️⃣ Adding navigation links...")
    add_navigation_section()
    
    # 2. Create Newsletter Hub
    print("\n2️⃣ Creating Newsletter Hub...")
    hub_id = create_newsletter_hub()
    
    # 3. Create today's daily newsletter page
    if hub_id:
        print("\n3️⃣ Creating today's daily newsletter page...")
        create_daily_newsletter_page(hub_id)
    
    # 4. Sync AI Job Briefing to Tracker
    print("\n4️⃣ Syncing AI Job Briefing to Application Tracker...")
    sync_ai_job_briefing_to_tracker()
    
    print("\n✅ Life Game Mode enhancement complete!")


if __name__ == "__main__":
    main()