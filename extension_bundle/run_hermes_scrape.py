#!/usr/bin/env python3
"""Metering extension runner: Chromium hosts the bundled extension headlessly.

Maps extraction is performed by the extension content scripts. Optional website
contact extraction is performed by the extension background worker, not Python
page scraping. This project-local copy does not modify the source extension repo.
"""
import argparse
import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_OUTPUT = ROOT / "output"

import asyncio
import re
import csv
import os
import tempfile
from urllib.parse import urlparse, urljoin
from playwright.async_api import async_playwright

# Path to the scrape-extension Chrome extension
EXTENSION_PATH = str(ROOT)

class CollectionIncomplete(RuntimeError):
    def __init__(self, message, leads, stop_reason):
        super().__init__(message)
        self.leads = leads
        self.stop_reason = stop_reason


class ScrapeResults(list):
    def __init__(self, rows, stop_reason):
        super().__init__(rows)
        self.stop_reason = stop_reason


ACCESS_BOUNDARY_SCRIPT = r"""() => {
    const text = document.body?.innerText || '';
    const challenge = [...document.querySelectorAll('#captcha-form, form[action*="/sorry/"], iframe[src*="recaptcha"]')]
        .some(node => node.getClientRects().length && node.offsetHeight > 0);
    if (/\/sorry(?:\/|\?)/i.test(location.href) || challenge || /our systems have detected unusual traffic|verify (?:that )?you(?:.re| are) (?:a )?human|i.m not a robot/i.test(text)) return 'verification';
    if (/^https?:\/\/consent\.google\./i.test(location.href) || /before you continue to google/i.test(text)) return 'consent';
    return '';
}"""


# Maximum pages to crawl concurrently
MAX_CONCURRENT_CRAWLS = 5


def query_to_filename(query: str) -> str:
    """Convert a search query to a safe filename.
    Example: 'salon in wah cantt' -> 'salon_in_wah_cantt'
    """
    name = query.lower().strip()
    name = re.sub(r'[^a-z0-9\s]', '', name)
    name = re.sub(r'\s+', '_', name)
    return name


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


async def enrich_leads_with_websites(leads: list) -> list:
    """Ask this extension's background worker to extract website contacts.

    Python never reads or parses business-site pages in this workflow.
    """
    if not any(lead.get("website") for lead in leads):
        return [dict(lead) for lead in leads]
    print(f"Enriching {len(leads)} listings through the extension service worker.", flush=True)
    enriched = []
    with tempfile.TemporaryDirectory(prefix="metering-extension-email-") as profile:
        async with async_playwright() as p:
            context = await p.chromium.launch_persistent_context(
                profile, headless=True, channel="chromium",
                args=[f"--disable-extensions-except={EXTENSION_PATH}",
                      f"--load-extension={EXTENSION_PATH}"],
            )
            try:
                worker = context.service_workers[0] if context.service_workers else await context.wait_for_event("serviceworker", timeout=15000)
                for original in leads:
                    row = dict(original)
                    if row.get("website"):
                        extra = await asyncio.wait_for(worker.evaluate(
                            """async ({website, name}) => {
                                const values = await extractemail(website, name, true);
                                return Object.fromEntries(Object.entries(values).map(
                                    ([key, value]) => [key, Array.from(value)]));
                            }""", {"website": row["website"], "name": row.get("name", "")}
                        ), timeout=90)
                        for field, values in extra.items():
                            if field == "email":
                                existing = [value.strip() for value in re.split(r"[,;]", row.get("email", "")) if value.strip()]
                                emails = list(dict.fromkeys(existing + values))
                                if emails:
                                    row["email"] = emails[0]
                                    additional = [value.strip() for value in row.get("additional_emails", "").split(";") if value.strip()]
                                    row["additional_emails"] = "; ".join(dict.fromkeys(additional + emails[1:]))
                            elif values and not row.get(field):
                                row[field] = "; ".join(values)
                    enriched.append(row)
            finally:
                await context.close()
    return enriched

