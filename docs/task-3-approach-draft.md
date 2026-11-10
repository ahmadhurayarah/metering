# Task 3: Research Approach and Validation

**Berlin property-management prospects for a metering service provider**  
**Version:** 0.1 — Draft for review

This working draft describes the collection and website-contact automation implemented so far, and the proposed qualification method. It is not a final report of validated companies, completed KPI research, or verified decision-maker contacts.

## 1. Objective and current position

The objective is to identify Berlin property management companies, explain which could be suitable customers for a metering service provider, and retain evidence for the qualification decisions.

| Deliverable | Current position |
| --- | --- |
| Task 1: Company collection and spreadsheet-compatible download | Complete for the agreed collection/export stage. The dashboard collects Maps listings and downloads CSV files that open in Excel. This does not establish exhaustive Berlin coverage or verify every listing's relevance. |
| Task 2: Qualifying KPI columns and company assessments | Proposed; not implemented or populated yet. Service-specific criteria and the research method need agreement. |
| Bonus: Relevant named contact and direct work email | Not completed. Extracted website emails are candidates, not verified personal decision-maker contacts. |
| Task 3: Approach document | This editable draft, to be finalized after qualification and validation are completed. |

## 2. Sources and their roles

**Google Maps — candidate discovery.** Search queries identify possible property managers and provide listing details such as name, address, phone, website, category, review information, and Maps identifiers or links where available. These are discovery records, not independent proof of portfolio size, current business activity, or buying interest.

**Maps-linked business websites — optional contact extraction.** A separate action checks the listed website and linked contact/about-related pages for public email addresses and social links. A website attached to a Maps listing can be incorrect or belong to a different branch; company identity still requires review.

**Official company evidence — planned for Task 2.** Services, portfolio, About, legal-notice and relevant announcement pages will support Berlin relevance, management type, published portfolio scale, and service-fit findings. Reliable industry sources can help resolve gaps, but their origin and age must be labelled. LinkedIn or similar professional pages may support the optional contact research. These additional research steps have not yet been completed.

## 3. Automation implemented

```text
Search query in the dashboard
        |
        v
FastAPI -> Playwright -> full Chromium + bundled extension
        |
        v
Extension collects Google Maps business records
        |
        v
Results table + preserved raw CSV
        |
        +-- Optional: Get emails
        |       |
        |       v
        |   Extension checks websites and contact pages sequentially
        |       |
        |       v
        |   Live activity + updated rows + separate email-enriched CSV
        |
        +-- Next: evidence research and qualification (Task 2, proposed)
```

### Raw business collection

The researcher enters a query and selects **Fetch raw data**. Python launches Chromium with a project-local copy of the scraping extension, operates its controls, and receives its exported records. The extension performs Maps collection; Python does not replace it with a separate direct scraper.

The dashboard uses no numeric listing cap. The implemented list-collection logic continues through idle pauses and limited-view notices, collects across scrolling, and waits for the extension's explicit completion signal for the same run. The rendered end-of-list message is required for an end-of-list outcome. Single-place and explicit no-results pages are separate terminal outcomes.

Consent or verification boundaries, errors, manual stops, or the two-hour orchestration safety budget can interrupt collection. Available rows are retained as partial results where possible. Limited-view coverage remains recorded internally even if the exposed list ends. An available download or observed end marker does not prove that Google exposed every relevant business.

Raw collection does not perform website-email lookup. Available Maps fields are saved locally as a raw export, or a partial export when collection is interrupted.

### Separate website-email lookup

The researcher selects **Get emails** for existing results. This does not rerun Maps collection. The extension's background worker fetches and parses public website content, processing one business and one contact page at a time. Python relays the extension's events and merges the returned fields.

The implementation reports the business being checked, actual page-request URLs, fetching/extraction activity, skips and errors. These are service-worker HTTP requests, not simulated browser-tab navigation. The dashboard polls during lookup and updates the table and a separate CSV after each business finishes. Subsequent CSV checkpoints use atomic replacement; the raw export is not overwritten.

Requests have per-page timeouts and a 90-second business safety budget. On that business deadline, outstanding extension requests are aborted and allowed to settle before the next business starts. If settlement fails, the job stops rather than overlapping work. Missing websites, unsuccessful checks, and checked pages with no email found are distinguished.

### Tools, configuration and storage

- **Interface:** HTML, CSS and JavaScript, with the search form above the results area and recent results in the left sidebar.
- **Backend:** Python FastAPI, served locally on port 3000.
- **Browser automation:** Playwright with full Chromium and a bundled browser extension.
- **Visibility:** `HEADLESS=true` or `HEADLESS=false` in the project configuration controls new browser launches. Visible mode requires a graphical desktop.
- **Outputs:** Local, collision-safe CSV exports, readable in Excel. There is no native Google Sheets integration or `.xlsx` workbook generator in the current workflow.
- **State:** Job history and progress are held in memory; CSV files persist on disk. A database and automatic persistent job recovery are not implemented.
- **AI:** AI assistance has been used to develop the workflow and draft the methodology. An AI-powered company-KPI research/scoring pipeline is proposed, not integrated into the application.

