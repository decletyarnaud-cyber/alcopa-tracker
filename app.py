#!/usr/bin/env python3
"""
Alcopa Auction Tracker - Marseille/Vitrolles
Find vehicles with the least contrôle technique (CT) defects
"""

import re
import json
import time
import io
import os
import atexit
from datetime import datetime
from flask import Flask, render_template, jsonify, request
import requests
from bs4 import BeautifulSoup

try:
    import PyPDF2
    PDF_AVAILABLE = True
except ImportError:
    PDF_AVAILABLE = False
    print("PyPDF2 not available - CT PDF parsing disabled")

try:
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger
    SCHEDULER_AVAILABLE = True
except ImportError:
    SCHEDULER_AVAILABLE = False
    print("APScheduler not available - automatic scraping disabled")

app = Flask(__name__)

# Data file for persistence
DATA_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(DATA_DIR, "vehicle_data.json")

# Configuration
BASE_URL = "https://www.alcopa-auction.fr"
SEARCH_URL = f"{BASE_URL}/recherche"
CALENDAR_URL = f"{BASE_URL}/calendrier-des-ventes"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
}

# Target location - ONLY scrape vehicles from this city
TARGET_LOCATION = "marseille"
# Allowed variations of the target location name.
# Since we now discover ALL sales and filter by vehicle location,
# this list must be comprehensive to catch every vehicle physically
# located in our target area regardless of the sale name.
# Includes "sud web" (Alcopa's online flash-sale hub for the south).
LOCATION_VARIANTS = [
    "marseille",
    "vitrolles",
    "marignane",
    "sud web",
    "sud-web",
    "sud de la france",
    "paca",
    "provence",
    "aix",
    "aix-en-provence",
    "aix en provence",
    "cote d'azur",
    "côte d'azur",
    "var",
    "toulon",
    "la seyne",
]


# Cache for scraped data
vehicle_cache = {
    "vehicles": [],
    "last_updated": None,
    "location": "marseille"
}


def _init_data():
    """Initialize data - called at module import"""
    global vehicle_cache
    try:
        if os.path.exists(DATA_FILE):
            with open(DATA_FILE, 'r', encoding='utf-8') as f:
                vehicle_cache = json.load(f)
            print(f"[INIT] Loaded {len(vehicle_cache.get('vehicles', []))} vehicles from {DATA_FILE}")
    except Exception as e:
        print(f"[INIT] Error loading data: {e}")


# Load data at module import (works with both 'flask run' and 'python app.py')
_init_data()


def save_data():
    """Save vehicle data to JSON file"""
    try:
        with open(DATA_FILE, 'w', encoding='utf-8') as f:
            json.dump(vehicle_cache, f, ensure_ascii=False, indent=2)
        print(f"Data saved to {DATA_FILE}")
    except Exception as e:
        print(f"Error saving data: {e}")


def load_data():
    """Load vehicle data from JSON file"""
    global vehicle_cache
    try:
        if os.path.exists(DATA_FILE):
            with open(DATA_FILE, 'r', encoding='utf-8') as f:
                vehicle_cache = json.load(f)
            print(f"Loaded {len(vehicle_cache.get('vehicles', []))} vehicles from {DATA_FILE}")
            print(f"Last updated: {vehicle_cache.get('last_updated', 'Never')}")
    except Exception as e:
        print(f"Error loading data: {e}")


def scheduled_scrape():
    """Scheduled scraping job - runs daily at 6 AM"""
    print(f"\n{'='*50}")
    print(f"SCHEDULED SCRAPE STARTED at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*50}\n")

    location = vehicle_cache.get("location", "marseille")

    try:
        vehicles = scrape_vehicle_list(location=location, max_pages=5)

        # Sort by CT defects (least first)
        vehicles.sort(key=lambda v: (
            v.get("ct_defects", {}).get("total", 999),
            -int(v.get("year", "0") or "0")
        ))

        # Remove duplicates
        seen_urls = set()
        unique_vehicles = []
        for v in vehicles:
            if v["url"] not in seen_urls:
                seen_urls.add(v["url"])
                unique_vehicles.append(v)

        vehicle_cache["vehicles"] = unique_vehicles
        vehicle_cache["last_updated"] = datetime.now().isoformat()
        vehicle_cache["location"] = location

        save_data()

        print(f"\n{'='*50}")
        print(f"SCHEDULED SCRAPE COMPLETED: {len(unique_vehicles)} vehicles")
        print(f"{'='*50}\n")

    except Exception as e:
        print(f"Scheduled scrape failed: {e}")


def get_session():
    """Create a requests session with proper headers"""
    session = requests.Session()
    session.headers.update(HEADERS)
    return session


