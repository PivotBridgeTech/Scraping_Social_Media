"""
scraper.py — unified Instagram profile URL finder with AI enhancements.

Supported engines:
  - "ddgs"        : DuckDuckGo (no keys, instant, ~50 results)
  - "google_api"  : Google Custom Search API (needs api_key + cse_id, 100 free/day)
  - "brave_api"   : Brave Search API (needs api_key, 2,000 free/month)

AI features (requires Anthropic API key):
  - Multi-query: Claude generates 6 diverse queries → 3-5x more unique profiles
  - Relevance scoring: Claude scores each profile 0–100 with a one-line reason
"""

import json
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


def dedupe_profiles(profiles: list) -> list:
    """Deduplicate profiles by URL, keeping the first occurrence."""
    seen, out = set(), []
    for p in profiles:
        if p["url"] not in seen:
            seen.add(p["url"])
            out.append(p)
    return out


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


def _make_profile(url: str, snippet: str = "", title: str = "") -> dict:
    username = urlparse(url).path.strip("/")
    return {
        "url": url,
        "username": username,
        "snippet": (snippet or title or "").strip(),
        "score": None,
        "reason": "",
    }


# ---------------------------------------------------------------------------
# Internal search runners (accept pre-built query strings)
# ---------------------------------------------------------------------------

def _ddgs_run(query: str, max_results: int = 50) -> dict:
    from ddgs import DDGS
    try:
        with DDGS() as ddgs:
            raw = list(ddgs.text(query, max_results=max_results))
    except Exception as e:
        return {"profiles": [], "error": str(e)}

    profiles = dedupe_profiles([
        _make_profile(
            clean_url(r["href"]),
            snippet=r.get("body", ""),
            title=r.get("title", ""),
        )
        for r in raw
        if is_profile_url(r.get("href", ""))
    ])
    return {"profiles": profiles, "error": None}


GOOGLE_CSE_URL = "https://www.googleapis.com/customsearch/v1"


def _google_run(
    query: str,
    max_results: int = 50,
    api_key: str = "",
    cse_id: str = "",
) -> dict:
    if not api_key or not cse_id:
        return {"profiles": [], "error": "Google API key and Custom Search Engine ID are required."}

    profiles: list = []
    pages_needed = -(-min(max_results, 100) // 10)

    for page in range(pages_needed):
        params = {
            "key": api_key,
            "cx": cse_id,
            "q": query,
            "start": page * 10 + 1,
            "num": 10,
        }
        try:
            resp = requests.get(GOOGLE_CSE_URL, params=params, timeout=15)
            data = resp.json()
        except Exception as e:
            return {"profiles": dedupe_profiles(profiles), "error": str(e)}

        if "error" in data:
            return {"profiles": dedupe_profiles(profiles), "error": data["error"].get("message", "Google API error")}

        items = data.get("items", [])
        if not items:
            break

        for item in items:
            link = item.get("link", "")
            if is_profile_url(link):
                profiles.append(_make_profile(
                    clean_url(link),
                    snippet=item.get("snippet", ""),
                    title=item.get("title", ""),
                ))

        if len(profiles) >= max_results:
            break

        if page < pages_needed - 1:
            time.sleep(random.uniform(0.5, 1.2))

    return {"profiles": dedupe_profiles(profiles)[:max_results], "error": None}


BRAVE_API_URL = "https://api.search.brave.com/res/v1/web/search"


def _brave_run(query: str, max_results: int = 50, api_key: str = "") -> dict:
    if not api_key:
        return {"profiles": [], "error": "Brave Search API key is required."}

    profiles: list = []
    per_page = 20
    pages_needed = -(-min(max_results, 100) // per_page)
    headers = {
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "X-Subscription-Token": api_key,
    }

    for page in range(pages_needed):
        params = {"q": query, "count": per_page, "offset": page * per_page}
        try:
            resp = requests.get(BRAVE_API_URL, headers=headers, params=params, timeout=15)
            data = resp.json()
        except Exception as e:
            return {"profiles": dedupe_profiles(profiles), "error": str(e)}

        if resp.status_code == 401:
            return {"profiles": dedupe_profiles(profiles), "error": "Invalid Brave API key."}
        if resp.status_code == 429:
            return {"profiles": dedupe_profiles(profiles), "error": "Brave API rate limit reached."}
        if resp.status_code != 200:
            return {"profiles": dedupe_profiles(profiles), "error": f"HTTP {resp.status_code}"}

        results = data.get("web", {}).get("results", [])
        if not results:
            break

        for item in results:
            link = item.get("url", "")
            if is_profile_url(link):
                profiles.append(_make_profile(
                    clean_url(link),
                    snippet=item.get("description", ""),
                    title=item.get("title", ""),
                ))

        if len(profiles) >= max_results:
            break

        if page < pages_needed - 1:
            time.sleep(random.uniform(0.3, 0.8))

    return {"profiles": dedupe_profiles(profiles)[:max_results], "error": None}


# ---------------------------------------------------------------------------
# Public: single-query search (used by the multi-query orchestrator)
# ---------------------------------------------------------------------------

def search_with_query(
    query: str,
    engine: str = "ddgs",
    api_key: str = "",
    cse_id: str = "",
    max_results: int = 50,
) -> dict:
    """Run a single pre-built query through the chosen engine."""
    engine = engine.lower().strip()
    if engine == "google_api":
        result = _google_run(query, max_results, api_key, cse_id)
    elif engine == "brave_api":
        result = _brave_run(query, max_results, api_key)
    else:
        result = _ddgs_run(query, max_results)

    result["query"] = query
    result["engine"] = engine
    return result


# ---------------------------------------------------------------------------
# AI helpers — Claude Haiku (fast, cost-effective for these simple tasks)
# ---------------------------------------------------------------------------

def generate_search_queries(
    niche: str,
    location: str = "",
    count: int = 6,
    anthropic_api_key: str = "",
) -> list[str]:
    """
    Use Claude Haiku to generate diverse Instagram search queries.
    Queries vary across: job titles, industry terms, self-descriptions, specializations.
    Falls back to a single default query on any error.
    """
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=anthropic_api_key or None)
        location_hint = f" in {location}" if location.strip() else ""

        response = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=600,
            messages=[{
                "role": "user",
                "content": (
                    f"Generate {count} diverse Instagram search queries to find "
                    f"{niche} influencers/creators{location_hint} for brand partnerships.\n\n"
                    f"Each query MUST start with: site:instagram.com\n"
                    f"Vary the terms across: professional titles, industry jargon, "
                    f"self-descriptions, niche specializations, and audience-facing language.\n"
                    f"Think about how these creators describe themselves in their bios.\n\n"
                    f"Return ONLY {count} queries, one per line. No numbers, no explanation."
                ),
            }],
        )

        lines = response.content[0].text.strip().splitlines()
        queries = []
        for line in lines:
            q = line.strip().lstrip("0123456789.-) ").strip()
            if q and "instagram.com" in q:
                queries.append(q)

        if queries:
            return queries[:count]

    except Exception:
        pass

    # Fallback to default single query
    return [build_query(niche, location)]


