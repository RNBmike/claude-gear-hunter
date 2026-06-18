"""
src/hunter.py
Gear Hunter — orchestrates FB Marketplace, eBay, and Craigslist searches,
evaluates listings with Claude Haiku, and emails alerts on good deals.

Sources (in priority order):
  1. Facebook Marketplace  — Playwright + Browserforge, geo: 40mi from Los Gatos CA
  2. eBay                  — Finding API, local (50mi/95124) + nationwide searches
  3. Craigslist            — RSS feed, sfbay/msa, 50mi from zip 95124

Geo coverage:
  - FB:  lat/lng radius, 40 miles from Los Gatos CA (37.2358, -121.9624)
  - eBay local: 50 miles from zip 95124 (San Jose / Los Gatos area)
  - CL:  sfbay subdomain, postal 95124, 50-mile radius

Alert logic:
  - Pre-filter: bad brands + "parts only" removed for free (no Claude call)
  - Claude Haiku evaluates remaining listings against plain-English criteria
  - Only HOT DEAL or WATCH with score >= 6 trigger an email
  - eBay local pickup flagged prominently — buyer wants to play before purchasing
  - seen_ids.json deduplicates across runs (cached in GitHub Actions)
"""

import os
import json
import time
import random
import hashlib
import smtplib
import logging
import xml.etree.ElementTree as ET
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import requests
from fb_scraper import fetch_facebook

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ─── Environment / secrets ────────────────────────────────────────────────────
# All set as GitHub Actions secrets — never hardcode these

ANTHROPIC_KEY  = os.environ["ANTHROPIC_API_KEY"]
EMAIL_FROM     = os.environ["EMAIL_FROM"]          # Gmail address
EMAIL_PASSWORD = os.environ["EMAIL_PASSWORD"]      # Gmail app password (not login password)
EMAIL_TO       = os.environ["EMAIL_TO"]
EBAY_APP_ID    = os.environ.get("EBAY_APP_ID", "") # Optional — eBay skipped if empty
SEEN_IDS_FILE  = "seen_ids.json"

# ─── Watchlist ────────────────────────────────────────────────────────────────
# Each entry is one instrument search. Add more by copying the block below.
# The `criteria` field is plain English — Claude reads it like a knowledgeable friend.

