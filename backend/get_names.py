import os
import requests

api_key = os.environ.get("THEIRSTACK_API_KEY")
if not api_key:
    print("Error: THEIRSTACK_API_KEY environment variable not set")
    exit(1)

url = "https://api.theirstack.com/v1/companies/search"
headers = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "Authorization": f"Bearer {api_key}"
}

data = {
    "company_technology_slug_or": ["ashby"]
}

resp = requests.post(url, json=data, headers=headers)
resp.raise_for_status()

result = resp.json()
companies = [company["name"] for company in result.get("data", [])]

print(companies)