"""End-to-end fixture check that the bundled MV3 extension scrapes via headless Chromium."""
import contextlib
import importlib.util
import io
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
RUNNER_PATH = ROOT / "extension_bundle" / "run_hermes_scrape.py"
SEARCH_HTML = '''<div class="w6VYqd"></div>
<div role="search"><button onclick="window.searchClicks=(window.searchClicks||0)+1">Search</button></div>
<div role="feed"><h1>Results</h1>
<div role="article"><a class="hfpxzc" aria-label="Fixture Hausverwaltung Alpha" href="https://www.google.com/maps/place/Fixture+Hausverwaltung+Alpha/data=!1s0x1:0x2!3d52.52!4d13.40!19sChIJ_fixture_alpha"></a>
<div class="W4Efsd"><span>Property management company</span> · <span>Alpha Street 1</span></div>
<span class="UsdlK">+49 30 5550001</span><span class="MW4etd">4.5</span><span class="UY7F9">(12)</span>
<a aria-label="Visit Fixture Hausverwaltung Alpha's website" href="https://fixture-alpha.test/">Website</a></div>
<div role="article"><a class="hfpxzc" aria-label="Fixture Hausverwaltung Beta" href="https://www.google.com/maps/place/Fixture+Hausverwaltung+Beta/data=!1s0x3:0x4!3d52.53!4d13.41!19sChIJ_fixture_beta"></a>
<div class="W4Efsd"><span>Property management company</span> · <span>Beta Street 2</span></div></div>
<div class="HlvSq">You've reached the end of the list.</div></div>'''


