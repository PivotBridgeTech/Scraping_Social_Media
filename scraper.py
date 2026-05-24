"""
scraper.py — unified Instagram profile URL finder.

Supported engines:
  - "ddgs"        : DuckDuckGo (no keys, instant, ~50 results)
  - "google_api"  : Google Custom Search API (needs api_key + cse_id, 100 free/day)
  - "playwright"  : Real Chromium browser, scrapes Google directly (no keys needed)
"""

import time
import random
import requests
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

NON_PROFILE_PREFIXES = [
    "p/", "explore/", "reel/", "tv/", "reels/",
    "stories/", "accounts/", "directory/", "hashtag/",
    "locations/", "AR/", "popular/",
]


def is_profile_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    if "instagram.com" not in parsed.netloc:
        return False
    path = parsed.path.strip("/")
    if not path:
        return False
    for prefix in NON_PROFILE_PREFIXES:
        if path.startswith(prefix):
            return False
    parts = [p for p in path.split("/") if p]
    return len(parts) == 1


def clean_url(url: str) -> str:
    parsed = urlparse(url)
    return f"https://www.instagram.com/{parsed.path.strip('/')}/"


def dedupe(urls: list) -> list:
    seen, out = set(), []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def build_query(niche: str, location: str = "") -> str:
    q = f"site:instagram.com {niche.strip()}"
    if location.strip():
        q += f" {location.strip()}"
    return q


# ---------------------------------------------------------------------------
# Engine 1: DuckDuckGo
# ---------------------------------------------------------------------------

def search_ddgs(niche: str, location: str = "", max_results: int = 50) -> dict:
    from ddgs import DDGS
    query = build_query(niche, location)
    try:
        with DDGS() as ddgs:
            raw = list(ddgs.text(query, max_results=max_results))
    except Exception as e:
        return {"urls": [], "query": query, "error": str(e)}

    urls = dedupe([
        clean_url(r["href"])
        for r in raw
        if is_profile_url(r.get("href", ""))
    ])
    return {"urls": urls, "query": query, "error": None}


# ---------------------------------------------------------------------------
# Engine 2: Google Custom Search API
# ---------------------------------------------------------------------------

GOOGLE_CSE_URL = "https://www.googleapis.com/customsearch/v1"


def search_google_api(
    niche: str,
    location: str = "",
    max_results: int = 50,
    api_key: str = "",
    cse_id: str = "",
) -> dict:
    if not api_key or not cse_id:
        return {
            "urls": [],
            "query": "",
            "error": "Google API key and Custom Search Engine ID are required.",
        }

    query = build_query(niche, location)
    all_urls: list = []
    # Google CSE returns max 10 per request; start is 1-based
    pages_needed = -(-min(max_results, 100) // 10)   # ceiling division

    for page in range(pages_needed):
        start = page * 10 + 1
        params = {
            "key": api_key,
            "cx": cse_id,
            "q": query,
            "start": start,
            "num": 10,
        }
        try:
            resp = requests.get(GOOGLE_CSE_URL, params=params, timeout=15)
            data = resp.json()
        except Exception as e:
            return {"urls": dedupe(all_urls), "query": query, "error": str(e)}

        if "error" in data:
            msg = data["error"].get("message", "Google API error")
            return {"urls": dedupe(all_urls), "query": query, "error": msg}

        items = data.get("items", [])
        if not items:
            break  # no more results

        for item in items:
            link = item.get("link", "")
            if is_profile_url(link):
                all_urls.append(clean_url(link))

        if len(all_urls) >= max_results:
            break

        # Small polite delay between API pages
        if page < pages_needed - 1:
            time.sleep(random.uniform(0.5, 1.2))

    return {"urls": dedupe(all_urls)[:max_results], "query": query, "error": None}


# ---------------------------------------------------------------------------
# Engine 3: Brave Search API
# ---------------------------------------------------------------------------
# Free tier: 2,000 queries/month. Get a key at https://api.search.brave.com
# Each request returns up to 20 results; paginate via `offset`.

BRAVE_API_URL = "https://api.search.brave.com/res/v1/web/search"


def search_brave_api(
    niche: str,
    location: str = "",
    max_results: int = 50,
    api_key: str = "",
) -> dict:
    if not api_key:
        return {
            "urls": [],
            "query": "",
            "error": "Brave Search API key is required. Get one free at https://api.search.brave.com",
        }

    query = build_query(niche, location)
    all_urls: list = []
    per_page = 20  # Brave max per request
    pages_needed = -(-min(max_results, 100) // per_page)

    headers = {
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "X-Subscription-Token": api_key,
    }

    for page in range(pages_needed):
        offset = page * per_page
        params = {"q": query, "count": per_page, "offset": offset}

        try:
            resp = requests.get(BRAVE_API_URL, headers=headers, params=params, timeout=15)
            data = resp.json()
        except Exception as e:
            return {"urls": dedupe(all_urls), "query": query, "error": str(e)}

        if resp.status_code == 401:
            return {
                "urls": dedupe(all_urls),
                "query": query,
                "error": "Invalid Brave API key. Check your key at https://api.search.brave.com",
            }
        if resp.status_code == 429:
            return {
                "urls": dedupe(all_urls),
                "query": query,
                "error": "Brave API rate limit reached. You've used your monthly free quota.",
            }
        if resp.status_code != 200:
            msg = data.get("error", {}).get("message", f"HTTP {resp.status_code}")
            return {"urls": dedupe(all_urls), "query": query, "error": msg}

        results = data.get("web", {}).get("results", [])
        if not results:
            break

        for item in results:
            link = item.get("url", "")
            if is_profile_url(link):
                all_urls.append(clean_url(link))

        if len(all_urls) >= max_results:
            break

        if page < pages_needed - 1:
            time.sleep(random.uniform(0.3, 0.8))

    return {"urls": dedupe(all_urls)[:max_results], "query": query, "error": None}


# ---------------------------------------------------------------------------
# Unified entry point
# ---------------------------------------------------------------------------

def search_instagram_profiles(
    niche: str,
    location: str = "",
    max_results: int = 50,
    engine: str = "ddgs",
    api_key: str = "",
    cse_id: str = "",
) -> dict:
    """
    Route to the right search engine and return:
      { urls: list, query: str, error: str|None, engine: str }

    Engines:
      "ddgs"       — DuckDuckGo (no key, unlimited)
      "google_api" — Google Custom Search API (api_key + cse_id, 100 free/day)
      "brave_api"  — Brave Search API (api_key, 2 000 free/month)
    """
    engine = engine.lower().strip()

    if engine == "google_api":
        result = search_google_api(niche, location, max_results, api_key, cse_id)
    elif engine == "brave_api":
        result = search_brave_api(niche, location, max_results, api_key)
    else:
        result = search_ddgs(niche, location, max_results)

    result["engine"] = engine
    return result