async def scrape_google_maps(query: str, limit: int = 0, output_dir: str | None = None, crawl_websites: bool = True, *, timeout_seconds: float | None = None):
    """Scrapes Google Maps by fully automating the scrape-extension Chrome Extension.

    Args:
        query: Search query (e.g. 'salon in wah cantt')
        limit: Maximum results; 0 means no numeric listing cap
        output_dir: If provided, saves results as CSV in this directory
        crawl_websites: If True, crawl each lead's website for emails & social links

    Returns:
        List of normalized lead dictionaries
    """
    leads = []
    stop_reason = "unknown"
    collection_error = None
    is_unlimited_run = limit <= 0 or limit >= 999

    if not os.path.isdir(EXTENSION_PATH):
        raise FileNotFoundError(
            f"Chrome extension not found at {EXTENSION_PATH}. "
            f"Clone it with: gh repo clone ahmadhurayarah/scrape-extension {EXTENSION_PATH}"
        )

    profile = tempfile.TemporaryDirectory(prefix="metering-maps-extension-")
    user_data_dir = profile.name

    is_headless_env = True  # Dashboard always uses headless full Chromium + extension.

    async with async_playwright() as p:
        # Launch Chromium in headless-new mode (supports extensions in newer Chrome)
        # or via xvfb virtual display on servers
        launch_args = [
            f"--disable-extensions-except={EXTENSION_PATH}",
            f"--load-extension={EXTENSION_PATH}",
            "--window-size=1280,800",
        ]

        if is_headless_env:
            # Server / no display: use Chrome's new headless mode
            launch_args.append("--headless=new")
            headless_flag = True
            print("Running in headless mode (no visible window)")
        else:
            # Desktop: run off-screen so it doesn't interrupt
            launch_args.append("--window-position=-10000,-10000")
            headless_flag = False
            print("Running off-screen (window hidden from view)")

        context = await p.chromium.launch_persistent_context(
            user_data_dir,
            headless=headless_flag,
            channel='chromium',
            args=launch_args,
            viewport={'width': 1280, 'height': 800},
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        )

        # Wait for extension background to initialize
        await asyncio.sleep(2)

        page = context.pages[0] if context.pages else await context.new_page()

        # We need a promise to resolve when the extension logs the leads
        future_leads = asyncio.Future()

        async def handle_console(msg):
            if msg.text.startswith("leads:"):
                try:
                    leads_data = await msg.args[1].json_value()
                    if not future_leads.done():
                        future_leads.set_result(leads_data)
                except Exception as e:
                    print("Error parsing extension console log:", e)

        page.on("console", handle_console)

        async def export_collected():
            export_btn = await page.query_selector('#extension_gms_download_btn')
            if not export_btn:
                raise RuntimeError("The extension export control is unavailable")
            await export_btn.click(timeout=3000)
            return await asyncio.wait_for(asyncio.shield(future_leads), timeout=15.0)

        async def stop_if_running():
            button = await page.query_selector('#extension_gms_start_btn')
            if button and await button.inner_text() == "Stop Auto Extract":
                await button.click(timeout=3000)


        search_url = f"https://www.google.com/maps/search/{query.replace(' ', '+')}"
        print(f"Navigating to {search_url}...")
        try:
            await page.goto(search_url, wait_until='domcontentloaded')
            boundary = await page.evaluate(ACCESS_BOUNDARY_SCRIPT)
            if boundary:
                stop_reason = boundary
                raise RuntimeError(f'Maps access boundary: {boundary}')
            # Wait for the extension UI to be injected into the page
            print("Waiting for extension UI...")
            await page.wait_for_selector('#extension_gms_start_btn', timeout=15000)

            # A zero limit leaves collection uncapped; the extension owns its stop conditions.
            await page.evaluate(f"window.SCRAPER_LIMIT = {limit};")

            start_btn = await page.query_selector('#extension_gms_start_btn')

            # Click the start button
            print("Starting Auto Extract via extension...")
            await start_btn.click()

            # The extension starts scrolling. It stops when text becomes "Start Auto Extract" again.
            max_wait_seconds = timeout_seconds if timeout_seconds is not None else (7200 if is_unlimited_run else (limit * 5 + 30))
            elapsed = 0

            while elapsed < max_wait_seconds:
                interval = min(2, max_wait_seconds - elapsed)
                await asyncio.sleep(interval)
                elapsed += interval
                boundary = await page.evaluate(ACCESS_BOUNDARY_SCRIPT)
                if boundary:
                    stop_reason = boundary
                    break

                # Check button state
                btn_text = await page.evaluate("document.getElementById('extension_gms_start_btn')?.innerText")
                if btn_text == "Start Auto Extract":
                    stop_reason = await page.evaluate("document.getElementById('extension_gms_start_btn')?.dataset.stopReason") or "unknown"
                    break

                # Also check if we reached the requested limit
                info_text = await page.evaluate("document.getElementById('extension_gms_leads_info')?.innerText")
                if info_text:
                    match = re.search(r'Leads: (\d+)', info_text)
                    if limit > 0 and match and int(match.group(1)) >= limit:
                        stop_reason = "limit_reached"
                        # Reached limit, stop extraction
                        stop_btn = await page.query_selector('#extension_gms_start_btn')
                        if stop_btn:
                            await stop_btn.click()
                        break

            else:
                stop_reason = "timeout"

            await stop_if_running()
            print(f"Extension stopped: {stop_reason}. Exporting available records...")
            leads = await export_collected()
            print(f"Successfully captured {len(leads)} leads from extension.")

        except Exception as e:
            collection_error = e
            if stop_reason in {"unknown", "end_of_list", "single_place", "no_results"}:
                stop_reason = "automation_error"
            # Checkpoint the extension's own records even after collection errors.
            try:
                await stop_if_running()
                leads = await export_collected()
            except Exception:
                if future_leads.done() and not future_leads.cancelled():
                    leads = future_leads.result()

        finally:
            await context.close()
            profile.cleanup()

    # Normalize the output to match our API schema
    normalized_leads = []
    for lead in leads:
        normalized_leads.append({
            "name": lead.get("name", ""),
            "phone": lead.get("phone", ""),
            "email": lead.get("email", ""),
            "website": lead.get("website", ""),
            "address": lead.get("address", ""),
            "instagram": lead.get("instagram", ""),
            "facebook": lead.get("facebook", ""),
            "twitter": lead.get("twitter", ""),
            "linkedin": lead.get("linkedin", ""),
            "tiktok": "",
            "yelp": lead.get("yelp", ""),
            "youtube": lead.get("youtube", ""),
            "place_id": lead.get("placeID", ""),
            "cid": lead.get("cID", ""),
            "category": lead.get("category", ""),
            "reviews": lead.get("reviewCount", ""),
            "rating": lead.get("averageRating", ""),
            "latitude": lead.get("latitude", ""),
            "longitude": lead.get("longitude", ""),
            "monday": lead.get("1_Monday", ""),
            "tuesday": lead.get("2_Tuesday", ""),
            "wednesday": lead.get("3_Wednesday", ""),
            "thursday": lead.get("4_Thursday", ""),
            "friday": lead.get("5_Friday", ""),
            "saturday": lead.get("6_Saturday", ""),
            "sunday": lead.get("7_Sunday", ""),
            "additional_emails": "",
            "maps_url": f"https://www.google.com/maps/place/?q=place_id:{lead.get('placeID', '')}" if lead.get("placeID") else ""
        })

    result = normalized_leads[:limit] if 0 < limit < len(normalized_leads) else normalized_leads

    incomplete = collection_error is not None or stop_reason not in {"end_of_list", "single_place", "no_results"}

    # Never enrich an incomplete collection automatically.
    if crawl_websites and result and not incomplete:
        result = await enrich_leads_with_websites(result)

    # Save to CSV if output_dir is provided
    if output_dir and result:
        os.makedirs(output_dir, exist_ok=True)
        filename = query_to_filename(query)
        csv_path = os.path.join(output_dir, f"{filename}.csv")

        with open(csv_path, 'w', newline='', encoding='utf-8') as f:
            if result:
                writer = csv.DictWriter(f, fieldnames=result[0].keys())
                writer.writeheader()
                writer.writerows(result)

        print(f"\n✅ Saved {len(result)} leads to {csv_path}")

    if incomplete:
        message = f"Collection stopped before the end of the list ({stop_reason}); available records are partial."
        if collection_error:
            message = f"Extension automation failed: {collection_error}. " + message
        raise CollectionIncomplete(message, result, stop_reason)
    return ScrapeResults(result, stop_reason)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("query")
    parser.add_argument("limit", nargs="?", type=int, default=0)
    parser.add_argument("crawl_websites", nargs="?", default="1",
                        help="0/false/no disables website crawling; enabled by default")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    args.crawl_websites = str(args.crawl_websites).lower() not in ("0", "false", "no")
    return args