def find_all_sales(session):
    """Find ALL active Alcopa sales (salle + flash) with no name filter.

    We used to filter sales by name (e.g. "Marseille" in title), which missed
    flash sales called "Vente Express", "Vente Internet", etc. that contain
    vehicles located in Marseille/Vitrolles/Sud Web. Now we discover EVERY
    sale, then filter by individual vehicle location in scrape_sale_vehicles().

    Alcopa sale types:
    - /vente-encheres-en-ligne/XXXXX - Online flash auctions
    - /salle-de-vente-encheres/CITY/XXXXX - Physical auction hall sales
    """
    sales = []
    seen_ids = set()

    def extract_sales_from_page_no_filter(soup, source_name):
        """Extract ALL sale links from a page, no location filter."""
        found = 0

        # Pattern 1: Physical auction hall - /salle-de-vente-encheres/ANYCITY/XXXXX
        # We now accept ANY city, because a vehicle physically located at
        # Marseille may belong to a salle listed under a different hub name.
        salle_links = soup.find_all("a", href=re.compile(r"/salle-de-vente-encheres/[^/]+/\d+"))
        for link in salle_links:
            href = link.get("href", "")
            match = re.search(r"/salle-de-vente-encheres/(marseille|vitrolles|marignane|sud-web|sud|aix-en-provence|toulon|multisite|national|province|hub)/(\d+)", href)
            if match:
                city_slug = match.group(1)
                sale_id = match.group(2)
                if sale_id in seen_ids:
                    continue
                seen_ids.add(sale_id)
                sale_url = f"{BASE_URL}/salle-de-vente-encheres/{city_slug}/{sale_id}"

                parent = link.find_parent(["tr", "div", "article", "section", "li"])
                link_text = link.get_text(strip=True)
                if link_text and len(link_text) > 5 and "voir la liste" not in link_text.lower():
                    sale_name = link_text
                else:
                    sale_name = f"Vente en Salle {city_slug.title()}"

                # Extract date from context
                context = parent.get_text(" ", strip=True) if parent else ""
                date_match = re.search(
                    r"(\d{1,2})\s+(janvier|février|mars|avril|mai|juin|juillet|août|septembre|octobre|novembre|décembre)\s+(\d{4})",
                    context, re.I
                )
                sale_date = f"{date_match.group(1)} {date_match.group(2)} {date_match.group(3)}" if date_match else ""

                sales.append({
                    "id": sale_id,
                    "url": sale_url,
                    "name": sale_name,
                    "date": sale_date,
                    "source": source_name,
                    "type": "salle"
                })
                found += 1
                print(f"  Found salle: #{sale_id} - {sale_name[:40]}{'...' if len(sale_name) > 40 else ''} ({source_name})")

        # Pattern 2: Online/Flash auctions - /vente-encheres-en-ligne/XXXXX
        # Accept EVERY flash sale — even if its calendar text doesn't mention
        # Marseille, it may still contain Marseille-located vehicles.
        online_links = soup.find_all("a", href=re.compile(r"/vente-encheres-en-ligne/\d+"))
        for link in online_links:
            href = link.get("href", "")
            match = re.search(r"/vente-encheres-en-ligne/(\d+)", href)
            if not match:
                continue

            sale_id = match.group(1)
            if sale_id in seen_ids:
                continue

            seen_ids.add(sale_id)
            sale_url = f"{BASE_URL}/vente-encheres-en-ligne/{sale_id}?site=internet"

            # Extract a readable name from calendar context if possible
            parent = link.find_parent(["div", "article", "section", "li"])
            context = parent.get_text(" ", strip=True) if parent else ""
            if context and len(context) > 10:
                # Clean up
                sale_name = context.replace("&amp;", "&")
                sale_name = re.sub(r'\s+', ' ', sale_name)
                sale_name = sale_name[:60]
            else:
                sale_name = f"Flash Sale #{sale_id}"

            # Extract date from context
            date_match = re.search(
                r"(\d{1,2})\s+(janvier|février|mars|avril|mai|juin|juillet|août|septembre|octobre|novembre|décembre)\s+(\d{4})",
                context, re.I
            )
            sale_date = f"{date_match.group(1)} {date_match.group(2)} {date_match.group(3)}" if date_match else ""

            sales.append({
                "id": sale_id,
                "url": sale_url,
                "name": sale_name,
                "date": sale_date,
                "source": source_name,
                "type": "flash"
            })
            found += 1
            print(f"  Found flash: #{sale_id} - {sale_name[:50]}{'...' if len(sale_name) > 50 else ''} ({source_name})")

        return found

    def discover_all_flash_sales(session):
        """Discover ALL flash sales from calendar with no location filter."""
        found = 0
        try:
            response = session.get(CALENDAR_URL, timeout=30)
            soup = BeautifulSoup(response.text, "lxml")

            flash_entries = soup.find_all("a", href=re.compile(r"/vente-encheres-en-ligne/\d+"))
            print(f"  Calendar has {len(flash_entries)} flash-sale links total")

            # Deduplicate by sale_id
            unique_flash_ids = set()
            sale_info_map = {}
            # Process ALL flash sales from calendar
            flash_to_process = flash_entries
            print(f"  Processing {len(flash_to_process)} flash sales from calendar")
            for link in flash_to_process:
                href = link.get("href", "")
                match = re.search(r"/vente-encheres-en-ligne/(\d+)", href)
                if not match:
                    continue
                sale_id = match.group(1)
                if sale_id in seen_ids or sale_id in unique_flash_ids:
                    continue
                unique_flash_ids.add(sale_id)

                parent = link.find_parent(["div"])
                if not parent:
                    parent = link.find_parent(["li", "article", "section"])
                context = parent.get_text(" ", strip=True) if parent else ""
                sale_info_map[sale_id] = context

                seen_ids.add(sale_id)
                sale_url = f"{BASE_URL}/vente-encheres-en-ligne/{sale_id}?site=internet"

                # Extract date from context
                date_match = re.search(
                    r"(\d{1,2})\s+(janvier|février|mars|avril|mai|juin|juillet|août|septembre|octobre|novembre|décembre)\s+(\d{4})",
                    context, re.I
                )
                sale_date = f"{date_match.group(1)} {date_match.group(2)} {date_match.group(3)}" if date_match else ""

                # Build name
                if context and len(context) > 10:
                    clean_name = context.replace("&amp;", "&")
                    clean_name = re.sub(r'\s+', ' ', clean_name)[:60]
                else:
                    clean_name = f"Flash Sale #{sale_id}"

                sales.append({
                    "id": sale_id,
                    "url": sale_url,
                    "name": clean_name,
                    "date": sale_date,
                    "source": "calendar-flash",
                    "type": "flash"
                })
                found += 1

            print(f"  Discovered {found} unique flash sales")
            if sale_info_map:
                print(f"  Sample names: {list(sale_info_map.values())[:3]}")

        except Exception as e:
            print(f"  Error discovering flash sales: {e}")
            import traceback
            traceback.print_exc()

        return found

    # Method 0: Add explicitly known sales from user intelligence
    # These are sales that we know contain Marseille/Vitrolles vehicles
    # but are sometimes missed by automatic calendar discovery
    explicit_flash_ids = [12088]  # Known flash sale with Vitrolles vehicles
    for fid in explicit_flash_ids:
        if str(fid) not in seen_ids:
            seen_ids.add(str(fid))
            sales.append({
                "id": str(fid),
                "url": f"{BASE_URL}/vente-encheres-en-ligne/{fid}?site=internet",
                "name": f"Flash Sale #{fid} (explicit)",
                "date": "",
                "source": "explicit-user",
                "type": "flash"
            })
            print(f"  Added explicit flash sale: #{fid}")

    # Also check /vente-encheres-en-ligne/All or listing page for ALL flash sales
    print("Scanning flash sale listing page...")
    try:
        # Alcopa might have a page listing all active flash sales
        for page_num in range(1, 5):
            list_url = f"{BASE_URL}/vente-encheres-en-ligne"
            if page_num > 1:
                list_url += f"?page={page_num}"
            resp = session.get(list_url, timeout=30)
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.text, "lxml")
                flash_links = soup.find_all("a", href=re.compile(r"/vente-encheres-en-ligne/\d+"))
                found_on_page = 0
                for flink in flash_links:
                    fhref = flink.get("href", "")
                    fmatch = re.search(r"/vente-encheres-en-ligne/(\d+)", fhref)
                    if fmatch:
                        fsale_id = fmatch.group(1)
                        if fsale_id not in seen_ids:
                            seen_ids.add(fsale_id)
                            sales.append({
                                "id": fsale_id,
                                "url": f"{BASE_URL}/vente-encheres-en-ligne/{fsale_id}?site=internet",
                                "name": f"Flash Sale #{fsale_id} (listing)",
                                "date": "",
                                "source": "flash-listing",
                                "type": "flash"
                            })
                            found_on_page += 1
                print(f"  Flash listing page {page_num}: {found_on_page} new sales")
                if found_on_page == 0:
                    break
    except Exception as e:
        print(f"  Flash listing scan failed: {e}")

    try:
        # Method 1: Calendar page (best source for both salle + flash)
        print("Fetching Alcopa calendar (discovering ALL sales)...")
        try:
            response = session.get(CALENDAR_URL, timeout=30)
            if response.status_code == 200 and len(response.text) > 5000:
                soup = BeautifulSoup(response.text, "lxml")
                found = extract_sales_from_page_no_filter(soup, "calendar")
                print(f"  Calendar: {found} sales found")
        except Exception as e:
            print(f"  Calendar fetch failed: {e}")

        # Method 1b: Explicit salle sales (multisite, etc.)
        explicit_salles = [
            ("multisite", "10296"),  # Motos Marseille
        ]
        for city_slug, sale_id in explicit_salles:
            if sale_id not in seen_ids:
                seen_ids.add(sale_id)
                sales.append({
                    "id": sale_id,
                    "url": f"{BASE_URL}/salle-de-vente-encheres/{city_slug}/{sale_id}",
                    "name": f"Vente en Salle {city_slug.title()}",
                    "date": "",
                    "source": "explicit-salle",
                    "type": "salle"
                })
                print(f"  Added explicit salle: {city_slug}/{sale_id}")

        # Method 2: Discover all remaining flash sales from calendar
        print("Discovering remaining flash sales from calendar...")
        found = discover_all_flash_sales(session)

        # Method 3: Any salle page with location slug (covers cities that might be hubs)
        # We try Marseille directly since it's our primary interest
        print("Checking Marseille/Vitrolles salle page...")
        for slug in ["marseille", "vitrolles", "sud-web", "sud", "internet"]:
            try:
                salle_url = f"{BASE_URL}/salle-de-vente-encheres/{slug}"
                response = session.get(salle_url, timeout=30)
                if response.status_code == 200:
                    soup = BeautifulSoup(response.text, "lxml")
                    found = extract_sales_from_page_no_filter(soup, f"salle-{slug}")
                    if found > 0:
                        print(f"  Salle {slug}: {found} additional sales")
            except Exception:
                pass  # 404 expected for non-existent slugs

        # Method 4: Homepage as fallback
        print("Fetching Alcopa homepage...")
        try:
            response = session.get(BASE_URL, timeout=30)
            if response.status_code == 200 and len(response.text) > 5000:
                soup = BeautifulSoup(response.text, "lxml")
                found = extract_sales_from_page_no_filter(soup, "homepage")
                if found > 0:
                    print(f"  Homepage: {found} additional sales")
        except Exception as e:
            print(f"  Homepage fetch failed: {e}")

        print(f"TOTAL SALES DISCOVERED: {len(sales)} ({sum(1 for s in sales if s['type']=='salle')} salle, {sum(1 for s in sales if s['type']=='flash')} flash)")

    except Exception as e:
        print(f"Error discovering sales: {e}")
        import traceback
        traceback.print_exc()

    return sales