def load_runner(test):
    test.assertTrue(RUNNER_PATH.is_file(), "Bundled extension runner is missing")
    spec = importlib.util.spec_from_file_location("metering_extension_runner", RUNNER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ExtensionRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_extension_automation_failure_is_not_an_empty_success(self):
        runner = load_runner(self)
        page = SimpleNamespace(
            on=MagicMock(), goto=AsyncMock(),
            wait_for_selector=AsyncMock(side_effect=RuntimeError("extension UI missing")),
        )
        context = SimpleNamespace(pages=[page], close=AsyncMock())

        @asynccontextmanager
        async def failed_extension():
            yield SimpleNamespace(chromium=SimpleNamespace(
                launch_persistent_context=AsyncMock(return_value=context)
            ))

        with patch.object(runner, "async_playwright", failed_extension):
            with self.assertRaisesRegex(RuntimeError, "Extension automation failed"):
                await runner.scrape_google_maps("Fixture", crawl_websites=False)
        context.close.assert_awaited_once()

    def test_extension_is_bundled_next_to_runner(self):
        runner = load_runner(self)
        extension_dir = Path(runner.EXTENSION_PATH).resolve()
        self.assertEqual(extension_dir, RUNNER_PATH.parent.resolve())
        self.assertTrue((extension_dir / "manifest.json").is_file())
        self.assertTrue((extension_dir / "contentScript.js").is_file())

    async def test_headless_chromium_loads_extension_and_collects_fixture_listings(self):
        runner = load_runner(self)
        observed = {}
        extra_cards = "".join(
            f'<div role="article"><a class="hfpxzc" aria-label="Extra fixture {i}" '
            f'href="https://www.google.com/maps/place/Extra/data=!19sChIJ_extra_{i}"></a></div>'
            for i in range(1000)
        )
        fixture_html = SEARCH_HTML.replace('<div class="HlvSq">', extra_cards + '<div class="HlvSq">')

        @asynccontextmanager
        async def fixture_playwright():
            async with async_playwright() as real:
                class Chromium:
                    async def launch_persistent_context(self, *args, **kwargs):
                        observed["headless"] = kwargs.get("headless")
                        observed["channel"] = kwargs.get("channel")
                        observed["args"] = kwargs.get("args", [])
                        # Enable the read-only CDP command-line diagnostic in this test.
                        kwargs["args"] = kwargs["args"] + ["--enable-automation"]
                        context = await real.chromium.launch_persistent_context(*args, **kwargs)
                        session = await context.browser.new_browser_cdp_session()
                        observed["browser_args"] = (await session.send("Browser.getBrowserCommandLine"))["arguments"]
                        await session.detach()

                        async def route(request):
                            url = request.request.url
                            if "/maps/search/" in url:
                                await request.fulfill(status=200, content_type="text/html; charset=utf-8", body=fixture_html)
                            else:
                                await request.abort()

                        await context.route("https://**/*", route)
                        return context

                yield SimpleNamespace(chromium=Chromium())

        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch.object(runner, "async_playwright", fixture_playwright),
                patch.dict(os.environ, {"HERMES_HEADLESS": "1"}),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                rows = await runner.scrape_google_maps(
                    "Fixture Hausverwaltungen Berlin", 0, temp_dir, crawl_websites=False
                )
            self.assertEqual(len(rows), 1002, "Uncapped collection must not truncate at 999 listings")
            self.assertEqual([row["name"] for row in rows[:2]], [
                "Fixture Hausverwaltung Alpha", "Fixture Hausverwaltung Beta"
            ])
            self.assertEqual(rows[0]["place_id"], "ChIJ_fixture_alpha")
            self.assertEqual(rows[0]["address"], "Alpha Street 1")
            self.assertEqual(rows[0]["phone"], "+49 30 5550001")
            self.assertEqual(rows[0]["latitude"], "52.52")
            self.assertEqual(rows[0]["longitude"], "13.40")
            self.assertTrue((Path(temp_dir) / "fixture_hausverwaltungen_berlin.csv").is_file())

        self.assertTrue(observed["headless"])
        self.assertEqual(observed["channel"], "chromium")
        self.assertTrue(any(arg.startswith("--load-extension=") for arg in observed["browser_args"]))
        self.assertTrue(any(arg.startswith("--disable-extensions-except=") for arg in observed["browser_args"]))


    async def test_raw_collection_does_not_visit_business_websites(self):
        runner = load_runner(self)
        requests = []
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append(self.path)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'<html><body>office@fixture.test</body></html>')
            def log_message(self, format, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        html = f'''<div class="w6VYqd"></div><h1 class="DUwDvf">Fixture Raw Business</h1>
            <a data-item-id="authority" href="http://127.0.0.1:{server.server_port}/">Website</a>
            <button data-item-id="address"><span class="Io6YTe">Fixture Street 1</span></button>'''
        @asynccontextmanager
        async def fixture_playwright():
            async with async_playwright() as real:
                class Chromium:
                    async def launch_persistent_context(self, *args, **kwargs):
                        context = await real.chromium.launch_persistent_context(*args, **kwargs)
                        async def route(request):
                            await request.fulfill(status=200, content_type="text/html", body=html)
                        await context.route("https://www.google.com/maps/**", route)
                        return context
                yield SimpleNamespace(chromium=Chromium())
        try:
            with patch.object(runner, "async_playwright", fixture_playwright):
                rows = await runner.scrape_google_maps("Fixture Raw Business", limit=1, crawl_websites=False)
            self.assertEqual(len(rows), 1)
            self.assertEqual(requests, [], "Raw collection must not fetch any business website")
            self.assertEqual(rows[0]["email"], "")
        finally:
            await __import__("asyncio").to_thread(server.shutdown)
            server.server_close()
            thread.join()

    async def assert_incomplete_fixture(self, fixture_html, expected_reason, timeout_seconds=None):
        runner = load_runner(self)

        @asynccontextmanager
        async def fixture_playwright():
            async with async_playwright() as real:
                class Chromium:
                    async def launch_persistent_context(self, *args, **kwargs):
                        context = await real.chromium.launch_persistent_context(*args, **kwargs)
                        async def route(request):
                            await request.fulfill(status=200, content_type="text/html", body=fixture_html)
                        await context.route("https://www.google.com/maps/**", route)
                        return context
                yield SimpleNamespace(chromium=Chromium())

        with patch.object(runner, "async_playwright", fixture_playwright):
            with self.assertRaisesRegex(RuntimeError, "before the end of the list") as caught:
                await runner.scrape_google_maps("Fixture partial", crawl_websites=False, timeout_seconds=timeout_seconds)
        self.assertEqual(getattr(caught.exception, "stop_reason"), expected_reason)
        self.assertEqual(len(getattr(caught.exception, "leads")), 2)

    async def test_limited_view_is_partial_not_a_completed_list(self):
        await self.assert_incomplete_fixture(
            SEARCH_HTML.replace("You've reached the end of the list.", "Limited view of Google Maps"), "limited_view"
        )

    async def test_timeout_retains_extension_rows_as_partial(self):
        await self.assert_incomplete_fixture(
            SEARCH_HTML.replace("You've reached the end of the list.", ""), "timeout", timeout_seconds=0.15
        )

    async def test_verification_stops_without_claiming_end_of_list(self):
        html = SEARCH_HTML.replace("You've reached the end of the list.", "")
        html += """<script>document.addEventListener('click', event => {
          if (event.target.id === 'extension_gms_start_btn' && !document.getElementById('challenge-fixture')) {
            document.body.insertAdjacentHTML('beforeend', '<div id="challenge-fixture">Our systems have detected unusual traffic</div>');
          }
        });</script>"""
        await self.assert_incomplete_fixture(html, "verification")

    async def test_export_error_after_end_marker_cannot_be_a_success(self):
        runner = load_runner(self)
        button = SimpleNamespace(click=AsyncMock(), inner_text=AsyncMock(return_value="Start Auto Extract"))
        async def selector(value):
            if value == '#extension_gms_download_btn':
                raise RuntimeError("Fixture export failure")
            return button
        page = SimpleNamespace(
            on=MagicMock(), goto=AsyncMock(), wait_for_selector=AsyncMock(),
            query_selector=AsyncMock(side_effect=selector),
            evaluate=AsyncMock(side_effect=["", None, "", "Start Auto Extract", "end_of_list"]),
        )
        context = SimpleNamespace(pages=[page], close=AsyncMock())
        @asynccontextmanager
        async def fixture_playwright():
            yield SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=AsyncMock(return_value=context)))
        with patch.object(runner, "async_playwright", fixture_playwright), patch.object(runner.asyncio, "sleep", AsyncMock()):
            with self.assertRaisesRegex(RuntimeError, "Fixture export failure"):
                await runner.scrape_google_maps("Fixture export", crawl_websites=False)

    async def test_website_emails_are_extracted_by_the_extension_service_worker(self):
        runner = load_runner(self)
        test = self
        observed = {}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = b'<html><body>Contact: <a href="mailto:office@fixture.test">office@fixture.test</a></body></html>'
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        @asynccontextmanager
        async def extension_only_playwright():
            async with async_playwright() as real:
                class Chromium:
                    async def launch(self, *args, **kwargs):
                        test.fail("Website scraping must use the extension, not a direct Chromium crawler")

                    async def launch_persistent_context(self, *args, **kwargs):
                        observed.update(kwargs)
                        return await real.chromium.launch_persistent_context(*args, **kwargs)

                yield SimpleNamespace(chromium=Chromium())

        try:
            original = [{"name": "Fixture", "website": f"http://127.0.0.1:{server.server_port}/", "email": ""}]
            with patch.object(runner, "async_playwright", extension_only_playwright):
                rows = await runner.enrich_leads_with_websites(original)
            self.assertEqual(rows[0]["email"], "office@fixture.test")
            self.assertTrue(observed["headless"])
            self.assertEqual(observed["channel"], "chromium")
            self.assertTrue(any(arg.startswith("--load-extension=") for arg in observed["args"]))
        finally:
            await __import__("asyncio").to_thread(server.shutdown)
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
