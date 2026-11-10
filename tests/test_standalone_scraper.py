"""Direct Maps runner tests: fixture data, real Chromium, no extension scripts."""
import csv
import importlib.util
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / 'maps_scraper.py'
ONE = 'https://www.google.com/maps/place/Fixture+School+One/data=!1s0x1:0x2!3d33.78!4d72.73!19sChIJ_test_school_one'
TWO = 'https://www.google.com/maps/place/Fixture+School+Two/data=!1s0x3:0x4!3d33.79!4d72.74!19sChIJ_test_school_two'
CARD_ONE = f'<div role="article"><a aria-label="Fixture School One" href="{ONE}">Fixture School One</a></div>'
CARD_TWO = f'<div role="article"><a aria-label="Fixture School Two" href="{TWO}">Fixture School Two</a></div>'
SEARCH = f'''<html><body><div role="feed" style="height:80px;overflow:auto"><h1>Results</h1>
{CARD_ONE}<div style="height:500px"></div></div><script>
const feed=document.querySelector('[role=feed]');
feed.addEventListener('scroll',()=>{{if(window.appended)return;window.appended=true;
feed.insertAdjacentHTML('beforeend',`{CARD_TWO}{CARD_ONE}<p>You've reached the end of the list.</p>`);}});
</script></body></html>'''

def detail(name, number, website=''):
    return f'''<html><body><div role="main"><h1 class="DUwDvf">{name}</h1>
<button class="DkEaL">Primary school</button>
<button data-item-id="address" aria-label="Address: Test Road {number}, New York, NY">Address</button>
<button data-item-id="phone:tel:+121****5000{number}" aria-label="Phone: +1 212-555-000{number}">Phone</button>
{f'<a data-item-id="authority" href="{website}">Website</a>' if website else ''}
<div class="F7nice"><span role="img" aria-label="4.5 stars">4.5</span><span aria-label="12 reviews">(12)</span></div>
<table><tr><td>Monday</td><td>9 AM-5 PM</td></tr></table></div></body></html>'''