EXCLUDED_LOCATIONS = ["paris", "nancy", "rennes", "beauvais", "lyon", "tours", "lille", "bordeaux", "strasbourg", "nantes", "nice"]

def is_marseille_location(location_str):
    """Check if a location string indicates Marseille area"""
    if not location_str:
        return False
    location_lower = location_str.lower().strip()
    # Explicitly reject known non-Marseille cities
    if any(excl in location_lower for excl in EXCLUDED_LOCATIONS):
        return False
    return any(variant in location_lower for variant in LOCATION_VARIANTS)


def scrape_sale_vehicles(sale_url, session, max_pages=20, sale_id=None):
    """Scrape all vehicle URLs from a specific auction sale page

    Handles both:
    - /salle-de-vente-encheres/marseille/XXXXX (paginated with ?page=N)
    - /vente-encheres-en-ligne/XXXXX (scrape directly from flash sale page)
    """
    # Regex matching both /voiture-occasion/ and /utilitaire-occasion/ links
    VEHICLE_LINK_RE = re.compile(r"/(voiture|utilitaire|moto)-occasion/[^/]+/[^/]+-\d+")

    result = {
        "vehicle_urls": set(),
        "sale_end_date": None,
        "sale_location": None,
        "sale_name": None
    }

    try:
        is_salle = "/salle-de-vente-encheres/" in sale_url
        is_flash = "/vente-encheres-en-ligne/" in sale_url
        print(f"Scraping {'salle' if is_salle else 'flash'} sale: {sale_url}")

        if is_salle:
            # Salle pages are paginated - scrape all pages
            for page in range(1, max_pages + 1):
                page_url = f"{sale_url}?page={page}"
                response = session.get(page_url, timeout=30)
                response.raise_for_status()
                soup = BeautifulSoup(response.text, "lxml")

                # Find vehicle links (both voiture and utilitaire)
                links = soup.find_all("a", href=VEHICLE_LINK_RE)
                page_urls = set()
                for link in links:
                    href = link.get("href", "")
                    if href:
                        if not href.startswith("http"):
                            href = BASE_URL + href
                        page_urls.add(href)

                # Check if we got new vehicles
                new_urls = page_urls - result["vehicle_urls"]
                if not new_urls:
                    print(f"  Page {page}: no new vehicles, stopping")
                    break

                result["vehicle_urls"].update(page_urls)
                print(f"  Page {page}: {len(new_urls)} new vehicles (total: {len(result['vehicle_urls'])})")

                # Extract sale info from first page
                if page == 1:
                    page_text = soup.get_text(" ", strip=True)
                    # Extract actual location from page text instead of hardcoding
                    loc_match = re.search(
                        r'(?:salle|vente)\s+(?:de|en)\s+([A-Za-zÀ-ÿ\s-]+?)(?:\s*[-–]\s*|\s+\d|$)',
                        page_text, re.I
                    )
                    if loc_match:
                        result["sale_location"] = loc_match.group(1).strip()
                    else:
                        # Fallback: look for any known location variant in page text
                        for variant in LOCATION_VARIANTS:
                            if variant in page_text.lower():
                                result["sale_location"] = variant.title()
                                break
                        else:
                            result["sale_location"] = None

                    # Try to find sale date
                    date_match = re.search(r'(\d{1,2})\s+(janvier|février|mars|avril|mai|juin|juillet|août|septembre|octobre|novembre|décembre)\s+(\d{4})', page_text, re.I)
                    if date_match:
                        result["sale_end_date"] = f"{date_match.group(1)} {date_match.group(2)} {date_match.group(3)}"
                        print(f"  Sale date: {result['sale_end_date']}")

                time.sleep(0.3)  # Be nice to server

        else:
            # Flash/Online sale - scrape directly from the flash sale page
            # The page renders vehicle links as /utilitaire-occasion/ or /voiture-occasion/
            if not sale_id:
                match = re.search(r'/vente-encheres-en-ligne/(\d+)', sale_url)
                sale_id = match.group(1) if match else None

            if sale_id:
                for page in range(1, max_pages + 1):
                    page_url = f"{sale_url}&page={page}" if "?" in sale_url else f"{sale_url}?page={page}"
                    response = session.get(page_url, timeout=30)
                    if response.status_code != 200:
                        break
                    soup = BeautifulSoup(response.text, "lxml")

                    # Extract flash start date from data attribute (Unix timestamp)
                    if page == 1:
                        flash_header = soup.find(attrs={"data-flash-sale-header-start-value": True})
                        if flash_header:
                            try:
                                ts = int(flash_header["data-flash-sale-header-start-value"])
                                flash_dt = datetime.fromtimestamp(ts)
                                result["sale_end_date"] = flash_dt.strftime("%-d %B %Y %H:%M").replace(
                                    "January", "janvier").replace("February", "février").replace(
                                    "March", "mars").replace("April", "avril").replace(
                                    "May", "mai").replace("June", "juin").replace(
                                    "July", "juillet").replace("August", "août").replace(
                                    "September", "septembre").replace("October", "octobre").replace(
                                    "November", "novembre").replace("December", "décembre")
                                print(f"  Flash start: {result['sale_end_date']}")
                            except (ValueError, KeyError):
                                pass

                    # Find vehicle links (both voiture and utilitaire)
                    links = soup.find_all("a", href=VEHICLE_LINK_RE)
                    page_urls = set()
                    for link in links:
                        href = link.get("href", "")
                        if href:
                            if not href.startswith("http"):
                                href = BASE_URL + href
                            href = href.split("?")[0]
                            page_urls.add(href)

                    new_urls = page_urls - result["vehicle_urls"]
                    if not new_urls:
                        print(f"  Page {page}: no new vehicles, stopping")
                        break

                    result["vehicle_urls"].update(page_urls)
                    print(f"  Page {page}: {len(new_urls)} new vehicles (total: {len(result['vehicle_urls'])})")
                    time.sleep(0.3)

                # Flash sale: don't hardcode location, each vehicle has its own
                result["sale_location"] = None

        print(f"  Total: {len(result['vehicle_urls'])} vehicles in this sale")

    except Exception as e:
        print(f"Error scraping sale {sale_url}: {e}")
        import traceback
        traceback.print_exc()

    return result


