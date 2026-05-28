import json
import time
import csv
import io
import os
import re
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, Response, render_template, request, jsonify
from scraper import (
    build_query,
    generate_search_queries,
    score_profiles,
    search_with_query,
)

load_dotenv()

app = Flask(__name__)

BATCH_SIZE = 5

ENGINE_LABELS = {
    "apify_google": "Apify Google Search",
    "ddgs":         "DuckDuckGo",
}


@app.route("/")
def index():
    anthropic_active = bool(os.getenv("ANTHROPIC_API_KEY", "").strip())
    return render_template("index.html", anthropic_active=anthropic_active)


@app.route("/scrape")
def scrape():
    niche       = request.args.get("niche", "").strip()
    location    = request.args.get("location", "").strip()
    max_results = min(int(request.args.get("max_results", 50)), 200)
    engine        = "ddgs"   # default; auto-upgrades to apify_google when token present
    apify_token   = os.getenv("APIFY_API_TOKEN", "").strip()
    anthropic_key = os.getenv("ANTHROPIC_API_KEY", "").strip()

    if not niche:
        return jsonify({"error": "Niche is required"}), 400

    # Auto-upgrade label when Apify token is available
    effective_engine = "apify_google" if apify_token else engine
    label = ENGINE_LABELS.get(effective_engine, effective_engine)

    def event(data: dict) -> str:
        return f"data: {json.dumps(data)}\n\n"

    def generate():
        # ── Step 1: Generate AI queries (or fall back to single query) ────────
        if anthropic_key:
            yield event({"type": "status", "message": "🤖 Generating smart search queries with AI…"})
            queries = generate_search_queries(
                niche, location, count=6, anthropic_api_key=anthropic_key
            )
            yield event({"type": "ai_queries", "queries": queries})
            yield event({
                "type": "status",
                "message": f"Searching via {label} with {len(queries)} AI queries…",
            })
        else:
            queries = [build_query(niche, location)]
            yield event({"type": "status", "message": f'Searching via {label} for "{niche}"…'})

        yield event({"type": "query", "query": queries[0], "engine": engine})

        # ── Step 2: Run all queries, stream URLs as they arrive ───────────────
        seen_urls: dict = {}   # url → profile dict
        first_error = None

        for q_idx, query in enumerate(queries):
            result = search_with_query(query, engine, max_results=max_results, apify_token=apify_token)

            if result["error"] and not seen_urls:
                first_error = result["error"]

            new_batch = []
            for profile in result.get("profiles", []):
                url = profile["url"]
                if url not in seen_urls:
                    seen_urls[url] = profile
                    new_batch.append(url)

                    # Emit in small batches for smooth UI
                    if len(new_batch) >= BATCH_SIZE:
                        yield event({
                            "type": "urls",
                            "new_urls": new_batch,
                            "total_count": len(seen_urls),
                        })
                        new_batch = []
                        time.sleep(0.02)

            if new_batch:
                yield event({
                    "type": "urls",
                    "new_urls": new_batch,
                    "total_count": len(seen_urls),
                })

            if len(seen_urls) >= max_results:
                break

            # Small delay between queries to be polite
            if q_idx < len(queries) - 1:
                time.sleep(0.1)

        if first_error and not seen_urls:
            yield event({"type": "error", "message": first_error})
            return

        profiles = list(seen_urls.values())[:max_results]

        if not profiles:
            yield event({
                "type": "done",
                "message": "No Instagram profiles found. Try a different niche or location.",
                "total_count": 0,
                "urls": [],
            })
            return

        # ── Step 3: Score profiles with Claude ───────────────────────────────
        if anthropic_key:
            yield event({
                "type": "scoring_start",
                "message": f"🤖 Scoring {len(profiles)} profiles for relevance…",
            })
            scored = score_profiles(profiles, niche, location, anthropic_api_key=anthropic_key)
            yield event({
                "type": "scores",
                "profiles": [
                    {"url": p["url"], "score": p["score"], "reason": p["reason"]}
                    for p in scored
                ],
            })
            final_urls = [p["url"] for p in scored]
        else:
            final_urls = [p["url"] for p in profiles]

        ai_note = " · AI-enhanced" if anthropic_key else ""
        yield event({
            "type": "done",
            "message": f"Found {len(final_urls)} Instagram profiles via {label}{ai_note}.",
            "total_count": len(final_urls),
            "urls": final_urls,
        })

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/enrich", methods=["POST"])
def enrich():
    """
    Stream enrichment results for a list of Instagram URLs.
    Accepts JSON body: { "urls": [...] }
    Streams SSE events: status | enriched | enrich_done | error
    """
    import random as _random
    from enricher import enrich_profile, get_ig_client, get_maps_client

    body      = request.get_json(silent=True) or {}
    urls        = [u for u in body.get("urls", []) if u.strip()]
    ig_user     = os.getenv("IG_USERNAME", "").strip()
    ig_pass     = os.getenv("IG_PASSWORD", "").strip()
    apify_token = os.getenv("APIFY_API_TOKEN", "").strip()
    maps_key    = os.getenv("GOOGLE_MAPS_KEY", "").strip()

    def event(data: dict) -> str:
        return f"data: {json.dumps(data)}\n\n"

    def generate():
        if not urls:
            yield event({"type": "error", "message": "No URLs provided."})
            return

        maps_client = get_maps_client(maps_key) if maps_key else None
        if not maps_client:
            yield event({"type": "status", "message": "⚠️ No Maps API key — location text only, no GPS"})

        enriched_count = 0
        email_count    = 0
        phone_count    = 0
        loc_count      = 0

        # ── Apify batch enrichment (preferred — fast, no IG login needed) ─────
        if apify_token:
            from enricher import enrich_profiles_via_apify
            yield event({"type": "status",
                         "message": f"🚀 Enriching {len(urls)} profiles via Apify (batch)…"})
            try:
                profiles = enrich_profiles_via_apify(urls, apify_token, maps_client)
            except Exception as exc:
                profiles = []
                yield event({"type": "status", "message": f"⚠️ Apify error: {exc} — falling back to direct scrape"})

            # If Apify returned results, stream them
            if profiles:
                for i, profile in enumerate(profiles):
                    enriched_count += 1
                    if profile.get("email"):        email_count += 1
                    if profile.get("phone"):        phone_count += 1
                    if profile.get("location_text"): loc_count  += 1
                    yield event({
                        "type": "enriched", "profile": profile,
                        "progress": i + 1, "total": len(profiles),
                    })
                yield event({
                    "type": "enrich_done", "total": enriched_count,
                    "emails": email_count, "phones": phone_count, "locations": loc_count,
                    "message": (f"Enriched {enriched_count} profiles via Apify · "
                                f"{email_count} emails · {phone_count} phones · {loc_count} locations"),
                })
                return

        # ── Fallback: instagrapi per-profile enrichment ───────────────────────
        ig_client = None
        if ig_user and ig_pass:
            yield event({"type": "status", "message": "🔐 Logging in to Instagram…"})
            ig_client = get_ig_client(ig_user, ig_pass)
            if ig_client:
                yield event({"type": "status", "message": "✅ Instagram connected — fetching contact data"})
            else:
                yield event({"type": "status", "message": "⚠️ Instagram login failed — using bio scrape only"})
        else:
            yield event({"type": "status", "message": "ℹ️ No IG credentials — using bio scrape only"})

        yield event({"type": "status", "message": f"🔬 Enriching {len(urls)} profiles…"})

        for i, url in enumerate(urls):
            try:
                profile = enrich_profile(url, ig_client=ig_client, maps_client=maps_client)
            except Exception as exc:
                profile = {"url": url,
                           "username": url.split("instagram.com/")[-1].strip("/"),
                           "error": str(exc)}

            enriched_count += 1
            if profile.get("email"):        email_count += 1
            if profile.get("phone"):        phone_count += 1
            if profile.get("location_text"): loc_count  += 1

            yield event({"type": "enriched", "profile": profile,
                         "progress": i + 1, "total": len(urls)})
            time.sleep(_random.uniform(0.5, 1.0) if ig_client else _random.uniform(0.1, 0.3))

        yield event({
            "type": "enrich_done", "total": enriched_count,
            "emails": email_count, "phones": phone_count, "locations": loc_count,
            "message": (f"Enriched {enriched_count} profiles · "
                        f"{email_count} emails · {phone_count} phones · {loc_count} locations"),
        })

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/send_dms", methods=["POST"])
def send_dms():
    """
    Stream DM send results one profile at a time.
    Body: { urls, message, do_follow, delay_min, delay_max }
    Streams SSE: dm_status | dm_result | dm_done | error
    """
    import random as _random
    from enricher import get_ig_client
    from dm_sender import follow_and_dm, personalise

    body      = request.get_json(silent=True) or {}
    urls      = [u for u in body.get("urls", []) if u.strip()]
    template  = body.get("message", "").strip()
    do_follow = body.get("do_follow", True)
    delay_min = float(body.get("delay_min", 45))
    delay_max = float(body.get("delay_max", 90))

    ig_user = os.getenv("IG_USERNAME", "").strip()
    ig_pass = os.getenv("IG_PASSWORD", "").strip()

    def event(data: dict) -> str:
        return f"data: {json.dumps(data)}\n\n"

    def generate():
        if not urls:
            yield event({"type": "error", "message": "No recipients selected."})
            return
        if not template:
            yield event({"type": "error", "message": "Message cannot be empty."})
            return
        if not ig_user or not ig_pass:
            yield event({"type": "error", "message": "IG_USERNAME / IG_PASSWORD not set in .env"})
            return

        yield event({"type": "dm_status", "message": "🔐 Connecting to Instagram…"})
        cl = get_ig_client(ig_user, ig_pass)
        if not cl:
            yield event({"type": "error", "message": "Instagram login failed. Check your credentials."})
            return

        yield event({
            "type": "dm_status",
            "message": f"✅ Connected · sending {len(urls)} DM{'s' if len(urls) != 1 else ''}…",
        })

        sent_count  = 0
        fail_count  = 0

        for i, url in enumerate(urls):
            from urllib.parse import urlparse
            username = urlparse(url).path.strip("/")
            msg      = personalise(template, username)

            yield event({
                "type":     "dm_status",
                "message":  f"Sending to @{username} ({i+1}/{len(urls)})…",
                "progress": i,
                "total":    len(urls),
            })

            result = follow_and_dm(cl, username, msg, do_follow=do_follow)

            # Auto-relogin if session expired and retry once
            if not result["sent"] and result.get("error") and "session expired" in (result["error"] or "").lower():
                yield event({"type": "dm_status", "message": "🔄 Session expired — reconnecting to Instagram…"})
                cl = get_ig_client(ig_user, ig_pass, force_relogin=True)
                if cl:
                    yield event({"type": "dm_status", "message": f"✅ Reconnected — retrying @{username}…"})
                    result = follow_and_dm(cl, username, msg, do_follow=do_follow)
                else:
                    yield event({"type": "error", "message": "Instagram reconnect failed. Check your credentials."})
                    return

            if result["sent"]:
                sent_count += 1
            else:
                fail_count += 1

            yield event({
                "type":      "dm_result",
                "username":  username,
                "url":       url,
                "sent":      result["sent"],
                "followed":  result["followed"],
                "error":     result["error"],
                "progress":  i + 1,
                "total":     len(urls),
                "sent_count": sent_count,
                "fail_count": fail_count,
            })

            # Hard stop if Instagram is clearly blocking us
            if result["error"] and (
                "flagged" in (result["error"] or "").lower()
                or "rate limited" in (result["error"] or "").lower()
            ):
                yield event({
                    "type":    "error",
                    "message": f"⚠️ {result['error']} — stopping to protect your account.",
                })
                break

            # Human-like delay between sends (skip after last)
            if i < len(urls) - 1:
                delay = _random.uniform(delay_min, delay_max)
                yield event({
                    "type":    "dm_status",
                    "message": f"Waiting {delay:.0f}s before next DM…",
                    "progress": i + 1,
                    "total":   len(urls),
                })
                time.sleep(delay)

        yield event({
            "type":       "dm_done",
            "sent_count": sent_count,
            "fail_count": fail_count,
            "message":    f"Done · {sent_count} sent · {fail_count} failed",
        })

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/download", methods=["POST"])
def download():
    data     = request.get_json(silent=True) or {}
    urls     = data.get("urls", [])
    niche    = data.get("niche", "instagram_profiles")
    enriched = data.get("enriched", {})   # url → enrichment dict
    scores   = data.get("scores",   {})   # url → {score, reason}

    has_enrich = bool(enriched)
    has_scores = bool(scores)

    output = io.StringIO()
    writer = csv.writer(output)

    # Build header row dynamically
    headers = ["instagram_url", "username"]
    if has_enrich:
        headers += [
            "full_name", "followers", "posts",
            "email", "phone",
            "city_state", "location_raw", "formatted_address", "lat", "lng",
            "category", "is_business", "is_verified",
        ]
    if has_scores:
        headers += ["score", "score_reason"]
    writer.writerow(headers)

    for url in urls:
        username = url.replace("https://www.instagram.com/", "").strip("/")
        row = [url, username]
        if has_enrich:
            e = enriched.get(url, {})
            row += [
                e.get("full_name",         ""),
                e.get("follower_count",    ""),
                e.get("media_count",       ""),
                e.get("email",             ""),
                e.get("phone",             ""),
                e.get("city_state",        ""),
                e.get("location_text",     ""),
                e.get("formatted_address", ""),
                e.get("lat",               ""),
                e.get("lng",               ""),
                e.get("category",          ""),
                "yes" if e.get("is_business") else "",
                "yes" if e.get("is_verified") else "",
            ]
        if has_scores:
            s = scores.get(url, {})
            row += [s.get("score", ""), s.get("reason", "")]
        writer.writerow(row)

    filename = f"{niche.replace(' ', '_')}_profiles.csv"
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


