# Instagram Scraper — Hosting & API Cost Proposal

**Prepared:** May 29, 2026  
**Project:** Instagram Scraper (Flask Web App)

---

## 1. Hosting Options

The app is a lightweight Flask server. Hosting cost is driven by **uptime**, not traffic — so 100 or 10,000 visits per month costs nearly the same.

| Platform | 0–100 visits/mo | 1,000 visits/mo | 10,000 visits/mo | 100,000 visits/mo |
|---|---|---|---|---|
| **Railway** | $5/mo | $5/mo | $5–10/mo | $10–20/mo |
| **Render** | Free* | $7/mo | $7/mo | $25/mo |
| **Fly.io** | Free* | $2–4/mo | $4–7/mo | $10–15/mo |

*Free tiers cold-start after 15 min idle — first request takes ~30 seconds.*

**Recommendation: Railway at $5/mo.** Always-on, no cold starts, zero config files required. Connect your GitHub repo and it deploys automatically. The $5/month is negligible compared to API costs below.

---

## 2. API Cost Breakdown (Current Stack)

Every search the app runs triggers multiple external API calls. These are the real costs to manage.

### Apify — Google Search + Instagram Enrichment

Apify is used for two things: searching Google for Instagram profiles, and scraping profile data (followers, bio, location).

| Monthly Searches | Apify Cost |
|---|---|
| 100 | $5–15 |
| 1,000 | $50–150 |
| 10,000 | $300–800 |

### Claude (Anthropic) — Query Generation + Relevance Scoring

The app already uses the cheapest Claude model (Haiku). Cost is low but compounds with volume.

| Monthly Searches | Claude Haiku Cost |
|---|---|
| 100 | $0.50–1.50 |
| 1,000 | $5–15 |
| 10,000 | $30–80 |

### Google Maps — Geocoding / Location Enrichment

Google gives $200/month in free credits (~11,000 requests), so this is effectively free at low-to-medium volume.

| Monthly Requests | Google Maps Cost |
|---|---|
| < 11,000 | Free (within credit) |
| 50,000 | ~$195/mo |
| 100,000 | ~$450/mo |

### Total — Current Stack by Usage

| Scenario | Hosting | Apify | Claude | Google Maps | **Total/mo** |
|---|---|---|---|---|---|
| 100 searches | $5 | $10 | $1 | $0 | **~$16** |
| 1,000 searches | $5 | $100 | $10 | $0 | **~$115** |
| 10,000 searches | $10 | $550 | $55 | $50 | **~$665** |

---

## 3. Cheaper Alternatives (Recommended Swaps)

Three drop-in replacements that cut costs by 80–95% with no meaningful loss in quality.

### Swap 1: Apify Google Search → Serper.dev

Serper.dev provides the same Google Search results via API at a fraction of the cost.

| | Apify | Serper.dev | Savings |
|---|---|---|---|
| Per 1,000 searches | ~$5–10 | $1 | ~80% |
| 10,000 searches/mo | ~$80 | $10 | ~87% |

**How to swap:** Replace the `_apify_google_run()` function in [scraper.py](scraper.py) with a `requests.post()` call to `api.serper.dev/search`. No change to app logic.

### Swap 2: Claude Haiku → Gemini 2.0 Flash

Google's Gemini Flash is 8× cheaper than Claude Haiku and handles classification and scoring tasks equally well.

| | Claude Haiku | Gemini 2.0 Flash | Savings |
|---|---|---|---|
| Per 1M input tokens | $0.80 | $0.10 | 87% |
| Per 1M output tokens | $4.00 | $0.40 | 90% |

**How to swap:** Replace the `anthropic` client in [scraper.py](scraper.py) with `google-generativeai`. The prompts stay identical — only the SDK call changes.

### Swap 3: Google Maps → Nominatim (OpenStreetMap)

Nominatim is a free, open-source geocoding API with no key required. The only limit is 1 request/second, which is fine for enrichment.

| | Google Maps | Nominatim | Savings |
|---|---|---|---|
| Cost | $0.005/request | Free | 100% |
| Setup | API key required | None | — |

**How to swap:** Replace `googlemaps.Client` in [enricher.py](enricher.py) with a `requests.get()` call to `nominatim.openstreetmap.org/search`.

---

## 4. Cost Comparison — Current vs. Optimized Stack

**Optimized stack:** Railway + Serper.dev + Gemini 2.0 Flash + Nominatim

| Scenario | Current Stack | Optimized Stack | Monthly Savings |
|---|---|---|---|
| 100 searches/mo | ~$16 | ~$3 | **$13 (81%)** |
| 1,000 searches/mo | ~$115 | ~$12 | **$103 (90%)** |
| 10,000 searches/mo | ~$665 | ~$55 | **$610 (92%)** |

---

## 5. Recommended Action Plan

| Priority | Action | Effort | Impact |
|---|---|---|---|
| 1 | Deploy to **Railway** | 30 min | Live URL, always-on |
| 2 | Swap to **Serper.dev** | 1–2 hrs | Biggest cost reduction |
| 3 | Swap to **Nominatim** | 1 hr | Eliminates Maps cost |
| 4 | Swap to **Gemini Flash** | 2–3 hrs | Further AI cost reduction |

Start with Railway to get live, then apply the API swaps in order of effort vs. savings. At low volume (< 500 searches/month), the optimized stack costs under $5/month total — essentially free to run.

---

*Note: External API prices are subject to change. Estimates based on published pricing as of May 2026.*
