# ruff: noqa: E501  (long lines are JavaScript expressions evaluated in the page)
"""Drive the playground in headless Chrome over CDP and check every interaction.

Usage: python tests/e2e/playground_ui.py <strict-base-url> <optin-base-url> <out-dir>

<strict-base-url> is a server on the default profile with no provider keys; <optin-base-url>
has FAIRSERP_OPT_IN=firecrawl. Both make live requests to Wikipedia, OpenStreetMap and
Firecrawl, so this runs in the canary workflow, not in required CI. Set CHROME to the browser
binary if it is not on the default path. Prints one PASS/FAIL line per check and writes the
snippets it extracted to <out-dir>.
"""
import asyncio
import json
import os
import shutil
import subprocess
import sys
import urllib.request

import websockets

CHROME = os.environ.get("CHROME") or shutil.which("google-chrome") or shutil.which("chromium") \
    or "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
STRICT, OPTIN, OUT = sys.argv[1], sys.argv[2], sys.argv[3]
results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}{'  ' + str(detail) if detail and not ok else ''}")


class Page:
    def __init__(self, ws):
        self.ws, self.n, self.events = ws, 0, []

    async def call(self, method, **params):
        self.n += 1
        await self.ws.send(json.dumps({"id": self.n, "method": method, "params": params}))
        while True:
            msg = json.loads(await self.ws.recv())
            if msg.get("id") == self.n:
                return msg.get("result", {})
            self.events.append(msg)

    async def js(self, expression):
        r = await self.call("Runtime.evaluate", expression=expression, awaitPromise=True,
                            returnByValue=True, userGesture=True)
        if "exceptionDetails" in r:
            raise RuntimeError(r["exceptionDetails"])
        return r["result"].get("value")

    async def until(self, expression, timeout=20.0):
        """Poll a condition in the page; returns True once it holds."""
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout
        while loop.time() < end:
            if await self.js(expression):
                return True
            await asyncio.sleep(0.1)
        return False

    async def goto(self, url):
        await self.call("Page.navigate", url=url)
        await self.until("document.readyState === 'complete' && !!document.getElementById('go')")

    async def search_done(self):
        return await self.until("!document.getElementById('go').disabled && "
                                "document.getElementById('loading').hidden")


