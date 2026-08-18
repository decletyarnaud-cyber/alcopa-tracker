# Alcopa Tracker — Scraping Optimization Plan

## Problem
The scrape takes ~40 minutes for ~487 vehicles because everything is sequential and fetches ALL cities before filtering to Marseille/Vitrolles.

## Current Flow (slow)
1. `find_all_sales()` → 8-12 HTTP requests across ALL cities
2. `scrape_sale_vehicles()` → 1 request per page per sale (ALL vehicles, ALL cities)
3. `scrape_vehicle_details()` → 1 request per vehicle + 0.5s sleep (sequential)
4. `fetch_ct_info()` → 1 request per vehicle with CT PDF (sequential, inside details)
5. `fetch_market_price()` → 2 requests per vehicle (LBC + LaCentrale + 0.5s sleep, inside scrape loop)
6. **Then** discard non-Marseille vehicles

## Optimizations to Implement

### 1. Search Page with Location Filter (biggest win)
- Use `/recherche?lieux=marseille` as **primary source** — returns 20 vehicles/page, cards already contain brand, model, year, mileage, price, fuel, transmission, location
- **Hybrid approach**: ALSO scan flash sales via `find_all_sales()` because some flash sales contain Marseille/Vitrolles vehicles but don't mention Marseille in their title
- Merge results, deduplicate by URL
- Only fetch detail pages for CT PDF links (not visible on search cards)
- **Preserve** sale metadata (sale_type, sale_id, sale_name) from flash sale discovery

### 2. Parallel HTTP Requests (5-10x faster)
- Use `concurrent.futures.ThreadPoolExecutor` with ~10 workers
- Fetch vehicle details + CT PDFs in parallel instead of sequentially
- Remove `time.sleep(0.5)` between vehicles (not needed with parallel)
- Thread-safe writes to shared lists (use lock or collect results after)

### 3. Defer Market Price Fetching (eliminates 2 requests/vehicle)
- Remove `fetch_market_price()` from the scrape loop
- Add new API endpoint: `GET /api/market-price/<brand>/<model>/<year>`
- Frontend lazy-loads market prices when rendering vehicle cards
- Keep `_market_price_cache` for dedup within a session

## Key Constraints
- **Marseille + Vitrolles** = valid locations (Vitrolles is near Marseille, same auction site)
- Flash sales may not mention "Marseille" in title → must still scan them
- Existing data format must be preserved (vehicle dict, JSON structure)
- `scheduled_scrape()` must also benefit from optimizations

## Files to Modify
- `app.py` — Main changes (scrape_vehicle_list, new parallel helpers, new API endpoint)
- `templates/index.html` — Add lazy-loading for market prices in `renderMarketPrice()`

## Expected Result
| Metric | Before | After |
|--------|--------|-------|
| Full scrape | ~40 min | ~30-60s |
| Daily incremental | ~40 min | ~5-15s |
| HTTP requests per vehicle | 4-5 | 1-2 (detail + CT only) |