WATCHLIST = [
    {
        "name":  "Fretless Bass",
        "query": "fretless bass",
        "max_price": 500,

        # Craigslist: sfbay subdomain, musical instruments, 50mi from zip 95124
        "cl_location": "sfbay",
        "cl_category": "msa",     # msa = musical instruments & gear

        # Facebook Marketplace: 40 miles (~64 km) from Los Gatos, CA
        "fb_lat":       37.2358,
        "fb_lng":      -121.9624,
        "fb_radius_km": 64,

        # eBay: local search within 50 miles of zip 95124 + nationwide shipping
        "ebay_zip":      "95124",
        "ebay_miles":    50,

        "criteria": """
You are a knowledgeable bass guitar expert evaluating listings for a discerning buyer.

BUYER CRITERIA:
- Budget: max $500 total (including any shipping)
- Goal: quality fretless bass for serious playing AND strong resale value
- Buyer wants to play the instrument before purchasing if at all possible

GOOD BRANDS (acceptable):
  Ibanez, Yamaha, Warwick, Fender, Squier Classic Vibe, Godin, Schecter, ESP,
  G&L, Music Man, Sterling by MM, MTD, Sadowsky, Spector, Pedulla, Zon,
  NS Design, Carvin, Peavey (Cirrus or TL-5 only), Alvarez, Harley Benton,
  Cort, Lakland, Lull, Tune, Aria Pro II

BAD BRANDS (auto-reject — do not recommend regardless of price):
  Glarry, Rogue, Ktone, Pyle, Lindo, Fever, SX, unbranded, unknown Chinese brands

CONDITION: Good or fair is acceptable — but flag specifically what is wrong.
  Cosmetic wear is fine. Structural issues are not.

DEAL-BREAKERS (always PASS regardless of price):
  - Self-defretted (buyer removed frets DIY) — kills resale value and playability
  - Heavy irreversible modifications
  - Missing major parts (tuners, bridge, neck)
  - Neck cracks, breaks, or structural damage
  - "As-is, no returns" with no description of condition

GREEN FLAGS (boost score significantly):
  - Price under $200 for an instrument that normally sells for $800+
  - Normally sells for 4–5x the asking price
  - Factory fretless (not converted from fretted)
  - Original hardshell case included
  - Made in Japan (MIJ) or USA
  - Local pickup available — buyer wants to play before purchasing
  - Seller includes photos of the fretboard and neck

RED FLAGS (lower score):
  - Vague descriptions hiding damage
  - "As-is" without explanation
  - Self-defretted or DIY-converted
  - Dead spots or buzzing mentioned
  - Neck issues (warped, twisted, broken headstock)
  - Heavily modified (pickups, preamp, hardware replaced with unknowns)

Respond ONLY with valid JSON, no markdown fences:
{
  "verdict": "HOT DEAL" | "WATCH" | "PASS",
  "score": 1-10,
  "reason": "2-3 sentence plain English explanation of the verdict",
  "flags": ["short descriptive label", ...],
  "flag_types": ["green" | "amber" | "red", ...]
}
""",
    },

    # ── Template for additional watchlist items ───────────────────────────────
    # {
    #     "name":  "Fender Jazz Bass",
    #     "query": "fender jazz bass",
    #     "max_price": 600,
    #     "cl_location": "sfbay",
    #     "cl_category": "msa",
    #     "fb_lat":       37.2358,
    #     "fb_lng":      -121.9624,
    #     "fb_radius_km": 64,
    #     "ebay_zip":     "95124",
    #     "ebay_miles":   50,
    #     "criteria": """
    # You are a guitar expert...
    # GOOD brands: ...
    # DEAL-BREAKERS: ...
    # GREEN FLAGS: ...
    # """,
    # },
]

# ─── Brand pre-filter — runs before Claude to eliminate obvious rejects free ──

BAD_BRANDS   = {"glarry", "rogue", "ktone", "pyle", "lindo", "fever", "sx"}
PARTS_PHRASES = ("parts only", "for parts", "not working", "as is")

def pre_filter(item: dict) -> bool:
    """Return True if listing should be evaluated by Claude. False = skip."""
    title = (item.get("title") or "").lower()

    if item.get("price_num", 9999) > item.get("max_price", 500):
        return False

    for brand in BAD_BRANDS:
        if brand in title:
            log.info(f"  Pre-filter [bad brand]: {item.get('title')}")
            return False

    for phrase in PARTS_PHRASES:
        if phrase in title:
            log.info(f"  Pre-filter [parts/broken]: {item.get('title')}")
            return False

    return True

# ─── Deduplication ────────────────────────────────────────────────────────────

def load_seen() -> set:
    try:
        with open(SEEN_IDS_FILE) as f:
            return set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()

def save_seen(seen: set):
    with open(SEEN_IDS_FILE, "w") as f:
        json.dump(list(seen), f)

def make_id(source: str, raw_id: str) -> str:
    return hashlib.md5(f"{source}:{raw_id}".encode()).hexdigest()

# ─── Claude evaluation ────────────────────────────────────────────────────────

