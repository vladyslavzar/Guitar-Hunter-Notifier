import os
import json
import time
import random
import threading
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

# Load cache safely
try:
    with open("shown_ids.json", "r") as f:
        shown_ids = json.loads(f.read())
except Exception as e:
    print(f"[INIT] Warning: Could not read shown_ids.json ({e}). Initializing empty cache.", flush=True)
    shown_ids = []

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
        url = search.get('url')
        min_price = search.get('min_price', 0)
        max_price = search.get('max_price', 999999)
        required_keyword = search.get('required_keyword', '')

        print(f"[{index}/{len(searches)}] Fetching: {url}", flush=True)

        try:
            # impersonate="chrome120" spoofs real browser TLS fingerprints to bypass 403 Cloudflare blocks
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

            # Safely extract window state JSON string
            data = data[data.find(start_tag) + len(start_tag):]
            data = data[:data.find(end_tag)]
            data = data[:data.rfind('";')]
            data = data.encode().decode('unicode_escape')

            parsed_json = json.loads(data)
            offers = parsed_json.get("listing", {}).get("listing", {}).get("ads", [])

            matches_found = 0
            for offer in offers:
                title = offer.get('title', '')
                offer_url = offer.get('url', '')
                offer_id = offer.get('id', 0)
                price = offer.get("price", {}).get("regularPrice", {}).get("value", 0)

                # Keyword check (case-insensitive)
                if required_keyword.lower() not in title.lower():
                    continue
                # Price boundary check
                if price < min_price or price > max_price:
                    continue
                # Duplicate listing check
                if offer_id in shown_ids:
                    continue

                matches_found += 1
                log_line = f"    [MATCH FOUND] {title} | {price} PLN | {offer_url}"
                print(log_line, flush=True)

                notifier.send_message(f"New Match: {title}\nPrice: {price} PLN\nLink: {offer_url}")

                shown_ids.append(offer_id)
                with open("shown_ids.json", "w") as f:
                    f.write(json.dumps(shown_ids))

            if matches_found == 0:
                print("    -> Parsed successfully (No new matching listings).", flush=True)

        except Exception as err:
            print(f"    [ERROR] Exception processing URL: {err}", flush=True)

        # Randomized delay between search requests to mimic human browsing behavior
        delay = random.uniform(SCRAPE_DELAY_MIN, SCRAPE_DELAY_MAX)
        time.sleep(delay)

    print(f"--- Cycle finished. Sleeping for {REPEAT_DELAY}s before next scan ---", flush=True)
    time.sleep(REPEAT_DELAY)