def load_runner(test):
    test.assertTrue(RUNNER.is_file(), 'extension-free standalone variant is missing')
    spec = importlib.util.spec_from_file_location('direct_runner', RUNNER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DirectPackagingTests(unittest.TestCase):
    def test_copied_runner_help_works_without_extension_folder_or_other_projects(self):
        import os
        import shutil
        import subprocess
        import sys
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp)/RUNNER.name
            shutil.copy2(RUNNER, copy)
            result = subprocess.run([sys.executable, '-B', str(copy), '--help'], cwd=tmp,
                                    env={**os.environ, 'HOME': tmp}, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('without an extension', result.stdout)
            self.assertFalse((Path(tmp)/'manifest.json').exists())

    def test_csv_preserves_values_headers_and_earlier_exports(self):
        runner = load_runner(self)
        row = runner.blank_lead(name='Fixture, School\nInternational', maps_url=ONE)
        with tempfile.TemporaryDirectory() as tmp:
            first = runner.save_results([row], '../../Fixture', tmp)
            second = runner.save_results([row], '../../Fixture', tmp)
            self.assertNotEqual(first, second)
            self.assertEqual(first.parent, Path(tmp))
            for path in [first, second]:
                with path.open(encoding='utf-8', newline='') as f:
                    reader = csv.DictReader(f)
                    self.assertEqual(reader.fieldnames, runner.CSV_FIELDS)
                    self.assertEqual(list(reader), [row])

    def test_invalid_cli_inputs_fail_and_runtime_failure_exits_nonzero(self):
        import contextlib
        import io
        from unittest.mock import AsyncMock, patch
        runner = load_runner(self)
        for argv in [[''], ['x', '0'], ['x', '999', '0', '--timeout', 'nan']]:
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
                runner.parse_args(argv)
            self.assertEqual(raised.exception.code, 2)
        with patch.object(runner, 'run', new=AsyncMock(side_effect=RuntimeError('fixture failure'))):
            with contextlib.redirect_stderr(io.StringIO()) as error:
                self.assertEqual(runner.main(['fixture', '999', '0']), 1)
            self.assertIn('fixture failure', error.getvalue())


class DirectBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def test_scroll_open_details_and_dedupe_with_no_extension_loaded(self):
        runner = load_runner(self)
        visited = []
        async def configure(context):
            session = await context.browser.new_browser_cdp_session()
            argv = (await session.send('Browser.getBrowserCommandLine'))['arguments']
            self.assertFalse(any(a.startswith(('--load-extension', '--disable-extensions-except')) for a in argv))
            self.assertIn('--disable-extensions', argv)
            self.assertEqual(context.service_workers, [])
            await session.detach()
            async def route(request):
                url = request.request.url
                visited.append(url)
                if '/maps/search/' in url:
                    body = SEARCH
                elif 'School+One' in url:
                    body = detail('Fixture School One', 1, 'https://school-one.test/')
                elif 'School+Two' in url:
                    body = detail('Fixture School Two', 2)
                else:
                    await request.abort()
                    return
                await request.fulfill(status=200, content_type='text/html; charset=utf-8', body=body)
            await context.route('**/*', route)
        leads = await runner.collect_maps('fixture schools', limit=999, timeout=20, configure_context=configure)
        self.assertEqual([r['name'] for r in leads], ['Fixture School One', 'Fixture School Two'])
        self.assertEqual(leads[0]['address'], 'Test Road 1, New York, NY')
        self.assertEqual(leads[0]['phone'], '+1 212-555-0001')
        self.assertEqual(leads[0]['website'], 'https://school-one.test/')
        self.assertEqual(leads[0]['place_id'], 'ChIJ_test_school_one')
        self.assertEqual(leads[0]['cid'], '2')
        self.assertEqual(leads[0]['latitude'], '33.78')
        self.assertEqual(leads[0]['longitude'], '72.73')
        self.assertEqual(str(leads[0]['rating']), '4.5')
        self.assertEqual(str(leads[0]['reviews']), '12')
        self.assertEqual(leads[0]['monday'], '9 AM-5 PM')
        self.assertEqual(leads[0]['email'], '')
        self.assertTrue(any('School+Two' in u for u in visited))
        self.assertEqual(list(leads[0]), runner.CSV_FIELDS)

    async def test_opens_hours_pane_without_losing_business_fields(self):
        from playwright.async_api import async_playwright
        runner = load_runner(self)
        html = detail('Fixture School One', 1).replace('<table><tr><td>Monday</td><td>9 AM-5 PM</td></tr></table>', '')
        html += '''<button data-item-id="oh" onclick="document.querySelector('h1').remove();this.insertAdjacentHTML('afterend', '<table><tr><td>Monday</td><td>9 AM-5 PM</td><td>Copy</td></tr></table>');this.remove()">Hours</button>'''
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, channel='chromium')
            try:
                page = await browser.new_page()
                await page.set_content(html)
                row = await runner.read_detail(page)
                self.assertEqual(row['monday'], '9 AM-5 PM')
                self.assertEqual(row['name'], 'Fixture School One')
                self.assertEqual(row['address'], 'Test Road 1, New York, NY')
            finally:
                await browser.close()

    async def test_already_expanded_week_is_not_collapsed_by_extraction(self):
        from playwright.async_api import async_playwright
        runner = load_runner(self)
        table = '<table id="week">' + ''.join(f'<tr><td>{day}</td><td>9 AM-5 PM</td></tr>' for day in ['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday']) + '</table>'
        html = detail('Fixture School One', 1).replace('<table><tr><td>Monday</td><td>9 AM-5 PM</td></tr></table>', '<button data-item-id="oh" onclick="openHours()">Hours</button>')
        html += '''<script>function openHours() {
            document.querySelector('[data-item-id="oh"]').remove();
            document.body.insertAdjacentHTML('beforeend', `<div role="button" aria-expanded="true" onclick="document.querySelectorAll('#week tr')[1].remove();this.setAttribute('aria-expanded','false')"><span aria-label="Show open hours for the week">Toggle</span></div>WEEK_TABLE`);
        }</script>'''.replace('WEEK_TABLE', table)
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, channel='chromium')
            try:
                page = await browser.new_page()
                await page.set_content(html)
                row = await runner.read_detail(page)
                self.assertEqual(row['tuesday'], '9 AM-5 PM')
            finally:
                await browser.close()

    async def test_keeps_scrolling_when_results_arrive_after_five_idle_rounds(self):
        runner = load_runner(self)
        delayed = SEARCH.replace('if(window.appended)return;window.appended=true;', 'window.scrolls=(window.scrolls||0)+1;if(window.scrolls<12||window.appended)return;window.appended=true;')
        async def configure(context):
            async def route(request):
                url = request.request.url
                body = delayed if '/maps/search/' in url else detail('Fixture School Two' if 'School+Two' in url else 'Fixture School One', 2 if 'School+Two' in url else 1)
                await request.fulfill(status=200, content_type='text/html; charset=utf-8', body=body)
            await context.route('**/*', route)
        rows = await runner.collect_maps('fixture delayed results', timeout=20, configure_context=configure)
        self.assertEqual(len(rows), 2)

    async def test_later_place_id_enriches_same_cid_without_duplicate(self):
        runner = load_runner(self)
        evolving = SEARCH.replace(CARD_ONE, CARD_ONE.replace('!19sChIJ_test_school_one', ''), 1)
        async def configure(context):
            async def route(request):
                url = request.request.url
                body = evolving if '/maps/search/' in url else detail('Fixture School Two' if 'School+Two' in url else 'Fixture School One', 2 if 'School+Two' in url else 1)
                await request.fulfill(status=200, content_type='text/html; charset=utf-8', body=body)
            await context.route('**/*', route)
        rows = await runner.collect_maps('fixture evolving', timeout=20, configure_context=configure)
        self.assertEqual([row['name'] for row in rows], ['Fixture School One', 'Fixture School Two'])

    async def test_feed_read_error_preserves_collected_rows(self):
        from unittest.mock import AsyncMock, patch
        from playwright.async_api import Error as BrowserError
        runner = load_runner(self)
        html = '<div role="feed">'+CARD_ONE+'</div>'
        async def configure(context):
            await context.route('**/*', lambda route: route.fulfill(status=200, content_type='text/html; charset=utf-8', body=html))
        with tempfile.TemporaryDirectory() as tmp:
            args = runner.parse_args(['fixture broken feed', '999', '0', '--output-dir', tmp])
            with patch('playwright.async_api.Locator.inner_text', new=AsyncMock(side_effect=BrowserError('fixture feed failure'))):
                with self.assertRaises(runner.CollectionIncomplete):
                    await runner.run(args, configure_context=configure)
            files = list(Path(tmp).glob('*_partial.csv'))
            self.assertEqual(len(files), 1)
            with files[0].open(encoding='utf-8', newline='') as f:
                self.assertEqual(list(csv.DictReader(f))[0]['name'], 'Fixture School One')

    async def test_timeout_preserves_partial_rows_and_does_not_claim_success(self):
        runner = load_runner(self)
        async def configure(context):
            await context.route('**/*', lambda route: route.fulfill(status=200, content_type='text/html; charset=utf-8', body='<div role="feed">'+CARD_ONE+'</div>'))
        with tempfile.TemporaryDirectory() as tmp:
            args = runner.parse_args(['fixture schools', '999', '0', '--output-dir', tmp, '--timeout', '1.5'])
            with self.assertRaises(runner.CollectionIncomplete):
                await runner.run(args, configure_context=configure)
            partials = list(Path(tmp).glob('*_partial.csv'))
            self.assertEqual(len(partials), 1)
            with partials[0].open(encoding='utf-8', newline='') as f:
                rows = list(csv.DictReader(f))
            self.assertEqual([row['name'] for row in rows], ['Fixture School One'])
            self.assertFalse((Path(tmp)/'fixture_schools.csv').exists())

    async def test_crawler_startup_failure_cannot_erase_raw_collection(self):
        from unittest.mock import AsyncMock, patch
        runner = load_runner(self)
        row = runner.blank_lead(name='Fixture School One', maps_url=ONE, website='https://school-one.test/')
        with tempfile.TemporaryDirectory() as tmp:
            args = runner.parse_args(['fixture', '999', '1', '--output-dir', tmp])
            with patch.object(runner, 'collect_maps', new=AsyncMock(return_value=[row])), patch.object(runner, 'enrich_leads_with_websites', new=AsyncMock(side_effect=RuntimeError('fixture browser failure'))):
                with self.assertRaisesRegex(RuntimeError, 'fixture browser failure'):
                    await runner.run(args)
            with (Path(tmp)/'fixture_raw.csv').open(encoding='utf-8', newline='') as f:
                self.assertEqual(list(csv.DictReader(f))[0]['name'], 'Fixture School One')
            self.assertFalse((Path(tmp)/'fixture.csv').exists())

    async def test_genuine_empty_search_writes_header_only_csv(self):
        runner = load_runner(self)
        async def configure(context):
            await context.route('**/*', lambda route: route.fulfill(status=200, content_type='text/html', body='<p>No results found</p>'))
        with tempfile.TemporaryDirectory() as tmp:
            args = runner.parse_args(['fixture empty', '999', '0', '--output-dir', tmp, '--timeout', '10'])
            path = await runner.run(args, configure_context=configure)
            with path.open(encoding='utf-8', newline='') as f:
                reader = csv.DictReader(f)
                self.assertEqual(reader.fieldnames, runner.CSV_FIELDS)
                self.assertEqual(list(reader), [])

    async def test_challenge_on_detail_page_stops_further_navigation(self):
        runner = load_runner(self)
        details = []
        async def configure(context):
            async def route(request):
                if '/maps/search/' in request.request.url:
                    body = SEARCH
                else:
                    details.append(request.request.url)
                    body = '<form id="captcha-form">Verify you are human</form>'
                await request.fulfill(status=200, content_type='text/html; charset=utf-8', body=body)
            await context.route('**/*', route)
        with self.assertRaises(runner.CollectionIncomplete) as error:
            await runner.collect_maps('fixture detail challenge', timeout=20, configure_context=configure)
        self.assertEqual(len(error.exception.leads), 2)
        self.assertEqual(len(details), 1)

    async def test_verification_challenge_is_reported_not_bypassed(self):
        runner = load_runner(self)
        async def configure(context):
            await context.route('**/*', lambda route: route.fulfill(status=200, content_type='text/html', body='<form id="captcha-form">Verify you are human</form>'))
        with self.assertRaisesRegex(RuntimeError, 'verification challenge'):
            await runner.collect_maps('fixture challenge', timeout=10, configure_context=configure)

    async def test_cli_orchestration_enriches_and_saves_actual_csv_rows(self):
        runner = load_runner(self)
        self.assertTrue(callable(getattr(runner, 'run', None)), 'local CLI orchestration is missing')
        async def configure(context):
            async def route(request):
                url = request.request.url
                if '/maps/search/' in url:
                    body = SEARCH
                elif 'School+One' in url:
                    body = detail('Fixture School One', 1, 'https://school-one.test/')
                elif 'School+Two' in url:
                    body = detail('Fixture School Two', 2)
                elif url.startswith('https://school-one.test/'):
                    body = '<p>Email: office@fixture-school.test</p><a href="https://www.instagram.com/fixture_school/">Instagram</a>'
                else:
                    await request.abort()
                    return
                await request.fulfill(status=200, content_type='text/html; charset=utf-8', body=body)
            await context.route('**/*', route)
        with tempfile.TemporaryDirectory() as tmp:
            args = runner.parse_args(['fixture schools', '999', '1', '--output-dir', tmp, '--timeout', '20'])
            path = await runner.run(args, configure_context=configure)
            self.assertTrue(path.is_file())
            with path.open(encoding='utf-8', newline='') as f:
                reader = csv.DictReader(f)
                rows = list(reader)
                self.assertEqual(reader.fieldnames, runner.CSV_FIELDS)
            self.assertEqual(len(rows), 2)
            by_name = {row['name']: row for row in rows}
            self.assertEqual(by_name['Fixture School One']['email'], 'office@fixture-school.test')
            self.assertEqual(by_name['Fixture School One']['instagram'], 'https://www.instagram.com/fixture_school/')
            self.assertEqual(by_name['Fixture School Two']['email'], '')
            raw = list(Path(tmp).glob('*_raw.csv'))
            self.assertEqual(len(raw), 1)
            with raw[0].open(encoding='utf-8', newline='') as f:
                self.assertTrue(all(not row['email'] for row in csv.DictReader(f)))
