"""End-to-end fixture check that the bundled MV3 extension scrapes via headless Chromium."""
import contextlib
import importlib.util
import os
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

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
    def test_extension_is_bundled_next_to_runner(self):
        runner = load_runner(self)
        extension_dir = Path(runner.EXTENSION_PATH).resolve()
        self.assertEqual(extension_dir, RUNNER_PATH.parent.resolve())
        self.assertTrue((extension_dir / "manifest.json").is_file())
        self.assertTrue((extension_dir / "contentScript.js").is_file())

    async def test_headless_chromium_loads_extension_and_collects_fixture_listings(self):
        runner = load_runner(self)
        observed = {}

        @asynccontextmanager
        async def fixture_playwright():
            async with async_playwright() as real:
                class Chromium:
                    async def launch_persistent_context(self, *args, **kwargs):
                        observed["headless"] = kwargs.get("headless")
                        observed["channel"] = kwargs.get("channel")
                        observed["args"] = kwargs.get("args", [])
                        context = await real.chromium.launch_persistent_context(*args, **kwargs)
                        session = await context.new_browser_cdp_session()
                        observed["browser_args"] = (await session.send("Browser.getBrowserCommandLine"))["arguments"]
                        await session.detach()

                        async def route(request):
                            url = request.request.url
                            if "/maps/search/" in url:
                                await request.fulfill(status=200, content_type="text/html; charset=utf-8", body=SEARCH_HTML)
                            else:
                                await request.abort()

                        await context.route("https://**/*", route)
                        return context

                yield SimpleNamespace(chromium=Chromium())

        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch.object(runner, "async_playwright", fixture_playwright),
                patch.dict(os.environ, {"HERMES_HEADLESS": "1"}),
                contextlib.redirect_stdout(open(os.devnull, "w")),
            ):
                rows = await runner.scrape_google_maps(
                    "Fixture Hausverwaltungen Berlin", 999, temp_dir, crawl_websites=False
                )
            self.assertEqual([row["name"] for row in rows], [
                "Fixture Hausverwaltung Alpha", "Fixture Hausverwaltung Beta"
            ])
            self.assertEqual(rows[0]["place_id"], "ChIJ_fixture_alpha")
            self.assertEqual(rows[0]["address"], "Alpha Street 1")
            self.assertEqual(rows[0]["phone"], "+49 30 5550001")
            self.assertEqual(rows[0]["latitude"], "52.52")
            self.assertEqual(rows[0]["longitude"], "13.4")
            self.assertTrue((Path(temp_dir) / "fixture_hausverwaltungen_berlin.csv").is_file())

        self.assertTrue(observed["headless"])
        self.assertEqual(observed["channel"], "chromium")
        self.assertTrue(any(arg.startswith("--load-extension=") for arg in observed["browser_args"]))
        self.assertTrue(any(arg.startswith("--disable-extensions-except=") for arg in observed["browser_args"]))


if __name__ == "__main__":
    unittest.main()
