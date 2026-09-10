import os
import json
import time
import random
import threading
from datetime import datetime, timedelta, timezone
import dotenv
from flask import Flask
from curl_cffi import requests
from telegram_notifier import Notifier

# --- RENDER HEALTH-CHECK SERVER ---
app = Flask(__name__)

@app.route('/')
def health_check():
    return "OLX Bot is active", 200

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

# Run Flask server as a daemon thread
threading.Thread(target=run_flask, daemon=True).start()
# ----------------------------------

SCRAPE_DELAY_MIN = 3
SCRAPE_DELAY_MAX = 6
REPEAT_DELAY = 300
MAX_AGE_DAYS = 14  # Cutoff for old listings

# Load cache safely and use a Set for O(1) lookup
try:
    with open("shown_ids.json", "r") as f:
        shown_ids_list = json.loads(f.read())
        shown_ids = set(str(x) for x in shown_ids_list)
except Exception as e:
    print(f"[INIT] Warning: Could not read shown_ids.json ({e}). Initializing empty cache.", flush=True)
    shown_ids = set()

dotenv.load_dotenv()
notifier = Notifier()

headers = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
    'Accept-Language': 'pl-PL,pl;q=0.9,en-US;q=0.8,en;q=0.7',
    'Accept-Encoding': 'gzip, deflate, br',
    'Connection': 'keep-alive',
    'Upgrade-Insecure-Requests': '1',
    'Sec-Fetch-Dest': 'document',
    'Sec-Fetch-Mode': 'navigate',
    'Sec-Fetch-Site': 'none',
    'Sec-Fetch-User': '?1',
    'Cache-Control': 'max-age=0'
}

def enforce_newest_sort(url: str) -> str:
    """Ensures search[order]=created_at:desc is appended to the OLX URL."""
    if "search%5Border%5D=" in url or "search[order]=" in url:
        return url
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}search%5Border%5D=created_at%3Adesc"

def is_listing_too_old(offer: dict, max_days=MAX_AGE_DAYS) -> bool:
    """Checks created_time or pushup_time to skip listings older than MAX_AGE_DAYS."""
    raw_time = offer.get("created_time") or offer.get("pushup_time") or offer.get("created_at")
    if not raw_time:
        return False  # If timestamp field is absent, default to processing

    try:
        # ISO string handling (e.g., '2026-09-01T12:00:00+02:00')
        if isinstance(raw_time, str):
            created_dt = datetime.fromisoformat(raw_time)
        # Unix timestamp handling
        elif isinstance(raw_time, (int, float)):
            created_dt = datetime.fromtimestamp(raw_time, tz=timezone.utc)
        else:
            return False

        cutoff_date = datetime.now(created_dt.tzinfo) - timedelta(days=max_days)
        return created_dt < cutoff_date
    except Exception:
        return False

INITIAL_RUN = True
print("[INIT] Initialization complete. Starting scraper loop...", flush=True)

while True:
    print("\n--- Starting new OLX scrape cycle ---", flush=True)

    try:
        with open("searches.json", "r") as f:
            searches = json.loads(f.read())
    except Exception as e:
        print(f"[ERROR] Failed to read searches.json: {e}", flush=True)
        time.sleep(REPEAT_DELAY)
        continue

    for index, search in enumerate(searches, start=1):
        raw_url = search.get('url')
        if not raw_url:
            continue

        url = enforce_newest_sort(raw_url)
        min_price = search.get('min_price', 0)
        max_price = search.get('max_price', 999999)
        required_keyword = search.get('required_keyword', '')

        print(f"[{index}/{len(searches)}] Fetching: {url}", flush=True)

        try:
            response = requests.get(url, headers=headers, impersonate="chrome120", timeout=15)
            print(f"    -> Response Status: {response.status_code}", flush=True)

            if response.status_code != 200:
                print(f"    [WARN] Skipped URL due to non-200 status code ({response.status_code})", flush=True)
                continue

            data = response.text
            start_tag = '__PRERENDERED_STATE__= "'
            end_tag = 'window.__TAURUS__'

            if start_tag not in data or end_tag not in data:
                print("    [WARN] Pre-rendered state JSON tags not found in response DOM.", flush=True)
                continue

            data = data[data.find(start_tag) + len(start_tag):]
            data = data[:data.find(end_tag)]
            data = data[:data.rfind('";')]
            data = data.encode().decode('unicode_escape')

            parsed_json = json.loads(data)
            offers = parsed_json.get("listing", {}).get("listing", {}).get("ads", [])

            matches_found = 0
            for offer in offers:
                if not isinstance(offer, dict):
                    continue

                offer_id = str(offer.get('id', ''))
                
                # 1. Skip duplicates immediately via set
                if offer_id in shown_ids:
                    continue

                # 2. Skip listings older than MAX_AGE_DAYS
                if is_listing_too_old(offer):
                    shown_ids.add(offer_id)  # Cache ID to avoid re-checking
                    continue

                title = offer.get('title', '')
                offer_url = offer.get('url', '')

                # Safe price parsing
                price_obj = offer.get("price") or {}
                regular_price = price_obj.get("regularPrice") or {}
                price = regular_price.get("value", 0)

                if not price:
                    continue

                if required_keyword.lower() not in title.lower():
                    continue
                if price < min_price or price > max_price:
                    continue

                shown_ids.add(offer_id)

                if INITIAL_RUN:
                    print(f"    [SEEDING] Cached existing listing: {title} ({price} PLN)", flush=True)
                    continue

                matches_found += 1
                log_line = f"    [MATCH FOUND] {title} | {price} PLN | {offer_url}"
                print(log_line, flush=True)

                notifier.send_message(f"New Match: {title}\nPrice: {price} PLN\nLink: {offer_url}")

            # Save state
            with open("shown_ids.json", "w") as f:
                f.write(json.dumps(list(shown_ids)))

            if matches_found == 0 and not INITIAL_RUN:
                print("    -> Parsed successfully (No new matching listings).", flush=True)

        except Exception as err:
            print(f"    [ERROR] Exception processing URL: {err}", flush=True)

        time.sleep(random.uniform(SCRAPE_DELAY_MIN, SCRAPE_DELAY_MAX))

    INITIAL_RUN = False
    print(f"--- Cycle finished. Sleeping for {REPEAT_DELAY}s before next scan ---", flush=True)
    time.sleep(REPEAT_DELAY)
