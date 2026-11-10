# Task 3: Research Approach and Validation

**Berlin property-management prospects for a metering service provider**  
**Version:** 0.2 - Draft for review

This draft describes the implemented collection, email-lookup, Maps-based KPI screening and Excel-export workflow. It distinguishes a provisional candidate shortlist from verified customer suitability. It is not a final report of validated companies or verified decision-maker contacts. This Markdown file is the editable source for the accompanying Word draft.

## 1. Objective and current position

The objective is to identify potential Berlin property-management customers, retain the source listing data, explain screening decisions and document the additional evidence needed before outreach.

| Deliverable | Current position |
| --- | --- |
| Task 1: Company collection and spreadsheet-compatible download | The agreed Maps collection and CSV-export milestone is complete. Raw exports remain preserved. The staged final download now has a native Excel workbook exporter implemented. Collection does not establish exhaustive Berlin coverage. |
| Task 2: KPI criteria and company screening | An explicit Run KPI action is implemented after the email stage. It applies transparent Maps-category rules and adds status, rationale and supporting KPI fields. It does not automatically qualify records during collection or email lookup. Service-specific research remains future work. |
| Bonus: Relevant named contact and direct work email | Not completed. Extracted public website emails are candidates, not verified personal decision-maker contacts. |
| Task 3: Approach document | This updated Markdown draft and its editable Word counterpart. Finalization depends on actual research validation and review. |

## 2. Sources and evidence boundaries

**Google Maps - discovery and initial screening.** Listings can provide name, category, address, phone, website, ratings, review counts and stable identifiers. These fields support a category-based shortlist. They do not independently establish managed units, current operating scale, metering needs, buying intent or purchasing authority.

**Search context - not geographic proof.** A business returned for a Berlin query is labelled as having Berlin search context. A street-only address or a street named Berliner Straße does not establish a Berlin office or managed-property coverage. A Maps address that explicitly states Berlin is a stronger listing signal, but still does not prove that the business manages properties there.

**Maps-linked websites - contact extraction.** The extension checks public website content for email addresses and social links. A listing's website can be an advertisement, tracking URL, outdated link or wrong-branch website. A listed link is not automatically verified as the official company website.

**Official company sources - deeper research still required.** Company services, portfolio, legal-notice, About and announcement pages should support final claims about identity, management type, portfolio scale, Berlin coverage and metering relevance. Professional profiles may support a published named contact. These facts must not be inferred from the Maps category alone.

## 3. Explicit four-stage workflow

```text
Fetch raw data
    |
    v
Get emails
    |
    v
Run KPI
    |
    v
Download Excel (.xlsx)
```

Each action starts only when selected. Run KPI requires a finished email-processing stage; the final Excel download requires completed KPI screening. These gates are enforced by the backend as well as the interface.

### Stage 1: Fetch raw data

FastAPI launches full Chromium through Playwright, loads the project-local scraping extension and operates its controls. The extension collects Maps records; Python does not replace it with a separate website or Maps crawler.

Collection has no numeric listing cap. The implemented scrolling logic continues through temporary idle periods and limited-view notices, and waits for the extension's explicit completion signal. List completion requires the exposed end-of-list marker. Single-place and explicit no-results pages have separate outcomes.

Verification boundaries, errors, manual stops or the orchestration safety budget can interrupt collection. Available rows are retained as partial results where possible. An exposed-list end marker or successful export does not guarantee exhaustive coverage. Raw collection does not visit business websites for email harvesting.

### Stage 2: Get emails

The extension's background worker processes one business at a time and one website/contact page at a time. Python relays events, merges returned contact fields and checkpoints completed businesses into a separate email-stage CSV. The original raw export is not overwritten.

The Email cell shows the actual lookup state, including an animated Fetching indicator for the active lookup. Existing email values remain visible. Missing websites, checked pages with no email found, HTTP failures and blocked pages are distinct outcomes. HTTP 403 is a failed or blocked check, not proof that an email does not exist.

The stage can finish with missing emails or incomplete website checks. Finishing means the processing pass ended, not that every website was accessible. If all listings lack websites, an explicit Get emails action records them as skipped without launching a browser. A job-level failure must be resolved or retried before KPI screening can start.

Requests have page timeouts and a business-level safety deadline. On that deadline, pending requests are aborted and allowed to settle before the next business. If settlement fails, processing stops rather than overlapping work. No email is sent.

### Stage 3: Run KPI

The researcher can inspect the criteria in the results toolbar and then select Run KPI. This local, deterministic operation uses the existing Maps fields and original query. It does not fetch additional websites, rerun Maps, invoke AI research or trigger outreach.

The operation preserves every row and its original values, adds the derived KPI fields, and saves a separate qualified CSV. The raw and email-stage files remain available. Rejected records are retained for audit rather than deleted. Missing contacts do not automatically reject an otherwise relevant company.

### Stage 4: Download Excel

After KPI screening, Download Excel generates a genuine .xlsx workbook, not a CSV renamed as Excel. It includes:

- **Qualified leads:** all original listing fields and the KPI fields, with filtering and frozen headings/key columns.
- **KPI criteria:** the applied rules, source query, qualification version/time and interpretation limits.

Accepted rows are green across every data column. Rejected rows are red across every data column. Needs review rows have no status fill. The dashboard uses the same status meanings and full-row coloring. Colors supplement the written status and reason; they do not replace them.

Contact values are exported as literal text rather than spreadsheet formulas. There is no numeric lead score or formula-based buying prediction.

## 4. Maps-only KPI criteria

These rules are provisional candidate-screening rules, not a final sales-qualification rubric.

