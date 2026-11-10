# Metering Lead Finder

Local FastAPI dashboard using a bundled Chrome extension inside headless Chromium. The extension collects the business records; Python automates its Start/Export controls and saves the returned data.

```text
Dashboard -> FastAPI -> headless full Chromium
                             -> bundled scraping extension -> Maps records
                             -> extension background worker -> optional website contacts
         <- result table + CSV exports
```

## Run locally

```bash
cd ~/Projects/metering
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m playwright install chromium
.venv/bin/uvicorn app:app --host 127.0.0.1 --port 3000
```

Open http://127.0.0.1:3000. The server binds to localhost only. Add authentication before exposing it to a network.

The dashboard uses two explicit actions: **Fetch raw data** collects Maps business listings without website/email lookup; **Get emails** above the results checks the saved listings' websites afterward. There is no numeric listing cap (`limit=0` internally). For list searches the extension keeps scrolling, including nudging an idle feed, until Maps displays "You've reached the end of the list." Unchanged heights or a matching CSS class alone are not completion. Verification, consent, limited view, missing feed, collection errors or the runner's two-hour safety budget produce an explicitly partial result with available rows saved and a `stop_reason`, not a successful complete search. Single-place and explicit no-result pages are separate terminal outcomes. This does not guarantee Maps exposes every business. One collection or email lookup can run at a time. Jobs/history are in memory until restart; CSV exports persist under `output/` with collision-safe filenames.

## Extension workflow

- `app.py` imports `extension_bundle/run_hermes_scrape.py`. It does not call the earlier direct `maps_scraper.py` collector.
- `extension_bundle/` contains a project-local copy of the extension and its assets. The original `~/Projects/scrape-extension` is not modified or required at runtime.
- The runner uses `launch_persistent_context`, `headless=True`, `channel="chromium"`, `--load-extension`, and `--disable-extensions-except`. It presses the extension's injected Start/Export controls and receives its exported records through the console handshake.
- Optional website-email enrichment invokes the bundled extension's `extractemail` function in its background service worker. Python does not navigate or parse business-site pages for that step. The extension has broad HTTP/HTTPS host permissions so it can fetch arbitrary public business websites.
- The earlier `maps_scraper.py` and `tests/test_standalone_scraper.py` remain as an unused legacy variant, not a fallback. Dashboard extension failures are reported instead of silently switching collectors.
- Native email lookup is disabled in the bundled `contentScript.js`, so raw collection does not fetch business websites. The separate `Get emails` action uses the extension service worker and never reruns Maps collection. The original source extension's setting is unchanged.

## Results and limitations

Raw records are saved to a separate `_raw.csv` export. The explicit email action saves an `_emails.csv` derivative and updates the table/download without overwriting the raw file. If email lookup fails, the original results/download remain available and the lookup can be retried.

Emails are extracted strings, not verified current, deliverable, or decision-maker contacts. No outreach is sent. The extension's DOM selectors and Maps response parser may need maintenance when Google changes its pages. End-of-list, limited-view detection, stalls, and the runner's time budget can all stop collection; a successful export does not establish exhaustive coverage. No CAPTCHA or verification bypass is implemented. Use in accordance with applicable terms and laws.

## Tests

```bash
.venv/bin/python -B -m unittest discover -s tests -v
```

Dashboard orchestration tests mock the worker seam. Extension integration tests load the actual bundled extension in real headless Chromium against routed Maps HTML fixtures and a local website fixture. They verify extension-produced records, real browser launch flags, extension service-worker email extraction, and automation error propagation. Legacy direct-collector tests also remain in the suite. Tests do not submit live Google Maps searches.
