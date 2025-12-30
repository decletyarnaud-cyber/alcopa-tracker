# CLAUDE.md - Alcopa Tracker

This file provides guidance to Claude Code (claude.ai/code) when working with this repository.

## Project Overview

Alcopa Tracker is a vehicle auction tracker for Alcopa Auction in Vitrolles (near Marseille), France. It scrapes vehicle listings, parses contrôle technique (CT) PDF reports to extract defect counts, and helps identify vehicles with the least technical issues.

## Service Info

| Property | Value |
|----------|-------|
| **Service Name** | alcopa |
| **Port** | 5001 |
| **Type** | Flask |
| **URL** | http://localhost:5001 |
| **GitHub** | https://github.com/decletyarnaud-cyber/alcopa-tracker |
| **launchd** | `com.ade.alcopa-tracker` |

### Service Management

```bash
# Via pctl (recommended)
pctl start alcopa
pctl stop alcopa
pctl restart alcopa
pctl logs alcopa
pctl open alcopa

# Direct launchctl
launchctl load ~/Library/LaunchAgents/com.ade.alcopa-tracker.plist
launchctl unload ~/Library/LaunchAgents/com.ade.alcopa-tracker.plist
```

## Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Run Flask app directly
python app.py

# Or via Flask CLI
FLASK_APP=app.py flask run --host 0.0.0.0 --port 5001

# Using start script
./start.sh
```

## Architecture

### Data Flow

```
Alcopa Website → Scraper → CT PDF Parser → Vehicle Ranking → Web UI
```

### Main Components

**app.py** - Single-file Flask application containing:
- `scrape_vehicles()` - Scrapes vehicle listings from alcopa-auction.fr
- `parse_ct_pdf()` - Extracts defect counts from contrôle technique PDFs
- `rank_vehicles()` - Sorts vehicles by CT defect count
- Flask routes for web interface
- APScheduler for automatic scraping (optional)

**templates/index.html** - Web interface showing:
- Vehicle listings with photos
- CT defect counts
- Price and auction date
- Links to Alcopa listings

### Key Features

1. **Vehicle Scraping**: Extracts vehicles from Alcopa Vitrolles auctions
2. **CT PDF Parsing**: Downloads and parses contrôle technique reports
3. **Defect Ranking**: Sorts vehicles by number of defects (fewer = better)
4. **Auto-refresh**: Optional scheduled scraping via APScheduler
5. **Data Persistence**: Saves vehicle data to JSON

## Data Model

**Vehicle**:
```python
{
    "id": "alcopa-123",
    "title": "PEUGEOT 308 1.6 HDI",
    "price": 5500,
    "year": 2018,
    "mileage": 85000,
    "fuel": "Diesel",
    "ct_url": "https://...",
    "ct_defects": 3,
    "ct_details": ["Feux", "Pneus", ...],
    "image_url": "https://...",
    "auction_date": "2025-01-15",
    "listing_url": "https://..."
}
```

## Configuration

Configuration is inline in `app.py`:

```python
BASE_URL = "https://www.alcopa-auction.fr"
SEARCH_URL = f"{BASE_URL}/recherche"
DATA_FILE = "vehicle_data.json"

# Filter for Vitrolles location
LOCATION = "Vitrolles"
```

## Contrôle Technique (CT) System

French vehicle inspection system:
- **Required**: Every 2 years after 4 years old
- **Categories**:
  - Défauts mineurs (minor)
  - Défauts majeurs (major)
  - Défauts critiques (critical)
- **Contre-visite**: Required if critical defects found

### CT Parsing

The app downloads CT PDFs and extracts:
- Total defect count
- Defect categories
- Validity date
- Contre-visite requirement

## Dependencies

Minimal dependencies (Flask-based):
```
flask==3.0.0
requests==2.31.0
beautifulsoup4==4.12.2
lxml==4.9.3
PyPDF2 (optional, for CT parsing)
APScheduler (optional, for auto-scraping)
```

## File Structure

```
alcopa-tracker/
├── app.py              # Main Flask application
├── requirements.txt    # Python dependencies
├── start.sh           # Startup script
├── vehicle_data.json  # Cached vehicle data
├── templates/
│   └── index.html     # Web interface
└── static/            # Static assets (if any)
```

## Alcopa Auction System

Alcopa is a major French vehicle auction house:

| Aspect | Details |
|--------|---------|
| **Location** | Vitrolles (near Marseille) |
| **Auction Type** | Physical + Online |
| **Vehicle Types** | Cars, vans, motorcycles |
| **Buyer Registration** | Required (free) |
| **Fees** | ~10% buyer premium |
| **Payment** | Bank transfer within 48h |

## Usage Tips

1. **Check CT carefully**: Lower defect count = better condition
2. **Verify mileage**: Cross-reference with CT report
3. **Inspect in person**: Visit Alcopa before auction
4. **Set max bid**: Account for fees + repairs
5. **Check history**: Use SIV or Histovec for vehicle history

## Language Note

This project uses French domain terms:
- **contrôle technique (CT)** = vehicle inspection
- **défaut** = defect
- **contre-visite** = re-inspection required
- **mise à prix** = starting price
- **enchère** = bid
