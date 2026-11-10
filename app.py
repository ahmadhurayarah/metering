from __future__ import annotations

import asyncio
import csv
import io
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field, field_validator

from extension_bundle import run_hermes_scrape as extension_scraper
from csv_output import CSV_FIELDS, query_to_filename, save_results, replace_results


BASE_DIR = Path(__file__).resolve().parent
STATIC_FILE = BASE_DIR / "static" / "index.html"
OUTPUT_DIR = BASE_DIR / "output"


app = FastAPI(title="Metering Lead Finder", version="0.1.0")
JOBS: dict[str, dict[str, Any]] = {}
ACTIVE_JOB_ID: str | None = None
BACKGROUND_TASKS: set[asyncio.Task] = set()


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
        "fields": CSV_FIELDS,
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
    job["enrichment_status"] = "running"
    job["updated_at"] = _now()
    job["log"].append("Checking businesses and website pages one at a time through the extension.")
    rows = [dict(row) for row in job["results"]]
    email_path = None

    async def on_progress(event):
        phase = event.get("phase", "starting")
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
    if not any(str(row.get("website", "")).strip() for row in job.get("results", [])):
        raise HTTPException(status_code=409, detail="These results have no websites to check")
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
    writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS, extrasaction="ignore")
    writer.writeheader()
    for row in job.get("results", []):
        writer.writerow({field: row.get(field, "") for field in CSV_FIELDS})
    filename = query_to_filename(job["query"]) + ".csv"
    return Response(
        content=stream.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