async def main():
    proc = subprocess.Popen([CHROME, "--headless=new", "--disable-gpu", "--remote-debugging-port=9334",
                             f"--user-data-dir={OUT}/chrome-e2e", "about:blank"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            try:
                tabs = json.load(urllib.request.urlopen("http://127.0.0.1:9334/json"))
                break
            except OSError:
                await asyncio.sleep(0.1)
        ws_url = [t for t in tabs if t["type"] == "page"][0]["webSocketDebuggerUrl"]
        async with websockets.connect(ws_url, max_size=50_000_000) as ws:
            page = Page(ws)
            for domain in ("Page", "Runtime", "Log"):
                await page.call(f"{domain}.enable")
            await page.call("Browser.grantPermissions", permissions=["clipboardReadWrite", "clipboardSanitizedWrite"])
            await page.call("Emulation.setDeviceMetricsOverride", width=1280, height=900,
                            deviceScaleFactor=1, mobile=False)

            # 1. First run, strict profile: setup panel and its buttons.
            await page.goto(STRICT + "/")
            check("engine select lists Auto plus enabled organic engines",
                  await page.until("document.getElementById('engine-select').options.length >= 2"),
                  await page.js("[...document.getElementById('engine-select').options].map(o => o.text)"))
            await page.js("document.getElementById('q').value = 'rust async runtime'; "
                          "document.getElementById('go').click()")
            await page.search_done()
            check("no-provider search shows the setup panel",
                  await page.until("!document.getElementById('panel').hidden && "
                                   "document.querySelector('#panel h2').textContent.includes('No web search provider')"))
            await page.js("[...document.querySelectorAll('#panel button')].find(b => b.textContent === 'Search Wikipedia').click()")
            await page.search_done()
            check("setup: Search Wikipedia returns results",
                  await page.until("document.querySelectorAll('#results .result').length > 0"))
            check("URL reflects the Wikipedia search",
                  "engine=wikipedia" in await page.js("location.search"))

            # 2. Example links and the local search.
            await page.js("document.querySelector('[data-near]').click()")
            await page.search_done()
            check("pizza near Brooklyn shows places",
                  await page.until("document.querySelectorAll('#places .place').length > 0"))
            check("map draws the boundary path and one pin per place",
                  await page.js("!!document.querySelector('#map-canvas path.land') && "
                                "document.querySelectorAll('#map-canvas .pin').length === "
                                "document.querySelectorAll('#places .place').length"))
            check("every place address names Brooklyn",
                  await page.js("[...document.querySelectorAll('.place-address')].every(e => e.textContent.includes('Brooklyn'))"))
            check("hours are rendered as text, not raw OSM syntax",
                  not await page.js("[...document.querySelectorAll('.place-extra span')].some(e => /Mo-Su|Mo-Fr/.test(e.textContent))"))
            overlap = await page.js("""(() => {
              const pins = [...document.querySelectorAll('#map-canvas .pin circle')].map(c => c.getBoundingClientRect());
              for (let i = 0; i < pins.length; i++) for (let j = i + 1; j < pins.length; j++) {
                const a = pins[i], b = pins[j];
                const d = Math.hypot(a.x - b.x, a.y - b.y);
                if (d < a.width * 0.95) return [i + 1, j + 1, Math.round(d)];
              }
              return null; })()""")
            check("no two map pins overlap", overlap is None, overlap)
            size = await page.js("Math.round(document.querySelector('#map-canvas .pin text').getBoundingClientRect().height)")
            check("pin numbers render at a readable size (>= 10px)", size >= 10, size)

            # Hover a place and its pin highlights, and the reverse.
            box = await page.js("(() => { const r = document.querySelector('#places .place').getBoundingClientRect(); return [r.x + 40, r.y + 10]; })()")
            await page.call("Input.dispatchMouseEvent", type="mouseMoved", x=box[0], y=box[1])
            check("hovering a place highlights its map pin",
                  await page.until("document.querySelector('#map-canvas .pin[data-place=\"0\"]').classList.contains('is-active')", 3))
            await page.call("Input.dispatchMouseEvent", type="mouseMoved", x=5, y=5)
            check("moving away clears the highlight",
                  await page.until("!document.querySelector('.is-active')", 3))

            # Resize redraws the map at the new scale.
            before = await page.js("document.querySelector('#map-canvas svg').getAttribute('viewBox') + document.querySelector('#map-canvas .pin circle').getAttribute('r')")
            await page.call("Emulation.setDeviceMetricsOverride", width=600, height=900, deviceScaleFactor=1, mobile=True)
            redrawn = await page.until(f"document.querySelector('#map-canvas svg').getAttribute('viewBox') + document.querySelector('#map-canvas .pin circle').getAttribute('r') !== {json.dumps(before)}", 3)
            check("resizing redraws the map", redrawn)
            await page.call("Emulation.setDeviceMetricsOverride", width=1280, height=900, deviceScaleFactor=1, mobile=False)

            # 3. Snippets: each format, and copy.
            snippets = {}
            for kind in ("curl", "python", "mcp"):
                await page.js(f"document.querySelector('[data-copy=\"{kind}\"]').click()")
                pressed = await page.js(f"document.querySelector('[data-copy=\"{kind}\"]').getAttribute('aria-pressed')")
                snippets[kind] = await page.js("document.getElementById('snippet').textContent")
                check(f"{kind} tab selects and renders", pressed == "true" and len(snippets[kind]) > 20)
            await page.js("document.querySelector('[data-copy=\"curl\"]').click()")
            await page.js("document.getElementById('copy').click()")
            check("Copy confirms", await page.until("document.querySelector('#copy span').textContent === 'Copied'", 3))
            clip = await page.js("navigator.clipboard.readText()")
            check("clipboard holds the curl snippet", clip == snippets["curl"])
            check("Copy label resets", await page.until("document.querySelector('#copy span').textContent === 'Copy'", 4))
            with open(f"{OUT}/snippets.json", "w") as f:
                json.dump(snippets, f)

            # Changing the engine updates the snippet without searching.
            await page.js("const s = document.getElementById('engine-select'); s.value = 'wikipedia'; s.dispatchEvent(new Event('input'))")
            check("engine change updates the snippet",
                  "engine=wikipedia" in await page.js("document.getElementById('snippet').textContent"))

            # 4. Theme toggle persists and flips the icon.
            start = await page.js("document.querySelector('#theme span').textContent")
            await page.js("document.getElementById('theme').click()")
            after = await page.js("[document.documentElement.dataset.theme, document.querySelector('#theme span').textContent, document.getElementById('theme').title]")
            check("theme toggle switches and relabels", after[1] != start and after[0] in ("dark", "light"), after)
            await page.goto(STRICT + "/")
            check("theme choice survives a reload",
                  await page.js("document.documentElement.dataset.theme") == after[0])

            # 5. Keyboard: "/" focuses the query.
            await page.js("document.activeElement.blur()")
            await page.call("Input.dispatchKeyEvent", type="keyDown", key="/", code="Slash", text="/")
            await page.call("Input.dispatchKeyEvent", type="keyUp", key="/", code="Slash")
            check("'/' focuses the query", await page.js("document.activeElement.id") == "q")

            # Raw response disclosure.
            await page.goto(STRICT + "/?q=einstein&engine=wikipedia")
            await page.until("document.querySelectorAll('#results .result').length > 0")
            await page.js("document.querySelector('#raw summary').click()")
            check("raw response opens with JSON",
                  await page.js("document.getElementById('raw').open && JSON.parse(document.getElementById('raw-json').textContent).status !== undefined"))
            check("empty query is blocked before any request",
                  await page.js("document.getElementById('q').value = ''; "
                                "const before = performance.getEntriesByType('resource').length; "
                                "document.getElementById('go').click(); "
                                "!document.getElementById('form').checkValidity() && "
                                "performance.getEntriesByType('resource').length === before"))

            # 6. Opt-in server: limit panel.
            await page.goto(OPTIN + "/?q=rust+async+runtime")
            await page.until("!document.getElementById('summary').hidden && document.getElementById('loading').hidden")
            panel = await page.js("document.getElementById('panel').hidden ? '' : document.getElementById('panel').textContent")
            results_n = await page.js("document.querySelectorAll('#results .result').length")
            check("opt-in server: results or a limit panel with a retry time",
                  results_n > 0 or ("resets in" in panel), panel[:120] or results_n)

            # Console errors and CSP violations across the whole run.
            problems = [e["params"] for e in page.events
                        if e.get("method") in ("Runtime.exceptionThrown",)
                        or (e.get("method") == "Log.entryAdded" and e["params"]["entry"]["level"] == "error"
                            and e["params"]["entry"]["source"] != "network")]
            check("no JS exceptions, console errors or CSP violations (HTTP 4xx/5xx statuses are expected API answers)", not problems, problems[:3])
    finally:
        proc.terminate()
    print(f"\n{sum(results)}/{len(results)} checks passed")
    sys.exit(0 if all(results) else 1)


asyncio.run(main())
