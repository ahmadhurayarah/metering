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

    async def test_search_fetches_only_raw_data_even_for_a_legacy_client(self):
        raw_row = {field: "" for field in dashboard.CSV_FIELDS}
        raw_row.update({"name": "Fixture Hausverwaltung", "website": "https://fixture.example"})
        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch.object(dashboard, "OUTPUT_DIR", Path(temp_dir)),
                patch.object(dashboard.extension_scraper, "scrape_google_maps", new=AsyncMock(return_value=[raw_row])) as scrape,
                patch.object(dashboard.extension_scraper, "enrich_leads_with_websites", new=AsyncMock(return_value=[raw_row])) as enrich,
            ):
                response = await self.client.post("/api/search", json={"query": "Hausverwaltung Berlin", "crawl_websites": True})
                self.assertEqual(response.status_code, 202)
                job = await self.wait_for_job(response.json()["job_id"])
                enrich.assert_not_awaited()
                scrape.assert_awaited_once_with(query="Hausverwaltung Berlin", limit=0, output_dir=None, crawl_websites=False)
                self.assertEqual(job["status"], "completed")
                self.assertEqual(job["enrichment_status"], "not_started")
                self.assertEqual(job["results"][0]["email"], "")
                self.assertEqual(job["csv_path"], job["raw_csv_path"])
                self.assertTrue(Path(job["raw_csv_path"]).is_file())

    async def test_get_emails_enriches_saved_rows_without_rescraping_or_overwriting_raw(self):
        row = {field: "" for field in dashboard.CSV_FIELDS}
        row.update(name="Fixture", website="https://fixture.test", place_id="fixture-id")
        enriched = {**row, "email": "office@fixture.test"}
        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch.object(dashboard, "OUTPUT_DIR", Path(temp_dir)),
                patch.object(dashboard.extension_scraper, "scrape_google_maps", AsyncMock(return_value=[row])) as scrape,
                patch.object(dashboard.extension_scraper, "enrich_leads_with_websites", AsyncMock(return_value=[enriched])) as enrich,
            ):
                started = await self.client.post("/api/search", json={"query": "Fixture"})
                job_id = started.json()["job_id"]
                raw_job = await self.wait_for_job(job_id)
                raw_path = Path(raw_job["raw_csv_path"])
                raw_bytes = raw_path.read_bytes()
                enrich.assert_not_awaited()
                accepted = await self.client.post(f"/api/jobs/{job_id}/emails")
                self.assertEqual(accepted.status_code, 202, accepted.text)
                job = raw_job
                for _ in range(100):
                    job = (await self.client.get(f"/api/jobs/{job_id}")).json()
                    if job["enrichment_status"] in {"completed", "failed"}:
                        break
                    await asyncio.sleep(0.01)
                self.assertEqual(job["enrichment_status"], "completed")
                self.assertEqual(job["results"][0]["email"], "office@fixture.test")
                self.assertEqual(job["results"][0]["place_id"], "fixture-id")
                self.assertEqual(raw_path.read_bytes(), raw_bytes)
                self.assertNotEqual(job["csv_path"], job["raw_csv_path"])
                self.assertEqual(scrape.await_count, 1)
                enrich.assert_awaited_once_with([row])
                export = await self.client.get(job["download_url"])
                self.assertIn("office@fixture.test", export.text)
                repeated = await self.client.post(f"/api/jobs/{job_id}/emails")
                self.assertEqual(repeated.status_code, 409)

    async def test_failed_email_lookup_preserves_raw_and_blocks_parallel_work(self):
        row = {field: "" for field in dashboard.CSV_FIELDS}
        row.update(name="Fixture", website="https://fixture.test")
        gate, entered = asyncio.Event(), asyncio.Event()

        async def broken_lookup(rows):
            rows[0]["email"] = "must-not-leak@fixture.test"
            entered.set()
            await gate.wait()
            raise RuntimeError("Fixture website lookup failed")

        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch.object(dashboard, "OUTPUT_DIR", Path(temp_dir)),
                patch.object(dashboard.extension_scraper, "scrape_google_maps", AsyncMock(return_value=[row])),
                patch.object(dashboard.extension_scraper, "enrich_leads_with_websites", broken_lookup),
            ):
                started = await self.client.post("/api/search", json={"query": "Fixture"})
                job_id = started.json()["job_id"]
                raw = await self.wait_for_job(job_id)
                original = Path(raw["raw_csv_path"]).read_bytes()
                accepted = await self.client.post(f"/api/jobs/{job_id}/emails")
                self.assertEqual(accepted.status_code, 202)
                await asyncio.wait_for(entered.wait(), timeout=2)
                self.assertEqual((await self.client.post(f"/api/jobs/{job_id}/emails")).status_code, 409)
                self.assertEqual((await self.client.post("/api/search", json={"query": "Other"})).status_code, 409)
                gate.set()
                job = raw
                for _ in range(100):
                    job = (await self.client.get(f"/api/jobs/{job_id}")).json()
                    if job["enrichment_status"] == "failed":
                        break
                    await asyncio.sleep(0.01)
                self.assertEqual(job["enrichment_status"], "failed")
                self.assertEqual(job["results"][0]["email"], "")
                self.assertEqual(job["csv_path"], raw["csv_path"])
                self.assertEqual(Path(raw["raw_csv_path"]).read_bytes(), original)
                self.assertIsNone(dashboard.ACTIVE_JOB_ID)
                self.assertEqual((await self.client.post("/api/jobs/missing/emails")).status_code, 404)

    async def test_extension_stop_before_end_saves_partial_rows_and_reason(self):
        row = {field: "" for field in dashboard.CSV_FIELDS}
        row.update(name="Available fixture", place_id="fixture-id")
        incomplete = dashboard.extension_scraper.CollectionIncomplete(
            "Collection stopped before the end of the list (timeout).", [row], "timeout"
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch.object(dashboard, "OUTPUT_DIR", Path(temp_dir)),
                patch.object(dashboard.extension_scraper, "scrape_google_maps", AsyncMock(side_effect=incomplete)),
            ):
                response = await self.client.post("/api/search", json={"query": "Fixture"})
                job = await self.wait_for_job(response.json()["job_id"])
                self.assertEqual(job["status"], "partial")
                self.assertEqual(job["stop_reason"], "timeout")
                self.assertEqual(job["result_count"], 1)
                self.assertIn("before the end", job["warning"])
                self.assertTrue(Path(job["raw_csv_path"]).is_file())
                with Path(job["raw_csv_path"]).open(newline="", encoding="utf-8") as handle:
                    saved = list(csv.DictReader(handle))
                self.assertEqual(saved[0]["place_id"], "fixture-id")

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
        self.assertIn("Fetch raw data", response.text)
        self.assertIn('id="get-emails"', response.text)
        for removed in ["Prospect discovery", "Find property managers.", "Review the evidence.",
                        "Run a Google Maps query, collect business listings", "mode-pill",
                        "Maximum listings to collect", 'id="limit"', 'id="crawl-websites"',
                        "999 is a ceiling", "Look for public emails", ">Run search<",
                        "Google Maps may limit or change results"]:
            self.assertNotIn(removed, response.text)

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
        fake_run.assert_awaited_once_with("Hausverwaltung Berlin", 0)

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

    async def test_blank_and_oversized_queries_are_rejected(self):
        blank = await self.client.post("/api/search", json={"query": "   "})
        too_large = await self.client.post("/api/search", json={
            "query": "x" * 201,
        })
        self.assertEqual(blank.status_code, 422)
        self.assertEqual(too_large.status_code, 422)

    async def test_only_one_search_can_run_at_a_time(self):
        gate = asyncio.Event()

        async def slow_run(query, limit):
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
