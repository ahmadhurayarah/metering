from __future__ import annotations

import asyncio
import csv
import io
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field, field_validator

from extension_bundle import run_hermes_scrape as extension_scraper
from csv_output import CSV_FIELDS, query_to_filename, save_results, replace_results, excel_bytes


BASE_DIR = Path(__file__).resolve().parent
STATIC_FILE = BASE_DIR / "static" / "index.html"
OUTPUT_DIR = BASE_DIR / "output"


app = FastAPI(title="Metering Lead Finder", version="0.1.0")
JOBS: dict[str, dict[str, Any]] = {}
ACTIVE_JOB_ID: str | None = None
BACKGROUND_TASKS: set[asyncio.Task] = set()
ACTIVE_ENRICHMENT_STATUSES = {"queued", "running", "pausing", "paused", "resuming"}


def _job_is_active(job: dict[str, Any]) -> bool:
    return job.get("status") in {"queued", "running"} or job.get("enrichment_status") in ACTIVE_ENRICHMENT_STATUSES

# Derived fields only: the original Maps rows and saved raw/email CSVs stay intact.
KPI_VERSION = "maps-category-v1"
KPI_CRITERIA = {
    "Accepted": "The Maps category explicitly identifies property management. This is a provisional shortlist candidate, not verified metering-customer fit.",
    "Rejected": "The Maps category is clearly unrelated to property management and the name has no conflicting property-management signal. Rows are retained, not deleted.",
    "Needs review": "Missing or ambiguous categories, broker-only/adjacent services, name-only matches, or conflicting listing signals need human review.",
    "Berlin relevance": "Distinguish a Berlin address from search-query context or a name signal. A street-only address, search query or Berlin-named street does not verify Berlin coverage.",
    "Contact details": "Website and phone presence are separate completeness KPIs, not acceptance rules. Website links and extracted emails remain unverified.",
    "Unknowns": "Portfolio size and metering-service fit remain Unknown. Ratings/review counts never determine qualification, scale or buying intent.",
}
KPI_FIELDS = [
    "qualification_status", "qualification_reason", "property_management_match",
    "berlin_relevance", "website_listed", "phone_listed", "portfolio_size",
    "metering_service_fit", "kpi_basis", "kpi_source",
]
RESULT_FIELDS = [
    "name", "qualification_status", "phone", "email", "qualification_reason",
    *[field for field in CSV_FIELDS if field not in {"name", "phone", "email"}],
    *[field for field in KPI_FIELDS if field not in {"qualification_status", "qualification_reason"}],
]
PROPERTY_MANAGEMENT = re.compile(
    r"\bproperty[\s-]*(?:management|administration)\b|"
    r"\breal[\s-]+estate[\s-]+management\b|"
    r"\b(?:haus|immobilien|wohnungs|miet|weg)[\s-]*verwaltung\w*",
    re.IGNORECASE,
)
UNRELATED_CATEGORY = re.compile(
    r"\b(?:restaurant|cafe|café|coffee shop|bakery|bar|pub|school|college|university|"
    r"education\w*|kindergarten|day care|hospital|clinic|dentist|doctor|pharmacy|"
    r"supermarket|grocery|boutique|clothing|retail|shopping|store|hair\w*|beauty|salon|"
    r"gym|fitness|hotel|hostel|lawyer|law firm|accountant|car dealer|auto repair|"
    r"roofing|waterproofing|plumber|plumbing|electrician|moving company|courier|"
    r"schule|schulen|schulzentrum|bildungszentrum|gaststätte|bäckerei|friseur|apotheke)\b",
    re.IGNORECASE,
)
BERLIN_ADDRESS = re.compile(
    r"\b1[0-4]\d{3}\s+berlin\b|\bberlin\s+1[0-4]\d{3}\b|"
    r"(?:^|,)\s*berlin(?:\s*,\s*(?:germany|deutschland))?\s*$",
    re.IGNORECASE,
)


