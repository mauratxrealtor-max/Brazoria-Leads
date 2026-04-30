#!/usr/bin/env python3
"""
Brazoria County Motivated Seller Lead Scraper
==============================================
Sources:
  - Brazoria County Clerk  : https://brazoriacountytx-web.tylerhost.net/web/user/disclaimer
  - Brazoria CAD parcel DBF: https://brazoriacad.org/public-gis-and-property-data-downloads/

Lead types: LP, NOFC, TAXDEED, JUD, CCJ, DRJUD, LNCORPTX, LNIRS, LNFED,
            LN, LNMECH, LNHOA, MEDLN, PRO, NOC, RELLP
Look-back  : 90 days
Output     : dashboard/records.json, data/records.json, data/leads.csv
"""

import asyncio
import csv
import io
import json
import logging
import os
import re
import sys
import time
import traceback
import urllib.parse
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ── Playwright is async; import lazily so unit tests don't need a browser ──
try:
    from playwright.async_api import async_playwright, TimeoutError as PWTimeout
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

try:
    from dbfread import DBF
    DBFREAD_AVAILABLE = True
except ImportError:
    DBFREAD_AVAILABLE = False

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("brazoria_scraper")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
REPO_ROOT   = Path(__file__).resolve().parent.parent
DASH_OUT    = REPO_ROOT / "dashboard" / "records.json"
DATA_OUT    = REPO_ROOT / "data"      / "records.json"
CSV_OUT     = REPO_ROOT / "data"      / "leads.csv"

CLERK_BASE       = "https://brazoriacountytx-web.tylerhost.net"
CLERK_DISCLAIMER = f"{CLERK_BASE}/web/user/disclaimer"
CLERK_SEARCH     = f"{CLERK_BASE}/web/search/DOCSEARCH"
CLERK_DOC_BASE   = f"{CLERK_BASE}/web/document"

CAD_DOWNLOAD_PAGE = "https://brazoriacad.org/public-gis-and-property-data-downloads/"
# Known direct-URL pattern (confirmed via page inspection).
# The page lists links ending in .zip containing parcel shapefile + DBF.
CAD_PARCEL_URL_PATTERN = re.compile(
    r'https?://[^"\'>\s]*(?:parcel|PARCEL|Parcel)[^"\'>\s]*\.zip',
    re.IGNORECASE,
)
# Fallback: full certified roll zip also contains owner data
CAD_ROLL_URL_PATTERN = re.compile(
    r'https?://[^"\'>\s]*(?:certified|CERTIFIED|roll|ROLL)[^"\'>\s]*\.zip',
    re.IGNORECASE,
)

LOOKBACK_DAYS = 90
RETRY_ATTEMPTS = 3
RETRY_DELAY   = 3   # seconds

