#!/usr/bin/env python3
"""Direct Google Maps DOM scraping in Chromium. No extension or other runner needed."""
import asyncio
import csv
import os
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, quote_plus, unquote, urlsplit

from playwright.async_api import async_playwright, Error as BrowserError

ROOT = Path(__file__).resolve().parent
DEFAULT_OUTPUT = ROOT / 'output' / 'no_extension'
CSV_FIELDS = [
    'name', 'phone', 'email', 'website', 'address', 'instagram', 'facebook',
    'twitter', 'linkedin', 'tiktok', 'yelp', 'youtube', 'place_id', 'cid',
    'category', 'reviews', 'rating', 'latitude', 'longitude', 'monday',
    'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday',
    'additional_emails', 'maps_url',
]


class AccessBlocked(RuntimeError):
    """A consent/verification boundary; stop instead of retrying other places."""


class CollectionIncomplete(RuntimeError):
    def __init__(self, message, leads):
        super().__init__(message)
        self.leads = leads


def blank_lead(**values):
    return {key: values.get(key, '') for key in CSV_FIELDS}


def place_identity(url):
    """Read place identifiers/coordinates, never the viewport's @coordinates."""
    result = {}
    parsed = urlsplit(url)
    decoded = unquote(url)
    match = re.search(r'!(?:19|1)s(ChI[^!/?&#]+)', decoded)
    if match:
        result['place_id'] = match.group(1)
    else:
        place_id = parse_qs(parsed.query).get('query_place_id', [''])[0]
        if place_id:
            result['place_id'] = place_id
    match = re.search(r'!1s0x[0-9a-fA-F]+:(0x[0-9a-fA-F]+)', decoded)
    if match:
        result['cid'] = str(int(match.group(1), 16))
    elif parse_qs(parsed.query).get('cid'):
        result['cid'] = parse_qs(parsed.query)['cid'][0]
    match = re.search(r'!3d(-?\d+(?:\.\d+)?)!4d(-?\d+(?:\.\d+)?)', decoded)
    if match:
        result['latitude'], result['longitude'] = match.groups()
    return result


def place_key(row):
    if row.get('place_id'):
        return 'place:' + row['place_id']
    if row.get('cid'):
        return 'cid:' + row['cid']
    url = urlsplit(row['maps_url'])
    return url.scheme + '://' + url.netloc + url.path


CARD_SCRIPT = r"""() => {
    const feed = document.querySelector('[role="feed"]');
    if (!feed) return [];
    return [...feed.querySelectorAll('a[href*="/maps/place/"]')].map(a => {
        const card = a.closest('[role="article"], .Nv2PK') || a.parentElement;
        return {
            name: (a.getAttribute('aria-label') || card?.querySelector('.qBF1Pd')?.textContent || a.textContent || '').trim(),
            maps_url: a.href,
        };
    });
}"""

DETAIL_SCRIPT = r"""() => {
    const text = el => (el?.innerText || el?.textContent || '').trim();
    const labeled = (selector, prefix) => {
        const el = document.querySelector(selector);
        return (el?.getAttribute('aria-label') || text(el)).replace(prefix, '').trim();
    };
    const phone = document.querySelector('[data-item-id^="phone:tel:"]');
    const website = document.querySelector('a[data-item-id="authority"]');
    const rating = document.querySelector('.F7nice [aria-label*="stars"], [role="img"][aria-label*="stars"]');
    const reviews = document.querySelector('.F7nice [aria-label*="reviews"], .F7nice [aria-label*="Reviews"]');
    const ratingMatch = (rating?.getAttribute('aria-label') || text(document.querySelector('.F7nice'))).match(/\d+(?:[.,]\d+)?/);
    const reviewMatch = (reviews?.getAttribute('aria-label') || text(document.querySelector('.F7nice'))).match(/([\d,]+)\s*reviews|\(([\d,]+)\)/i);
    const data = {
        name: text(document.querySelector('h1.DUwDvf, [role="main"] h1')),
        address: labeled('[data-item-id="address"]', /^Address:\s*/i),
        phone: (phone?.getAttribute('aria-label') || '').replace(/^Phone:\s*/i, '').trim() || (phone?.getAttribute('data-item-id') || '').replace('phone:tel:', ''),
        website: website?.href || '',
        category: text(document.querySelector('button.DkEaL, button[jsaction*="category"]')),
        rating: ratingMatch ? ratingMatch[0].replace(',', '.') : '',
        reviews: reviewMatch ? (reviewMatch[1] || reviewMatch[2]).replaceAll(',', '') : '',
    };
    for (const tr of document.querySelectorAll('table tr')) {
        const cells = [...tr.querySelectorAll('td, th')].map(text);
        if (/^(monday|tuesday|wednesday|thursday|friday|saturday|sunday)$/i.test(cells[0] || ''))
            data[cells[0].toLowerCase()] = cells[1] || '';
    }
    return data;
}"""