def qualify_maps_row(row: dict[str, Any], query: str) -> dict[str, Any]:
    """Provisional category screening, not verified service, scale or buying fit."""
    category = str(row.get("category") or "").strip()
    name = str(row.get("name") or "").strip()
    address = str(row.get("address") or "").strip()
    category_match = bool(PROPERTY_MANAGEMENT.search(category))
    name_match = bool(PROPERTY_MANAGEMENT.search(name))
    unrelated = bool(UNRELATED_CATEGORY.search(category))

    # A street named Berliner Straße and a Berlin search are not city verification.
    if BERLIN_ADDRESS.search(address):
        berlin = "Address states Berlin (Maps)"
        location_note = "The Maps address states Berlin; managed-property coverage is unverified."
    elif re.search(r"\bberlin\b", query, re.IGNORECASE):
        berlin = "Search context only - unverified"
        location_note = "Berlin comes from the search query only; actual coverage is unverified."
    elif re.search(r"\bberlin\b", name, re.IGNORECASE):
        berlin = "Name signal only - unverified"
        location_note = "The name mentions Berlin, but location/coverage is unverified."
    else:
        berlin = "Not established"
        location_note = "Berlin location/coverage is not established by this listing."

    if unrelated and (category_match or name_match):
        status = "Needs review"
        match = "Conflicting listing signals"
        reason = f"Maps category '{category}' conflicts with a property-management signal; confirm the business type."
    elif category_match:
        status = "Accepted"
        match = "Category match"
        reason = f"Maps category '{category}' identifies a property-management candidate. " + location_note
    elif unrelated:
        status = "Rejected"
        match = "Unrelated category"
        reason = f"Maps category '{category}' is outside this property-management shortlist; no property-management signal is listed."
    elif name_match:
        status = "Needs review"
        match = "Name signal only"
        reason = "The name suggests property management, but the Maps category does not confirm it."
    else:
        status = "Needs review"
        match = "Not established"
        reason = (f"Maps category '{category}' does not establish property-management activity; "
                  "brokers and adjacent services require review." if category else
                  "Maps category is missing; the business type cannot be established.")

    return {
        **row,
        "qualification_status": status,
        "qualification_reason": reason,
        "property_management_match": match,
        "berlin_relevance": berlin,
        "website_listed": "Yes" if str(row.get("website") or "").strip() else "No",
        "phone_listed": "Yes" if str(row.get("phone") or "").strip() else "No",
        "portfolio_size": "Unknown - not supplied by Maps",
        "metering_service_fit": "Unknown - needs service-specific research",
        "kpi_basis": "Google Maps category screening v1 - provisional",
        "kpi_source": str(row.get("maps_url") or "").strip(),
    }


