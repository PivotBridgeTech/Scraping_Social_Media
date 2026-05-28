"""
enricher.py — Instagram profile enrichment pipeline

Layer 1 (no login): scrape profile page → extract email/phone/location from bio text
Layer 2 (login):    instagrapi mobile API → public_email, public_phone,
                    business_address (may include lat/lng directly from Instagram)
Layer 2b:           follow link-in-bio URL (Linktree, Beacons, website…) → scrape email
Layer 3:            Google Maps Geocoding → lat/lng from location text
"""

import re
import time
import random
import requests
from pathlib import Path
from urllib.parse import urlparse, urljoin

# ---------------------------------------------------------------------------
# Regex helpers
# ---------------------------------------------------------------------------

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")

# Patterns that look like location in a bio
_LOC_PIN   = r"(?:📍|🌍|🌎|🌏)"
_LOC_WORDS = r"(?:based in|located in|living in|from|location[:\s])"
_LOC_NAME  = r"([A-Z][a-zA-Z\s]{2,}(?:,\s*[A-Z][a-zA-Z\s]+)?)"
LOCATION_RE = re.compile(
    rf"(?:{_LOC_PIN}|{_LOC_WORDS})\s*{_LOC_NAME}", re.IGNORECASE
)
# Also match "City, Country" standalone (e.g. "London, UK 🇬🇧")
CITY_RE = re.compile(r"\b([A-Z][a-zA-Z]+(?:\s[A-Z][a-zA-Z]+)?,\s*[A-Z][a-zA-Z]+)\b")

_SKIP_WORDS = {
    "dm", "me", "us", "link", "bio", "here", "contact", "email",
    "my", "the", "and", "for", "follow", "check", "out",
}


def _extract_email(text: str) -> str:
    m = EMAIL_RE.search(text or "")
    return m.group(0).lower() if m else ""


def _extract_phone(text: str) -> str:
    """Extract and E.164-format a phone number from text using phonenumbers lib."""
    try:
        import phonenumbers

        # Try international format first (works best for numbers with + prefix)
        for match in phonenumbers.PhoneNumberMatcher(text or "", None):
            return phonenumbers.format_number(
                match.number, phonenumbers.PhoneNumberFormat.INTERNATIONAL
            )
        # Fallback: try common regions
        for region in ["GB", "US", "AU", "AE", "CA", "NG", "ZA"]:
            for match in phonenumbers.PhoneNumberMatcher(text or "", region):
                return phonenumbers.format_number(
                    match.number, phonenumbers.PhoneNumberFormat.INTERNATIONAL
                )
    except Exception:
        pass
    return ""


_US_STATES = {
    "Alabama","Alaska","Arizona","Arkansas","California","Colorado","Connecticut",
    "Delaware","Florida","Georgia","Hawaii","Idaho","Illinois","Indiana","Iowa",
    "Kansas","Kentucky","Louisiana","Maine","Maryland","Massachusetts","Michigan",
    "Minnesota","Mississippi","Missouri","Montana","Nebraska","Nevada",
    "New Hampshire","New Jersey","New Mexico","New York","North Carolina",
    "North Dakota","Ohio","Oklahoma","Oregon","Pennsylvania","Rhode Island",
    "South Carolina","South Dakota","Tennessee","Texas","Utah","Vermont",
    "Virginia","Washington","West Virginia","Wisconsin","Wyoming",
    # Common abbreviations
    "NYC","LA","DC","SF",
}
_US_STATES_LOWER = {s.lower() for s in _US_STATES}

# Booking/availability phrases common in creator bios
_BOOKING_RE = re.compile(
    r"(?:booking|available|serving|based|located?|covering|visit(?:ing)?)"
    r"\s+(?:in\s+|for\s+)?([A-Z][a-zA-Z\s,]{2,30}?)(?:\s+\d{4}|\s*[·|•&,]|$)",
    re.IGNORECASE,
)


