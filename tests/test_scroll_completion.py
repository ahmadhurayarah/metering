"""Exercise the actual extension's scroll loop on deterministic local DOM fixtures."""
import unittest
from pathlib import Path
from playwright.async_api import async_playwright

SCRIPT = Path(__file__).resolve().parents[1] / "extension_bundle/contentScript.js"
HTML = '''<div class="w6VYqd"></div><div role="feed" style="height:180px;overflow:auto">
<div role="article" style="height:300px"><a class="hfpxzc" aria-label="Early fixture"
href="https://www.google.com/maps/place/Early/data=!19sChIJ_early">Early fixture</a></div>
</div>'''


class ExtensionScrollTests(unittest.IsolatedAsyncioTestCase):
    async def run_delayed_end_fixture(self, misleading_marker=False):
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.set_content(HTML)
                await page.evaluate("window.chrome = {runtime: {sendMessage: async () => ({})}}")
                await page.add_script_tag(path=str(SCRIPT))
                await page.evaluate('''() => {
                    window.scrollRounds = 0;
                    wait = async function(ms) {
                        if (ms >= 1000) {
                            window.scrollRounds++;
                            if (window.scrollRounds === 25) {
                                document.querySelector('[role="feed"]').insertAdjacentHTML('beforeend',
                                    '<div role="article"><a class="hfpxzc" aria-label="Late fixture" href="https://www.google.com/maps/place/Late/data=!19sChIJ_late">Late fixture</a></div>');
                            }
                            if (window.scrollRounds === 30) {
                                document.querySelector('[role="feed"]').insertAdjacentHTML('beforeend',
                                    '<div class="HlvSq">You&#39;ve reached the end of the list.</div>');
                            }
                        }
                        await new Promise(resolve => setTimeout(resolve, 1));
                    };
                }''')
                if misleading_marker:
                    await page.evaluate('''document.querySelector('[role="feed"]').insertAdjacentHTML('beforeend', '<div class="HlvSq">Still loading results…</div>')''')
                await page.wait_for_selector("#extension_gms_start_btn")
                await page.locator("#extension_gms_start_btn").click()
                await page.wait_for_function("document.getElementById('extension_gms_start_btn').innerText === 'Start Auto Extract'")
                result = await page.evaluate('''({rounds:window.scrollRounds, names:leads.map(row=>row.name), reason:document.getElementById('extension_gms_start_btn').dataset.stopReason})''')
                self.assertGreaterEqual(result["rounds"], 30, "The extension must not stop after unchanged-height checks")
                self.assertEqual(result["names"], ["Early fixture", "Late fixture"])
                self.assertEqual(result.get("reason"), "end_of_list")
            finally:
                await browser.close()

    async def test_scrolls_past_twenty_idle_rounds_until_real_end_marker(self):
        await self.run_delayed_end_fixture()

    async def test_a_css_class_without_end_text_is_not_completion(self):
        await self.run_delayed_end_fixture(misleading_marker=True)


if __name__ == "__main__":
    unittest.main()