def evaluate(item: dict, criteria: str) -> dict:
    """Send a listing to Claude Haiku for deal evaluation."""

    # Tell Claude about local pickup — buyer wants to play before purchasing
    if item.get("local_pickup"):
        pickup_note = (
            "LOCAL PICKUP AVAILABLE — this is important, as buyer wants to "
            "play the instrument before committing to purchase. Flag as green flag."
        )
    elif item.get("source") == "eBay":
        pickup_note = (
            "Ships only (no local pickup indicated). "
            "Buyer may still contact seller to ask about local viewing."
        )
    else:
        pickup_note = "Local listing — buyer can arrange to play before purchasing."

    prompt = (
        f"Title: {item.get('title')}\n"
        f"Price: ${item.get('price_num')}\n"
        f"Condition: {item.get('condition') or 'not specified'}\n"
        f"Description: {item.get('description') or 'No description provided'}\n"
        f"Seller location: {item.get('location') or 'unknown'}\n"
        f"Platform: {item.get('source')}\n"
        f"Pickup: {pickup_note}"
    )

    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key":          ANTHROPIC_KEY,
            "anthropic-version":  "2023-06-01",
            "content-type":       "application/json",
        },
        json={
            "model":      "claude-haiku-4-5-20251001",
            "max_tokens": 400,
            "system":     criteria.strip(),
            "messages":   [{"role": "user", "content": prompt}],
        },
        timeout=30,
    )
    resp.raise_for_status()
    text = resp.json()["content"][0]["text"]
    text = text.replace("```json", "").replace("```", "").strip()
    return json.loads(text)

# ─── eBay source ──────────────────────────────────────────────────────────────

def fetch_ebay(watch: dict) -> list:
    """
    Two searches: local (within N miles of buyer's zip) + nationwide shipping.
    Local listings get local_pickup=True so the email flags them prominently.
    """
    if not EBAY_APP_ID:
        log.info("[eBay] EBAY_APP_ID not set — skipping eBay")
        return []

    log.info(f"[eBay] Searching: {watch['query']}")
    zip_code = watch.get("ebay_zip", "95124")
    miles    = watch.get("ebay_miles", 50)

    base_url = (
        "https://svcs.ebay.com/services/search/FindingService/v1"
        "?OPERATION-NAME=findItemsByKeywords"
        "&SERVICE-VERSION=1.0.0"
        f"&SECURITY-APPNAME={EBAY_APP_ID}"
        "&RESPONSE-DATA-FORMAT=JSON"
        f"&keywords={requests.utils.quote(watch['query'])}"
        f"&itemFilter(0).name=MaxPrice&itemFilter(0).value={watch['max_price']}"
        "&itemFilter(1).name=Condition&itemFilter(1).value=3000"
        "&itemFilter(2).name=ListingType&itemFilter(2).value=FixedPrice"
        "&sortOrder=StartTimeNewest"
        "&paginationInput.entriesPerPage=20"
    )

    searches = [
        {
            "label": "local",
            "params": (
                f"&itemFilter(3).name=LocalPickupOnly&itemFilter(3).value=true"
                f"&buyerPostalCode={zip_code}"
                f"&itemFilter(4).name=MaxDistance&itemFilter(4).value={miles}"
            ),
        },
        {
            "label": "ships",
            "params": "",   # nationwide, any shipping method
        },
    ]

    raw_items = []
    for search in searches:
        try:
            # was this: data  = requests.get(base_url + search["params"], timeout=15).json()
            resp = requests.get(base_url + search["params"], timeout=15)
            log.info(f"[eBay] {search['label']} status: {resp.status_code}, starts: {resp.text[:300]}")
            data = resp.json()
            items = (data.get("findItemsByKeywordsResponse", [{}])[0]
                         .get("searchResult", [{}])[0]
                         .get("item", []))
            raw_items.extend([(item, search["label"]) for item in items])
            log.info(f"[eBay] {search['label']}: {len(items)} listings")
        except Exception as e:
            log.warning(f"[eBay] {search['label']} search error: {e}")

    # Deduplicate by item ID — local + nationwide searches may overlap
    seen_ids: set = set()
    results = []
    for item, label in raw_items:
        try:
            item_id = item["itemId"][0]
            if item_id in seen_ids:
                continue
            seen_ids.add(item_id)

            price_num  = float(item["sellingStatus"][0]["currentPrice"][0]["__value__"])
            shipping   = item.get("shippingInfo", [{}])[0]
            is_local   = (label == "local") or (shipping.get("localPickup", ["false"])[0] == "true")

            results.append({
                "id":           item_id,
                "title":        item["title"][0],
                "price_num":    price_num,
                "price":        f"${price_num:.2f}",
                "condition":    item.get("condition", [{}])[0].get("conditionDisplayName", [""])[0],
                "description":  "",
                "location":     item.get("location", [""])[0],
                "url":          item["viewItemURL"][0],
                "source":       "eBay",
                "local_pickup": is_local,
                "max_price":    watch["max_price"],
            })
        except Exception as e:
            log.warning(f"[eBay] Parse error: {e}")

    log.info(f"[eBay] {len(results)} unique listings ({sum(1 for r in results if r['local_pickup'])} local)")
    return results

