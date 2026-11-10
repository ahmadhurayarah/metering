import asyncio
import csv
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

import app as dashboard


class DashboardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        dashboard.JOBS.clear()
        dashboard.ACTIVE_JOB_ID = None
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=dashboard.app),
            base_url="http://testserver",
        )

    async def asyncTearDown(self):
        await self.client.aclose()

    async def test_local_scraper_pipeline_saves_raw_and_enriched_email_csvs(self):
        raw_row = {field: "" for field in dashboard.CSV_FIELDS}
        raw_row.update({"name": "Fixture Hausverwaltung", "website": "https://fixture.example"})
        enriched_row = {**raw_row, "email": "office@fixture.example"}
        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch.object(dashboard, "OUTPUT_DIR", Path(temp_dir)),
                patch.object(dashboard.extension_scraper, "scrape_google_maps", new=AsyncMock(return_value=[raw_row])) as scrape,
                patch.object(dashboard.extension_scraper, "enrich_leads_with_websites", new=AsyncMock(return_value=[enriched_row])) as enrich,
            ):
                outcome = await dashboard.run_scraper("Hausverwaltung Berlin", 10, True)

            scrape.assert_awaited_once_with(
                query="Hausverwaltung Berlin", limit=10, output_dir=None, crawl_websites=False
            )
            enrich.assert_awaited_once_with([raw_row])
            self.assertEqual(outcome["status"], "completed")
            self.assertEqual(outcome["results"][0]["email"], "office@fixture.example")
            self.assertTrue(Path(outcome["raw_csv_path"]).is_file())
            self.assertTrue(Path(outcome["csv_path"]).is_file())
            with Path(outcome["csv_path"]).open(encoding="utf-8", newline="") as stream:
                saved_rows = list(csv.DictReader(stream))
            self.assertEqual(saved_rows[0]["email"], "office@fixture.example")

    def test_maps_collection_uses_the_bundled_chromium_extension(self):
        extension_dir = dashboard.BASE_DIR / "extension_bundle"
        self.assertEqual(Path(dashboard.extension_scraper.EXTENSION_PATH).resolve(), extension_dir.resolve())
        self.assertTrue((extension_dir / "manifest.json").is_file())
        self.assertTrue((extension_dir / "contentScript.js").is_file())

    async def wait_for_job(self, job_id):
        for _ in range(100):
            response = await self.client.get(f"/api/jobs/{job_id}")
            self.assertEqual(response.status_code, 200)
            job = response.json()
            if job["status"] in {"completed", "partial", "failed"}:
                return job
            await asyncio.sleep(0.01)
        self.fail("Background search did not finish")

    async def test_dashboard_serves_query_form_and_email_results_ui(self):
        response = await self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Start a Google Maps search", response.text)
        self.assertIn("email", response.text.lower())
        self.assertIn("/api/search", response.text)

    async def test_query_results_include_emails_and_download_every_scraper_column(self):
        row = {field: "" for field in dashboard.CSV_FIELDS}
        row.update({
            "name": "Fixture Hausverwaltung",
            "email": "office@fixture.example",
            "additional_emails": "team@fixture.example",
            "website": "https://fixture.example/",
            "place_id": "ChIJ_fixture_1",
            "maps_url": "https://www.google.com/maps/place/Fixture/",
        })
        fake_run = AsyncMock(return_value={
            "status": "completed",
            "results": [row],
            "csv_path": "/tmp/fixture.csv",
            "raw_csv_path": "/tmp/fixture_raw.csv",
            "warning": None,
        })
        with patch.object(dashboard, "run_scraper", fake_run):
            submitted = await self.client.post("/api/search", json={
                "query": "  Hausverwaltung Berlin  ",
                "limit": 999,
                "crawl_websites": True,
            })
            self.assertEqual(submitted.status_code, 202, submitted.text)
            job_id = submitted.json()["job_id"]
            job = await self.wait_for_job(job_id)

        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["query"], "Hausverwaltung Berlin")
        self.assertEqual(job["result_count"], 1)
        self.assertEqual(job["results"][0]["email"], "office@fixture.example")
        self.assertEqual(job["results"][0]["additional_emails"], "team@fixture.example")
        self.assertEqual(job["download_url"], f"/api/jobs/{job_id}/csv")
        fake_run.assert_awaited_once_with("Hausverwaltung Berlin", 999, True)

        download = await self.client.get(job["download_url"])
        self.assertEqual(download.status_code, 200)
        reader = csv.DictReader(io.StringIO(download.text))
        self.assertEqual(reader.fieldnames, dashboard.CSV_FIELDS)
        downloaded = list(reader)
        self.assertEqual(len(downloaded), 1)
        self.assertEqual(downloaded[0]["email"], "office@fixture.example")
        self.assertEqual(downloaded[0]["place_id"], "ChIJ_fixture_1")

    async def test_partial_collection_keeps_available_rows_and_warning(self):
        row = {field: "" for field in dashboard.CSV_FIELDS}
        row.update({"name": "Partial Hausverwaltung", "email": "kontakt@partial.example"})
        fake_run = AsyncMock(return_value={
            "status": "partial",
            "results": [row],
            "csv_path": "/tmp/partial.csv",
            "raw_csv_path": None,
            "warning": "Verification boundary encountered; available rows were saved.",
        })
        with patch.object(dashboard, "run_scraper", fake_run):
            submitted = await self.client.post("/api/search", json={"query": "Hausverwaltung Berlin"})
            job = await self.wait_for_job(submitted.json()["job_id"])
        self.assertEqual(job["status"], "partial")
        self.assertEqual(job["result_count"], 1)
        self.assertEqual(job["results"][0]["email"], "kontakt@partial.example")
        self.assertIn("Verification boundary", job["warning"])

    async def test_blank_query_and_invalid_limit_are_rejected(self):
        blank = await self.client.post("/api/search", json={"query": "   "})
        too_large = await self.client.post("/api/search", json={
            "query": "Hausverwaltung Berlin", "limit": 1000,
        })
        self.assertEqual(blank.status_code, 422)
        self.assertEqual(too_large.status_code, 422)

    async def test_only_one_search_can_run_at_a_time(self):
        gate = asyncio.Event()

        async def slow_run(query, limit, crawl_websites):
            await gate.wait()
            return {"status": "completed", "results": [], "csv_path": None,
                    "raw_csv_path": None, "warning": None}

        with patch.object(dashboard, "run_scraper", new=slow_run):
            first = await self.client.post("/api/search", json={"query": "first"})
            second = await self.client.post("/api/search", json={"query": "second"})
            self.assertEqual(first.status_code, 202)
            self.assertEqual(second.status_code, 409)
            gate.set()
            await self.wait_for_job(first.json()["job_id"])


if __name__ == "__main__":
    unittest.main()