def _extract_location_from_bio(bio: str) -> str:
    """Best-effort: pull a city/country/state string from bio text."""
    if not bio:
        return ""

    # 1. Explicit pin emoji / "based in…" patterns — highest confidence
    m = LOCATION_RE.search(bio)
    if m:
        candidate = m.group(1).strip().rstrip(".,!;")
        words = candidate.lower().split()
        if words and words[0] not in _SKIP_WORDS and len(candidate) > 2:
            return candidate

    # 2. Booking / availability phrase: "Booking Michigan 2026", "Available in London"
    m = _BOOKING_RE.search(bio)
    if m:
        candidate = m.group(1).strip().rstrip(".,!; ")
        if len(candidate) > 2 and candidate.lower().split()[0] not in _SKIP_WORDS:
            return candidate

    # 3. Known US state name anywhere in the bio
    for state in _US_STATES:
        # whole-word match
        if re.search(rf"\b{re.escape(state)}\b", bio, re.IGNORECASE):
            return state

    # 4. "City, Country/State" standalone — only if comma-separated, ≤2 words each part
    m = CITY_RE.search(bio)
    if m:
        candidate = m.group(1).strip()
        parts = [p.strip() for p in candidate.split(",")]
        if (len(parts) == 2
                and all(1 <= len(p.split()) <= 2 for p in parts)
                and parts[0].lower() not in _SKIP_WORDS):
            return candidate

    return ""


# ---------------------------------------------------------------------------
# Link-in-bio follower — fetch external URL and scrape email/phone from it
# ---------------------------------------------------------------------------

# URL regex for extracting links from bio text (fallback when instagrapi external_url is empty)
URL_RE = re.compile(
    r"https?://[^\s\"'<>]+|(?:linktr\.ee|beacons\.ai|stan\.store|msha\.ke|bio\.site"
    r"|lnk\.bio|linkin\.bio|allmylinks\.com|taplink\.cc|campsite\.bio)[/\w.\-?=%&]*",
    re.IGNORECASE,
)

_LINK_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Domains to skip (social platforms, trackers — unlikely to have contact email)
_SKIP_DOMAINS = {
    "instagram.com", "facebook.com", "twitter.com", "x.com",
    "tiktok.com", "youtube.com", "spotify.com", "apple.com",
    "amazon.com", "google.com", "bit.ly", "t.co",
}


