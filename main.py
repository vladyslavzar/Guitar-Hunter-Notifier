import requests
import dotenv
from telegram_notifier import Notifier
import json
import time

import os
import threading
from flask import Flask

# Dummy HTTP server for Render health checks
app = Flask(__name__)

@app.route('/')
def health_check():
    return "OLX Bot is active", 200

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

# Run Flask server in a background thread
threading.Thread(target=run_flask, daemon=True).start()

SCRAPE_DELAY=1
REPEAT_DELAY=300

with open("shown_ids.json") as f:
    shown_ids=json.loads(f.read())

dotenv.load_dotenv()
notifier = Notifier()

headers = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Safari/605.1.15'}

with open("searches.json") as f:
    searches=json.loads(f.read())
while True:
    for search in searches:
        response = requests.get(search['url'], headers=headers)

        MIN_PRICE=search['min_price']
        MAX_PRICE=search['max_price']
        REQUIRED_KEYWORD=search['required_keyword']

        print(response.status_code)
        if response.status_code!=200: continue
        data = response.text
        start_tag='__PRERENDERED_STATE__= "'
        end_tag='window.__TAURUS__'

        data = data[data.find(start_tag)+len(start_tag):]
        data = data[:data.find(end_tag)]
        data = data[:data.rfind('";')]
        data = data.encode().decode('unicode_escape')
        offers=json.loads(data).get("listing").get("listing").get("ads")
        for offer in offers:
            title=offer.get('title')
            url=offer.get('url')
            price=offer.get("price", {}).get("regularPrice", {}).get("value", 0)
            if REQUIRED_KEYWORD not in title: continue
            if price<MIN_PRICE or price>MAX_PRICE: continue
            if offer.get('id', 0) in shown_ids: continue
            print(title, str(price)+"PLN", url, sep=" | ")
            notifier.send_message(url)
            shown_ids.append(offer.get('id', 0))
            with open("shown_ids.json", "w") as f:
                f.write(json.dumps(shown_ids))
        time.sleep(SCRAPE_DELAY)
    time.sleep(REPEAT_DELAY)