# ---------------------------------------------------------------------------
# Doc-type catalogue
# ---------------------------------------------------------------------------
DOC_TYPES = {
    # code : (search_keyword, category, label)
    "LP"       : ("LIS PENDENS",              "distress",   "Lis Pendens"),
    "NOFC"     : ("NOTICE OF FORECLOSURE",    "distress",   "Notice of Foreclosure"),
    "TAXDEED"  : ("TAX DEED",                 "distress",   "Tax Deed"),
    "JUD"      : ("JUDGMENT",                 "judgment",   "Judgment"),
    "CCJ"      : ("CERTIFIED JUDGMENT",       "judgment",   "Certified Judgment"),
    "DRJUD"    : ("DOMESTIC JUDGMENT",        "judgment",   "Domestic Relations Judgment"),
    "LNCORPTX" : ("CORP TAX LIEN",            "lien",       "Corporate Tax Lien"),
    "LNIRS"    : ("IRS LIEN",                 "lien",       "IRS Lien"),
    "LNFED"    : ("FEDERAL LIEN",             "lien",       "Federal Lien"),
    "LN"       : ("LIEN",                     "lien",       "Lien"),
    "LNMECH"   : ("MECHANIC",                 "lien",       "Mechanic's Lien"),
    "LNHOA"    : ("HOA LIEN",                 "lien",       "HOA Lien"),
    "MEDLN"    : ("MEDICAID LIEN",            "lien",       "Medicaid Lien"),
    "PRO"      : ("PROBATE",                  "probate",    "Probate"),
    "NOC"      : ("NOTICE OF COMMENCEMENT",   "notice",     "Notice of Commencement"),
    "RELLP"    : ("RELEASE LIS PENDENS",      "release",    "Release of Lis Pendens"),
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def retry(fn, *args, attempts=RETRY_ATTEMPTS, delay=RETRY_DELAY, **kwargs):
    """Call fn(*args, **kwargs) up to `attempts` times, returning result or None."""
    for attempt in range(1, attempts + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            log.warning("Attempt %d/%d failed: %s", attempt, attempts, exc)
            if attempt < attempts:
                time.sleep(delay)
    return None


def safe_str(val):
    if val is None:
        return ""
    return str(val).strip()


def parse_amount(text: str) -> float:
    """Extract first dollar-like number from a string."""
    if not text:
        return 0.0
    m = re.search(r"\$?([\d,]+(?:\.\d{1,2})?)", text.replace(",", ""))
    if m:
        try:
            return float(m.group(1).replace(",", ""))
        except ValueError:
            pass
    return 0.0


def split_name(full_name: str):
    """Return (first, last) best-effort from a full name string."""
    name = full_name.strip()
    # Remove common suffixes
    name = re.sub(r"\b(LLC|INC|CORP|LTD|LP|TRUST|ESTATE)\b\.?", "", name, flags=re.I).strip()
    # "LAST, FIRST" format
    if "," in name:
        parts = [p.strip() for p in name.split(",", 1)]
        return parts[1], parts[0]
    # "FIRST LAST" format
    parts = name.split()
    if len(parts) >= 2:
        return parts[0].title(), " ".join(parts[1:]).title()
    return name.title(), ""


def name_variants(full_name: str):
    """Return list of lookup variants for owner name matching."""
    n = full_name.strip().upper()
    variants = [n]
    if "," in n:
        parts = [p.strip() for p in n.split(",", 1)]
        # "LAST, FIRST" → "FIRST LAST"
        variants.append(f"{parts[1]} {parts[0]}")
    else:
        parts = n.split()
        if len(parts) >= 2:
            variants.append(f"{parts[-1]}, {' '.join(parts[:-1])}")
            variants.append(f"{parts[-1]} {' '.join(parts[:-1])}")
    return list(dict.fromkeys(variants))  # dedupe, preserve order


def compute_score(record: dict, all_records: list) -> tuple[int, list]:
    """
    Compute seller motivation score (0-100) and flag list.

    Base: 30
    +10 per flag
    +20 LP + foreclosure combo
    +15 amount > $100k
    +10 amount > $50k (mutually exclusive with above)
    +5  filed within last 7 days
    +5  has property address
    """
    flags = []
    score = 30

    cat   = record.get("cat", "")
    code  = record.get("doc_type", "")
    amt   = record.get("amount", 0.0) or 0.0
    owner = record.get("owner", "")

    # Flag assignments
    if code in ("LP", "RELLP"):
        flags.append("Lis pendens")
    if code in ("NOFC", "TAXDEED"):
        flags.append("Pre-foreclosure")
    if code in ("JUD", "CCJ", "DRJUD"):
        flags.append("Judgment lien")
    if code in ("LNCORPTX", "LNIRS", "LNFED", "LNHOA", "LN", "MEDLN"):
        flags.append("Tax lien" if code in ("LNCORPTX", "LNIRS", "LNFED") else "Other lien")
    if code == "LNMECH":
        flags.append("Mechanic lien")
    if code == "PRO":
        flags.append("Probate / estate")
    if re.search(r"\b(LLC|INC|CORP|LTD)\b", owner, re.I):
        flags.append("LLC / corp owner")

    # Check if this owner also has LP + foreclosure elsewhere
    owner_docs = [r["doc_type"] for r in all_records
                  if r.get("owner", "").upper() == owner.upper()]
    if "LP" in owner_docs and any(d in owner_docs for d in ("NOFC", "TAXDEED")):
        score += 20

    # Filed this week?
    try:
        filed = datetime.strptime(record.get("filed", ""), "%Y-%m-%d")
        if (datetime.now() - filed).days <= 7:
            flags.append("New this week")
    except (ValueError, TypeError):
        pass

    # Address present?
    has_addr = bool(record.get("prop_address") or record.get("mail_address"))

    score += len(flags) * 10
    if amt > 100_000:
        score += 15
    elif amt > 50_000:
        score += 10
    if "New this week" in flags:
        score += 5
    if has_addr:
        score += 5

    return min(score, 100), flags


# ---------------------------------------------------------------------------
# Parcel / CAD lookup
# ---------------------------------------------------------------------------

class ParcelLookup:
    """
    Downloads the Brazoria CAD parcel ZIP, extracts the DBF, and builds
    an in-memory dict keyed on uppercase owner-name variants.
    """

    def __init__(self):
        self._index: dict[str, dict] = {}   # name_variant → parcel dict
        self._loaded = False

    # ------------------------------------------------------------------
    def _find_parcel_url(self, html: str) -> str | None:
        """Scan the CAD downloads page HTML for a parcel or certified-roll zip URL."""
        for pat in (CAD_PARCEL_URL_PATTERN, CAD_ROLL_URL_PATTERN):
            m = pat.search(html)
            if m:
                return m.group(0)
        # Fall back: find any .zip link on the page
        soup = BeautifulSoup(html, "lxml")
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if href.lower().endswith(".zip") and ("parcel" in href.lower() or "roll" in href.lower()):
                return href if href.startswith("http") else f"https://brazoriacad.org{href}"
        return None

    def _download_and_parse(self):
        """Fetch downloads page, find ZIP URL, download, extract DBF."""
        log.info("Fetching CAD download page …")
        resp = requests.get(CAD_DOWNLOAD_PAGE, timeout=30)
        resp.raise_for_status()

        zip_url = self._find_parcel_url(resp.text)
        if not zip_url:
            log.warning("Could not find parcel ZIP URL on CAD downloads page.")
            return

        log.info("Downloading parcel ZIP from: %s", zip_url)
        r = requests.get(zip_url, timeout=120, stream=True)
        r.raise_for_status()

        raw = io.BytesIO()
        for chunk in r.iter_content(65536):
            raw.write(chunk)
        raw.seek(0)

        log.info("Extracting DBF from ZIP …")
        with zipfile.ZipFile(raw) as zf:
            dbf_names = [n for n in zf.namelist() if n.lower().endswith(".dbf")]
            if not dbf_names:
                log.warning("No DBF found in parcel ZIP.")
                return

            # prefer a file with 'parcel' or 'real' in the name
            chosen = next(
                (n for n in dbf_names if re.search(r"parcel|real|prop", n, re.I)),
                dbf_names[0],
            )
            log.info("Reading DBF: %s", chosen)
            dbf_bytes = zf.read(chosen)

        self._parse_dbf(dbf_bytes, chosen)

    def _parse_dbf(self, raw_bytes: bytes, filename: str):
        """Parse DBF bytes into owner index."""
        if not DBFREAD_AVAILABLE:
            log.warning("dbfread not installed – skipping parcel enrichment.")
            return

        tmp_path = Path("/tmp/_bcad_parcel.dbf")
        tmp_path.write_bytes(raw_bytes)

        try:
            tbl = DBF(str(tmp_path), load=True, ignore_missing_memofile=True)
            fields = [f.name.upper() for f in tbl.fields]
            log.info("DBF fields: %s", fields)

            def col(row, *candidates):
                for c in candidates:
                    if c in row:
                        v = row[c]
                        return safe_str(v) if v is not None else ""
                return ""

            count = 0
            for row in tbl:
                row = {k.upper(): v for k, v in row.items()}
                owner = col(row, "OWNER", "OWN1", "OWNERNAME", "OWNER_NAME")
                if not owner:
                    continue

                parcel = {
                    "prop_address": col(row, "SITE_ADDR", "SITEADDR", "SITE_ADDRESS", "PROP_ADDR"),
                    "prop_city"   : col(row, "SITE_CITY", "SITECITY", "CITY"),
                    "prop_state"  : col(row, "SITE_STATE", "STATE", "ST") or "TX",
                    "prop_zip"    : col(row, "SITE_ZIP", "SITEZIP", "ZIP"),
                    "mail_address": col(row, "ADDR_1", "MAILADR1", "MAIL_ADDR", "MAILING_ADDR"),
                    "mail_city"   : col(row, "CITY", "MAILCITY", "MAIL_CITY"),
                    "mail_state"  : col(row, "STATE", "MAILSTATE", "MAIL_STATE") or "TX",
                    "mail_zip"    : col(row, "ZIP", "MAILZIP", "MAIL_ZIP"),
                }

                for variant in name_variants(owner):
                    self._index[variant] = parcel
                count += 1

            log.info("Parcel index built: %d owners, %d name variants", count, len(self._index))
        except Exception as exc:
            log.error("DBF parse error: %s", exc)
        finally:
            tmp_path.unlink(missing_ok=True)

    # ------------------------------------------------------------------
    def load(self):
        if self._loaded:
            return
        self._loaded = True
        try:
            self._download_and_parse()
        except Exception as exc:
            log.error("Parcel lookup load failed: %s", exc)

    def lookup(self, owner_name: str) -> dict:
        """Return parcel data dict for owner_name, or empty dict."""
        for variant in name_variants(owner_name):
            hit = self._index.get(variant)
            if hit:
                return hit
        return {}


# ---------------------------------------------------------------------------
# Clerk scraper (Playwright async)
# ---------------------------------------------------------------------------

class ClerkScraper:
    """
    Scrapes the Tyler Technologies clerk portal for Brazoria County.
    Navigates disclaimer → sets date range → iterates doc types.
    """

    BASE = CLERK_BASE
    DISCLAIMER = CLERK_DISCLAIMER

    def __init__(self, lookback_days: int = LOOKBACK_DAYS):
        self.date_from = (datetime.now() - timedelta(days=lookback_days)).strftime("%m/%d/%Y")
        self.date_to   = datetime.now().strftime("%m/%d/%Y")
        self.records: list[dict] = []

    # ------------------------------------------------------------------
    async def _accept_disclaimer(self, page):
        """Click through the disclaimer page."""
        log.info("Accepting disclaimer …")
        await page.goto(self.DISCLAIMER, wait_until="domcontentloaded", timeout=60_000)
        # Look for an Accept / Continue / I Agree button
        for selector in [
            "input[value*='Accept']",
            "input[value*='Continue']",
            "input[value*='Agree']",
            "a:has-text('Accept')",
            "a:has-text('Continue')",
            "button:has-text('Accept')",
            "#btnContinue",
            "#ctl00_cphMain_btnContinue",
        ]:
            try:
                btn = page.locator(selector).first
                if await btn.is_visible(timeout=2_000):
                    await btn.click()
                    await page.wait_for_load_state("domcontentloaded", timeout=30_000)
                    log.info("Disclaimer accepted via: %s", selector)
                    return
            except Exception:
                pass
        log.warning("Could not find disclaimer button – proceeding anyway.")

    # ------------------------------------------------------------------
    async def _search_doc_type(self, page, code: str, keyword: str) -> list[dict]:
        """
        Navigate to the clerk document search, fill in the form for `keyword`,
        scrape all result pages, and return list of raw record dicts.
        """
        results = []
        search_url = f"{self.BASE}/web/search/DOCSEARCH3S1"
        log.info("Searching doc type %-10s  keyword: %s", code, keyword)

        try:
            await page.goto(search_url, wait_until="domcontentloaded", timeout=60_000)
        except Exception as exc:
            log.warning("Could not load search page: %s", exc)
            return results

        # Fill date range
        for date_sel, date_val in [
            ("#cphMain_dteFrom_txtDate,#ctl00_cphMain_dteFrom_txtDate,input[id*='From']", self.date_from),
            ("#cphMain_dteTo_txtDate,#ctl00_cphMain_dteTo_txtDate,input[id*='To']",       self.date_to),
        ]:
            for sel in date_sel.split(","):
                try:
                    loc = page.locator(sel).first
                    if await loc.is_visible(timeout=1_500):
                        await loc.fill(date_val)
                        break
                except Exception:
                    pass

        # Fill document type / description keyword
        for kw_sel in [
            "#cphMain_eDocType,#ctl00_cphMain_eDocType",
            "input[id*='DocType'],input[id*='doctype'],input[id*='description']",
            "select[id*='DocType']",
        ]:
            for sel in kw_sel.split(","):
                try:
                    loc = page.locator(sel).first
                    if await loc.is_visible(timeout=1_500):
                        tag = await loc.evaluate("el => el.tagName.toLowerCase()")
                        if tag == "select":
                            # Try to select matching option
                            await loc.select_option(label=keyword)
                        else:
                            await loc.fill(keyword)
                        break
                except Exception:
                    pass

        # Submit
        for sub_sel in [
            "input[type='submit']",
            "button[type='submit']",
            "#btnSearch,#ctl00_cphMain_btnSearch",
            "input[value='Search']",
        ]:
            try:
                btn = page.locator(sub_sel).first
                if await btn.is_visible(timeout=2_000):
                    await btn.click()
                    await page.wait_for_load_state("domcontentloaded", timeout=30_000)
                    break
            except Exception:
                pass

        # Scrape paginated results
        page_num = 0
        while True:
            page_num += 1
            html = await page.content()
            page_results = self._parse_results_html(html, code, keyword)
            results.extend(page_results)
            log.debug("  Page %d: %d rows", page_num, len(page_results))

            # Try to click "Next" pagination link
            try:
                next_btn = page.locator("a:has-text('Next'),a:has-text('>'),#lnkNextPage").first
                if await next_btn.is_visible(timeout=2_000):
                    await next_btn.click()
                    await page.wait_for_load_state("domcontentloaded", timeout=30_000)
                else:
                    break
            except Exception:
                break

            if page_num > 50:   # safety cap
                log.warning("Hit page cap for %s", code)
                break

        log.info("  → %d records for %s", len(results), code)
        return results

    # ------------------------------------------------------------------
    def _parse_results_html(self, html: str, code: str, keyword: str) -> list[dict]:
        """Parse a search-results HTML page into a list of record dicts."""
        soup = BeautifulSoup(html, "lxml")
        rows = []

        # Tyler Tech portals typically render results in a <table> with class
        # containing 'searchResults' or similar.
        tables = soup.find_all("table")
        result_table = None
        for t in tables:
            headers = [th.get_text(strip=True).lower() for th in t.find_all("th")]
            if any(h in headers for h in ("document", "doc number", "filed", "grantor", "grantee")):
                result_table = t
                break

        if result_table is None:
            # Try rows with links to document pages
            links = soup.find_all("a", href=lambda h: h and "/web/document/" in h)
            for link in links:
                doc_url  = link.get("href", "")
                doc_num  = link.get_text(strip=True)
                tr = link.find_parent("tr")
                if not tr:
                    continue
                cells = [td.get_text(strip=True) for td in tr.find_all("td")]
                rows.append(self._build_record(code, keyword, doc_num, doc_url, cells))
            return rows

        # Parse table rows
        header_row = result_table.find("tr")
        if not header_row:
            return rows
        col_names = [th.get_text(strip=True).lower() for th in header_row.find_all(["th", "td"])]

        for tr in result_table.find_all("tr")[1:]:
            cells = tr.find_all("td")
            if not cells:
                continue
            cell_texts = [c.get_text(strip=True) for c in cells]

            # Find doc number (usually a link)
            doc_url = ""
            doc_num = ""
            for cell in cells:
                a = cell.find("a", href=True)
                if a and ("/document/" in a["href"] or "/web/" in a["href"]):
                    doc_num = a.get_text(strip=True)
                    doc_url = a["href"]
                    if not doc_url.startswith("http"):
                        doc_url = self.BASE + doc_url
                    break

            if not doc_num and cell_texts:
                doc_num = cell_texts[0]

            rows.append(self._build_record(code, keyword, doc_num, doc_url, cell_texts, col_names))

        return rows

    # ------------------------------------------------------------------
    def _build_record(
        self, code: str, keyword: str, doc_num: str, doc_url: str,
        cells: list[str], col_names: list[str] | None = None
    ) -> dict:
        """Map raw cell values into a normalised record dict."""
        col_names = col_names or []

        def _col(*candidates):
            for c in candidates:
                for i, h in enumerate(col_names):
                    if c in h and i < len(cells):
                        return cells[i]
            return ""

        # Fallback positional guesses (Tyler Tech standard layout)
        # Columns are typically: Doc#, Type, Filed Date, Book/Page, Grantor, Grantee, Legal, Amount
        filed    = _col("filed", "date", "recorded") or (cells[2] if len(cells) > 2 else "")
        grantor  = _col("grantor", "owner", "party 1")  or (cells[4] if len(cells) > 4 else "")
        grantee  = _col("grantee", "party 2")            or (cells[5] if len(cells) > 5 else "")
        legal    = _col("legal", "description")          or (cells[6] if len(cells) > 6 else "")
        amount   = _col("amount", "consideration")       or (cells[7] if len(cells) > 7 else "")

        # Normalise filed date → YYYY-MM-DD
        filed_norm = ""
        for fmt in ("%m/%d/%Y", "%m-%d-%Y", "%Y-%m-%d", "%m/%d/%y"):
            try:
                filed_norm = datetime.strptime(filed.strip(), fmt).strftime("%Y-%m-%d")
                break
            except ValueError:
                pass

        _, cat_label = DOC_TYPES.get(code, ("", "Unknown"))[:1] + [DOC_TYPES.get(code, ("", "", "Unknown"))[2]]

        return {
            "doc_num"     : safe_str(doc_num),
            "doc_type"    : code,
            "filed"       : filed_norm,
            "cat"         : DOC_TYPES.get(code, ("", "other", ""))[1],
            "cat_label"   : DOC_TYPES.get(code, ("", "", keyword))[2],
            "owner"       : safe_str(grantor),
            "grantee"     : safe_str(grantee),
            "amount"      : parse_amount(amount),
            "legal"       : safe_str(legal),
            "prop_address": "",
            "prop_city"   : "",
            "prop_state"  : "TX",
            "prop_zip"    : "",
            "mail_address": "",
            "mail_city"   : "",
            "mail_state"  : "TX",
            "mail_zip"    : "",
            "clerk_url"   : safe_str(doc_url) or f"{CLERK_DOC_BASE}/{doc_num}",
            "flags"       : [],
            "score"       : 0,
        }

    # ------------------------------------------------------------------
    async def run(self) -> list[dict]:
        """Run full scrape using Playwright."""
        if not PLAYWRIGHT_AVAILABLE:
            log.error("Playwright not installed. Run: pip install playwright && playwright install chromium")
            return []

        log.info("Starting Playwright scrape …  date range: %s → %s", self.date_from, self.date_to)
        all_records: list[dict] = []

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                )
            )
            page = await context.new_page()

            # Accept disclaimer once
            await self._accept_disclaimer(page)

            # Scrape each doc type
            for code, (keyword, cat, label) in DOC_TYPES.items():
                try:
                    records = await self._search_doc_type(page, code, keyword)
                    all_records.extend(records)
                except Exception as exc:
                    log.error("Error scraping %s: %s\n%s", code, exc, traceback.format_exc())

            await browser.close()

        log.info("Total raw records scraped: %d", len(all_records))
        return all_records