def scrape_vehicle_list(location="marseille", max_pages=20, use_sales_approach=True):
    """Scrape vehicle listings from Marseille auction sales

    Two approaches:
    1. Sales-based (preferred): Find Marseille sales, scrape only those
    2. Search-based (fallback): Use search with location filter, then verify each vehicle

    Both approaches filter vehicles by location to ensure only Marseille vehicles.
    """
    session = get_session()
    vehicle_data = []  # List of (url, sale_end_date) tuples
    all_sale_ids = set()

    # Try to find Marseille-specific sales first
    sale_info_map = {}  # Map vehicle URL to sale info (type, id, name, date)

    if use_sales_approach:
        print("\n=== FINDING MARSEILLE SALES ===")
        all_sales = find_all_sales(session)

        if all_sales:
            # Process salle sales first, then flash (so salle info is preserved for shared URLs)
            all_sales.sort(key=lambda s: 0 if s.get("type") == "salle" else 1)
            print(f"\nScraping {len(all_sales)} Marseille sales directly...")
            for sale in all_sales:
                all_sale_ids.add(sale["id"])
                sale_type = sale.get("type", "salle")  # salle or flash
                sale_info = {
                    "type": sale_type,
                    "id": sale["id"],
                    "name": sale.get("name", f"Vente #{sale['id']}"),
                    "date": sale.get("date", "")
                }
                result = scrape_sale_vehicles(sale["url"], session)
                for url in result.get("vehicle_urls", []):
                    vehicle_data.append((url, result.get("sale_end_date")))
                    # Only set sale info for first occurrence (don't overwrite salle with flash)
                    if url not in sale_info_map:
                        sale_info_map[url] = sale_info
                time.sleep(0.5)

            if vehicle_data:
                flash_count = sum(1 for url, _ in vehicle_data if sale_info_map.get(url, {}).get("type") == "flash")
                salle_count = len(vehicle_data) - flash_count
                print(f"Found {len(vehicle_data)} vehicles ({salle_count} salle, {flash_count} flash)")

    # Fallback to search if no sales found or no vehicles
    if not vehicle_data:
        print(f"\n=== USING SEARCH FALLBACK (max {max_pages} pages) ===")
        print(f"Note: Will filter by location={TARGET_LOCATION} after scraping")

        for page in range(1, max_pages + 1):
            params = {"salle[]": location, "page": page}
            try:
                print(f"  Page {page}...", end=" ")
                response = session.get(SEARCH_URL, params=params, timeout=30)
                response.raise_for_status()
                soup = BeautifulSoup(response.text, "lxml")

                # Find all vehicle links (both voiture and utilitaire)
                links = soup.find_all("a", href=re.compile(r"/(voiture|utilitaire)-occasion/[^/]+/[^/]+-\d+"))

                if not links:
                    print("no more vehicles")
                    break

                page_urls = set()
                for link in links:
                    href = link.get("href", "")
                    if href and ("-occasion/" in href):
                        if not href.startswith("http"):
                            href = BASE_URL + href
                        href = href.split("?")[0]
                        page_urls.add(href)

                print(f"{len(page_urls)} vehicles")

                for url in page_urls:
                    vehicle_data.append((url, None))

                time.sleep(0.5)

            except Exception as e:
                print(f"Error on page {page}: {e}")
                break

    # Remove duplicates
    seen_urls = set()
    unique_vehicles = []
    for url, sale_end in vehicle_data:
        if url not in seen_urls:
            seen_urls.add(url)
            unique_vehicles.append((url, sale_end))

    print(f"\nTotal unique vehicle URLs found: {len(unique_vehicles)}")

    # Fetch details for each vehicle and FILTER BY LOCATION
    vehicles = []
    skipped_count = 0
    session = get_session()

    print(f"\n=== FETCHING VEHICLE DETAILS (filtering for {TARGET_LOCATION.upper()}) ===")

    for i, (url, sale_end) in enumerate(unique_vehicles):
        print(f"Fetching {i+1}/{len(unique_vehicles)}: {url.split('/')[-1]}", end=" ")
        vehicle = scrape_vehicle_details(url, session)

        if vehicle:
            vehicle_location = vehicle.get("location", "").lower().strip()

            # CRITICAL: Only include vehicles from Marseille area
            if is_marseille_location(vehicle_location):
                vehicle["sale_end_date"] = sale_end
                # Add sale info (type, id, name, date)
                sale_info = sale_info_map.get(url, {"type": "salle", "id": "unknown", "name": "Vente Salle", "date": ""})
                vehicle["sale_type"] = sale_info.get("type", "salle")
                vehicle["sale_id"] = sale_info.get("id", "unknown")
                vehicle["sale_name"] = sale_info.get("name", "Vente")
                vehicle["sale_url"] = url
                vehicles.append(vehicle)
                # Fetch market price for comparison
                if vehicle.get("price") and vehicle.get("brand"):
                    market = fetch_market_price(
                        vehicle["brand"], vehicle["model"],
                        vehicle.get("year"), vehicle.get("mileage"),
                        vehicle.get("fuel")
                    )
                    if market:
                        vehicle["market_price"] = market["market_price"]
                        vehicle["market_count"] = market["market_count"]
                        vehicle["market_sources"] = market["market_sources"]
                        try:
                            alcopa_price = float(vehicle["price"])
                            vehicle["market_diff_pct"] = round(
                                ((alcopa_price - market["market_price"]) / market["market_price"]) * 100
                            )
                        except (ValueError, ZeroDivisionError):
                            vehicle["market_diff_pct"] = None

                type_marker = "⚡" if vehicle["sale_type"] == "flash" else "✓"
                print(f"{type_marker} {vehicle_location}")
            else:
                skipped_count += 1
                print(f"✗ SKIPPED ({vehicle_location or 'unknown'})")
        else:
            print("✗ FAILED")

        time.sleep(0.5)

    # Count by sale type
    flash_count = sum(1 for v in vehicles if v.get("sale_type") == "flash")
    salle_count = len(vehicles) - flash_count

    print(f"\n=== RESULTS ===")
    print(f"Total scraped: {len(unique_vehicles)}")
    print(f"Marseille vehicles: {len(vehicles)}")
    if flash_count > 0:
        print(f"  ⚡ Flash (vente en ligne): {flash_count}")
    if salle_count > 0:
        print(f"  📍 Salle (vente physique): {salle_count}")
    print(f"Skipped (other cities): {skipped_count}")

    if len(vehicles) == 0 and len(unique_vehicles) > 0:
        print("\n⚠️  NO MARSEILLE VEHICLES CURRENTLY AVAILABLE")
        print("   All current vehicles are from other cities.")
        print("   Check the calendar for upcoming Marseille sales:")
        print("   https://www.alcopa-auction.fr/calendrier-des-ventes")

    return vehicles