# ---------------------------------------------------------------------------
# Excel helper
# ---------------------------------------------------------------------------

def _save_city_excel(profiles: list, city: str, run_dir: Path) -> Path:
    """Save enriched profiles for one city to an .xlsx file. Returns the path."""
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment

    safe = re.sub(r'[^\w\s-]', '', city).strip().replace(' ', '_')
    path = run_dir / f"{safe}.xlsx"

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = city[:31]

    headers = [
        "Instagram URL", "Username", "Full Name", "Followers", "Posts",
        "Email", "Phone", "City/State", "Location Raw",
        "Category", "Business", "Verified",
    ]
    header_fill = PatternFill("solid", fgColor="1e1b4b")
    header_font = Font(bold=True, color="FFFFFF")

    for col, h in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col, value=h)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center")

    for row, p in enumerate(profiles, 2):
        ws.cell(row=row, column=1,  value=p.get("url", ""))
        ws.cell(row=row, column=2,  value=p.get("username", ""))
        ws.cell(row=row, column=3,  value=p.get("full_name", ""))
        ws.cell(row=row, column=4,  value=p.get("follower_count", ""))
        ws.cell(row=row, column=5,  value=p.get("media_count", ""))
        ws.cell(row=row, column=6,  value=p.get("email", ""))
        ws.cell(row=row, column=7,  value=p.get("phone", ""))
        ws.cell(row=row, column=8,  value=p.get("city_state", ""))
        ws.cell(row=row, column=9,  value=p.get("location_text", ""))
        ws.cell(row=row, column=10, value=p.get("category", ""))
        ws.cell(row=row, column=11, value="Yes" if p.get("is_business") else "")
        ws.cell(row=row, column=12, value="Yes" if p.get("is_verified") else "")

    # Auto-width columns
    for col in ws.columns:
        max_len = max((len(str(c.value or "")) for c in col), default=10)
        ws.column_dimensions[col[0].column_letter].width = min(max_len + 4, 50)

    wb.save(path)
    return path