# ---------------------------------------------------------------------------
# Alternative HTTP-based scraper (fallback if Playwright unavailable / blocked)
# ---------------------------------------------------------------------------

class ClerkHTTPScraper:
    """
    Stateless HTTP scraper using requests + BeautifulSoup.
    Handles session cookies and __doPostBack patterns.
    """

    def __init__(self, lookback_days: int = LOOKBACK_DAYS):
        self.date_from = (datetime.now() - timedelta(days=lookback_days)).strftime("%m/%d/%Y")
        self.date_to   = datetime.now().strftime("%m/%d/%Y")
        self.session   = requests.Session()
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        })

    # ------------------------------------------------------------------
    def _get_with_retry(self, url: str, **kwargs):
        for attempt in range(1, RETRY_ATTEMPTS + 1):
            try:
                r = self.session.get(url, timeout=30, **kwargs)
                r.raise_for_status()
                return r
            except Exception as exc:
                log.warning("GET %s attempt %d: %s", url, attempt, exc)
                if attempt < RETRY_ATTEMPTS:
                    time.sleep(RETRY_DELAY)
        return None

    def _post_with_retry(self, url: str, data: dict, **kwargs):
        for attempt in range(1, RETRY_ATTEMPTS + 1):
            try:
                r = self.session.post(url, data=data, timeout=30, **kwargs)
                r.raise_for_status()
                return r
            except Exception as exc:
                log.warning("POST %s attempt %d: %s", url, attempt, exc)
                if attempt < RETRY_ATTEMPTS:
                    time.sleep(RETRY_DELAY)
        return None

    # ------------------------------------------------------------------
    def _accept_disclaimer(self) -> bool:
        """GET disclaimer page and POST the acceptance form."""
        r = self._get_with_retry(CLERK_DISCLAIMER)
        if r is None:
            return False
        soup = BeautifulSoup(r.text, "lxml")

        # Extract hidden form fields (__VIEWSTATE etc.)
        form_data = {}
        form = soup.find("form")
        if form:
            for inp in form.find_all("input"):
                name  = inp.get("name", "")
                value = inp.get("value", "")
                if name:
                    form_data[name] = value

        # Set the accept field
        for key in list(form_data.keys()):
            lower = key.lower()
            if "accept" in lower or "continue" in lower or "agree" in lower:
                form_data[key] = "I Accept"
        if not form_data:
            # Nothing to post; maybe no disclaimer
            return True

        action = form.get("action", CLERK_DISCLAIMER) if form else CLERK_DISCLAIMER
        if not action.startswith("http"):
            action = CLERK_BASE + action

        r2 = self._post_with_retry(action, form_data)
        if r2 is None:
            return False
        log.info("Disclaimer accepted (HTTP).")
        return True

    # ------------------------------------------------------------------
    def _search_doc_type(self, code: str, keyword: str) -> list[dict]:
        """POST search form for one doc type and scrape all pages."""
        search_url = f"{CLERK_BASE}/web/search/DOCSEARCH3S1"
        r = self._get_with_retry(search_url)
        if r is None:
            return []

        soup = BeautifulSoup(r.text, "lxml")
        form_data = {}
        form = soup.find("form")
        if form:
            for inp in form.find_all("input"):
                name  = inp.get("name", "")
                value = inp.get("value", "")
                if name:
                    form_data[name] = value
            for sel in form.find_all("select"):
                name = sel.get("name", "")
                if name:
                    opt = sel.find("option", selected=True)
                    form_data[name] = opt["value"] if opt else ""

        # Fill search criteria
        for key in list(form_data.keys()):
            lower_key = key.lower()
            if "from" in lower_key and "date" in lower_key:
                form_data[key] = self.date_from
            elif "to" in lower_key and "date" in lower_key:
                form_data[key] = self.date_to
            elif "doctype" in lower_key or "description" in lower_key:
                form_data[key] = keyword
            elif "btnSearch" in key or "search" in lower_key:
                form_data[key] = "Search"

        action = form.get("action", search_url) if form else search_url
        if not action.startswith("http"):
            action = CLERK_BASE + action

        results = []
        page_num = 0

        while True:
            page_num += 1
            r2 = self._post_with_retry(action, form_data)
            if r2 is None:
                break

            page_soup = BeautifulSoup(r2.text, "lxml")
            page_records = self._parse_table(page_soup, code, keyword)
            results.extend(page_records)
            log.debug("  HTTP page %d: %d rows for %s", page_num, len(page_records), code)

            # Pagination: look for __doPostBack next-page call
            next_link = page_soup.find("a", string=re.compile(r"Next|>$", re.I))
            if next_link:
                onclick = next_link.get("href", "")
                m = re.search(r"__doPostBack\('([^']+)',\s*'([^']*)'\)", onclick)
                if m:
                    form_data["__EVENTTARGET"]   = m.group(1)
                    form_data["__EVENTARGUMENT"] = m.group(2)
                    # Update __VIEWSTATE etc. from current page
                    for inp in page_soup.find_all("input", {"type": "hidden"}):
                        n = inp.get("name", "")
                        if n:
                            form_data[n] = inp.get("value", "")
                    continue
            break

            if page_num > 50:
                break

        return results

    # ------------------------------------------------------------------
    def _parse_table(self, soup: BeautifulSoup, code: str, keyword: str) -> list[dict]:
        """Parse result table from a soup object."""
        records = []
        for table in soup.find_all("table"):
            headers = [th.get_text(strip=True).lower() for th in table.find_all("th")]
            if not any(h in headers for h in ("document", "doc", "filed", "grantor")):
                continue
            for tr in table.find_all("tr")[1:]:
                cells = tr.find_all("td")
                if not cells:
                    continue
                texts = [c.get_text(strip=True) for c in cells]
                doc_url = ""
                doc_num = ""
                for cell in cells:
                    a = cell.find("a", href=True)
                    if a:
                        doc_num = a.get_text(strip=True)
                        doc_url = a["href"]
                        if not doc_url.startswith("http"):
                            doc_url = CLERK_BASE + doc_url
                        break
                if not doc_num:
                    doc_num = texts[0] if texts else ""

                filed    = texts[2] if len(texts) > 2 else ""
                grantor  = texts[4] if len(texts) > 4 else ""
                grantee  = texts[5] if len(texts) > 5 else ""
                legal    = texts[6] if len(texts) > 6 else ""
                amount   = texts[7] if len(texts) > 7 else ""

                filed_norm = ""
                for fmt in ("%m/%d/%Y", "%m-%d-%Y", "%Y-%m-%d"):
                    try:
                        filed_norm = datetime.strptime(filed.strip(), fmt).strftime("%Y-%m-%d")
                        break
                    except ValueError:
                        pass

                records.append({
                    "doc_num"     : safe_str(doc_num),
                    "doc_type"    : code,
                    "filed"       : filed_norm,
                    "cat"         : DOC_TYPES.get(code, ("", "other", ""))[1],
                    "cat_label"   : DOC_TYPES.get(code, ("", "", keyword))[2],
                    "owner"       : safe_str(grantor),
                    "grantee"     : safe_str(grantee),
                    "amount"      : parse_amount(amount),
                    "legal"       : safe_str(legal),
                    "prop_address": "",
                    "prop_city"   : "",
                    "prop_state"  : "TX",
                    "prop_zip"    : "",
                    "mail_address": "",
                    "mail_city"   : "",
                    "mail_state"  : "TX",
                    "mail_zip"    : "",
                    "clerk_url"   : doc_url or f"{CLERK_DOC_BASE}/{doc_num}",
                    "flags"       : [],
                    "score"       : 0,
                })
        return records

    # ------------------------------------------------------------------
    def run(self) -> list[dict]:
        log.info("Starting HTTP scrape …  date range: %s → %s", self.date_from, self.date_to)
        if not self._accept_disclaimer():
            log.warning("Disclaimer acceptance failed; attempting to continue anyway.")

        all_records: list[dict] = []
        for code, (keyword, cat, label) in DOC_TYPES.items():
            try:
                recs = self._search_doc_type(code, keyword)
                all_records.extend(recs)
            except Exception as exc:
                log.error("Error scraping %s (HTTP): %s", code, exc)

        log.info("Total raw records (HTTP): %d", len(all_records))
        return all_records


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