def scrape_vehicle_details(vehicle_url, session=None):
    """Scrape detailed info from a vehicle page"""
    if session is None:
        session = get_session()

    try:
        response = session.get(vehicle_url, timeout=30)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "lxml")

        # Get full page text for parsing
        page_text = soup.get_text(" ", strip=True)

        vehicle = {
            "url": vehicle_url,
            "title": "",
            "brand": "",
            "model": "",
            "year": "",
            "mileage": "",
            "price": "",
            "fuel": "",
            "transmission": "",
            "location": "",
            "sale_date": "",
            "ct_defects": {
                "critiques": 0,
                "majeures": 0,
                "mineures": 0,
                "total": 0
            },
            "ct_pdf_url": "",
            "ct_details": [],
            "ct_result": "",
            "ct_type": "standard",
            "is_professional_only": False,
            "notes": []
        }

        # Check if vehicle is reserved for professionals (critical defects)
        # Must match EXACTLY "Véhicule réservé aux professionnels" - NOT the registration link
        if re.search(r"véhicule\s+réservé\s+aux\s+professionnels", page_text, re.IGNORECASE):
            vehicle["is_professional_only"] = True
            vehicle["notes"].append("Réservé aux professionnels (défauts critiques)")

        # Extract brand/model from URL
        match = re.search(r"/(voiture|utilitaire)-occasion/([^/]+)/(.+)-(\d+)$", vehicle_url)
        if match:
            vehicle["brand"] = match.group(2).upper().replace("-", " ")
            model_slug = match.group(3)
            # Clean up model name
            model_parts = model_slug.replace("-", " ").split()
            vehicle["model"] = " ".join(model_parts).title()
            vehicle["title"] = f"{vehicle['brand']} {vehicle['model']}"

        # Try to get title from page
        title_elem = soup.find("h1")
        if title_elem:
            title_text = title_elem.get_text(strip=True)
            if title_text:
                vehicle["title"] = title_text

        # Extract mileage - look for pattern like "183 846 km" or "183846 km"
        km_match = re.search(r"(\d[\d\s]{2,})\s*km", page_text, re.IGNORECASE)
        if km_match:
            km_str = km_match.group(1).replace(" ", "").replace("\xa0", "")
            # Sanity check - mileage should be reasonable (< 1 million)
            try:
                km_val = int(km_str)
                if km_val < 1000000:
                    vehicle["mileage"] = str(km_val)
            except:
                pass

        # Extract year - look for date patterns
        # Format: "24/10/2019" for circulation date
        date_match = re.search(r"(\d{2})/(\d{2})/(20[0-2]\d|19\d{2})", page_text)
        if date_match:
            vehicle["year"] = date_match.group(3)
        else:
            # Try simple year pattern
            year_match = re.search(r"\b(20[0-2]\d)\b", page_text)
            if year_match:
                vehicle["year"] = year_match.group(1)

        # Extract price - look for "Mise à prix" or "MAP" pattern
        # Format: "MAP : Mise à prix : 6 400 €" or "Mise à prix : 11 000 €"
        price_patterns = [
            r"mise à prix\s*:\s*(\d[\d\s\u00a0]*)\s*€",  # "Mise à prix : 6 400 €"
            r"MAP\s*:\s*(?:mise à prix\s*:\s*)?(\d[\d\s\u00a0]*)\s*€",  # "MAP : 6 400 €"
            r"(\d{1,3}(?:[\s\u00a0]\d{3})+)\s*€",  # "11 000 €" with space separators
            r"(\d{4,})\s*€",  # "11000 €" without separators
        ]

        for pattern in price_patterns:
            price_match = re.search(pattern, page_text, re.IGNORECASE)
            if price_match:
                price_str = price_match.group(1)
                # Remove all spaces and non-breaking spaces
                price_str = price_str.replace(" ", "").replace("\u00a0", "").replace("\xa0", "")
                try:
                    price_val = int(price_str)
                    # Sanity check - price should be reasonable (100 to 500000)
                    if 100 <= price_val <= 500000:
                        vehicle["price"] = str(price_val)
                        break
                except ValueError:
                    continue

        # Extract fuel type
        if re.search(r"\bGO\b|diesel", page_text, re.IGNORECASE):
            vehicle["fuel"] = "Diesel"
        elif re.search(r"\bES\b|essence", page_text, re.IGNORECASE):
            vehicle["fuel"] = "Essence"
        elif re.search(r"électrique|EL\b", page_text, re.IGNORECASE):
            vehicle["fuel"] = "Électrique"
        elif re.search(r"hybride", page_text, re.IGNORECASE):
            vehicle["fuel"] = "Hybride"

        # Extract transmission
        if re.search(r"automatique|auto\b|bva|tiptronic|dsg|edc", page_text, re.IGNORECASE):
            vehicle["transmission"] = "Automatique"
        elif re.search(r"manuelle|bvm", page_text, re.IGNORECASE):
            vehicle["transmission"] = "Manuelle"

        # Extract location - try multiple patterns
        location = None

        # Pattern 1: "Lieu de stockage BEAUVAIS" (most reliable - found in page)
        location_match = re.search(r"Lieu de stockage\s+([A-Z]+(?:\s+[A-Z]+)*)", page_text)
        if location_match:
            location = location_match.group(1).strip()

        # Pattern 2: "stockage MARSEILLE" or "Stockage : NANCY"
        if not location:
            location_match = re.search(r"(lieu de stockage|stockage)[:\s]+([A-Za-zÀ-ÿ\s-\.]+?)(?=\s*(?:Voir|MAP|EUR|\d+\s*€|Mise|Photos|Enchère|$))", page_text, re.IGNORECASE)
            if location_match:
                location = location_match.group(1).strip()

        # Pattern 3: City name in page title (e.g., "- Beauvais | Alcopa Auction")
        if not location:
            title_match = re.search(r"-\s*([A-Za-zÀ-ÿ]+)\s*\|\s*Alcopa", page_text)
            if title_match:
                city = title_match.group(1).strip()
                if city.lower() in ["marseille", "nancy", "rennes", "beauvais", "lyon", "tours", "paris"]:
                    location = city

        # Pattern 4: "Site : Marseille" or "Site: VITROLLES"
        if not location:
            site_match = re.search(r"site[:\s]+([A-Za-zÀ-ÿ\-]+)", page_text, re.IGNORECASE)
            if site_match:
                location = site_match.group(1).strip()

        if location:
            vehicle["location"] = location.title()

        # Look for CT PDF link
        ct_link = soup.find("a", href=re.compile(r"getDocument/ct/", re.IGNORECASE))
        if ct_link:
            ct_href = ct_link.get("href", "")
            if ct_href:
                if not ct_href.startswith("http"):
                    ct_href = BASE_URL + ct_href
                vehicle["ct_pdf_url"] = ct_href

                # Try to fetch and parse CT PDF info
                ct_info = fetch_ct_info(ct_href, session)
                if ct_info:
                    vehicle["ct_defects"] = ct_info.get("ct_defects", vehicle["ct_defects"])
                    vehicle["ct_details"] = ct_info.get("ct_details", [])
                    vehicle["ct_result"] = ct_info.get("ct_result", "")
                    vehicle["ct_type"] = ct_info.get("ct_type", "standard")

                    # Mark as professional only if critical defects
                    if ct_info.get("ct_result") == "critique" or ct_info["ct_defects"]["critiques"] > 0:
                        vehicle["is_professional_only"] = True
                        if "Réservé aux professionnels" not in str(vehicle["notes"]):
                            vehicle["notes"].append("Défaillances critiques - Réservé aux pros")

        # Look for notes/observations
        info_match = re.search(r"informations?[:\s]*([^.]+)", page_text, re.IGNORECASE)
        if info_match:
            note = info_match.group(1).strip()
            if note and len(note) > 5:
                vehicle["notes"].append(note)

        # Calculate CT score
        vehicle["ct_defects"]["total"] = (
            vehicle["ct_defects"]["critiques"] * 10 +
            vehicle["ct_defects"]["majeures"] * 5 +
            vehicle["ct_defects"]["mineures"]
        )

        return vehicle

    except Exception as e:
        print(f"Error scraping vehicle {vehicle_url}: {e}")
        return None