# ─── Craigslist source ────────────────────────────────────────────────────────

def fetch_craigslist(watch: dict) -> list:
    """RSS feed search — no auth, no key needed. Always local by nature."""
    log.info(f"[CL] Searching: {watch['query']}")
    loc      = watch.get("cl_location", "sfbay")
    category = watch.get("cl_category", "msa")
    query    = requests.utils.quote(watch["query"])
    zip_code = watch.get("ebay_zip", "95124")   # reuse zip for CL radius too

    url = (
        f"https://{loc}.craigslist.org/search/{category}"
        f"?format=rss&query={query}&max_price={watch['max_price']}"
        f"&postal={zip_code}&search_distance=50"
    )

    try:
        resp = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        log.info(f"[CL] Response status: {resp.status_code}, starts: {resp.text[:200]}")
        ns   = {
            "rss": "http://purl.org/rss/1.0/",
            "cl":  "http://www.craigslist.org/about/namespace/1.0",
        }
        root  = ET.fromstring(resp.text)
        items = root.findall(".//rss:item", ns)
        log.info(f"[CL] Found {len(items)} listings")
    except Exception as e:
        log.warning(f"[CL] Error: {e}")
        return []

    results = []
    for item in items[:20]:
        try:
            title     = item.findtext("title", "")
            link      = item.findtext("link", "")
            desc      = item.findtext("description", "")
            ptag      = item.find("cl:price", ns)
            price_str = ptag.text if ptag is not None else "$0"
            price_num = float("".join(c for c in price_str if c.isdigit() or c == ".") or "0")
            results.append({
                "id":           link,
                "title":        title,
                "price_num":    price_num,
                "price":        price_str,
                "condition":    "",
                "description":  desc[:500],
                "location":     loc,
                "url":          link,
                "source":       "Craigslist",
                "local_pickup": True,   # CL is always local
                "max_price":    watch["max_price"],
            })
        except Exception as e:
            log.warning(f"[CL] Parse error: {e}")

    return results

# ─── Email alert ──────────────────────────────────────────────────────────────