def _save_master_excel(all_profiles: list, run_dir: Path) -> Path:
    """Deduplicate by URL and save master list."""
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment

    seen = set()
    unique = []
    for p in all_profiles:
        url = p.get("url", "")
        if url and url not in seen:
            seen.add(url)
            unique.append(p)

    path = run_dir / "MASTER_LIST.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Master List"

    headers = [
        "Instagram URL", "Username", "Full Name", "City Searched",
        "Followers", "Posts", "Email", "Phone",
        "City/State", "Location Raw", "Category", "Business", "Verified",
    ]
    header_fill = PatternFill("solid", fgColor="065f46")
    header_font = Font(bold=True, color="FFFFFF")

    for col, h in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col, value=h)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center")

    for row, p in enumerate(unique, 2):
        ws.cell(row=row, column=1,  value=p.get("url", ""))
        ws.cell(row=row, column=2,  value=p.get("username", ""))
        ws.cell(row=row, column=3,  value=p.get("full_name", ""))
        ws.cell(row=row, column=4,  value=p.get("city_searched", ""))
        ws.cell(row=row, column=5,  value=p.get("follower_count", ""))
        ws.cell(row=row, column=6,  value=p.get("media_count", ""))
        ws.cell(row=row, column=7,  value=p.get("email", ""))
        ws.cell(row=row, column=8,  value=p.get("phone", ""))
        ws.cell(row=row, column=9,  value=p.get("city_state", ""))
        ws.cell(row=row, column=10, value=p.get("location_text", ""))
        ws.cell(row=row, column=11, value=p.get("category", ""))
        ws.cell(row=row, column=12, value="Yes" if p.get("is_business") else "")
        ws.cell(row=row, column=13, value="Yes" if p.get("is_verified") else "")

    for col in ws.columns:
        max_len = max((len(str(c.value or "")) for c in col), default=10)
        ws.column_dimensions[col[0].column_letter].width = min(max_len + 4, 50)

    wb.save(path)
    return path