def fetch_ct_info(ct_url, session):
    """Fetch CT PDF and extract defect information"""
    ct_info = {
        "ct_defects": {
            "critiques": 0,
            "majeures": 0,
            "mineures": 0,
            "total": 0
        },
        "ct_details": [],
        "ct_result": "",
        "ct_type": "standard"  # standard, volontaire, or non_lisible
    }

    if not PDF_AVAILABLE:
        print(f"  [CT] PyPDF2 not available")
        ct_info["ct_details"] = ["CT disponible (PDF non parsé)"]
        ct_info["ct_type"] = "non_lisible"
        return ct_info

    try:
        print(f"  [CT] Downloading PDF from {ct_url[-30:]}...")
        response = session.get(ct_url, timeout=30)
        response.raise_for_status()
        print(f"  [CT] PDF downloaded: {len(response.content)} bytes")

        # Parse PDF
        pdf_file = io.BytesIO(response.content)
        reader = PyPDF2.PdfReader(pdf_file)

        full_text = ""
        for page in reader.pages:
            full_text += page.extract_text() or ""

        # Check if PDF text extraction failed (scanned/image PDF)
        if len(full_text) < 100:
            print(f"  [CT] PDF text extraction failed (scanned PDF?)")
            ct_info["ct_type"] = "non_lisible"
            ct_info["ct_details"] = ["PDF scanné - lecture automatique impossible"]
            return ct_info

        # Check if this is a "Contrôle Volontaire" (voluntary pre-sale inspection)
        is_volontaire = "Contrôle Volontaire" in full_text or "CONTRÔLE VOLONTAIRE" in full_text

        if is_volontaire:
            print(f"  [CT] Contrôle Volontaire (voluntary inspection)")
            ct_info["ct_type"] = "volontaire"

        # Extract CT result
        if "Défavorable pour défaillances critiques" in full_text:
            ct_info["ct_result"] = "critique"
        elif "Défavorable pour défaillances majeures" in full_text or "Défavorable" in full_text:
            ct_info["ct_result"] = "majeure"
        elif "Favorable" in full_text:
            ct_info["ct_result"] = "favorable"
        elif is_volontaire:
            ct_info["ct_result"] = "volontaire"

        # Check brake efficiency
        brake_match = re.search(r'Efficacité[:\s]*(\d+)%', full_text)
        if brake_match:
            ct_info["ct_details"].append(f"Efficacité freinage: {brake_match.group(1)}%")

        # Count defects by finding all defect codes in the PDF
        # Defect codes: X.X.X.a.1 = mineure, X.X.X.a.2 = majeure, X.X.X.a.3 = critique
        # Format: 1.1.12.b.2 or 4.13.1.a.2 etc.

        critiques = []
        majeures = []
        mineures = []

        # Find ALL defect codes in the document
        # Pattern matches codes like 1.1.12.b.2 followed by description
        all_defects = re.findall(
            r'(\d+\.\d+\.?\d*\.[a-z]\.[123])\.?\s*([A-ZÉÈÊÀÂÔÎÛÇŒ][^\d\n]{5,150})',
            full_text
        )

        for code, description in all_defects:
            defect_text = f"{code} {description.strip()[:80]}"

            # Last digit determines severity: .1 = mineure, .2 = majeure, .3 = critique
            if code.endswith('.3'):
                critiques.append(defect_text)
            elif code.endswith('.2'):
                majeures.append(defect_text)
            elif code.endswith('.1'):
                mineures.append(defect_text)

        # Remove duplicates while preserving order
        critiques = list(dict.fromkeys(critiques))
        majeures = list(dict.fromkeys(majeures))
        mineures = list(dict.fromkeys(mineures))

        ct_info["ct_defects"]["critiques"] = len(critiques)
        ct_info["ct_defects"]["majeures"] = len(majeures)
        ct_info["ct_defects"]["mineures"] = len(mineures)

        print(f"  [CT] Found: {len(critiques)} critiques, {len(majeures)} majeures, {len(mineures)} mineures")

        # Store defect details
        if critiques:
            ct_info["ct_details"].extend([f"CRITIQUE: {d}" for d in critiques[:5]])
        if majeures:
            ct_info["ct_details"].extend([f"MAJEURE: {d}" for d in majeures[:5]])
        if mineures:
            ct_info["ct_details"].extend([f"MINEURE: {d}" for d in mineures[:5]])

        # Calculate weighted total
        ct_info["ct_defects"]["total"] = (
            ct_info["ct_defects"]["critiques"] * 10 +
            ct_info["ct_defects"]["majeures"] * 5 +
            ct_info["ct_defects"]["mineures"]
        )

        return ct_info

    except Exception as e:
        print(f"  [CT] ERROR: {e}")
        ct_info["ct_details"] = ["CT non disponible"]
        ct_info["ct_type"] = "non_lisible"
        return ct_info