def send_alert(deals: list):
    body = "<h2 style='font-family:sans-serif;margin-bottom:4px'>🎸 Gear Hunter</h2>\n"
    body += f"<p style='font-family:sans-serif;color:#666;margin-top:0'>{len(deals)} deal(s) found</p>\n"

    for d in deals:
        item   = d["item"]
        result = d["result"]
        watch_name = d["watch"]

        verdict_color = "#2d6a4f" if result["verdict"] == "HOT DEAL" else "#7d4e00"
        score_bar     = "█" * result["score"] + "░" * (10 - result["score"])

        # Pickup badge
        if item.get("local_pickup"):
            pickup_html = (
                "<span style='background:#d4edda;color:#155724;padding:3px 10px;"
                "border-radius:4px;font-size:11px;font-weight:700'>"
                "📍 LOCAL — can play before buying</span>"
            )
        elif item.get("source") == "eBay":
            pickup_html = (
                "<span style='background:#fff3cd;color:#856404;padding:3px 10px;"
                "border-radius:4px;font-size:11px'>"
                "📦 Ships only — ask seller about local viewing</span>"
            )
        else:
            pickup_html = ""

        # Flags
        flags_html = " &nbsp; ".join(
            f"<span style='background:{'#d4edda' if t=='green' else '#f8d7da' if t=='red' else '#fff3cd'};"
            f"color:{'#155724' if t=='green' else '#721c24' if t=='red' else '#856404'};"
            f"padding:2px 8px;border-radius:3px;font-size:11px'>{f}</span>"
            for f, t in zip(result.get("flags", []), result.get("flag_types", []))
        )

        seller_loc = f" · {item['location']}" if item.get("location") else ""

        body += f"""
<div style="border:1px solid #ddd;border-radius:10px;padding:18px;margin-bottom:18px;font-family:sans-serif;max-width:600px">
  <p style="margin:0 0 2px;font-size:11px;color:#999;text-transform:uppercase;letter-spacing:1px">{watch_name}</p>
  <p style="margin:0 0 6px;font-size:17px;font-weight:700;color:#111">{item['title']}</p>
  <p style="margin:0 0 10px;font-size:13px;color:#555">
    {item['source']}{seller_loc} &nbsp;·&nbsp; <strong>{item['price']}</strong> &nbsp;·&nbsp; {item.get('condition') or 'condition unknown'}
    &nbsp; {pickup_html}
  </p>
  <p style="margin:0 0 4px;font-size:15px;font-weight:700;color:{verdict_color}">
    {result['verdict']} &nbsp; <span style="font-family:monospace;font-size:12px;color:#888">{score_bar} {result['score']}/10</span>
  </p>
  <p style="margin:0 0 10px;font-size:14px;color:#333;line-height:1.5">{result['reason']}</p>
  <p style="margin:0 0 14px">{flags_html}</p>
  <a href="{item['url']}"
     style="background:#1a1a2e;color:white;padding:9px 18px;border-radius:5px;
            text-decoration:none;font-size:13px;font-weight:600">
    View listing →
  </a>
</div>
"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"🎸 Gear Hunter: {len(deals)} deal(s) found"
    msg["From"]    = EMAIL_FROM
    msg["To"]      = EMAIL_TO
    msg.attach(MIMEText(body, "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(EMAIL_FROM, EMAIL_PASSWORD)
        server.sendmail(EMAIL_FROM, EMAIL_TO, msg.as_string())

    log.info(f"[Alert] Email sent — {len(deals)} deal(s) to {EMAIL_TO}")

# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    seen      = load_seen()
    all_deals = []

    for watch in WATCHLIST:
        log.info(f"\n{'═'*55}\n  {watch['name']}  (max ${watch['max_price']})\n{'═'*55}")

        # Polite delay between watchlist items
        time.sleep(random.randint(5, 15))

        # ── Collect listings: FB first, then eBay, then Craigslist ──
        listings = []

        fb_listings = fetch_facebook(
            query      = watch["query"],
            max_price  = watch["max_price"],
            lat        = watch.get("fb_lat",       37.2358),
            lng        = watch.get("fb_lng",      -121.9624),
            radius_km  = watch.get("fb_radius_km", 64),
        )
        for item in fb_listings:
            item["max_price"] = watch["max_price"]
        listings += fb_listings
        listings += fetch_ebay(watch)
        listings += fetch_craigslist(watch)

        log.info(f"\nTotal collected: {len(listings)} listings")

        # ── Evaluate each new listing ──
        for item in listings:
            uid = make_id(item["source"], item["id"])
            if uid in seen:
                continue
            seen.add(uid)

            if not pre_filter(item):
                continue

            time.sleep(random.uniform(0.5, 2.0))  # pace Claude calls

            try:
                result  = evaluate(item, watch["criteria"])
                verdict = result.get("verdict", "PASS")
                score   = result.get("score", 0)
                pickup  = "📍" if item.get("local_pickup") else "📦"
                log.info(f"  {pickup} [{item['source']}] {item['title'][:50]} → {verdict} {score}/10")

                if verdict in ("HOT DEAL", "WATCH") and score >= 6:
                    all_deals.append({
                        "item":   item,
                        "result": result,
                        "watch":  watch["name"],
                    })
            except Exception as e:
                log.warning(f"  Claude error on '{item.get('title', '')}': {e}")

    save_seen(seen)

    if all_deals:
        log.info(f"\n✅ {len(all_deals)} deal(s) to alert on")
        send_alert(all_deals)
    else:
        log.info("\nNo new deals this run.")

if __name__ == "__main__":
    main()
