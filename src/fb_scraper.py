"""
src/fb_scraper.py
Facebook Marketplace scraper — Playwright + Browserforge, no login required.

Strategy:
  1. Launch headless Chromium with a Browserforge-generated fingerprint
     (realistic browser headers, canvas fingerprint, navigator properties)
  2. Navigate to public FB Marketplace search URL with lat/lng radius
  3. Dismiss the login modal (overlay only — not a true auth wall)
  4. Extract listing data from embedded JSON payloads in <script> tags
  5. Fall back to DOM parsing if JSON extraction fails

Geo defaults: 40 miles (~64 km) from Los Gatos, CA (37.2358, -121.9624)

Maintenance:
  - If modal dismiss stops working: inspect FB page and update MODAL_CLOSE_SELECTOR
  - If JSON extraction stops working: inspect page source, find __typename "MarketplaceListing"
  - If detection increases: bump playwright + browserforge versions in requirements.txt
"""

import json
import re
import time
import random
import logging

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
from browserforge.fingerprints import FingerprintGenerator
from browserforge.injectors.playwright import NewContext

log = logging.getLogger(__name__)

# Geo-aware search URL — no city slug needed, uses coordinates + radius
FB_SEARCH_URL = (
    "https://www.facebook.com/marketplace/search"
    "?query={query}&maxPrice={max_price}"
    "&latitude={lat}&longitude={lng}&radius={radius_km}"
    "&condition=used%2Cgood%2Cfair%2Cexcellent"
    "&sortBy=creation_time_descend"
)

MODAL_CLOSE_SELECTOR = 'div[aria-label="Close"]'
MODAL_ALT_SELECTOR   = 'div[role="dialog"] [aria-label="Close"]'


def _random_delay(lo=1.0, hi=3.5):
    time.sleep(random.uniform(lo, hi))


def _dismiss_modal(page) -> bool:
    """Click the login modal's close button. Returns True if found and dismissed."""
    for selector in (MODAL_CLOSE_SELECTOR, MODAL_ALT_SELECTOR):
        try:
            page.wait_for_selector(selector, timeout=6000)
            page.click(selector)
            _random_delay(0.8, 2.0)
            log.info("[FB] Login modal dismissed")
            return True
        except PlaywrightTimeout:
            continue
    log.warning("[FB] Modal not found — continuing anyway (may already be dismissed)")
    return False


def _extract_json_blobs(html: str) -> list:
    """Pull all <script type='application/json'> payloads from the page HTML."""
    blobs = []
    for match in re.finditer(
        r'<script[^>]+type="application/json"[^>]*>(.*?)</script>', html, re.DOTALL
    ):
        try:
            blobs.append(json.loads(match.group(1)))
        except json.JSONDecodeError:
            pass
    return blobs


def _find_listings_in_blob(blob, max_price: float) -> list:
    """
    Recursively walk a JSON blob looking for FB MarketplaceListing objects.
    FB embeds structured listing data for its own React frontend — we read it directly.
    """
    results = []

    def walk(obj):
        if isinstance(obj, dict):
            if obj.get("__typename") in ("MarketplaceListing", "Marketplace2Listing"):
                try:
                    price_raw = (
                        obj.get("listing_price", {}).get("amount")
                        or obj.get("price_amount")
                        or "0"
                    )
                    price_num = float(str(price_raw).replace(",", ""))
                    if price_num <= max_price:
                        desc = obj.get("description", "")
                        results.append({
                            "id":          obj.get("id", ""),
                            "title":       obj.get("marketplace_listing_title", obj.get("name", "")),
                            "price_num":   price_num,
                            "price":       f"${price_num:.0f}",
                            "condition":   obj.get("condition", ""),
                            "description": desc.get("text", "")[:600]
                                           if isinstance(desc, dict) else str(desc)[:600],
                            "location":    (obj.get("location") or {})
                                           .get("reverse_geocode", {}).get("city", ""),
                            "url":         f"https://www.facebook.com/marketplace/item/{obj.get('id', '')}",
                            "source":      "Facebook Marketplace",
                            "local_pickup": True,  # FB Marketplace is always local by nature
                        })
                except (ValueError, TypeError, KeyError):
                    pass
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    walk(blob)
    return results


