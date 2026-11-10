"""Browser UI tests use intercepted fixture APIs, never live scraping."""
import copy
import json
import unittest
from pathlib import Path

from playwright.async_api import async_playwright, expect

from csv_output import CSV_FIELDS

ROOT = Path(__file__).resolve().parents[1]


class DashboardBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def test_raw_then_email_controls_and_query_only_submission(self):
        row = {field: "" for field in CSV_FIELDS}
        row.update(name="Fixture Business", website="https://fixture.test", place_id="fixture-id")
        job = {
            "job_id": "fixture", "query": "Fixture Berlin", "status": "completed",
            "enrichment_status": "not_started", "results": [row], "fields": CSV_FIELDS,
            "result_count": 1, "email_count": 0, "output_file": "fixture_raw.csv",
            "download_url": "/api/jobs/fixture/csv", "warning": None, "error": None,
        }
        posts = []
        submitted = False
        email_requested = False
        email_polls = 0
        errors = []

        async def route(request):
            nonlocal submitted, email_requested, email_polls
            path = request.request.url.removeprefix("http://metering.test")
            method = request.request.method
            if path == "/":
                await request.fulfill(content_type="text/html", body=(ROOT / "static/index.html").read_text())
                return
            if method == "POST":
                posts.append((path, request.request.post_data_json if request.request.post_data else None))
            if path == "/api/search":
                submitted = True
                data = {"job_id": "fixture", "status": "queued"}
            elif path == "/api/jobs/fixture/emails":
                email_requested = True
                data = {"job_id": "fixture", "status": "queued"}
            elif path == "/api/jobs/fixture":
                data = copy.deepcopy(job)
                if email_requested:
                    email_polls += 1
                    data["enrichment_status"] = "running" if email_polls == 1 else "completed"
                    if email_polls > 1:
                        data["results"][0]["email"] = "office@fixture.test"
                        data["email_count"] = 1
                        data["output_file"] = "fixture_emails.csv"
            elif path == "/api/jobs":
                data = {"jobs": [job] if submitted else []}
            else:
                await request.abort()
                return
            await request.fulfill(content_type="application/json", body=json.dumps(data))

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                page = await browser.new_page(viewport={"width": 1280, "height": 900})
                page.on("pageerror", lambda error: errors.append(str(error)))
                await page.route("http://metering.test/**", route)
                await page.goto("http://metering.test/")
                emails = page.locator("#get-emails")
                await expect(emails).to_be_disabled()
                self.assertEqual(await page.locator('input[type="number"],input[type="checkbox"],.intro,.mode-pill').count(), 0)
                await page.get_by_label("Search query", exact=True).fill("Fixture Berlin")
                await page.get_by_role("button", name="Fetch raw data", exact=True).click()
                await expect(page.locator("#table-container")).to_contain_text("Fixture Business")
                await expect(emails).to_be_enabled()
                self.assertEqual(await page.locator("#email-count").inner_text(), "0")
                self.assertEqual(posts, [("/api/search", {"query": "Fixture Berlin"})])
                await emails.click()
                await expect(emails).to_have_text("Getting emails…")
                await expect(emails).to_be_disabled()
                await expect(page.locator("#table-container")).to_contain_text("office@fixture.test", timeout=7000)
                await expect(emails).to_have_text("Emails fetched")
                await expect(emails).to_be_disabled()
                self.assertEqual([path for path, _ in posts], ["/api/search", "/api/jobs/fixture/emails"])
                self.assertEqual(errors, [])
                await page.set_viewport_size({"width": 390, "height": 844})
                self.assertTrue(await page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth"))
            finally:
                await browser.close()


if __name__ == "__main__":
    unittest.main()