def score_profiles(
    profiles: list[dict],
    niche: str,
    location: str = "",
    anthropic_api_key: str = "",
) -> list[dict]:
    """
    Score profile dicts for relevance to the niche using Claude Haiku.
    Adds 'score' (0–100) and 'reason' (str) to each profile dict.
    Returns profiles sorted best-first.
    On any error, returns profiles unscored (score=None).
    """
    if not profiles:
        return profiles

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=anthropic_api_key or None)
        location_hint = f" in {location}" if location.strip() else ""

        BATCH = 30
        scored_all: list[dict] = []

        for offset in range(0, len(profiles), BATCH):
            batch = profiles[offset: offset + BATCH]

            lines = []
            for j, p in enumerate(batch, 1):
                snippet = (p.get("snippet") or "").strip()[:120]
                snap = f' — "{snippet}"' if snippet else ""
                lines.append(f"{j}. @{p['username']}{snap}")

            prompt = (
                f"Rate each Instagram profile's relevance (0–100) for finding "
                f"{niche} creators/influencers{location_hint}.\n\n"
                + "\n".join(lines)
                + '\n\nReturn ONLY a JSON array:\n'
                + '[{"i":1,"score":85,"reason":"reason under 10 words"}, ...]\n'
                + "No other text."
            )

            response = client.messages.create(
                model="claude-haiku-4-5",
                max_tokens=1024,
                system=(
                    "You are an influencer marketing analyst. "
                    "Score Instagram profiles for niche relevance. Be concise."
                ),
                messages=[{"role": "user", "content": prompt}],
            )

            text = response.content[0].text.strip()
            start = text.find("[")
            end = text.rfind("]") + 1
            score_map: dict = {}
            if start >= 0 and end > start:
                try:
                    score_map = {item["i"]: item for item in json.loads(text[start:end])}
                except Exception:
                    pass

            for j, p in enumerate(batch, 1):
                item = score_map.get(j, {})
                p["score"] = int(item.get("score", 50)) if item else None
                p["reason"] = item.get("reason", "") if item else ""
                scored_all.append(p)

        # Sort by score, best first (None scores go last)
        scored_all.sort(key=lambda x: x.get("score") or 0, reverse=True)
        return scored_all

    except Exception:
        for p in profiles:
            p.setdefault("score", None)
            p.setdefault("reason", "")
        return profiles


# ---------------------------------------------------------------------------
# Legacy entry point (backward compatible)
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
      { urls, profiles, query, error, engine }
    """
    query = build_query(niche, location)
    result = search_with_query(query, engine, api_key, cse_id, max_results)
    result["urls"] = [p["url"] for p in result.get("profiles", [])]
    return result