## 4. Proposed qualification method — Task 2

Agree on the provider's exact metering services, relevant property types, Berlin eligibility rule, and any useful portfolio-size thresholds before assigning final ratings.

| Proposed KPI | Evidence to record | Qualification purpose |
| --- | --- | --- |
| Berlin property-management relevance | Official evidence that the business manages property in Berlin; distinguish an office address from managed-property evidence | Exclude unrelated businesses and establish geographic relevance |
| Management/property type | WEG, rental, residential, commercial or mixed management, with a supporting page | Match the company to the provider's actual services |
| Portfolio scale | Published units or buildings, date and scope; separate Berlin figures from company-wide figures | Understand potential customer scale without inventing numbers |
| Metering-service relevance | Relevant building operations, heating/water billing or other service evidence | Explain a plausible service match, not assumed demand |
| Opportunity signals | Dated expansion, new management contracts, modernization or digitization evidence | Prioritize further review without implying buying intent |
| Lead-fit rating and reason | High, Medium, Low or Needs research, applying agreed rules | Make the assessment understandable and consistent |
| Evidence confidence | Strong, Partial or Insufficient, with sources and date checked | Separate customer fit from the completeness of public evidence |

Review one company first to confirm that the criteria can be supported by public evidence, then proceed company by company. A future **Qualify leads** action would be separate from email lookup. KPI evidence and the rating would be added to an enriched derivative, leaving the raw collection intact.

Unknown values remain **Unknown**, not zero. Reviews and ratings are not proxies for managed units or buying fit. Missing contact details do not lower the company's underlying fit. Numerical scoring weights are not agreed.

## 5. Data validation and quality controls

### Implemented controls

- Raw collection and website-contact extraction are separate operations.
- One collection or email-lookup job can run at a time.
- Raw exports are preserved; email enrichment creates a separate derivative and checkpoints completed rows.
- Collection completion, partial outcomes and errors are distinguished in backend metadata. A hidden summary banner does not change that metadata.
- Existing identifiers and listing links are retained where supplied by the collector. These support later identity checks but do not establish company-level deduplication across exports.

### Validation still required for the research deliverable

1. **Confirm identity and Berlin relevance.** Match the listing to the official company using its name, legal notice, address, phone and website. Distinguish property managers from brokers, groups from branches, and similarly named businesses.
2. **Resolve duplicate companies.** Use place identifiers as clues and official identity evidence to reconcile multiple searches or branches. Preserve the original exports; do not merge on company name alone.
3. **Support every material KPI.** Record the value, specific source page, short supporting passage or note, date checked, and any scope or uncertainty. The current raw CSV does not yet contain this per-KPI evidence ledger.
4. **Review extracted contacts.** Confirm that an email belongs to the correct company and has an appropriate published purpose. Reject placeholders and wrong-branch or unrelated matches. Extraction is not deliverability verification.
5. **Verify the bonus contact separately.** Record a relevant person's published role and direct professional email or profile. A generic inbox does not satisfy the personal-contact bonus. Never guess an email pattern.
6. **Review the final ratings.** Check that each decision follows the agreed rubric and that missing evidence has not been treated as negative evidence.

These research-validation steps are planned, not claims that every collected record has already been checked.

## 6. Limits, review status and finalization

Google Maps results depend on the query and what the site exposes. An end-of-exposed-list outcome is not a guarantee of comprehensive city coverage. Websites may be inaccessible, outdated, dynamically rendered or incorrectly linked. Publicly available evidence may not disclose portfolio size, an incumbent metering provider or purchasing authority. None of those facts should be inferred without support.

The recent collector-completion and live email-progress changes are implemented in source, but their latest end-to-end behavior has not yet been exercised under the current testing deferral. Application startup, an exported file, and extracted contact text are not substitutes for record validation. No new tests, collection, email lookup or company research were performed to prepare this draft.

Before issuing the final methodology document:

- Confirm the qualification rules and complete the agreed company research.
- Retain the actual queries, research period, source/evidence ledger, and relevant collection outcomes.
- Summarize the real final dataset, duplicate resolution, unknown fields and validation findings; do not invent coverage or quality figures.
- State precisely which automation was exercised, what was manually reviewed, and what remains uncertain.
- Review the wording and choose the final shareable document or presentation format.

No outreach is sent by this workflow. Contact discovery and qualification do not authorize contacting prospects. No consent or verification bypass is part of the method.