async def check_access(page):
    if 'consent.google.' in page.url or '/sorry/' in page.url:
        raise AccessBlocked('Google consent/verification page encountered; no bypass attempted')
    if await page.locator('#captcha-form, iframe[src*="recaptcha"]').count():
        raise AccessBlocked('Google verification challenge encountered; no bypass attempted')


async def read_detail(page):
    await check_access(page)
    await page.locator('h1.DUwDvf, [role="main"] h1').first.wait_for(state='visible', timeout=15000)
    source_url = page.url
    values = await page.evaluate(DETAIL_SCRIPT)
    if not values['name'] or values['name'].lower() in {'results', 'google maps'}:
        raise RuntimeError('Place details did not contain a business heading')
    hours = page.locator('button[data-item-id="oh"]').first
    if await hours.count():
        try:
            await hours.click(timeout=3000)
            await page.locator('table').first.wait_for(state='attached', timeout=3000)
            expand = page.locator('[role="button"][aria-expanded]:has([aria-label="Show open hours for the week"])').first
            if await expand.count() and await expand.is_visible():
                if await expand.get_attribute('aria-expanded') != 'true':
                    await expand.click(timeout=2000)
                try:
                    await page.wait_for_function(r"""() => new Set(
                        [...document.querySelectorAll('table tr')]
                        .map(tr => tr.querySelector('td,th')?.textContent.trim().toLowerCase())
                        .filter(day => /^(monday|tuesday|wednesday|thursday|friday|saturday|sunday)$/.test(day || ''))
                    ).size === 7""", timeout=2000)
                except BrowserError:
                    pass  # Some places expose fewer than seven daily rows.
            weekly = await page.evaluate(r"""() => {
                const days = {};
                for (const tr of document.querySelectorAll('table tr')) {
                    const cells = [...tr.querySelectorAll('td,th')].map(e => (e.innerText || e.textContent || '').trim());
                    const day = (cells[0] || '').toLowerCase();
                    if (/^(monday|tuesday|wednesday|thursday|friday|saturday|sunday)$/.test(day) && !days[day])
                        days[day] = cells[1] || '';
                }
                return days;
            }""")
            values.update(weekly)
        except BrowserError as exc:
            print(f'    Warning: optional hours panel unavailable: {exc}', flush=True)
    return blank_lead(**{**values, **place_identity(source_url), 'maps_url': source_url})


