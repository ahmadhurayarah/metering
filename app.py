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

import maps_scraper


BASE_DIR = Path(__file__).resolve().parent
STATIC_FILE = BASE_DIR / "static" / "index.html"
OUTPUT_DIR = BASE_DIR / "output"
CSV_FIELDS = maps_scraper.CSV_FIELDS

app = FastAPI(title="Metering Lead Finder", version="0.1.0")
JOBS: dict[str, dict[str, Any]] = {}
ACTIVE_JOB_ID: str | None = None
BACKGROUND_TASKS: set[asyncio.Task] = set()


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=200)
    limit: int = Field(default=999, ge=1, le=999)
    crawl_websites: bool = True

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
        "log": job.get("log", []),
        "download_url": f"/api/jobs/{job['job_id']}/csv",
    }
    if include_results:
        result["results"] = job.get("results", [])
    return result


async def run_scraper(query: str, limit: int, crawl_websites: bool) -> dict[str, Any]:
    """Run the local standalone Maps collector and save its CSV output."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    try:
        leads = await maps_scraper.collect_maps(
            query,
            limit=limit,
            timeout=7200,
            headless=True,
        )
    except maps_scraper.CollectionIncomplete as exc:
        partial_path = maps_scraper.save_results(
            exc.leads, query + "_partial", OUTPUT_DIR
        )
        return {
            "status": "partial",
            "results": exc.leads,
            "csv_path": str(partial_path),
            "raw_csv_path": None,
            "warning": str(exc),
        }

    raw_path = None
    if crawl_websites and leads:
        raw_path = maps_scraper.save_results(leads, query + "_raw", OUTPUT_DIR)
        try:
            leads = await maps_scraper.enrich_leads_with_websites(leads)
        except Exception as exc:
            return {
                "status": "partial",
                "results": leads,
                "csv_path": str(raw_path),
                "raw_csv_path": str(raw_path),
                "warning": f"Maps results were collected, but website email enrichment failed: {exc}",
            }

    csv_path = maps_scraper.save_results(leads, query, OUTPUT_DIR)
    return {
        "status": "completed",
        "results": leads,
        "csv_path": str(csv_path),
        "raw_csv_path": str(raw_path) if raw_path else None,
        "warning": None,
    }


async def _execute_job(job_id: str) -> None:
    global ACTIVE_JOB_ID
    job = JOBS[job_id]
    job["status"] = "running"
    job["updated_at"] = _now()
    job["log"].append("Starting the standalone Google Maps collector.")
    try:
        outcome = await run_scraper(
            job["query"], job["limit"], job["crawl_websites"]
        )
        job["status"] = outcome.get("status", "completed")
        job["results"] = outcome.get("results", [])
        job["csv_path"] = outcome.get("csv_path")
        job["raw_csv_path"] = outcome.get("raw_csv_path")
        job["warning"] = outcome.get("warning")
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


@app.get("/", response_class=HTMLResponse)
async def dashboard() -> HTMLResponse:
    if not STATIC_FILE.is_file():
        raise HTTPException(status_code=503, detail="Dashboard HTML is missing")
    return HTMLResponse(STATIC_FILE.read_text(encoding="utf-8"))


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "app": "Metering Lead Finder"}


@app.post("/api/search", status_code=202)
async def start_search(request: SearchRequest) -> dict[str, Any]:
    global ACTIVE_JOB_ID
    if ACTIVE_JOB_ID:
        active = JOBS.get(ACTIVE_JOB_ID)
        if active and active["status"] in {"queued", "running"}:
            raise HTTPException(status_code=409, detail="A Maps search is already running")
        ACTIVE_JOB_ID = None

    job_id = str(uuid.uuid4())
    JOBS[job_id] = {
        "job_id": job_id,
        "query": request.query,
        "limit": request.limit,
        "crawl_websites": request.crawl_websites,
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
    filename = (maps_scraper.query_to_filename(job["query"]) or "maps_results") + ".csv"
    return Response(
        content=stream.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
