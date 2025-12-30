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
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
}

# Cache for scraped data
vehicle_cache = {
    "vehicles": [],
    "last_updated": None,
    "location": "marseille"
}


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


def find_marseille_sales(session):
    """Find all active Marseille auction sales from the homepage"""
    sales = []
    seen_ids = set()

    try:
        print("Fetching Alcopa homepage to find Marseille sales...")
        response = session.get(BASE_URL, timeout=30)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "lxml")

        # Find elements containing "Marseille" and look for associated sale links
        for element in soup.find_all(string=re.compile(r'Marseille', re.I)):
            parent = element.find_parent()
            if not parent:
                continue

            # Go up the DOM tree to find a container with a sale link
            for ancestor in parent.parents:
                ancestor_text = ancestor.get_text(" ", strip=True)

                # Only process if this ancestor mentions Marseille specifically
                # and it's a sale card (contains "lots")
                if "marseille" in ancestor_text.lower() and "lots" in ancestor_text.lower():
                    sale_link = ancestor.find("a", href=re.compile(r"/vente-encheres-en-ligne/\d+"))
                    if sale_link:
                        href = sale_link.get("href", "")
                        match = re.search(r"/vente-encheres-en-ligne/(\d+)", href)
                        if match:
                            sale_id = match.group(1)
                            if sale_id not in seen_ids:
                                seen_ids.add(sale_id)
                                sale_url = f"{BASE_URL}/vente-encheres-en-ligne/{sale_id}?site=internet"

                                # Extract sale name from context
                                name_match = re.search(r'Marseille[^,\n]+', ancestor_text)
                                sale_name = name_match.group(0)[:50] if name_match else f"Marseille #{sale_id}"

                                sales.append({
                                    "id": sale_id,
                                    "url": sale_url,
                                    "name": sale_name
                                })
                                print(f"  Found: {sale_id} - {sale_name}")
                        break

        print(f"Found {len(sales)} Marseille sales")

    except Exception as e:
        print(f"Error finding Marseille sales: {e}")
        import traceback
        traceback.print_exc()

    return sales


def scrape_sale_vehicles(sale_url, session):
    """Scrape all vehicle URLs from a specific auction sale page"""
    result = {
        "vehicle_urls": set(),
        "sale_end_date": None,
        "sale_location": None,
        "sale_name": None
    }

    try:
        print(f"Scraping sale: {sale_url}")
        response = session.get(sale_url, timeout=30)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "lxml")
        page_text = soup.get_text(" ", strip=True)

        # Verify this is actually a Marseille sale by checking the page content
        # Look for "Vente internet du ... : Marseille" or similar patterns
        location_match = re.search(r'Vente\s+internet[^:]+:\s*([A-Za-zÀ-ÿ\-]+)\s*-', page_text)
        if location_match:
            result["sale_location"] = location_match.group(1).strip()
            print(f"  Detected location: {result['sale_location']}")

            # Skip non-Marseille sales
            if result["sale_location"].lower() != "marseille":
                print(f"  SKIPPING: Not a Marseille sale!")
                return result

        # Extract sale end date - look for "Flash : 26 décembre 2025, 13:00"
        flash_match = re.search(r'Flash\s*:\s*(\d{1,2}\s+\w+\s+\d{4}),?\s*(\d{1,2}:\d{2})', page_text)
        if flash_match:
            date_str = flash_match.group(1)
            time_str = flash_match.group(2)
            result["sale_end_date"] = f"{date_str} {time_str}"
            print(f"  Sale ends: {result['sale_end_date']}")

        # Find all vehicle links on the sale page
        links = soup.find_all("a", href=re.compile(r"/voiture-occasion/[^/]+/[^/]+-\d+$"))

        for link in links:
            href = link.get("href", "")
            if href:
                if not href.startswith("http"):
                    href = BASE_URL + href
                result["vehicle_urls"].add(href)

        print(f"  Found {len(result['vehicle_urls'])} vehicles in this sale")

    except Exception as e:
        print(f"Error scraping sale {sale_url}: {e}")

    return result