async def collect_maps(query, limit=999, timeout=7200, headless=True, configure_context=None):
    """Scroll the public feed, then open detail pages; fresh non-persistent browser."""
    leads = []
    browser = None
    async with async_playwright() as playwright:
        args = ['--disable-extensions', '--enable-automation', '--window-size=1280,800']
        if not headless:
            args.append('--window-position=-10000,-10000')
        browser = await playwright.chromium.launch(headless=headless, channel='chromium', args=args)
        try:
            context = await browser.new_context(viewport={'width': 1280, 'height': 800}, locale='en-US')
            if configure_context:
                await configure_context(context)
            async with asyncio.timeout(timeout):
                page = await context.new_page()
                search_url = 'https://www.google.com/maps/search/' + quote_plus(query) + '?hl=en'
                print('Navigating to ' + search_url + '...', flush=True)
                await page.goto(search_url, wait_until='domcontentloaded', timeout=30000)
                await check_access(page)
                await page.wait_for_function(r"""() =>
                    document.querySelector('[role="feed"], h1.DUwDvf') ||
                    /no results found|couldn.t find/i.test(document.body?.innerText || '')
                """, timeout=30000)
                feed = page.locator('[role="feed"]').first
                if not await feed.count():
                    if await page.locator('h1.DUwDvf').count():
                        leads.append(await read_detail(page))
                    else:
                        print('Maps reports no results for this query.', flush=True)
                    return leads
                print('Scrolling Google Maps results directly (no extension)...', flush=True)
                seen = {}
                unchanged = 0
                stop_reason = 'stalled: 21 scroll rounds without new places (end not confirmed)'
                while True:
                    await check_access(page)
                    before = len(seen)
                    for card in await page.evaluate(CARD_SCRIPT):
                        name, url = card['name'], card['maps_url']
                        parsed = urlsplit(url)
                        if (not name or name.lower() in {'results', 'google maps'} or
                                parsed.hostname not in {'www.google.com', 'maps.google.com', 'google.com'} or
                                not parsed.path.startswith('/maps/place/')):
                            continue
                        row = blank_lead(**{**card, **place_identity(url)})
                        matches = [key for key, existing in seen.items() if
                                   (row['place_id'] and row['place_id'] == existing['place_id']) or
                                   (row['cid'] and row['cid'] == existing['cid']) or
                                   place_key(row) == place_key(existing)]
                        if matches:
                            existing = seen[matches[0]]
                            for source in [row] + [seen.pop(key) for key in matches[1:]]:
                                existing.update({key: value for key, value in source.items() if value != '' and not existing[key]})
                        else:
                            seen[place_key(row)] = row
                        if len(seen) >= limit:
                            break
                    leads[:] = list(seen.values())[:limit]
                    print(f'  Collected {len(leads)} unique place links.', flush=True)
                    body = await feed.inner_text()
                    if len(seen) >= limit:
                        stop_reason = 'requested limit reached'
                        break
                    if "you've reached the end" in body.lower():
                        stop_reason = 'end of exposed results'
                        break
                    if 'limited view' in body.lower():
                        stop_reason = 'Google Maps limited view'
                        break
                    unchanged = unchanged + 1 if len(seen) == before else 0
                    if unchanged > 20:
                        break
                    await feed.evaluate('(el) => {el.scrollBy(0, Math.max(el.clientHeight, 800));el.dispatchEvent(new Event("scroll"));}')
                    await page.wait_for_timeout(1000)
                print(f'Scrolling stopped: {stop_reason}. Opening {len(leads)} place pages...', flush=True)
                detail_page = await context.new_page()
                failures = []
                for index, lead in enumerate(leads, 1):
                    print(f'  [{index}/{len(leads)}] {lead["name"]}', flush=True)
                    try:
                        await detail_page.goto(lead['maps_url'], wait_until='domcontentloaded', timeout=25000)
                        detail = await read_detail(detail_page)
                        lead.update({key: value for key, value in detail.items() if value != ''})
                    except AccessBlocked:
                        raise
                    except Exception as exc:
                        failures.append(lead['name'])
                        print(f'    Warning: details unavailable; preserving card/link: {exc}', flush=True)
                if failures:
                    raise CollectionIncomplete('Some place detail pages failed: ' + ', '.join(failures), leads)
                print(f'Successfully captured {len(leads)} leads directly from Google Maps.', flush=True)
                return leads
        except AccessBlocked as exc:
            raise CollectionIncomplete(str(exc), leads) from exc
        except TimeoutError as exc:
            raise CollectionIncomplete('Google Maps collection timed out; results are partial', leads) from exc
        except CollectionIncomplete:
            raise
        except Exception as exc:
            raise CollectionIncomplete(f'Google Maps collection failed; results are partial: {exc}', leads) from exc
        finally:
            await browser.close()