# In-memory cache for market prices (brand+model+year → price data)
_market_price_cache = {}


def fetch_market_price(brand, model, year, mileage=None, fuel=None):
    """Fetch average market price from LeBonCoin (and LaCentrale as fallback).

    Returns dict with market_price, market_count, market_sources or None.
    """
    if not brand or not model:
        return None

    # Build cache key
    cache_key = f"{brand.lower()}_{model.lower()}_{year or ''}"
    if cache_key in _market_price_cache:
        return _market_price_cache[cache_key]

    all_prices = []
    sources = {}

    # --- LeBonCoin ---
    try:
        lbc_prices = _fetch_lbc_prices(brand, model, year, mileage)
        if lbc_prices:
            all_prices.extend(lbc_prices)
            sources["LBC"] = len(lbc_prices)
    except Exception as e:
        print(f"  [MARKET] LeBonCoin error: {e}")

    time.sleep(0.5)

    # --- LaCentrale ---
    try:
        lc_prices = _fetch_lacentrale_prices(brand, model, year)
        if lc_prices:
            all_prices.extend(lc_prices)
            sources["LC"] = len(lc_prices)
    except Exception as e:
        print(f"  [MARKET] LaCentrale error: {e}")

    if not all_prices:
        _market_price_cache[cache_key] = None
        return None

    # Remove outliers (below 10th or above 90th percentile)
    all_prices.sort()
    if len(all_prices) >= 5:
        low = int(len(all_prices) * 0.1)
        high = int(len(all_prices) * 0.9)
        all_prices = all_prices[low:high]

    avg_price = int(sum(all_prices) / len(all_prices))
    source_str = " ".join(f"{k}({v})" for k, v in sources.items())

    result = {
        "market_price": avg_price,
        "market_count": sum(sources.values()),
        "market_sources": source_str,
    }

    _market_price_cache[cache_key] = result
    print(f"  [MARKET] {brand} {model} {year}: {avg_price} € ({source_str})")
    return result


