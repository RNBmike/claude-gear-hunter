# Gear Hunter 🎸
**Free · headless · no machine required**

Monitors Facebook Marketplace, eBay, and Craigslist for instrument deals.
Runs 4x/day in GitHub Actions cloud. Evaluates every listing with Claude AI.
Emails you only when something is genuinely worth looking at.

---

## Stack

| Layer | Technology | Cost |
|---|---|---|
| Cloud scheduler + host | GitHub Actions | Free |
| FB Marketplace scraper | Playwright + Browserforge | Free |
| eBay search | eBay Finding API | Free |
| Craigslist search | RSS feed | Free |
| AI deal evaluation | Claude Haiku | ~$0.50–1.50/mo |
| Email alerts | Gmail SMTP | Free |

---

## Geo coverage

| Source | Coverage |
|---|---|
| Facebook Marketplace | 40 miles from Los Gatos, CA (lat/lng radius) |
| eBay local | 50 miles from zip 95124 (San Jose area) |
| eBay nationwide | All US shipping listings |
| Craigslist | sfbay/msa, 50 miles from zip 95124 |

eBay local pickup listings are flagged **📍 LOCAL — can play before buying** in alerts.
eBay shipping listings show **📦 Ships only — ask seller about local viewing**.
FB Marketplace and Craigslist are always local.

---

## Setup (~20 minutes)

### 1. Fork this repo to your GitHub account

### 2. Get your keys

| Key | Where to get it |
|---|---|
| `ANTHROPIC_API_KEY` | console.anthropic.com → API Keys |
| `EMAIL_FROM` + `EMAIL_PASSWORD` | Gmail → Security → 2-Step Verification → App Passwords |
| `EBAY_APP_ID` | developer.ebay.com → My Account → Application Keys *(optional)* |

> **Gmail App Password:** generate one specifically for this app — do NOT use your Google login password.

### 3. Add GitHub Secrets

Repo → Settings → Secrets and variables → Actions → New repository secret

| Secret name | Value |
|---|---|
| `ANTHROPIC_API_KEY` | Anthropic API key |
| `EMAIL_FROM` | your@gmail.com |
| `EMAIL_PASSWORD` | Gmail app password |
| `EMAIL_TO` | where to receive alerts |
| `EBAY_APP_ID` | eBay App ID *(optional — eBay skipped if absent)* |

### 4. Configure your watchlist

Edit `src/hunter.py` → find `WATCHLIST`.

The fretless bass search is pre-configured. Add more items by copying the
commented template block at the bottom of `WATCHLIST` and filling in:

- `name` — display label in alerts
- `query` — search string
- `max_price` — your ceiling
- `criteria` — plain English description of what makes a good deal

The `criteria` field is read by Claude like instructions from a knowledgeable
friend. Be specific: good brands, deal-breakers, green flags.

### 5. Push and test

```bash
git add .
git commit -m "gear hunter setup"
git push
```

GitHub → Actions → Gear Hunter → Run workflow → Run workflow

Check your email in a few minutes.

---

## How FB scraping works

Facebook Marketplace shows listings publicly — the login prompt is just a
dismissable overlay. The scraper:

1. Launches headless Chromium with a **Browserforge** fingerprint
   (mimics real browser hardware, canvas, navigator, TLS fingerprint)
2. Navigates to the public search URL with your lat/lng + radius
3. Clicks away the login modal
4. Extracts listing data from JSON payloads FB embeds in its own page
5. Falls back to DOM parsing if JSON structure changes

**If FB scraping breaks:** bump `playwright` and `browserforge` versions
in `requirements.txt` and redeploy. Usually fixes it.

---

## Bot evasion summary

- Cron runs at 4 irregular times per day
- Additional 0–30 minute random jitter added per run
- Browserforge injects a realistic, unique browser fingerprint each run
- GitHub Actions IPs rotate naturally across Microsoft's infrastructure
- Random delays between listing evaluations within each run
- Short sessions (search → extract → done) rather than prolonged browsing

---

## Tuning

**Too many alerts?** Raise the score threshold in `src/hunter.py`:
```python
if verdict in ("HOT DEAL", "WATCH") and score >= 7:  # was 6
```

**Too few?** Lower to `>= 5`, or add WATCH as its own tier with a lower bar.

**Add a new instrument to watch:** copy the commented template in `WATCHLIST`,
fill in `query`, `max_price`, and `criteria`. Push. Done.
