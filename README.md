# Metering Lead Finder

Local FastAPI dashboard using a bundled Chrome extension inside full Chromium, with visible or headless mode selected by `HEADLESS`. The extension collects the business records; Python automates its Start/Export controls and saves the returned data.

```text
Dashboard -> FastAPI -> full Chromium (HEADLESS=true/false)
                             -> bundled scraping extension -> Maps records
                             -> extension background worker -> optional website contacts
         <- result table + CSV sources + staged KPI/Excel export
```

## Research documentation

- [Task 3: Research approach and validation - Markdown draft](docs/task-3-approach-draft.md): v0.2 methodology covering the staged workflow, provisional KPI criteria, validation limits and future work.
- [Editable Word version](docs/task-3-approach-draft.docx): generated from the Markdown master. Neither document claims completed official-source qualification or verified decision-maker contacts.
- The original planning page in `../berlin-hausverwaltung-research/index.html` has been updated to reflect the built FastAPI/extension/CSV workflow rather than the earlier proposed frontend/database stack.

## Run locally

```bash
cd ~/Projects/metering
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m playwright install chromium
.venv/bin/uvicorn app:app --host 127.0.0.1 --port 3000
```

Open http://127.0.0.1:3000. The server binds to localhost only. Add authentication before exposing it to a network.

### Browser visibility

Set `HEADLESS` in the project-root `.env`:

```dotenv
HEADLESS=true
```

- `true`: run Chromium without a visible window (default).
- `false`: show the Chromium window; requires a working graphical desktop/X server. No off-screen positioning is applied.

Both Maps collection and the separate email lookup use this setting. Restart an already-running dashboard once after installing this code change. Thereafter `.env` is read for each new browser launch, so changing the flag affects the next job without another restart; an existing browser is not changed. An exported `HEADLESS` environment variable takes precedence. Only `true` or `false` are accepted. `.env` stays gitignored; `.env.example` documents the default.


The workflow is **Fetch raw data → Get emails → Run KPI → Download Excel**. Collection does not fetch websites, email lookup does not qualify companies, and KPI screening runs only after an explicit click once the email stage has finished. The final download is a genuine `.xlsx` workbook; source CSVs remain saved separately. There is no numeric listing cap (`limit=0` internally). For list searches the extension keeps scrolling, including nudging an idle feed, until Maps displays "You've reached the end of the list." Unchanged heights or a matching CSS class alone are not completion. The extension continues through a limited-view notice and temporary feed replacement, scrolling each viewport of the results container. Python waits for the same extension run's explicit `finished` acknowledgement and confirmed end marker; it does not infer completion from a reset button label or low count. Verification, consent, collection errors or the runner's two-hour safety budget produce an explicitly partial result with available rows saved and a `stop_reason`. If Maps reports limited view even when the exposed list ends, the export retains a restricted-coverage warning and partial status. Single-place and explicit no-result pages are separate terminal outcomes. This does not guarantee Maps exposes every business. One collection or email lookup can run at a time. Jobs/history are in memory until restart; CSV exports persist under `output/` with collision-safe filenames.

## Extension workflow

- The runtime Python files are `app.py` (dashboard/API), `csv_output.py` (CSV and XLSX export), and `extension_bundle/run_hermes_scrape.py` (extension orchestration).
- `extension_bundle/` contains a project-local copy of the extension and its assets. The original `~/Projects/scrape-extension` is not modified or required at runtime.
- The runner uses `launch_persistent_context`, the configured `HEADLESS` value, `channel="chromium"`, `--load-extension`, and `--disable-extensions-except`. It presses the extension's injected Start/Export controls and receives its exported records through the console handshake.
- Explicit website-email enrichment invokes the bundled extension's `extractemail` function in its background service worker. Python does not navigate or parse business-site pages for that step. The extension has broad HTTP/HTTPS host permissions so it can fetch arbitrary public business websites.
- Dashboard extension failures are reported instead of silently switching to another collector.
- Native email lookup is disabled in the bundled `contentScript.js`, so raw collection does not fetch business websites. The separate `Get emails` action uses the extension service worker and never reruns Maps collection. The original source extension's setting is unchanged.

## KPI screening and Excel export

