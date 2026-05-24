import json
import time
import csv
import io
import os

from dotenv import load_dotenv
from flask import Flask, Response, render_template, request, jsonify
from scraper import search_instagram_profiles

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
    return render_template("index.html")


@app.route("/scrape")
def scrape():
    niche       = request.args.get("niche", "").strip()
    location    = request.args.get("location", "").strip()
    max_results = min(int(request.args.get("max_results", 50)), 200)
    engine      = request.args.get("engine", "ddgs").strip()
    api_key     = request.args.get("api_key", "").strip()
    cse_id      = request.args.get("cse_id", "").strip()

    # Fall back to env vars if not provided in the request
    if engine == "google_api":
        api_key = api_key or os.getenv("GOOGLE_API_KEY", "")
        cse_id  = cse_id  or os.getenv("GOOGLE_CSE_ID", "")
    elif engine == "brave_api":
        api_key = api_key or os.getenv("BRAVE_API_KEY", "")

    if not niche:
        return jsonify({"error": "Niche is required"}), 400

    label = ENGINE_LABELS.get(engine, engine)

    def event(data: dict) -> str:
        return f"data: {json.dumps(data)}\n\n"

    def generate():
        yield event({"type": "status",
                     "message": f'Searching via {label} for "{niche}"…'})

        result = search_instagram_profiles(
            niche=niche,
            location=location,
            max_results=max_results,
            engine=engine,
            api_key=api_key,
            cse_id=cse_id,
        )

        yield event({"type": "query", "query": result["query"],
                     "engine": result.get("engine", engine)})

        if result["error"]:
            yield event({"type": "error", "message": result["error"]})
            # Still emit any partial results
            if result["urls"]:
                for i in range(0, len(result["urls"]), BATCH_SIZE):
                    yield event({"type": "urls",
                                 "new_urls": result["urls"][i:i+BATCH_SIZE],
                                 "total_count": min(i + BATCH_SIZE, len(result["urls"]))})
            return

        urls = result["urls"]

        if not urls:
            yield event({"type": "done",
                         "message": "No Instagram profiles found. Try a different niche or location.",
                         "total_count": 0, "urls": []})
            return

        for i in range(0, len(urls), BATCH_SIZE):
            batch = urls[i:i + BATCH_SIZE]
            yield event({"type": "urls", "new_urls": batch,
                         "total_count": min(i + BATCH_SIZE, len(urls))})
            time.sleep(0.04)

        yield event({"type": "done",
                     "message": f"Found {len(urls)} Instagram profiles via {label}.",
                     "total_count": len(urls), "urls": urls})

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
    print("\n🚀  Instagram Scraper running at http://localhost:8080\n")
    app.run(debug=False, port=8080)