def _qualified_rows(job: dict[str, Any]) -> list[dict[str, Any]]:
    # New dictionaries preserve source fields, row order and stable identifiers.
    return [qualify_maps_row(row, job["query"]) for row in job.get("results", [])]


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=200)
    # Collection is always uncapped and raw-only; website lookup is a separate action.

    @field_validator("query")
    @classmethod
    def normalize_query(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Enter a search query")
        return value


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _email_count(rows: list[dict[str, Any]]) -> int:
    return sum(1 for row in rows if str(row.get("email", "")).strip())


def _public_job(job: dict[str, Any], include_results: bool) -> dict[str, Any]:
    result = {
        "job_id": job["job_id"],
        "query": job["query"],
        "limit": job["limit"],
        "crawl_websites": job["crawl_websites"],
        "status": job["status"],
        "enrichment_status": job.get("enrichment_status", "not_started"),
        "enrichment_error": job.get("enrichment_error"),
        "enrichment_progress": job.get("enrichment_progress"),
        "results_revision": job.get("results_revision", 0),
        "created_at": job["created_at"],
        "updated_at": job["updated_at"],
        "result_count": len(job.get("results", [])),
        "email_count": _email_count(job.get("results", [])),
        "fields": RESULT_FIELDS if job.get("kpi_status") == "completed" else CSV_FIELDS,
        "kpi_status": job.get("kpi_status", "not_started"),
        "kpi_version": job.get("kpi_version"),
        "kpi_completed_at": job.get("kpi_completed_at"),
        "kpi_csv_path": job.get("kpi_csv_path"),
        "email_csv_path": job.get("email_csv_path"),
        "excel_download_url": f"/api/jobs/{job['job_id']}/excel",
        "csv_path": job.get("csv_path"),
        "raw_csv_path": job.get("raw_csv_path"),
        "output_file": Path(job["csv_path"]).name if job.get("csv_path") else None,
        "warning": job.get("warning"),
        "error": job.get("error"),
        "stop_reason": job.get("stop_reason"),
        "log": job.get("log", []),
        "download_url": f"/api/jobs/{job['job_id']}/csv",
    }
    if include_results:
        result["results"] = job.get("results", [])
        result["kpi_criteria"] = job.get("kpi_criteria", KPI_CRITERIA)
        result["enrichment_events"] = job.get("enrichment_events", [])
    return result


async def run_scraper(query: str, limit: int = 0) -> dict[str, Any]:
    """Collect uncapped Maps records through the extension, without website lookup."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    try:
        leads = await extension_scraper.scrape_google_maps(
            query=query, limit=limit, output_dir=None, crawl_websites=False
        )
    except extension_scraper.CollectionIncomplete as exc:
        partial_path = save_results(exc.leads, query + "_partial", OUTPUT_DIR)
        return {
            "status": "partial", "results": exc.leads, "warning": str(exc),
            "csv_path": str(partial_path), "raw_csv_path": str(partial_path),
            "stop_reason": exc.stop_reason,
        }
    raw_path = save_results(leads, query + "_raw", OUTPUT_DIR)
    return {
        "status": "partial" if getattr(leads, "coverage_warning", None) else "completed", "results": leads,
        "csv_path": str(raw_path), "raw_csv_path": str(raw_path),
        "warning": getattr(leads, "coverage_warning", None),
        "stop_reason": getattr(leads, "stop_reason", None),
    }

async def _execute_job(job_id: str) -> None:
    global ACTIVE_JOB_ID
    job = JOBS[job_id]
    job["status"] = "running"
    job["updated_at"] = _now()
    job["log"].append("Loading the bundled scraping extension in Chromium. The extension collects Maps records.")
    try:
        outcome = await run_scraper(
            job["query"], job["limit"]
        )
        job["status"] = outcome.get("status", "completed")
        job["results"] = outcome.get("results", [])
        job["results_revision"] = job.get("results_revision", 0) + 1
        job["csv_path"] = outcome.get("csv_path")
        job["raw_csv_path"] = outcome.get("raw_csv_path")
        job["warning"] = outcome.get("warning")
        job["stop_reason"] = outcome.get("stop_reason")
        if job["stop_reason"]:
            job["log"].append(f"Extension stop reason: {job['stop_reason']}.")
        job["log"].append(
            f"Finished with {len(job['results'])} result(s); "
            f"{_email_count(job['results'])} listing(s) contain an extracted email."
        )
    except Exception as exc:
        job["status"] = "failed"
        job["error"] = f"{type(exc).__name__}: {exc}"
        job["log"].append("The search failed. See the error details.")
    finally:
        job["updated_at"] = _now()
        if ACTIVE_JOB_ID == job_id:
            ACTIVE_JOB_ID = None


async def _execute_email_job(job_id: str) -> None:
    global ACTIVE_JOB_ID
    job = JOBS[job_id]
    job["enrichment_status"] = "pausing" if job.get("email_pause_requested") else "running"
    job["updated_at"] = _now()
    job["log"].append("Checking businesses and website pages one at a time through the extension.")
    rows = [dict(row) for row in job["results"]]
    email_path = None

    async def on_progress(event):
        phase = event.get("phase", "starting")
        resume_event = job.get("_email_resume_event")
        if phase in {"starting", "business_start"} and resume_event is not None and not resume_event.is_set():
            job["enrichment_status"] = "paused"
            paused_progress = dict(job.get("enrichment_progress") or {})
            paused_progress.update({
                "phase": "paused",
                "current": event.get("current", paused_progress.get("current", 0)),
                "total": event.get("total", len(rows)),
                "name": event.get("name", ""),
                "url": event.get("url", ""),
                "message": "Paused between listings",
            })
            job["enrichment_progress"] = paused_progress
            job["updated_at"] = _now()
            await resume_event.wait()
            job["email_pause_requested"] = False
            job["enrichment_status"] = "running"
            job["updated_at"] = _now()
        messages = {
            "starting": "Starting the extension for email lookup",
            "business_start": "Checking website",
            "fetching": "Fetching website page",
            "page_loaded": "Page loaded",
            "extracting": "Extracting email addresses from the page",
            "page_error": "Could not fetch this page",
            "completed": "Email lookup finished",
            "failed": "Email lookup stopped; completed rows were preserved",
        }
        if phase == "business_done":
            message = {"found": "Email available", "not_found": "No email found on the checked pages",
                       "skipped": "Skipped - no website", "error": "Website lookup incomplete"}.get(
                           event.get("outcome"), "Business processed")
        else:
            message = messages.get(phase, phase)
        if event.get("error"):
            message += ": " + str(event["error"])
        entry = {**event, "at": event.get("at") or _now(), "message": message}
        progress = dict(job.get("enrichment_progress") or {})
        progress.update(entry)
        progress["error"] = event.get("error", "")
        if phase == "page_loaded":
            progress["pages_visited"] = progress.get("pages_visited", 0) + 1
        job["enrichment_progress"] = progress
        events = job.setdefault("enrichment_events", [])
        events.append(entry)
        del events[:-80]
        if phase == "business_done":
            job["log"].append(f"{event['completed']}/{len(rows)} - {event.get('name', '')}: {message}")
        job["updated_at"] = _now()

    async def on_row(index, row):
        nonlocal email_path
        rows[index] = dict(row)
        # Keep one derivative file, refreshing it atomically after each business.
        # The saved raw CSV is never overwritten, including on failure/retry.
        if email_path is None:
            email_path = await asyncio.to_thread(save_results, rows, job["query"] + "_emails", OUTPUT_DIR)
        else:
            await asyncio.to_thread(replace_results, rows, email_path)
        job["results"] = [dict(value) for value in rows]
        job["csv_path"] = str(email_path)
        job["results_revision"] = job.get("results_revision", 0) + 1
        job["updated_at"] = _now()

    try:
        enriched = await extension_scraper.enrich_leads_with_websites(
            rows, on_progress=on_progress, on_row=on_row
        )
        if len(enriched) != len(rows):
            raise RuntimeError("Email lookup changed the number of business rows")
        if email_path is None:
            email_path = await asyncio.to_thread(save_results, enriched, job["query"] + "_emails", OUTPUT_DIR)
        else:
            await asyncio.to_thread(replace_results, enriched, email_path)
        job["results"] = enriched
        job["csv_path"] = str(email_path)
        job["results_revision"] = job.get("results_revision", 0) + 1
        job["enrichment_status"] = "completed"
        await on_progress({"phase": "completed", "completed": len(enriched), "total": len(enriched),
                           "current": len(enriched), "name": "", "url": ""})
        job["log"].append(f"Email lookup finished; {_email_count(enriched)} listing(s) contain an extracted email.")
    except Exception as exc:
        job["enrichment_status"] = "failed"
        job["enrichment_error"] = f"{type(exc).__name__}: {exc}"
        await on_progress({"phase": "failed", "error": job["enrichment_error"]})
        job["log"].append("Email lookup stopped. Completed rows, their export and the raw CSV were preserved.")
    finally:
        job["updated_at"] = _now()
        if ACTIVE_JOB_ID == job_id:
            ACTIVE_JOB_ID = None


@app.post("/api/jobs/{job_id}/emails", status_code=202)
async def start_email_lookup(job_id: str) -> dict[str, Any]:
    global ACTIVE_JOB_ID
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Search job not found")
    if ACTIVE_JOB_ID:
        raise HTTPException(status_code=409, detail="A collection or email lookup is already running")
    if job["status"] not in {"completed", "partial"}:
        raise HTTPException(status_code=409, detail="Fetch raw business data first")
    if job.get("enrichment_status") == "completed":
        raise HTTPException(status_code=409, detail="Emails have already been fetched for these results")
    if not job.get("results"):
        raise HTTPException(status_code=409, detail="There are no listings to process")
    if not any(str(row.get("website") or "").strip() for row in job["results"]):
        # An explicit click can finish an all-skipped stage without launching a browser.
        count = len(job["results"])
        job["enrichment_status"] = "completed"
        job["enrichment_error"] = None
        job["enrichment_events"] = [
            {"phase": "business_done", "outcome": "skipped", "current": index + 1,
             "completed": index + 1, "name": row.get("name", ""), "at": _now()}
            for index, row in enumerate(job["results"])
        ][-80:]
        job["enrichment_progress"] = {
            "phase": "completed", "completed": count, "current": count,
            "total": count, "skipped": count, "failed": 0, "pages_visited": 0,
        }
        job["updated_at"] = _now()
        job["log"].append("Email stage finished: every listing had no website; all skipped and no pages fetched.")
        return {"job_id": job_id, "status": "completed", "operation": "emails"}
    job["enrichment_status"] = "queued"
    job["enrichment_error"] = None
    job["enrichment_events"] = []
    job["enrichment_progress"] = {
        "phase": "queued", "current": 0, "completed": 0,
        "total": len(job["results"]), "failed": 0, "skipped": 0,
        "pages_visited": 0, "name": "", "url": "",
        "message": "Email lookup queued",
    }
    job["updated_at"] = _now()
    ACTIVE_JOB_ID = job_id
    task = asyncio.create_task(_execute_email_job(job_id))
    BACKGROUND_TASKS.add(task)
    task.add_done_callback(BACKGROUND_TASKS.discard)
    return {"job_id": job_id, "status": "queued", "operation": "emails"}


@app.post("/api/jobs/{job_id}/kpi")
async def run_kpi(job_id: str) -> dict[str, Any]:
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Search job not found")
    if ACTIVE_JOB_ID:
        raise HTTPException(status_code=409, detail="Wait for the active collection or email lookup to finish")
    if job["status"] not in {"completed", "partial"} or not job.get("results"):
        raise HTTPException(status_code=409, detail="Fetch business listings first")
    if job.get("enrichment_status") != "completed":
        raise HTTPException(status_code=409, detail="Finish Get emails before running KPI qualification")
    if job.get("kpi_status") == "completed":
        return _public_job(job, include_results=True)

    qualified = _qualified_rows(job)
    # No await between the concurrency guard and this short local transaction.
    # A separate derivative preserves both raw and email-stage source exports.
    path = save_results(qualified, job["query"] + "_qualified", OUTPUT_DIR, fields=RESULT_FIELDS)
    job["email_csv_path"] = job.get("csv_path")
    job["kpi_csv_path"] = str(path)
    job["csv_path"] = str(path)
    job["results"] = qualified
    job["kpi_status"] = "completed"
    job["kpi_version"] = KPI_VERSION
    job["kpi_criteria"] = dict(KPI_CRITERIA)
    job["kpi_completed_at"] = _now()
    job["results_revision"] = job.get("results_revision", 0) + 1
    job["updated_at"] = _now()
    job["log"].append("Maps-only KPI screening completed on explicit request; original rows retained in order.")
    return _public_job(job, include_results=True)


@app.get("/api/jobs/{job_id}/excel")
async def download_excel(job_id: str) -> Response:
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Search job not found")
    if job.get("kpi_status") != "completed":
        raise HTTPException(status_code=409, detail="Run KPI qualification before downloading Excel")
    criteria = {
        "Search query": job["query"],
        "KPI version": job.get("kpi_version", KPI_VERSION),
        "Qualified at": job.get("kpi_completed_at", ""),
        **job.get("kpi_criteria", KPI_CRITERIA),
        "Email stage": "Finished processing; blocked pages and missing emails do not prove no public email exists.",
        "Colors": "Green = Accepted; red = Rejected; unfilled = Needs review. Acceptance is provisional Maps category fit only.",
    }
    content = await asyncio.to_thread(excel_bytes, job["results"], RESULT_FIELDS, criteria)
    filename = query_to_filename(job["query"]) + "_qualified.xlsx"
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/", response_class=HTMLResponse)
async def dashboard() -> HTMLResponse:
    if not STATIC_FILE.is_file():
        raise HTTPException(status_code=503, detail="Dashboard HTML is missing")
    return HTMLResponse(STATIC_FILE.read_text(encoding="utf-8"))


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "app": "Metering Lead Finder", "scraper": "headless-chromium-extension"}


@app.post("/api/search", status_code=202)
async def start_search(request: SearchRequest) -> dict[str, Any]:
    global ACTIVE_JOB_ID
    if ACTIVE_JOB_ID:
        active = JOBS.get(ACTIVE_JOB_ID)
        if active and (active["status"] in {"queued", "running"} or active.get("enrichment_status") in {"queued", "running"}):
            raise HTTPException(status_code=409, detail="A Maps search is already running")
        ACTIVE_JOB_ID = None

    job_id = str(uuid.uuid4())
    JOBS[job_id] = {
        "job_id": job_id,
        "query": request.query,
        "limit": 0,
        "crawl_websites": False,
        "enrichment_status": "not_started",
        "enrichment_error": None,
        "status": "queued",
        "created_at": _now(),
        "updated_at": _now(),
        "results": [],
        "csv_path": None,
        "raw_csv_path": None,
        "warning": None,
        "error": None,
        "log": ["Search accepted and queued."],
    }
    ACTIVE_JOB_ID = job_id
    task = asyncio.create_task(_execute_job(job_id))
    BACKGROUND_TASKS.add(task)
    task.add_done_callback(BACKGROUND_TASKS.discard)
    return {"job_id": job_id, "status": "queued", "query": request.query}


@app.get("/api/jobs")
async def list_jobs() -> dict[str, Any]:
    recent = sorted(JOBS.values(), key=lambda job: job["created_at"], reverse=True)[:25]
    return {"jobs": [_public_job(job, include_results=False) for job in recent]}


@app.delete("/api/jobs")
async def clear_jobs() -> dict[str, int]:
    global ACTIVE_JOB_ID
    active_jobs = [
        job for job in JOBS.values()
        if job["status"] in {"queued", "running"}
        or job.get("enrichment_status") in {"queued", "running"}
    ]
    if active_jobs:
        raise HTTPException(
            status_code=409,
            detail="Wait for active collection or email lookup work to finish before clearing history",
        )
    cleared_count = len(JOBS)
    JOBS.clear()
    ACTIVE_JOB_ID = None
    return {"cleared_count": cleared_count, "remaining": len(JOBS)}


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str) -> dict[str, Any]:
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Search job not found")
    return _public_job(job, include_results=True)


@app.get("/api/jobs/{job_id}/csv")
async def download_csv(job_id: str) -> Response:
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Search job not found")
    if job["status"] not in {"completed", "partial"}:
        raise HTTPException(status_code=409, detail="Results are not ready to download")

    stream = io.StringIO(newline="")
    fields = RESULT_FIELDS if job.get("kpi_status") == "completed" else CSV_FIELDS
    writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for row in job.get("results", []):
        writer.writerow({field: row.get(field, "") for field in fields})
    suffix = "_qualified.csv" if job.get("kpi_status") == "completed" else ".csv"
    filename = query_to_filename(job["query"]) + suffix
    return Response(
        content=stream.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