# ---------------------------------------------------------------------------
# Bulk search route
# ---------------------------------------------------------------------------

@app.route("/bulk_search")
def bulk_search():
    """
    Stream bulk search across multiple cities.
    Query params: niche, cities (newline-separated), max_results, do_enrich (true/false)
    SSE events: bulk_status | city_start | city_done | city_error | bulk_done
    """
    from enricher import enrich_profile, get_ig_client, get_maps_client, enrich_profiles_via_apify
    import concurrent.futures

    niche       = request.args.get("niche", "").strip()
    cities_raw  = request.args.get("cities", "").strip()
    max_results = min(int(request.args.get("max_results", 25)), 200)
    do_enrich   = request.args.get("do_enrich", "true").lower() == "true"

    cities = [c.strip() for c in cities_raw.splitlines() if c.strip()]

    ig_user     = os.getenv("IG_USERNAME", "").strip()
    ig_pass     = os.getenv("IG_PASSWORD", "").strip()
    apify_token = os.getenv("APIFY_API_TOKEN", "").strip()
    maps_key    = os.getenv("GOOGLE_MAPS_KEY", "").strip()
    anthropic_key = os.getenv("ANTHROPIC_API_KEY", "").strip()

    # Create exports/run_YYYYMMDD_HHMMSS folder
    run_ts  = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = Path(__file__).parent / "exports" / f"run_{run_ts}"
    run_dir.mkdir(parents=True, exist_ok=True)

    def event(data: dict) -> str:
        return f"data: {json.dumps(data)}\n\n"

    def _do_enrich_profiles(profiles, city, ig_client, maps_client):
        """Enrich a list of profiles with per-profile timeout. Yields (enriched_list, status_msgs)."""
        enriched = []
        status_msgs = []
        PROFILE_TIMEOUT = 25

        for prof_idx, p in enumerate(profiles):
            username = p["url"].rstrip("/").split("/")[-1]
            status_msgs.append(f"  [{city}] Enriching {prof_idx+1}/{len(profiles)}: @{username}…")
            try:
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                    future = ex.submit(enrich_profile, p["url"], ig_client, maps_client)
                    data = future.result(timeout=PROFILE_TIMEOUT)
                data["city_searched"] = city
                enriched.append(data)
            except concurrent.futures.TimeoutError:
                status_msgs.append(f"  ⚠️ @{username} timed out — skipping")
                p["city_searched"] = city
                enriched.append(p)
            except Exception as exc:
                status_msgs.append(f"  ⚠️ @{username} error: {exc}")
                p["city_searched"] = city
                enriched.append(p)
            time.sleep(0.2)
        return enriched

    def generate():
        if not niche:
            yield event({"type": "bulk_status", "message": "❌ Niche is required."})
            return
        if not cities:
            yield event({"type": "bulk_status", "message": "❌ No cities provided."})
            return

        use_apify  = do_enrich and bool(apify_token)
        mode_label = ("🚀 Full via Apify" if use_apify
                      else "🔬 Full (with enrichment)" if do_enrich
                      else "⚡ Fast (URLs only)")
        yield event({"type": "bulk_status",
                     "message": f"🚀 {mode_label} · {len(cities)} cities · {niche}"})

        ig_client   = (get_ig_client(ig_user, ig_pass) if do_enrich and not use_apify and ig_user and ig_pass else None)
        maps_client = (get_maps_client(maps_key) if do_enrich and maps_key else None)

        all_profiles = []
        city_results = {}
        # Store run_dir path so /bulk_enrich can use it
        run_dir_str = str(run_dir.relative_to(Path(__file__).parent))

        for city_idx, city in enumerate(cities):
            yield event({
                "type": "city_start", "city": city,
                "index": city_idx + 1, "total": len(cities),
                "message": f"🔍 [{city_idx+1}/{len(cities)}] Searching {city}…",
            })

            # ── Search ────────────────────────────────────────────────────────
            try:
                queries = (generate_search_queries(niche, city, count=3, anthropic_api_key=anthropic_key)
                           if anthropic_key else [build_query(niche, city)])
                seen_urls = {}
                for q in queries:
                    res = search_with_query(q, "ddgs", max_results=max_results, apify_token=apify_token)
                    for p in res.get("profiles", []):
                        if p["url"] not in seen_urls:
                            seen_urls[p["url"]] = p
                    if len(seen_urls) >= max_results:
                        break
                profiles = list(seen_urls.values())[:max_results]
            except Exception as exc:
                yield event({"type": "city_error", "city": city,
                             "message": f"Search failed for {city}: {exc}"})
                continue

            # ── Enrich (optional) ─────────────────────────────────────────────
            if use_apify:
                yield event({"type": "bulk_status",
                             "message": f"  Found {len(profiles)} profiles · enriching via Apify…"})
                try:
                    urls_batch = [p["url"] for p in profiles]
                    apify_results = enrich_profiles_via_apify(urls_batch, apify_token, maps_client)
                    # Tag with city and fill gaps
                    url_to_apify = {r["url"]: r for r in apify_results}
                    enriched = []
                    for p in profiles:
                        r = url_to_apify.get(p["url"], {**p})
                        r["city_searched"] = city
                        enriched.append(r)
                except Exception as exc:
                    yield event({"type": "bulk_status",
                                 "message": f"  ⚠️ Apify error: {exc} — falling back to direct scrape"})
                    enriched = _do_enrich_profiles(profiles, city, ig_client, maps_client)
            elif do_enrich:
                yield event({"type": "bulk_status",
                             "message": f"  Found {len(profiles)} profiles · enriching…"})
                enriched = _do_enrich_profiles(profiles, city, ig_client, maps_client)
            else:
                # Fast mode — just tag with city_searched
                enriched = [{**p, "city_searched": city} for p in profiles]

            # ── Save city Excel ───────────────────────────────────────────────
            try:
                fpath  = _save_city_excel(enriched, city, run_dir)
                rel    = str(fpath.relative_to(Path(__file__).parent))
                emails = sum(1 for p in enriched if p.get("email"))
                phones = sum(1 for p in enriched if p.get("phone"))
                locs   = sum(1 for p in enriched if p.get("location_text"))
                city_results[city] = {"count": len(enriched), "file": rel}
                all_profiles.extend(enriched)

                yield event({
                    "type": "city_done", "city": city,
                    "index": city_idx + 1, "total": len(cities),
                    "count": len(enriched), "emails": emails,
                    "phones": phones, "locs": locs,
                    "file": rel,
                    "download_url": f"/download_export?path={rel}",
                    "enriched": do_enrich,
                    "message": (
                        f"✅ {city}: {len(enriched)} profiles"
                        + (f" · {emails} emails · {phones} phones" if do_enrich else " · URLs saved")
                    ),
                })
            except Exception as exc:
                yield event({"type": "city_error", "city": city,
                             "message": f"Failed to save Excel for {city}: {exc}"})

            time.sleep(0.5)

        # ── Master list (always) ──────────────────────────────────────────────
        if all_profiles:
            try:
                master_path  = _save_master_excel(all_profiles, run_dir)
                rel_master   = str(master_path.relative_to(Path(__file__).parent))
                unique_count = len({p.get("url") for p in all_profiles if p.get("url")})
                yield event({
                    "type": "bulk_done",
                    "total_cities": len(cities),
                    "total_profiles": len(all_profiles),
                    "unique_profiles": unique_count,
                    "enriched": do_enrich,
                    "run_dir": run_dir_str,
                    "profiles": all_profiles,   # sent to frontend for enrich-later
                    "master_file": rel_master,
                    "master_download_url": f"/download_export?path={rel_master}",
                    "city_results": city_results,
                    "message": (
                        f"🎉 Done! {unique_count} unique profiles across {len(cities)} cities."
                        + (" Master list saved." if do_enrich else " Click Enrich to add contact data.")
                    ),
                })
            except Exception as exc:
                yield event({"type": "bulk_status",
                             "message": f"⚠️ Could not save master list: {exc}"})
        else:
            yield event({"type": "bulk_done", "total_cities": len(cities),
                         "total_profiles": 0, "unique_profiles": 0,
                         "enriched": do_enrich,
                         "message": "No profiles found across any city."})

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/bulk_enrich", methods=["POST"])
def bulk_enrich():
    """
    Stream enrichment over an existing fast-mode bulk run folder.
    Body: { run_dir: "exports/run_XXXX", profiles: [{url, city_searched, ...}] }
    Re-saves each city Excel and the master list after enriching.
    """
    from enricher import enrich_profile, get_ig_client, get_maps_client
    import concurrent.futures

    body     = request.get_json(silent=True) or {}
    run_dir_rel = body.get("run_dir", "")
    profiles    = body.get("profiles", [])   # [{url, city_searched, ...}]

    ig_user  = os.getenv("IG_USERNAME", "").strip()
    ig_pass  = os.getenv("IG_PASSWORD", "").strip()
    maps_key = os.getenv("GOOGLE_MAPS_KEY", "").strip()

    run_dir = Path(__file__).parent / run_dir_rel

    def event(data: dict) -> str:
        return f"data: {json.dumps(data)}\n\n"

    def generate():
        if not profiles:
            yield event({"type": "bulk_enrich_status", "message": "❌ No profiles to enrich."})
            return

        yield event({"type": "bulk_enrich_status",
                     "message": f"🔬 Enriching {len(profiles)} profiles across all cities…"})

        ig_client   = get_ig_client(ig_user, ig_pass) if ig_user and ig_pass else None
        maps_client = get_maps_client(maps_key) if maps_key else None
        PROFILE_TIMEOUT = 25

        # Group by city
        from collections import defaultdict
        city_map = defaultdict(list)
        for p in profiles:
            city_map[p.get("city_searched", "Unknown")].append(p)

        all_enriched = []
        total = len(profiles)
        done  = 0

        for city, city_profiles in city_map.items():
            yield event({"type": "bulk_enrich_status",
                         "message": f"  Enriching {city} ({len(city_profiles)} profiles)…"})
            enriched_city = []
            for p in city_profiles:
                username = p["url"].rstrip("/").split("/")[-1]
                yield event({"type": "bulk_enrich_progress",
                             "message": f"  @{username}…", "done": done, "total": total})
                try:
                    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                        future = ex.submit(enrich_profile, p["url"], ig_client, maps_client)
                        data = future.result(timeout=PROFILE_TIMEOUT)
                    data["city_searched"] = city
                    enriched_city.append(data)
                    all_enriched.append(data)
                except Exception:
                    p["city_searched"] = city
                    enriched_city.append(p)
                    all_enriched.append(p)
                done += 1
                time.sleep(0.2)

            # Re-save city Excel
            try:
                fpath = _save_city_excel(enriched_city, city, run_dir)
                rel   = str(fpath.relative_to(Path(__file__).parent))
                emails = sum(1 for p in enriched_city if p.get("email"))
                phones = sum(1 for p in enriched_city if p.get("phone"))
                yield event({
                    "type": "bulk_enrich_city_done", "city": city,
                    "count": len(enriched_city), "emails": emails, "phones": phones,
                    "download_url": f"/download_export?path={rel}",
                })
            except Exception as exc:
                yield event({"type": "bulk_enrich_status",
                             "message": f"⚠️ Could not save {city} Excel: {exc}"})

        # Re-save master list
        try:
            master_path  = _save_master_excel(all_enriched, run_dir)
            rel_master   = str(master_path.relative_to(Path(__file__).parent))
            unique_count = len({p.get("url") for p in all_enriched if p.get("url")})
            yield event({
                "type": "bulk_enrich_done",
                "unique_profiles": unique_count,
                "master_download_url": f"/download_export?path={rel_master}",
                "message": f"🎉 Enrichment complete! {unique_count} unique profiles · master list updated.",
            })
        except Exception as exc:
            yield event({"type": "bulk_enrich_status",
                         "message": f"⚠️ Could not save master list: {exc}"})

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/download_export")
def download_export():
    """Serve a generated Excel file for download."""
    from flask import send_file
    rel_path = request.args.get("path", "")
    abs_path = Path(__file__).parent / rel_path
    if not abs_path.exists() or not str(abs_path).startswith(str(Path(__file__).parent / "exports")):
        return "File not found", 404
    return send_file(abs_path, as_attachment=True,
                     download_name=abs_path.name,
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


if __name__ == "__main__":
    ak = os.getenv("ANTHROPIC_API_KEY", "")
    ai_status = "✅ AI features active" if ak else "⚠️  No Anthropic API key — AI features disabled"
    print(f"\n🚀  Instagram Scraper running at http://localhost:8080")
    print(f"    {ai_status}\n")
    app.run(debug=False, port=8080)
