# Metering Lead Finder

A local FastAPI dashboard with a standalone Google Maps collector. Enter a Maps query, run it from the dashboard, review every scraper field (including website emails), and download the results as CSV. LangChain is intentionally not included yet.

## Run locally

```bash
cd ~/Projects/metering
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m playwright install chromium
.venv/bin/uvicorn app:app --host 127.0.0.1 --port 8000
```

Open <http://127.0.0.1:8000>. The server binds to localhost only. Keep it local unless you add authentication and access controls before exposing it to a network.

The dashboard starts searches in the background and shows status/results when available. Only one Maps search can run at a time. The default cap is 999 listings; this is a collection ceiling, not a guarantee that Maps will return that many results. Search jobs/history live in memory until the server restarts; CSVs remain in `output/`.

## Scraper and emails

- `maps_scraper.py` is a local copy of the extension-free Playwright runner. It has no runtime dependency on the original scraper project, Hermes, an AI provider, or a search API.
- It can also be run directly from this folder: `.venv/bin/python maps_scraper.py "Hausverwaltung Berlin" 999 1`. Use `0` instead of `1` to skip website crawling. The dashboard calls the same local scraper functions.
- Google Maps is used for business discovery. When website email lookup is enabled, the runner checks publicly available website content for email strings and stores them in the result columns. Those addresses are not verified as current, deliverable, or belonging to a decision-maker; no outreach is sent.
- Raw Maps data and enriched results are saved separately when website lookup is enabled. Partial results are kept if collection encounters a verification boundary. The runner does not bypass CAPTCHAs or other verification challenges.
- Google Maps page structure and access may change. Use the collector in accordance with applicable terms and laws.

## Tests

```bash
.venv/bin/python -B -m unittest discover -s tests -v
```

The dashboard tests use mocked search jobs. The scraper tests use real Chromium against routed local HTML fixtures; they do not send a live query to Google Maps or business websites.