def deduplicate(records: list[dict]) -> list[dict]:
    """Remove duplicate doc_num entries, keeping the first occurrence."""
    seen = set()
    out  = []
    for r in records:
        key = r.get("doc_num", "").strip()
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        out.append(r)
    return out


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def enrich_and_score(records: list[dict], parcel: ParcelLookup) -> list[dict]:
    """Add parcel address data and compute scores for all records."""
    for r in records:
        owner = r.get("owner", "")
        if owner:
            p = parcel.lookup(owner)
            if p:
                for k in ("prop_address","prop_city","prop_state","prop_zip",
                          "mail_address","mail_city","mail_state","mail_zip"):
                    if p.get(k) and not r.get(k):
                        r[k] = p[k]

    for r in records:
        score, flags = compute_score(r, records)
        r["score"] = score
        r["flags"] = flags

    return records


def write_json(records: list[dict], out_path: Path, date_from: str, date_to: str):
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "fetched_at" : datetime.utcnow().isoformat() + "Z",
        "source"     : "Brazoria County Clerk / Brazoria Central Appraisal District",
        "date_range" : f"{date_from} to {date_to}",
        "total"      : len(records),
        "with_address": sum(1 for r in records if r.get("prop_address") or r.get("mail_address")),
        "records"    : records,
    }
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    log.info("Written %d records → %s", len(records), out_path)