def scrape_vehicle_list(location="marseille", max_pages=5):
    """Scrape vehicle listings from Marseille auction sales"""
    session = get_session()
    vehicle_data = []  # List of (url, sale_end_date) tuples

    # Step 1: Find all active Marseille sales
    marseille_sales = find_marseille_sales(session)

    if not marseille_sales:
        print("No Marseille sales found! Falling back to search...")
        # Fallback to old method if no sales found
        for page in range(1, max_pages + 1):
            params = {"salle[]": location, "page": page}
            try:
                response = session.get(SEARCH_URL, params=params, timeout=30)
                response.raise_for_status()
                soup = BeautifulSoup(response.text, "lxml")
                links = soup.find_all("a", href=re.compile(r"/voiture-occasion/[^/]+/[^/]+-\d+$"))
                if not links:
                    break
                for link in links:
                    href = link.get("href", "")
                    if href and "/voiture-occasion/" in href:
                        if not href.startswith("http"):
                            href = BASE_URL + href
                        vehicle_data.append((href, None))
                time.sleep(1)
            except Exception as e:
                print(f"Error on page {page}: {e}")
                break
    else:
        # Step 2: Scrape vehicles from each Marseille sale
        for sale in marseille_sales:
            result = scrape_sale_vehicles(sale["url"], session)

            # Only process if location was confirmed as Marseille (or not detected)
            if result["sale_location"] and result["sale_location"].lower() != "marseille":
                continue  # Skip non-Marseille sales

            sale_end = result.get("sale_end_date")
            for url in result["vehicle_urls"]:
                vehicle_data.append((url, sale_end))
            time.sleep(1)

    # Remove duplicates while keeping first occurrence (with sale_end_date)
    seen_urls = set()
    unique_vehicles = []
    for url, sale_end in vehicle_data:
        if url not in seen_urls:
            seen_urls.add(url)
            unique_vehicles.append((url, sale_end))

    print(f"\nTotal unique vehicles found: {len(unique_vehicles)}")

    # Step 3: Fetch details for each vehicle
    vehicles = []
    session = get_session()

    for i, (url, sale_end) in enumerate(unique_vehicles):
        print(f"Fetching vehicle {i+1}/{len(unique_vehicles)}: {url.split('/')[-1]}")
        vehicle = scrape_vehicle_details(url, session)
        if vehicle:
            # Add sale end date to vehicle
            vehicle["sale_end_date"] = sale_end
            vehicles.append(vehicle)
        time.sleep(0.5)

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
        match = re.search(r"/voiture-occasion/([^/]+)/(.+)-(\d+)$", vehicle_url)
        if match:
            vehicle["brand"] = match.group(1).upper().replace("-", " ")
            model_slug = match.group(2)
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

        # Extract location
        location_match = re.search(r"stockage[:\s]*([A-Z]+)", page_text)
        if location_match:
            vehicle["location"] = location_match.group(1).title()

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
        if "Contrôle Volontaire" in full_text or "CONTRÔLE VOLONTAIRE" in full_text:
            print(f"  [CT] This is a Contrôle Volontaire (voluntary inspection)")
            ct_info["ct_type"] = "volontaire"
            ct_info["ct_result"] = "volontaire"
            ct_info["ct_details"] = ["Contrôle Volontaire (inspection pré-vente, pas un CT officiel)"]

            # Try to extract some info from voluntary control
            # Check brake efficiency
            brake_match = re.search(r'Efficacité[:\s]*(\d+)%', full_text)
            if brake_match:
                ct_info["ct_details"].append(f"Efficacité freinage: {brake_match.group(1)}%")

            return ct_info

        # Standard CT processing
        # Extract CT result
        if "Défavorable pour défaillances critiques" in full_text:
            ct_info["ct_result"] = "critique"
        elif "Défavorable pour défaillances majeures" in full_text or "Défavorable" in full_text:
            ct_info["ct_result"] = "majeure"
        elif "Favorable" in full_text:
            ct_info["ct_result"] = "favorable"

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
    return jsonify({
        "scheduler_active": SCHEDULER_AVAILABLE,
        "next_scrape": "06:00 (daily)",
        "last_updated": vehicle_cache.get("last_updated"),
        "vehicle_count": len(vehicle_cache.get("vehicles", [])),
        "location": vehicle_cache.get("location", "marseille")
    })


@app.route("/api/vehicle/<path:vehicle_id>")
def get_vehicle_details(vehicle_id):
    """Get details for a specific vehicle"""
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

    app.run(debug=False, port=8080, host="0.0.0.0")