- Click the **i** icon beside **Run KPI** to inspect the Maps-only screening criteria and row-color key before applying them.
- **Clear all** under **Recent results** opens a confirmation modal and removes the current dashboard’s in-memory search records and selected result view. It is disabled during active collection/email lookup; saved export files remain untouched.
- **Get emails** must finish before **Run KPI** is enabled. Missing websites can be skipped; a finished pass does not mean all pages were accessible or emails were found. A job-level failure still requires retry/completion.
- Clicking **Run KPI** applies deterministic Maps-category screening to the selected results only. No website requests, AI research, outreach or automatic classification during raw/email stages are involved.
- An explicit property-management category is a provisional **Accepted** candidate. Clearly unrelated categories without conflicting management signals are **Rejected**. Missing/ambiguous categories, broker-only listings, name-only signals and conflicts are **Needs review**.
- Accepted rows are fully green; rejected rows are fully red; review rows remain neutral. Every row stays in its original order, including rejected ones.
- KPI fields cover status/reason, management-category match, Berlin evidence level, listed website/phone presence, source link and method. Portfolio size and metering-service fit remain Unknown. Ratings/review counts do not determine fit. A Berlin query or street-only address is not geographic verification.
- The backend enforces the same sequence as the buttons. It saves a separate `_qualified.csv`, preserving raw and email-stage exports.
- **Download Excel** is enabled only after KPI completion. Its `.xlsx` contains **Qualified leads** and **KPI criteria** sheets, full-row green/red fills, filters and frozen headings/key columns. Contact and listing strings are exported as literal text, not formulas.
- The existing CSV API remains available for compatibility; the final UI download is Excel. No Google Sheets integration is present.
- New routes: `POST /api/jobs/{job_id}/kpi`, `GET /api/jobs/{job_id}/excel`, and `DELETE /api/jobs` to clear in-memory dashboard history. KPI rejects requests before email-stage completion; Excel rejects requests before KPI completion; clearing rejects requests while a collection or email lookup is active.

## Results and limitations

Raw records are saved to a separate `_raw.csv` export. The explicit email action saves an `_emails.csv` derivative without overwriting the raw file. It checks one business at a time, and that business's contact/about pages are also fetched one at a time. The extension emits real request, page-loaded, extraction and page-error events. Python relays these events; it does not parse website content. These are service-worker HTTP requests, not simulated browser-tab navigation.

The dashboard shows lookup progress directly in each result's Email cell, polling every 500 ms. The active row has an animated `Fetching…` indicator with the requested page URL in its tooltip; other rows can show queued, missing-website, no-email or failure outcomes from observed events. HTTP 403 is shown as `Blocked (403)`, not as an email-free successful check. Existing email values remain visible. The separate email summary/activity panel is removed. These UI labels never modify CSV email values. Missing websites are explicitly skipped; failed/timeout lookups are distinguished from a checked page with no email found. The table and saved email-stage CSV are checkpointed after each business, using atomic replacement for subsequent updates. If lookup stops, completed rows and their derivative remain available, the raw CSV stays unchanged, and lookup can be retried. A 90-second business safety deadline aborts outstanding extension requests and waits for them to settle before another business can start. If settlement fails, the job stops instead of overlapping requests.

The finished raw-collection summary banner is hidden, including its partial-results/limited-view copy; internal status/warnings and the saved data are retained. Active collection, failures and email-lookup progress are still displayed.

Emails are extracted strings, not verified current, deliverable, or decision-maker contacts. No outreach is sent. The extension's DOM selectors and Maps response parser may need maintenance when Google changes its pages. An idle feed or limited-view notice does not itself stop collection. The actual end marker finishes a list run; access boundaries, errors, manual stop or the runner's safety budget can interrupt it. A successful export does not establish exhaustive coverage. No CAPTCHA or verification bypass is implemented. Use in accordance with applicable terms and laws.

Future work includes provider/challenge detection and permitted browser-backed access to public pages, owner-approved allowlisting or official APIs, durable lookup outcomes, deeper company evidence and persistent job recovery. These are planned capabilities, not a working Cloudflare/provider bypass; see the methodology draft.

The new KPI/Excel actions have not been exercised under the application-testing deferral. Source checks and a history-preserving restart do not validate the research records or workbook behavior.
