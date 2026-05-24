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


@app.route("/download", methods=["POST"])
def download():
    data  = request.get_json(silent=True) or {}
    urls  = data.get("urls", [])
    niche = data.get("niche", "instagram_profiles")

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["instagram_url"])
    for url in urls:
        writer.writerow([url])

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