def _dom_fallback(page, max_price: float) -> list:
    """
    Fallback DOM parser for when JSON extraction yields nothing.
    Less reliable — FB CSS class names change occasionally.
    """
    results = []
    try:
        page.wait_for_selector('[data-testid="marketplace_feed_item"]', timeout=8000)
        cards = page.query_selector_all('[data-testid="marketplace_feed_item"]')
        for card in cards[:25]:
            try:
                title_el  = card.query_selector('span[dir="auto"]')
                price_el  = card.query_selector('span.x193iq5w')
                link_el   = card.query_selector('a[href*="/marketplace/item/"]')
                title     = title_el.inner_text() if title_el else ""
                price_str = price_el.inner_text() if price_el else "$0"
                href      = link_el.get_attribute("href") if link_el else ""
                price_num = float("".join(c for c in price_str if c.isdigit() or c == ".") or "0")
                if title and price_num <= max_price:
                    item_id = re.search(r"/item/(\d+)", href or "")
                    results.append({
                        "id":          item_id.group(1) if item_id else href,
                        "title":       title,
                        "price_num":   price_num,
                        "price":       f"${price_num:.0f}",
                        "condition":   "",
                        "description": "",
                        "location":    "",
                        "url":         f"https://www.facebook.com{href}"
                                       if href.startswith("/") else href,
                        "source":      "Facebook Marketplace",
                        "local_pickup": True,
                    })
            except Exception:
                pass
    except PlaywrightTimeout:
        log.warning("[FB] DOM fallback: listing cards not found — FB may have changed structure")
    return results


def fetch_facebook(
    query: str,
    max_price: float,
    lat: float = 37.2358,    # Los Gatos, CA
    lng: float = -121.9624,
    radius_km: int = 64,     # 40 miles
) -> list:
    """
    Scrape FB Marketplace. Returns list of listing dicts.
    Defaults to 40-mile radius from Los Gatos, CA.
    """
    url = FB_SEARCH_URL.format(
        query=query.replace(" ", "%20"),
        max_price=int(max_price),
        lat=lat,
        lng=lng,
        radius_km=radius_km,
    )
    log.info(f"[FB] URL: {url}")

    fingerprint = FingerprintGenerator().generate(
        browser=("chrome",),
        os=("windows", "macos", "linux"),
        device=("desktop",),
    )

    results = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
            ],
        )
        context = NewContext(browser, fingerprint=fingerprint)
        page    = context.new_page()

        page.set_extra_http_headers({
            "Accept-Language": "en-US,en;q=0.9",
            "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Sec-Fetch-Dest":  "document",
            "Sec-Fetch-Mode":  "navigate",
            "Sec-Fetch-Site":  "none",
        })

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            _random_delay(2.0, 4.0)
            _dismiss_modal(page)
            _random_delay(1.5, 3.0)
            page.mouse.wheel(0, random.randint(200, 600))   # human-like scroll
            _random_delay(1.0, 2.0)

            html = page.content()

            # Strategy 1: JSON blob extraction (preferred)
            for blob in _extract_json_blobs(html):
                results.extend(_find_listings_in_blob(blob, max_price))

            # Strategy 2: DOM fallback
            if not results:
                log.info("[FB] JSON extraction empty — trying DOM fallback")
                results = _dom_fallback(page, max_price)

            log.info(f"[FB] Found {len(results)} raw listings")

        except Exception as e:
            log.error(f"[FB] Scrape failed: {e}")
        finally:
            context.close()
            browser.close()

    # Deduplicate by listing ID
    seen: set = set()
    deduped = []
    for r in results:
        if r["id"] and r["id"] not in seen:
            seen.add(r["id"])
            deduped.append(r)

    log.info(f"[FB] {len(deduped)} unique listings after dedup")
    return deduped
