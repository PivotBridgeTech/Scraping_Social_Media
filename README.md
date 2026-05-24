# Instagram Profile Scraper

A local web app that finds Instagram profile URLs by niche and location using Google search — no API keys required.

## Setup

```bash
cd instagram-scraper

# Create a virtual environment (recommended)
python3 -m venv venv
source venv/bin/activate      # macOS/Linux
# venv\Scripts\activate       # Windows

# Install dependencies
pip install -r requirements.txt

# Run the app
python app.py
```

Then open **http://localhost:5000** in your browser.

## How it works

1. Builds a Google query: `site:instagram.com "your niche" "location" -inurl:/p/ -inurl:/explore/ …`
2. Paginates through results with a randomised delay (4–12 s by default) to avoid rate limiting
3. Extracts and filters only profile URLs (not posts, reels, explore pages)
4. Streams results live into the browser
5. Lets you copy or download all URLs as a CSV

## Tips

- **Be patient with delays** — the randomised wait between pages keeps Google from blocking you
- If you get blocked (CAPTCHA), wait 10–15 minutes before trying again, or increase the delay range
- Combine niche + location for the best targeted results (e.g. "personal trainer" + "Chicago")
- Run multiple searches and merge the CSVs for larger lists

## Adding Apify enrichment (optional)

Once you have a list of profile URLs, you can enrich them using [Apify's Instagram Scraper](https://apify.com/apify/instagram-scraper). Sign up, grab your API token, and add an `/enrich` endpoint to `app.py` using the Apify REST API.