def write_csv(records: list[dict], out_path: Path):
    """Write GHL-compatible CSV export."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    columns = [
        "First Name", "Last Name",
        "Mailing Address", "Mailing City", "Mailing State", "Mailing Zip",
        "Property Address", "Property City", "Property State", "Property Zip",
        "Lead Type", "Document Type", "Date Filed", "Document Number",
        "Amount/Debt Owed", "Seller Score", "Motivated Seller Flags",
        "Source", "Public Records URL",
    ]
    with out_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns)
        writer.writeheader()
        for r in records:
            first, last = split_name(r.get("owner", ""))
            writer.writerow({
                "First Name"          : first,
                "Last Name"           : last,
                "Mailing Address"     : r.get("mail_address", ""),
                "Mailing City"        : r.get("mail_city", ""),
                "Mailing State"       : r.get("mail_state", "TX"),
                "Mailing Zip"         : r.get("mail_zip", ""),
                "Property Address"    : r.get("prop_address", ""),
                "Property City"       : r.get("prop_city", ""),
                "Property State"      : r.get("prop_state", "TX"),
                "Property Zip"        : r.get("prop_zip", ""),
                "Lead Type"           : r.get("cat_label", ""),
                "Document Type"       : r.get("doc_type", ""),
                "Date Filed"          : r.get("filed", ""),
                "Document Number"     : r.get("doc_num", ""),
                "Amount/Debt Owed"    : r.get("amount", ""),
                "Seller Score"        : r.get("score", 0),
                "Motivated Seller Flags": "; ".join(r.get("flags", [])),
                "Source"              : "Brazoria County Clerk",
                "Public Records URL"  : r.get("clerk_url", ""),
            })
    log.info("Written CSV → %s", out_path)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main_async():
    date_from = (datetime.now() - timedelta(days=LOOKBACK_DAYS)).strftime("%m/%d/%Y")
    date_to   = datetime.now().strftime("%m/%d/%Y")

    # 1. Scrape clerk records
    if PLAYWRIGHT_AVAILABLE:
        scraper  = ClerkScraper(LOOKBACK_DAYS)
        records  = await scraper.run()
        if not records:
            log.info("Playwright returned no records; falling back to HTTP scraper.")
            records = ClerkHTTPScraper(LOOKBACK_DAYS).run()
    else:
        log.warning("Playwright not available – using HTTP scraper.")
        records = ClerkHTTPScraper(LOOKBACK_DAYS).run()

    records = deduplicate(records)
    log.info("Deduplicated records: %d", len(records))

    # 2. Load parcel data
    parcel = ParcelLookup()
    parcel.load()

    # 3. Enrich + score
    records = enrich_and_score(records, parcel)

    # Sort by score descending
    records.sort(key=lambda r: r.get("score", 0), reverse=True)

    # 4. Write outputs
    write_json(records, DASH_OUT, date_from, date_to)
    write_json(records, DATA_OUT, date_from, date_to)
    write_csv(records, CSV_OUT)

    log.info("=" * 60)
    log.info("DONE.  %d leads  |  %d with address  |  date range: %s → %s",
             len(records),
             sum(1 for r in records if r.get("prop_address") or r.get("mail_address")),
             date_from, date_to)
    log.info("=" * 60)


def main():
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