def sha256_file(path):
    with open(path, "rb") as file:
        return hashlib.sha256(file.read()).hexdigest()


async def run(args):
    # Verify only files belonging to this project. Never read another project.
    protected = [Path(__file__).resolve(), ROOT / "manifest.json", ROOT / "contentScript.js",
                 ROOT / "contentScript2.js", ROOT / "injected.js", ROOT / "bg.js", ROOT / "js/mybg.js"]
    before = {path: sha256_file(path) for path in protected if path.is_file()}
    output_dir = str(args.output_dir.resolve())
    os.makedirs(output_dir, exist_ok=True)
    try:
        print(f"🔍 Scraping: {args.query} (limit={args.limit}, crawl_websites={args.crawl_websites})")
        leads = await scrape_google_maps(query=args.query, limit=args.limit,
                                        output_dir=output_dir, crawl_websites=args.crawl_websites)
        print(f"\n✅ Scraped {len(leads)} leads -> {output_dir}")
        for lead in leads:
            print(f"   - {lead.get('name')} | {lead.get('phone')} | {lead.get('email')}")
        return leads
    finally:
        changed = [str(path) for path, digest in before.items()
                   if not path.is_file() or sha256_file(path) != digest]
        if changed:
            raise RuntimeError(f"Local scraper source changed during run: {', '.join(changed)}")
        print("🔒 Verified local runner and extension files are byte-identical after run")


def main(argv=None):
    asyncio.run(run(parse_args(argv)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
