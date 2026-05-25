import json
import time
import csv
import io
import os

from dotenv import load_dotenv
from flask import Flask, Response, render_template, request, jsonify
from scraper import (
    build_query,
    generate_search_queries,
    score_profiles,
    search_with_query,
    search_instagram_profiles,
)

load_dotenv()

app = Flask(__name__)

BATCH_SIZE = 5

ENGINE_LABELS = {
    "ddgs":       "DuckDuckGo",
    "google_api": "Google API",
    "brave_api":  "Brave Search",
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
    engine      = request.args.get("engine", "ddgs").strip()
    api_key     = request.args.get("api_key", "").strip()
    cse_id      = request.args.get("cse_id", "").strip()

    # Fall back to env vars
    if engine == "google_api":
        api_key = api_key or os.getenv("GOOGLE_API_KEY", "")
        cse_id  = cse_id  or os.getenv("GOOGLE_CSE_ID", "")
    elif engine == "brave_api":
        api_key = api_key or os.getenv("BRAVE_API_KEY", "")

    anthropic_key = os.getenv("ANTHROPIC_API_KEY", "").strip()

    if not niche:
        return jsonify({"error": "Niche is required"}), 400

    label = ENGINE_LABELS.get(engine, engine)

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
            result = search_with_query(query, engine, api_key, cse_id, max_results=max_results)

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
    urls      = [u for u in body.get("urls", []) if u.strip()]
    ig_user   = os.getenv("IG_USERNAME", "").strip()
    ig_pass   = os.getenv("IG_PASSWORD", "").strip()
    maps_key  = (
        os.getenv("GOOGLE_MAPS_KEY", "").strip()
        or os.getenv("GOOGLE_API_KEY", "").strip()
    )

    def event(data: dict) -> str:
        return f"data: {json.dumps(data)}\n\n"

    def generate():
        if not urls:
            yield event({"type": "error", "message": "No URLs provided."})
            return

        # ── Initialise Instagram client ───────────────────────────────────────
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

        # ── Initialise Maps client ────────────────────────────────────────────
        maps_client = get_maps_client(maps_key) if maps_key else None
        if not maps_client:
            yield event({"type": "status", "message": "⚠️ No Maps API key — location text only, no GPS"})

        yield event({
            "type": "status",
            "message": f"🔬 Enriching {len(urls)} profiles…",
        })

        # ── Enrich each profile ───────────────────────────────────────────────
        enriched_count = 0
        email_count    = 0
        phone_count    = 0
        loc_count      = 0

        for i, url in enumerate(urls):
            try:
                profile = enrich_profile(url, ig_client=ig_client, maps_client=maps_client)
            except Exception as exc:
                profile = {
                    "url": url,
                    "username": url.split("instagram.com/")[-1].strip("/"),
                    "error": str(exc),
                }

            enriched_count += 1
            if profile.get("email"):    email_count += 1
            if profile.get("phone"):    phone_count += 1
            if profile.get("location_text"): loc_count += 1

            yield event({
                "type":     "enriched",
                "profile":  profile,
                "progress": i + 1,
                "total":    len(urls),
            })

            # Polite delay between requests (less when no IG client)
            time.sleep(_random.uniform(1.5, 3.0) if ig_client else _random.uniform(0.3, 0.7))

        yield event({
            "type":    "enrich_done",
            "total":   enriched_count,
            "emails":  email_count,
            "phones":  phone_count,
            "locations": loc_count,
            "message": (
                f"Enriched {enriched_count} profiles · "
                f"{email_count} emails · {phone_count} phones · {loc_count} locations"
            ),
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


if __name__ == "__main__":
    ak = os.getenv("ANTHROPIC_API_KEY", "")
    ai_status = "✅ AI features active" if ak else "⚠️  No Anthropic API key — AI features disabled"
    print(f"\n🚀  Instagram Scraper running at http://localhost:8080")
    print(f"    {ai_status}\n")
    app.run(debug=False, port=8080)
