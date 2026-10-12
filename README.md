# Site Feasibility Dashboard (real-data version)

Flow: place names -> Nominatim (geocode) -> OSRM (road route + distance) -> Overpass/OpenStreetMap
(land use within 250 m of the route, 10 slices) -> score and verdict -> Leaflet map, Chart.js graphs, chat advisor.

## Run
```bash
cd site-feasibility
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
# optional: LLM advisor (otherwise a rule-based fallback answers)
export ANTHROPIC_API_KEY=sk-ant-...
uvicorn main:app --reload
```
Open http://127.0.0.1:8000

Before use, set a real contact email in `UA` in `main.py` (Nominatim policy).

## Map and search features
- Live Leaflet map on OpenStreetMap tiles; click it to drop Start, then End pins; drag pins to adjust.
- Place search with autocomplete (`/api/search`, cached and throttled to respect Nominatim's 1 request/second rule).
- "My location" tracks your live position with an accuracy circle and sets it as Start. Browsers allow this only on `localhost` or HTTPS.

## Nearby-area history
Ask the chatbot things like "history of nearby areas", "heritage near the destination" or "history of Davanagere". It looks up Wikipedia articles within 10 km of the start, destination or route middle (or a named place), summarises them, and flags possible heritage or ecological sensitivity. Text is from Wikipedia (CC BY-SA). This is background only, not a legal heritage clearance.

## Google and web search
Google no longer offers a search API to new projects (the Custom Search JSON API retires on 1 Jan 2027), so results can't be embedded. The dashboard has a Google search bar with one-click queries that open Google in a new tab, and the chatbot adds a Google link after history answers.
Optional: set `BRAVE_API_KEY` (Brave Search API) and the chatbot also pulls live web results into history answers.

## Known limits (read before relying on results)
- OSM polygons are not clipped to the corridor, so a big forest or farm touching the route is counted whole. Clip with `shapely` for accuracy.
- Agricultural land counts as "green"; edit `GREEN_LANDUSE` to match your policy.
- OSM coverage varies by region; sparse mapping reads as low built-up share.
- Public OSRM, Nominatim and Overpass servers are for light use. For production, self-host them or use a paid provider.
- Scoring thresholds and cost rates are placeholders; set them from your department's norms.
- This is a screening aid. Approval needs ground survey, hydrology, ESIA and legal land-title checks.

## Next upgrades
1. Replace OSM land use with ESA WorldCover or Sentinel-2 via Google Earth Engine for satellite-based shares.
2. Add alternative-route comparison (OSRM `alternatives=true`) and rank by score.
3. Store analyses in PostgreSQL/PostGIS and add an approval workflow with login.

## Data credits and terms
Map data © OpenStreetMap contributors, available under the ODbL. Keep the attribution if you redistribute results.
Please follow the usage policies of Nominatim, OSRM and Overpass.

## Contributing
Issues and pull requests are welcome. Ideas: polygon clipping, alternative-route ranking, satellite land cover, tests.

## License
MIT, see `LICENSE`.
