# Job Hunter — Agent Skill

## What this project does
A daily Python script that scrapes Ashby, Lever, and Greenhouse job board pages (no API keys), 
filters postings against Nitin's profile using Claude API, and outputs a ranked HTML digest 
with outreach links (Twitter, LinkedIn, Apollo) for each matched company.

## Owner profile (for filtering decisions)
- Name: Nitin | MS AI, Northeastern | Based in Boston
- Target: AI Engineer / LLM Engineer / ML Engineer at Series A-C startups
- Stack: LangGraph, LangChain, FastAPI, Python, React Native, AWS, Docker, MongoDB
- Wants: Healthcare AI, agentic systems, LLM infra companies — full AI stack ownership
- Hard pass: pure frontend, pure data analyst, 10+ yrs required, legacy enterprise tech

## File structure
```
job_hunter.py     # main script — all logic lives here
.env              # ANTHROPIC_API_KEY only
README.md         # setup and usage guide
job_digest_*.html # daily output files (gitignored)
```

## Key functions to know
- `scrape_ashby / scrape_lever / scrape_greenhouse` — HTML scraping, no auth
- `keyword_prefilter` — fast local filter before Claude API calls
- `fetch_description` — fetches full JD page for better Claude context
- `filter_with_claude` — batches 5 jobs per Claude call, returns KEEP/DISCARD + fit_score
- `outreach_links` — builds Twitter/LinkedIn/Apollo search URLs per company
- `save_html` — generates the daily digest HTML report
- `ddg_search` — DuckDuckGo HTML scraper utility (no key needed)

## ATS_TARGETS list
Tuples of ("Company Name", "ats_type", "slug") where ats_type is ashby/lever/greenhouse.
Slug comes from the job board URL: jobs.ashbyhq.com/SLUG, jobs.lever.co/SLUG, boards.greenhouse.io/SLUG.

## How to run
```bash
python job_hunter.py                    # full run
python job_hunter.py --output html      # HTML only
python job_hunter.py --no-descriptions  # faster, title-only filtering
```

## Common tasks for the agent

### Add a new company
Append to ATS_TARGETS in job_hunter.py:
```python
("Company Name", "ashby|lever|greenhouse", "board-slug")
```
Find slug from their job board URL.

### Debug a scraper returning 0 jobs
1. Check if the board URL is still valid (sites change slugs occasionally)
2. Inspect the HTML structure — Lever/Greenhouse sometimes update their CSS class names
3. Playwright is NOT needed — all three ATSes render server-side HTML
4. Print `r.text[:2000]` inside the scraper to inspect raw HTML

### Modify the profile filter
Edit the `YOUR_PROFILE` string at the top of job_hunter.py.
The KEEP/DISCARD criteria directly influence Claude's batch filtering.

### Add a new ATS platform (e.g. Workday, Rippling)
1. Find the public job board URL pattern for that platform
2. Inspect HTML structure with browser devtools (look for job link patterns)
3. Create a new `scrape_X(slug)` function following the same pattern as existing ones
4. Add the new ats_type to the if/elif chain in `main()`

### Change output format
- Terminal output: edit `print_terminal()`
- HTML report: edit `save_html()` — it's a single f-string template
- To add JSON output: serialize `all_matched` list directly

## Dependencies
```
requests
anthropic
python-dotenv
beautifulsoup4
lxml
```

## Do NOT use Playwright
The current ATSes (Ashby, Lever, Greenhouse) all render server-side HTML.
BeautifulSoup + requests is sufficient and much faster.
Only switch to Playwright if a specific scraper starts returning empty results 
AND you've confirmed the site requires JavaScript rendering.

## Scheduling (macOS cron)
```
0 8 * * 1-5 cd /path/to/project && python job_hunter.py --output html >> job_hunt.log 2>&1
```