| Decision | Rule | Interpretation |
| --- | --- | --- |
| Accepted - green | The Maps category explicitly indicates property management, including supported English/German property-management category terms, with no conflicting unrelated category signal. | A category-matched candidate worth further research. This is not proof of Berlin coverage, managed units, metering demand or buying interest. |
| Rejected - red | The Maps category is clearly unrelated to the property-management shortlist, and no conflicting property-management name/category signal exists. | Excluded from this specific shortlist; not a judgment on business quality. The record is retained. |
| Needs review - neutral | Category is missing, ambiguous, broker-only or adjacent; only the name suggests management; or listing signals conflict. | Do not invent a category or treat missing evidence as a confirmed mismatch. Human review is required. |

Ratings and review counts do not drive acceptance, rejection, portfolio estimates or buying-fit judgments. Website and phone presence are completeness indicators only. No minimum portfolio threshold or weighted numeric score has been agreed.

### Added KPI fields

| Field | Meaning |
| --- | --- |
| qualification_status | Accepted, Rejected or Needs review. |
| qualification_reason | The rule-based rationale, including relevant uncertainty. |
| property_management_match | Category match, unrelated category, name-only signal, conflicting signals or not established. |
| berlin_relevance | Address states Berlin, search context only, name signal only or not established. Listing evidence remains unverified. |
| website_listed | Whether the listing contains a website value; not confirmation of an official or accessible website. |
| phone_listed | Whether the listing contains a phone value; not ownership or deliverability verification. |
| portfolio_size | Unknown because Maps does not supply reliable managed-unit/building totals. |
| metering_service_fit | Unknown pending service-specific evidence and the provider's confirmed offer. |
| kpi_basis | The provisional Maps-category screening method and version. |
| kpi_source | The original Maps listing link, where supplied. |

## 5. Tools, configuration and storage

- **Interface:** HTML, CSS and JavaScript; search above the workspace, history on the left and the results table on the right.
- **Backend:** FastAPI on localhost port 3000. The active Python components are app.py, csv_output.py and the bundled extension runner.
- **Collection and email extraction:** Playwright, full Chromium and the project-local extension. The original extension project is not modified.
- **Visibility:** HEADLESS=true or HEADLESS=false controls new browser launches. Visible mode requires a graphical desktop.
- **Exports:** CSV source/checkpoint files and an openpyxl-generated Excel workbook. There is no native Google Sheets integration.
- **Documents:** This Markdown master is converted to an editable Word draft using structured headings, paragraphs and tables.
- **State:** Jobs and workflow state are in memory. CSV files persist on disk. Manual history snapshots can preserve state during planned restarts; automatic database-backed recovery is not implemented.
- **AI boundary:** AI assistance supports development and document drafting. The current KPI action is rule-based, not an integrated AI company-research agent.

## 6. Validation still required

1. **Confirm company identity.** Match the listing against the official company, legal notice and contact details; distinguish brokers, managers, branches and similarly named businesses.
2. **Verify Berlin relevance.** Establish the relevant office or managed-property coverage from appropriate evidence, not the search query alone.
3. **Resolve duplicates carefully.** Use stable identifiers and official identity evidence. A shared name or website does not automatically justify merging branches or companies.
4. **Support material KPIs.** Record the specific source page, supporting statement, date and scope for management type, portfolio scale and service relevance.
5. **Review contacts.** Confirm the correct company/branch and the published purpose of an email. Extraction does not verify deliverability or decision-maker authority.
6. **Review final decisions.** Reassess provisional category acceptance against the provider's actual services and agreed eligibility rules. A green row is not a validated customer endorsement.

The controls and new staged KPI/Excel workflow are implemented in source. Application startup and history restoration were checked, but the new KPI action and Excel download have not been exercised under the current application-testing deferral. No new collection, website-email lookup or KPI run was started to prepare this revision. Document conversion is separate from application verification.

## 7. Future work

### Provider-blocked public websites

Improve access handling for public pages protected or intercepted by Cloudflare and other website-security providers. First identify the actual response/challenge provider rather than attributing every HTTP 403 to Cloudflare. The cloudflared tunnel client and a website's Cloudflare protection are different components.

Investigate normal browser-backed rendering for public pages when permitted, with sequential processing, bounded timeouts and truthful outcomes. Where automated access is not permitted or remains challenged, use owner-approved allowlisting, an official API/feed, permission-based access or manual review. Keep Blocked distinct from No email found and retain the reason and source URL.

This is planned work, not a claim that a bypass exists. No authentication, CAPTCHA or access-control bypass is implemented or promised, and protected content must not be treated as accessible merely because a fallback is planned.

### Further qualification and reliability improvements

- Confirm the provider's exact metering services, target property types, geographic eligibility and any meaningful portfolio-size requirements before adding deeper fit rules.
- Add official-source research for management type, published portfolio, relevant building operations and dated opportunity signals, with evidence and uncertainty recorded separately.
- Persist per-business lookup outcomes, blocked-page reasons and evidence timestamps beyond the recent in-memory event window.
- Add durable job history, interruption recovery, explicit retry controls and versioned qualification rules.
- Improve identity/branch matching and company-level deduplication without silently discarding original records.
- Research relevant named professional contacts only from published, attributable sources; never infer personal email patterns.
- Validate the staged workflow and workbook output when application testing is authorized, then review the actual dataset before finalizing the methodology.

## 8. Limits and finalization

Maps coverage depends on the query and what the site exposes. Websites may be blocked, outdated, dynamic or incorrectly linked. Public evidence may not disclose portfolio scale, an incumbent metering provider, commercial intent or purchasing authority. Unknown values remain Unknown, not zero.

Before issuing a final report, retain the actual queries and research period, review the company/evidence ledger, document duplicate resolution and unresolved gaps, and state precisely which automation was exercised and which records were manually validated. This remains a draft for review. Contact discovery and screening do not authorize outreach.