def _fetch_lbc_prices(brand, model, year, mileage=None):
    """Fetch prices from LeBonCoin API."""
    headers = {
        'Content-Type': 'application/json',
        'api_key': 'ba0c2dad52b3ec',
        'Accept': 'application/json',
        'User-Agent': 'LBC;Android;6.32.2;Google;sdk_gphone_x86;29;1080x1920',
    }

    # Build search keywords - use brand + first word of model for better matching
    model_first = model.split()[0] if model else ""
    keywords = f"{brand} {model_first}".strip()

    # Build ranges
    ranges = {}
    if year:
        try:
            y = int(year)
            ranges["regdate"] = {"min": y - 2, "max": y + 2}
        except ValueError:
            pass
    if mileage:
        try:
            km = int(mileage)
            km_min = max(0, int(km * 0.7))
            km_max = int(km * 1.3)
            ranges["mileage"] = {"min": km_min, "max": km_max}
        except ValueError:
            pass

    payload = {
        "limit": 30,
        "filters": {
            "category": {"id": "2"},
            "location": {
                "area": {
                    "lat": 43.2965,
                    "lng": 5.3698,
                    "radius": 100000
                }
            },
            "keywords": {"text": keywords},
            "ranges": ranges,
        }
    }

    response = requests.post(
        "https://api.leboncoin.fr/finder/search",
        headers=headers,
        json=payload,
        timeout=15
    )

    if response.status_code != 200:
        print(f"  [MARKET] LBC status {response.status_code}")
        return []

    data = response.json()
    ads = data.get("ads", [])
    prices = []

    for ad in ads:
        prix = ad.get("price", [None])
        if isinstance(prix, list):
            prix = prix[0] if prix else None
        if prix:
            try:
                p = float(prix)
                if 500 <= p <= 200000:
                    prices.append(p)
            except (ValueError, TypeError):
                pass

    return prices


def _fetch_lacentrale_prices(brand, model, year):
    """Fetch prices from LaCentrale via __NEXT_DATA__ extraction."""
    brand_slug = brand.lower().replace(" ", "-")
    model_first = model.split()[0].lower().replace(" ", "-") if model else ""

    url = f"https://www.lacentrale.fr/listing?makesModelsCommercialNames={brand_slug}%3A{model_first}"
    if year:
        try:
            y = int(year)
            url += f"&yearMin={y - 2}&yearMax={y + 2}"
        except ValueError:
            pass

    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        if resp.status_code != 200:
            return []

        # Extract __NEXT_DATA__ JSON
        match = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', resp.text)
        if not match:
            return []

        next_data = json.loads(match.group(1))

        # Navigate to listings
        props = next_data.get("props", {}).get("pageProps", {})
        listings = props.get("searchResults", {}).get("listings", [])
        if not listings:
            listings = props.get("listings", [])

        prices = []
        for listing in listings:
            price = listing.get("price") or listing.get("priceListing")
            if price:
                try:
                    p = float(price)
                    if 500 <= p <= 200000:
                        prices.append(p)
                except (ValueError, TypeError):
                    pass

        return prices

    except Exception as e:
        print(f"  [MARKET] LaCentrale parse error: {e}")
        return []


@app.route("/")
def index():
    """Main page"""
    return render_template("index.html")


@app.route("/api/vehicles")
def get_vehicles():
    """API endpoint to get vehicles"""
    return jsonify({
        "vehicles": vehicle_cache["vehicles"],
        "last_updated": vehicle_cache["last_updated"],
        "count": len(vehicle_cache["vehicles"])
    })


@app.route("/api/scrape")
def trigger_scrape():
    """Trigger a new scrape"""
    location = request.args.get("location", "marseille")
    max_pages = int(request.args.get("pages", 3))

    try:
        vehicles = scrape_vehicle_list(location=location, max_pages=max_pages)

        # Sort by CT defects (least first), then by year (newest first)
        vehicles.sort(key=lambda v: (
            v.get("ct_defects", {}).get("total", 999),
            -int(v.get("year", "0") or "0")
        ))

        # Remove duplicates by URL
        seen_urls = set()
        unique_vehicles = []
        for v in vehicles:
            if v["url"] not in seen_urls:
                seen_urls.add(v["url"])
                unique_vehicles.append(v)

        vehicle_cache["vehicles"] = unique_vehicles
        vehicle_cache["last_updated"] = datetime.now().isoformat()
        vehicle_cache["location"] = location

        # Save to file for persistence
        save_data()

        return jsonify({
            "success": True,
            "count": len(unique_vehicles),
            "vehicles": unique_vehicles
        })
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@app.route("/api/status")
def get_status():
    """Get scheduler and data status"""
    vehicles = vehicle_cache.get("vehicles", [])
    vehicle_count = len(vehicles)

    # Check if we have no vehicles (might mean no Marseille sales active)
    status_message = "OK"
    if vehicle_count == 0:
        status_message = "No Marseille vehicles currently available - check calendar for upcoming sales"

    return jsonify({
        "scheduler_active": SCHEDULER_AVAILABLE,
        "next_scrape": "06:00 (daily)",
        "last_updated": vehicle_cache.get("last_updated"),
        "vehicle_count": vehicle_count,
        "location": vehicle_cache.get("location", "marseille"),
        "target_location": TARGET_LOCATION,
        "status_message": status_message,
        "calendar_url": "https://www.alcopa-auction.fr/calendrier-des-ventes"
    })


@app.route("/api/vehicle/<path:vehicle_id>")
def get_vehicle_details(vehicle_id):
    """Get details for a specific vehicle"""
    # Support both voiture and utilitaire paths
    if vehicle_id.startswith("utilitaire-occasion/"):
        url = f"{BASE_URL}/{vehicle_id}"
    else:
        url = f"{BASE_URL}/voiture-occasion/{vehicle_id}"
    session = get_session()
    details = scrape_vehicle_details(url, session)

    if details:
        return jsonify(details)
    return jsonify({"error": "Vehicle not found"}), 404


if __name__ == "__main__":
    print("="*50)
    print("ALCOPA TRACKER - Starting...")
    print("="*50)

    # Load existing data
    load_data()

    # Set up scheduler for daily scraping at 6 AM
    if SCHEDULER_AVAILABLE:
        scheduler = BackgroundScheduler()
        scheduler.add_job(
            func=scheduled_scrape,
            trigger=CronTrigger(hour=6, minute=0),
            id='daily_scrape',
            name='Daily vehicle scrape at 6 AM',
            replace_existing=True
        )
        scheduler.start()
        print("Scheduler started - Daily scrape at 06:00")

        # Shut down scheduler when app exits
        atexit.register(lambda: scheduler.shutdown())
    else:
        print("Scheduler not available - install APScheduler for automatic scraping")

    print(f"\nData file: {DATA_FILE}")
    print(f"Vehicles loaded: {len(vehicle_cache.get('vehicles', []))}")
    print(f"Last update: {vehicle_cache.get('last_updated', 'Never')}")
    print("\nVisit http://localhost:8080 to use the app")
    print("="*50)

    app.run(debug=False, port=5001, host="0.0.0.0", threaded=True)