MAX_CONCURRENT_CRAWLS = 5

def extract_emails(text: str) -> list:
    """Extract all email addresses from text."""
    pattern = r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b'
    return list(set(re.findall(pattern, text)))


def extract_social_links(html: str, base_url: str) -> dict:
    """Extract social media links from HTML content."""
    social = {
        "facebook": "",
        "instagram": "",
        "twitter": "",
        "linkedin": "",
        "youtube": "",
        "tiktok": "",
    }

    # Find all href attributes
    hrefs = re.findall(r'href=["\'](https?://[^"\']+)["\']', html, re.IGNORECASE)

    for href in hrefs:
        href_lower = href.lower()
        if not social["facebook"] and ("facebook.com/" in href_lower or "fb.com/" in href_lower):
            social["facebook"] = href
        elif not social["instagram"] and "instagram.com/" in href_lower:
            social["instagram"] = href
        elif not social["twitter"] and ("twitter.com/" in href_lower or "x.com/" in href_lower):
            social["twitter"] = href
        elif not social["linkedin"] and "linkedin.com/" in href_lower:
            # Prefer company URLs over personal
            if "/company/" in href_lower or "/in/" not in social.get("linkedin", ""):
                social["linkedin"] = href
        elif not social["youtube"] and ("youtube.com/" in href_lower or "youtu.be/" in href_lower):
            social["youtube"] = href
        elif not social["tiktok"] and "tiktok.com/" in href_lower:
            social["tiktok"] = href

    return social


async def crawl_website(page, url: str, lead: dict) -> dict:
    """Crawl a business website to extract emails and social links."""
    if not url or url == "":
        return lead

    # Skip mailto:, tel:, javascript: links
    if url.startswith("mailto:") or url.startswith("tel:") or url.startswith("javascript:"):
        return lead

    # Ensure URL has scheme
    if not url.startswith("http://") and not url.startswith("https://"):
        url = "https://" + url

    try:
        print(f"  🌐 Crawling website: {url}")
        await page.goto(url, wait_until='domcontentloaded', timeout=20000)
        await asyncio.sleep(1)

        # Get page content
        html = await page.content()
        text = await page.inner_text('body') if await page.query_selector('body') else ""

        # Extract emails
        emails = extract_emails(html + text)
        if emails:
            # Filter out common placeholder/system emails
            valid_emails = [e for e in emails if not any(
                domain in e.lower() for domain in [
                    "example.com", "domain.com", "yourdomain", "gmail.com",
                    "yahoo.com", "hotmail.com", "outlook.com", "noreply@",
                    "donotreply@", "no-reply@"
                ]
            )]
            if valid_emails:
                lead["email"] = valid_emails[0]
                if len(valid_emails) > 1:
                    lead["additional_emails"] = "; ".join(valid_emails[1:])

        # Extract social links
        social = extract_social_links(html, url)
        for key, val in social.items():
            if val and not lead.get(key):
                lead[key] = val

        print(f"    ✅ Found email: {lead.get('email', 'N/A')} | Social: {sum(1 for k in social.values() if k)} links")

    except Exception as e:
        print(f"    ⚠️ Could not crawl {url}: {e}")

    return lead


