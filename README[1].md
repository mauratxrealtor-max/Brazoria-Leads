# Brazoria County — Motivated Seller Lead Scraper

Automated daily scraper for motivated seller leads from Brazoria County, TX public records.

## Sources
| Source | Data |
|--------|------|
| Brazoria County Clerk (Tyler Tech portal) | Lis pendens, foreclosures, judgments, liens, probate, NOC |
| Brazoria Central Appraisal District (CAD) | Owner name, site address, mailing address |

## Lead Types Collected
| Code | Description |
|------|-------------|
| LP | Lis Pendens |
| NOFC | Notice of Foreclosure |
| TAXDEED | Tax Deed |
| JUD / CCJ / DRJUD | Judgment / Certified Judgment / Domestic Judgment |
| LNCORPTX / LNIRS / LNFED | Corporate / IRS / Federal Tax Lien |
| LN / LNMECH / LNHOA | General / Mechanic / HOA Lien |
| MEDLN | Medicaid Lien |
| PRO | Probate Documents |
| NOC | Notice of Commencement |
| RELLP | Release of Lis Pendens |

## Seller Score (0–100)
| Factor | Points |
|--------|--------|
| Base | 30 |
| Per motivation flag | +10 |
| LP + foreclosure combo (same owner) | +20 |
| Amount > $100k | +15 |
| Amount > $50k | +10 |
| Filed within last 7 days | +5 |
| Has property address | +5 |

## File Structure
```
.
├── scraper/
│   ├── fetch.py          # Main scraper (Playwright async + HTTP fallback)
│   └── requirements.txt  # Python dependencies
├── dashboard/
│   ├── index.html        # Lead management dashboard (served via GitHub Pages)
│   └── records.json      # Latest leads (auto-updated by GitHub Actions)
├── data/
│   ├── records.json      # Duplicate of dashboard/records.json
│   └── leads.csv         # GHL-compatible CSV export
└── .github/
    └── workflows/
        └── scrape.yml    # Daily cron + manual dispatch workflow
```

## Setup

### 1. Fork / Clone this repo to GitHub

### 2. Enable GitHub Pages
- Go to **Settings → Pages**
- Source: **GitHub Actions**
- Save

### 3. Enable GitHub Actions
- Go to **Actions** tab
- Click **"I understand my workflows, go ahead and enable them"**

### 4. Run the first scrape manually
- Go to **Actions → Brazoria Motivated Seller Scraper**
- Click **"Run workflow"** → **"Run workflow"**
- Wait ~5–10 minutes for completion

### 5. View your dashboard
After the first successful run, your dashboard will be live at:
```
https://<your-username>.github.io/<repo-name>/
```

## Running Locally
```bash
# Install dependencies
pip install -r scraper/requirements.txt
python -m playwright install --with-deps chromium

# Run the scraper
python scraper/fetch.py
```

## Outputs
- **`dashboard/records.json`** — JSON used by the live dashboard
- **`data/records.json`** — Mirror of the above
- **`data/leads.csv`** — GHL / CRM import-ready CSV with all lead data

## Schedule
Runs automatically every day at **7:00 AM UTC** (2 AM CDT / 1 AM CST).

## Notes
- Looks back **90 days** by default (configurable via workflow dispatch input)
- All scraping is from **100% public** government records
- The scraper uses Playwright (headless Chromium) to handle the Tyler Tech portal's JavaScript-rendered search
- Falls back to HTTP/BeautifulSoup if Playwright is unavailable
- Retry logic: 3 attempts per request with 3-second delay
- Never crashes on bad/malformed records