def _follow_link_in_bio(external_url: str, bio: str = "") -> dict:
    """
    Fetch the link-in-bio page and extract email and phone.
    Tries external_url first, then falls back to any URL found in the bio text.
    Returns {"email": str, "phone": str}.
    """
    result = {"email": "", "phone": ""}

    # Build candidate URL list
    candidates = []
    if external_url and external_url.startswith("http"):
        candidates.append(external_url)
    # Also check bio text for URLs (some accounts have URL directly in bio)
    for m in URL_RE.finditer(bio or ""):
        u = m.group(0)
        if not u.startswith("http"):
            u = "https://" + u
        if u not in candidates:
            candidates.append(u)

    # Email patterns to skip (boilerplate, tracking, asset filenames)
    _EMAIL_SKIP = (
        "noreply", "no-reply", "donotreply", "example.com", "sentry.io",
        "placeholder", "youremail", "@2x", "wixpress", "squarespace",
        "wordpress", "shopify", "cdn.", "static.", "assets.",
    )

    MAILTO_RE = re.compile(
        r'mailto:([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})', re.IGNORECASE
    )
    NEXT_DATA_RE = re.compile(r'id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL)

    for url in candidates[:3]:  # max 3 links per profile
        try:
            domain = urlparse(url).netloc.lower().replace("www.", "")
            if any(skip in domain for skip in _SKIP_DOMAINS):
                continue

            resp = requests.get(
                url, headers=_LINK_HEADERS, timeout=10, allow_redirects=True
            )
            if resp.status_code != 200:
                continue

            text = resp.text

            # ── A. Parse Linktree / Next.js __NEXT_DATA__ JSON ────────────────
            # Link-in-bio platforms (Linktree, Beacons…) embed all their data
            # here before JS hydration — much more reliable than HTML scanning.
            next_emails = []
            nd_match = NEXT_DATA_RE.search(text)
            if nd_match:
                try:
                    import json as _json
                    nd = _json.loads(nd_match.group(1))
                    nd_str = _json.dumps(nd)
                    # Pull every email-looking string out of the full JSON dump
                    next_emails = [e.lower() for e in EMAIL_RE.findall(nd_str)]
                    # Also check for mailto: links embedded in JSON
                    next_emails += [m.group(1).lower() for m in MAILTO_RE.finditer(nd_str)]
                except Exception:
                    pass

            # ── B. mailto: links in raw HTML ──────────────────────────────────
            mailto_emails = [m.group(1).lower() for m in MAILTO_RE.finditer(text)]

            # ── C. General email scan across full page text ───────────────────
            all_emails = [e.lower() for e in EMAIL_RE.findall(text)]

            # Merge in confidence order: JSON data > mailto > general
            seen = set()
            combined = []
            for e in next_emails + mailto_emails + all_emails:
                if e not in seen:
                    seen.add(e)
                    combined.append(e)

            # Filter noise
            clean = [
                e for e in combined
                if not any(skip in e for skip in _EMAIL_SKIP)
            ]

            if clean:
                priority = set(next_emails + mailto_emails)
                clean.sort(key=lambda e: (0 if e in priority else 1, len(e), e))
                result["email"] = clean[0]

            # ── D. Phone ──────────────────────────────────────────────────────
            if not result["phone"]:
                result["phone"] = _extract_phone(text)

            if result["email"]:
                break  # found what we need

        except Exception:
            continue

    return result


# ---------------------------------------------------------------------------
# Layer 1 — public page scrape (no login)
# ---------------------------------------------------------------------------

_SCRAPE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


def _scrape_profile_page(username: str) -> dict:
    """
    Fetch the public profile page and extract what we can without login.
    Instagram's og:description contains: "N Followers, N Following, N Posts - <bio>"
    """
    result = {"bio": "", "email": "", "phone": "", "location_text": ""}
    try:
        url  = f"https://www.instagram.com/{username}/"
        resp = requests.get(url, headers=_SCRAPE_HEADERS, timeout=12)
        if resp.status_code != 200:
            return result

        # og:description holds the bio summary
        og = re.search(
            r'<meta\s+property="og:description"\s+content="([^"]*)"', resp.text
        )
        if og:
            content = og.group(1)
            # Bio comes after the last " - " separator in the description string
            parts = content.split(" - ", 1)
            bio = parts[1] if len(parts) > 1 else content
            result["bio"]          = bio.strip()
            result["email"]        = _extract_email(bio)
            result["phone"]        = _extract_phone(bio)
            result["location_text"] = _extract_location_from_bio(bio)
    except Exception:
        pass
    return result


# ---------------------------------------------------------------------------
# Layer 2 — instagrapi (login-based)
# ---------------------------------------------------------------------------

_IG_CLIENT   = None
_SESSION_FILE = Path("/tmp/ig_scraper_session.json")


def get_ig_client(username: str, password: str, force_relogin: bool = False):
    """
    Return a cached instagrapi Client.  Logs in once per process; re-uses the
    saved session file on subsequent runs to avoid repeated logins.
    Pass force_relogin=True to clear the cache and re-authenticate.
    Returns None on any auth failure.
    """
    global _IG_CLIENT
    if force_relogin:
        _IG_CLIENT = None
        _SESSION_FILE.unlink(missing_ok=True)

    if _IG_CLIENT is not None:
        return _IG_CLIENT

    try:
        from instagrapi import Client

        cl = Client()
        cl.delay_range = [0.5, 1.5]  # reduced delay — still safe from rate limiting

        # Try session file first (avoids repeated logins)
        if _SESSION_FILE.exists():
            try:
                cl.load_settings(_SESSION_FILE)
                cl.login(username, password)
                _IG_CLIENT = cl
                return cl
            except Exception:
                _SESSION_FILE.unlink(missing_ok=True)

        # Fresh login
        cl.login(username, password)
        cl.dump_settings(_SESSION_FILE)
        _IG_CLIENT = cl
        return cl

    except Exception:
        return None


def _enrich_via_instagrapi(username: str, cl) -> dict:
    """
    Fetch rich profile data via the Instagram mobile API.
    Returns a dict with all available fields (empty strings / None for missing).
    """
    out = {
        "full_name": "", "bio": "", "email": "", "phone": "",
        "location_text": "", "lat": None, "lng": None,
        "category": "", "is_business": False,
        "follower_count": None, "following_count": None,
        "media_count": None, "is_verified": False,
        "data_sources": [],
    }
    try:
        from instagrapi.exceptions import UserNotFound, PleaseWaitFewMinutes

        try:
            user_id = cl.user_id_from_username(username)
            info    = cl.user_info(user_id)
        except UserNotFound:
            return out
        except PleaseWaitFewMinutes:
            time.sleep(random.uniform(30, 60))
            return out

        out["full_name"]       = info.full_name or ""
        out["bio"]             = info.biography or ""
        out["email"]           = (info.public_email or "").strip().lower()
        out["follower_count"]  = getattr(info, "follower_count",  None)
        out["following_count"] = getattr(info, "following_count", None)
        out["media_count"]     = getattr(info, "media_count",     None)
        out["is_verified"]     = bool(getattr(info, "is_verified", False))
        out["is_business"] = bool(
            getattr(info, "is_business_account", False)
            or getattr(info, "is_professional_account", False)
        )
        out["category"] = getattr(info, "category_name", "") or ""

        # Phone — normalise with phonenumbers if possible
        raw_phone = str(
            getattr(info, "public_phone_number", "")
            or getattr(info, "contact_phone_number", "")
            or ""
        ).strip()
        if raw_phone:
            try:
                import phonenumbers
                cc = str(getattr(info, "public_phone_country_code", "") or "")
                parsed = phonenumbers.parse(
                    f"+{cc}{raw_phone}" if cc else raw_phone, None
                )
                raw_phone = phonenumbers.format_number(
                    parsed, phonenumbers.PhoneNumberFormat.INTERNATIONAL
                )
            except Exception:
                pass
        out["phone"] = raw_phone

        # Business address — may already contain lat/lng from Instagram
        addr = getattr(info, "business_address_json", None)
        if addr:
            # instagrapi may return a Pydantic model or a plain dict
            if hasattr(addr, "dict"):
                addr = addr.dict()
            if isinstance(addr, dict):
                city = addr.get("city_name") or addr.get("city", "")
                out["location_text"] = city
                lat = addr.get("latitude")
                lng = addr.get("longitude")
                if lat and lng:
                    out["lat"] = float(lat)
                    out["lng"] = float(lng)

        # Fall back to location_name field
        if not out["location_text"]:
            out["location_text"] = getattr(info, "location_name", "") or ""

        # Derive email/phone/location from bio text if not already set
        if not out["email"]:
            out["email"] = _extract_email(out["bio"])
        if not out["phone"]:
            out["phone"] = _extract_phone(out["bio"])
        if not out["location_text"]:
            out["location_text"] = _extract_location_from_bio(out["bio"])

        # ── Layer 2b: follow link-in-bio if still no email ───────────────────
        external_url = str(getattr(info, "external_url", "") or "")
        if not out["email"] or not out["phone"]:
            link_data = _follow_link_in_bio(external_url, out["bio"])
            if link_data["email"] and not out["email"]:
                out["email"] = link_data["email"]
                out["data_sources"] = out.get("data_sources", []) + ["link_in_bio"]
            if link_data["phone"] and not out["phone"]:
                out["phone"] = link_data["phone"]

    except Exception:
        pass
    return out


# ---------------------------------------------------------------------------
# Layer 2b — Apify Instagram Profile Scraper (batch)
# ---------------------------------------------------------------------------

def enrich_profiles_via_apify(
    urls: list,
    api_token: str,
    maps_client=None,
) -> list:
    """
    Enrich a batch of Instagram URLs using the Apify Instagram Profile Scraper.
    Sends ALL profiles in ONE actor run (much faster than one-by-one).

    Returns a list of dicts in the same schema as enrich_profile().
    """
    if not urls or not api_token:
        return []

    from apify_client import ApifyClient
    from urllib.parse import urlparse

    usernames = [urlparse(u).path.strip("/") for u in urls]

    # Build a url→username lookup for result mapping
    url_map = {u: urlparse(u).path.strip("/") for u in urls}

    try:
        client = ApifyClient(api_token)
        run    = client.actor("apify/instagram-profile-scraper").call(
            run_input={"usernames": usernames},
            timeout_secs=300,   # 5 min max for large batches
        )
        raw_items = list(client.dataset(run["defaultDatasetId"]).iterate_items())
    except Exception as e:
        print(f"[Apify] Error: {e}")
        return []

    # Index by username for quick lookup
    apify_map = {item.get("username", "").lower(): item for item in raw_items}

    results = []
    for url in urls:
        username = urlparse(url).path.strip("/").lower()
        item     = apify_map.get(username, {})

        bio           = item.get("biography", "") or ""
        location_text = _extract_location_from_bio(bio)
        email         = _extract_email(bio)
        phone         = _extract_phone(bio)

        # Try link-in-bio for email/phone if not found in bio
        external_url = item.get("externalUrl", "") or ""
        if (not email or not phone) and external_url:
            link_data = _follow_link_in_bio(external_url, bio)
            if link_data["email"] and not email:
                email = link_data["email"]
            if link_data["phone"] and not phone:
                phone = link_data["phone"]

        # Geocode location text → lat/lng/city_state
        lat = lng = None
        formatted_address = city_state = ""
        if location_text and maps_client:
            lat, lng, formatted_address, city_state = _geocode(location_text, maps_client)
            if lat is not None and not _city_state_has_city(city_state):
                formatted_address, city_state = _reverse_geocode(lat, lng, maps_client)

        data_sources = ["apify"]
        if email or phone:
            data_sources.append("bio_parse")
        if lat is not None:
            data_sources.append("geocoded")

        results.append({
            "url":               url,
            "username":          urlparse(url).path.strip("/"),
            "full_name":         item.get("fullName", "") or "",
            "bio":               bio,
            "email":             email,
            "phone":             phone,
            "location_text":     location_text,
            "formatted_address": formatted_address,
            "lat":               lat,
            "lng":               lng,
            "city_state":        city_state,
            "category":          item.get("businessCategoryName", "") or "",
            "is_business":       bool(item.get("isBusinessAccount", False)),
            "is_verified":       bool(item.get("verified", False)),
            "follower_count":    item.get("followersCount"),
            "following_count":   item.get("followsCount"),
            "media_count":       item.get("postsCount"),
            "data_sources":      data_sources,
        })

    return results


def _city_state_has_city(cs: str) -> bool:
    """Return True only if cs contains a real city name."""
    if not cs:
        return False
    city_part = cs.split(",")[0].strip()
    if len(city_part) <= 2 and city_part.isupper():
        return False
    if city_part in _US_STATES:
        return False
    return True


# ---------------------------------------------------------------------------
# Layer 3 — Google Maps Geocoding
# ---------------------------------------------------------------------------

_MAPS_CLIENT = None


def get_maps_client(api_key: str):
    """Return a cached googlemaps.Client."""
    global _MAPS_CLIENT
    if _MAPS_CLIENT is not None:
        return _MAPS_CLIENT
    if not api_key:
        return None
    try:
        import googlemaps
        _MAPS_CLIENT = googlemaps.Client(key=api_key)
        return _MAPS_CLIENT
    except Exception:
        return None


def _parse_city_state(components: list) -> str:
    """
    Extract a clean 'City, State' (or 'City, Country') string from
    Google Maps address_components.
    Priority: locality > sublocality > county (admin_level_2) > state-only fallback.
    """
    city    = ""
    state   = ""
    country = ""
    county  = ""
    for c in components:
        types = c.get("types", [])
        if "locality" in types or "postal_town" in types:
            city = c["long_name"]
        elif "sublocality_level_1" in types and not city:
            city = c["long_name"]
        elif "administrative_area_level_2" in types and not city:
            # e.g. "Osceola County" → strip suffix for display
            county = (
                c["long_name"]
                .replace(" County", "")
                .replace(" Parish", "")
                .replace(" Borough", "")
                .strip()
            )
        elif "administrative_area_level_1" in types:
            state = c["short_name"]   # e.g. "MI", "NY", "TX"
        elif "country" in types:
            country = c["short_name"]  # e.g. "US", "GB"

    # Prefer a real city; fall back to county when no city found
    best_city = city or county

    if best_city and state:
        return f"{best_city}, {state}"
    if best_city and country:
        return f"{best_city}, {country}"
    if state and country == "US":
        return f"{state}, US"
    return ""


def _geocode(location_text: str, maps_client) -> tuple:
    """
    Forward-geocode a location string → (lat, lng, formatted_address, city_state).
    """
    if not location_text or not maps_client:
        return None, None, "", ""
    try:
        results = maps_client.geocode(location_text)
        if results:
            loc        = results[0]["geometry"]["location"]
            addr       = results[0].get("formatted_address", "")
            city_state = _parse_city_state(results[0].get("address_components", []))
            return loc["lat"], loc["lng"], addr, city_state
    except Exception:
        pass
    return None, None, "", ""


def _reverse_geocode(lat: float, lng: float, maps_client) -> tuple:
    """
    Reverse-geocode lat/lng → (formatted_address, city_state).
    Scans all returned results to find the one with the best city-level precision.
    Used when Instagram provides coordinates directly (business accounts).
    """
    if lat is None or lng is None or not maps_client:
        return "", ""
    try:
        results = maps_client.reverse_geocode((lat, lng))
        if not results:
            return "", ""

        addr = results[0].get("formatted_address", "")

        # Scan all results — prefer a result that resolves to an actual city
        # (Google returns most-specific → least-specific; sometimes the first
        # result is a street/postal level that lacks a locality).
        best_city_state = ""
        best_has_city   = False

        for r in results:
            comps = r.get("address_components", [])
            cs    = _parse_city_state(comps)
            if not cs:
                continue
            has_city = any(
                "locality" in c.get("types", []) or "postal_town" in c.get("types", [])
                for c in comps
            )
            if has_city and not best_has_city:
                # Upgrade from county/state-only to a real city
                best_city_state = cs
                best_has_city   = True
            elif not best_city_state:
                # Take the first result with any city_state (county level is fine too)
                best_city_state = cs

            if best_has_city:
                break  # Can't do better than an actual locality

        return addr, best_city_state
    except Exception:
        pass
    return "", ""


# ---------------------------------------------------------------------------
# Main enrichment function
# ---------------------------------------------------------------------------

def enrich_profile(
    url: str,
    ig_client=None,
    maps_client=None,
) -> dict:
    """
    Enrich one Instagram profile URL with contact and location data.

    Returns:
        {
          url, username, full_name, bio,
          email, phone,
          location_text, formatted_address, lat, lng,
          category, is_business,
          data_sources: list[str]
        }
    """
    username = urlparse(url).path.strip("/")

    out = {
        "url":               url,
        "username":          username,
        "full_name":         "",
        "bio":               "",
        "email":             "",
        "phone":             "",
        "location_text":     "",
        "formatted_address": "",
        "lat":               None,
        "lng":               None,
        "category":          "",
        "is_business":       False,
        "follower_count":    None,
        "following_count":   None,
        "media_count":       None,
        "is_verified":       False,
        "city_state":        "",
        "data_sources":      [],
    }

    # ── Layer 2: instagrapi (richer, preferred) ───────────────────────────────
    if ig_client:
        ig = _enrich_via_instagrapi(username, ig_client)
        for key in ("full_name", "bio", "email", "phone",
                    "location_text", "lat", "lng", "category", "is_business",
                    "follower_count", "following_count", "media_count", "is_verified",
                    "city_state"):
            val = ig.get(key)
            if val is not None and val != "" and val is not False:
                out[key] = val
        # Merge any sub-sources (e.g. link_in_bio) added inside _enrich_via_instagrapi
        for src in ig.get("data_sources", []):
            if src not in out["data_sources"]:
                out["data_sources"].append(src)
        if any(ig.get(k) for k in ("bio", "email", "phone", "location_text", "follower_count")):
            if "instagram_api" not in out["data_sources"]:
                out["data_sources"].append("instagram_api")

    # ── Layer 1: page scrape — fill gaps if instagrapi missed anything ────────
    if not out["bio"] or not out["email"]:
        scraped = _scrape_profile_page(username)
        if scraped["bio"] and not out["bio"]:
            out["bio"] = scraped["bio"]
            out["data_sources"].append("web_scrape")
        if scraped["email"]        and not out["email"]:
            out["email"] = scraped["email"]
        if scraped["phone"]        and not out["phone"]:
            out["phone"] = scraped["phone"]
        if scraped["location_text"] and not out["location_text"]:
            out["location_text"] = scraped["location_text"]

    # ── Layer 3a: forward-geocode location text → lat/lng + city_state ────────
    if out["location_text"] and out["lat"] is None and maps_client:
        lat, lng, formatted, city_state = _geocode(out["location_text"], maps_client)
        out["lat"]               = lat
        out["lng"]               = lng
        out["formatted_address"] = formatted
        out["city_state"]        = city_state
        if lat is not None:
            out["data_sources"].append("geocoded")

    # ── Layer 3b: reverse-geocode existing lat/lng → city_state ───────────────
    # Runs when:
    #   a) Instagram business_address_json provided coordinates but no city_state yet, OR
    #   b) Forward-geocode gave only a state/region (no actual city name) — e.g.
    #      geocoding "Michigan" yields city_state="MI, US"; reverse-geocoding the
    #      resulting lat/lng gives us the nearest real town ("Boon, MI").
    if out["lat"] is not None and not _city_state_has_city(out["city_state"]) and maps_client:
        formatted, city_state = _reverse_geocode(out["lat"], out["lng"], maps_client)
        if city_state:
            out["city_state"] = city_state
        if formatted and not out["formatted_address"]:
            out["formatted_address"] = formatted
        if city_state and "reverse_geocoded" not in out["data_sources"]:
            out["data_sources"].append("reverse_geocoded")

    return out