async def enrich_leads_with_websites(leads: list, configure_context=None) -> list:
    """Crawl websites for all leads that have a URL to extract email & socials."""
    if not leads:
        return leads

    print(f"\n{'='*60}")
    print(f"🔍 Crawling websites to enrich {len(leads)} leads...")
    print(f"{'='*60}")

    enriched = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True, channel="chromium",
            args=["--disable-extensions", "--enable-automation"]
        )

        try:
            # Process in batches to avoid overwhelming
            for i in range(0, len(leads), MAX_CONCURRENT_CRAWLS):
                batch = leads[i:i + MAX_CONCURRENT_CRAWLS]
                batch_tasks = []

                for lead in batch:
                    website = lead.get("website", "")
                    if website and website.strip():
                        context = await browser.new_context(
                            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
                        )
                        if configure_context:
                            await configure_context(context)
                        page = await context.new_page()
                        batch_tasks.append((context, page, lead))
                    else:
                        enriched.append(lead)

                # Crawl concurrently (but sequential within each to avoid context issues)
                for ctx, pg, ld in batch_tasks:
                    ld = await crawl_website(pg, ld.get("website", ""), ld)
                    await ctx.close()
                    enriched.append(ld)

        finally:
            await browser.close()

    return enriched


def query_to_filename(query):
    return re.sub(r'\s+', '_', re.sub(r'[^a-z0-9_\s]', '', query.lower().strip())) or 'maps_results'


def save_results(leads, query, output_dir=DEFAULT_OUTPUT):
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    stem = query_to_filename(query)
    index = 1
    while True:
        path = directory / (stem + (f'_{index}' if index > 1 else '') + '.csv')
        try:
            stream = path.open('x', encoding='utf-8', newline='')
            break
        except FileExistsError:
            index += 1
    with stream:
        writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(blank_lead(**lead) for lead in leads)
    print(f'\n✅ Saved {len(leads)} leads to {path.resolve()}', flush=True)
    return path


def parse_args(argv=None):
    import argparse
    import math
    parser = argparse.ArgumentParser(description='Scrape Maps directly with Chromium, without an extension.')
    parser.add_argument('query')
    parser.add_argument('limit', type=int, nargs='?', default=999)
    parser.add_argument('crawl_websites', nargs='?', default='1', help='1 enables website crawling; 0/false/no disables it')
    parser.add_argument('--output-dir', type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument('--timeout', type=float, default=7200, help='Maps phase budget in seconds, after browser startup')
    args = parser.parse_args(argv)
    if not args.query.strip():
        parser.error('query must not be empty')
    if args.limit <= 0:
        parser.error('limit must be positive')
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error('timeout must be finite and positive')
    args.crawl_websites = str(args.crawl_websites).lower() not in {'0', 'false', 'no'}
    return args


async def run(args, configure_context=None):
    headless = os.environ.get('HERMES_HEADLESS') != '0' or not os.environ.get('DISPLAY')
    print(f'🔍 Scraping: {args.query} (limit={args.limit}, crawl_websites={args.crawl_websites})', flush=True)
    print('Running in headless mode without extension' if headless else 'Running off-screen without extension', flush=True)
    try:
        leads = await collect_maps(args.query, args.limit, args.timeout, headless, configure_context)
    except CollectionIncomplete as exc:
        if exc.leads:
            path = save_results(exc.leads, args.query + '_partial', args.output_dir)
            print(f'⚠️ Partial collection only; preserved at {path.resolve()}', file=sys.stderr)
        raise
    if args.crawl_websites and leads:
        raw_path = save_results(leads, args.query + '_raw', args.output_dir)
        print(f'Raw Maps output preserved before website crawling: {raw_path.resolve()}', flush=True)
        leads = await enrich_leads_with_websites(leads, configure_context=configure_context)
    path = save_results(leads, args.query, args.output_dir)
    print(f'\n✅ Scraped {len(leads)} leads -> {path.parent.resolve()}', flush=True)
    for lead in leads[:10]:
        print(f"   - {lead.get('name', '')} | {lead.get('phone', '')} | {lead.get('email', '')}", flush=True)
    return path


def main(argv=None):
    args = parse_args(argv)
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        print('Interrupted.', file=sys.stderr)
        return 130
    except Exception as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
