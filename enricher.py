"""
enricher.py — Instagram profile enrichment pipeline

Layer 1 (no login): scrape profile page → extract email/phone/location from bio text
Layer 2 (login):    instagrapi mobile API → public_email, public_phone,
                    business_address (may include lat/lng directly from Instagram)
Layer 3:            Google Maps Geocoding → lat/lng from location text
"""

import re
import time
import random
import requests
from pathlib import Path
from urllib.parse import urlparse

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


def _extract_location_from_bio(bio: str) -> str:
    """Best-effort: pull a city/country string from bio text."""
    if not bio:
        return ""
    # Try pin emoji / "based in …" patterns first
    m = LOCATION_RE.search(bio)
    if m:
        candidate = m.group(1).strip().rstrip(".,!;")
        if candidate.lower().split()[0] not in _SKIP_WORDS and len(candidate) > 2:
            return candidate
    # Try "City, Country" standalone
    m = CITY_RE.search(bio)
    if m:
        candidate = m.group(1).strip()
        if candidate.lower().split()[0] not in _SKIP_WORDS:
            return candidate
    return ""


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


def get_ig_client(username: str, password: str):
    """
    Return a cached instagrapi Client.  Logs in once per process; re-uses the
    saved session file on subsequent runs to avoid repeated logins.
    Returns None on any auth failure.
    """
    global _IG_CLIENT
    if _IG_CLIENT is not None:
        return _IG_CLIENT

    try:
        from instagrapi import Client
        from instagrapi.exceptions import (
            LoginRequired, TwoFactorRequired, ChallengeRequired,
        )

        cl = Client()
        cl.delay_range = [2, 4]  # polite delays between requests

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

        out["full_name"]  = info.full_name or ""
        out["bio"]        = info.biography or ""
        out["email"]      = (info.public_email or "").strip().lower()
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

        # Derive email/phone/location from bio if not already set
        if not out["email"]:
            out["email"] = _extract_email(out["bio"])
        if not out["phone"]:
            out["phone"] = _extract_phone(out["bio"])
        if not out["location_text"]:
            out["location_text"] = _extract_location_from_bio(out["bio"])

    except Exception:
        pass
    return out


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


def _geocode(location_text: str, maps_client) -> tuple:
    """
    Convert a location string to (lat, lng, formatted_address).
    Returns (None, None, '') on failure.
    """
    if not location_text or not maps_client:
        return None, None, ""
    try:
        results = maps_client.geocode(location_text)
        if results:
            loc  = results[0]["geometry"]["location"]
            addr = results[0].get("formatted_address", "")
            return loc["lat"], loc["lng"], addr
    except Exception:
        pass
    return None, None, ""


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
        "data_sources":      [],
    }

    # ── Layer 2: instagrapi (richer, preferred) ───────────────────────────────
    if ig_client:
        ig = _enrich_via_instagrapi(username, ig_client)
        for key in ("full_name", "bio", "email", "phone",
                    "location_text", "lat", "lng", "category", "is_business"):
            val = ig.get(key)
            if val is not None and val != "" and val is not False:
                out[key] = val
        if any(ig.get(k) for k in ("bio", "email", "phone", "location_text")):
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

    # ── Layer 3: geocode location text if we still have no coordinates ────────
    if out["location_text"] and out["lat"] is None and maps_client:
        lat, lng, formatted = _geocode(out["location_text"], maps_client)
        out["lat"]               = lat
        out["lng"]               = lng
        out["formatted_address"] = formatted
        if lat is not None:
            out["data_sources"].append("geocoded")

    return out
