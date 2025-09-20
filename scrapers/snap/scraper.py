import asyncio
import hashlib
import logging
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Set
from urllib.parse import urlencode, urlparse, urljoin

from playwright.async_api import async_playwright, Page, TimeoutError as PlaywrightTimeoutError

def now_iso() -> str:
    return datetime.now().isoformat()

class SnapScraper:
    """
    Scrapes Snap Inc. jobs from https://careers.snap.com/jobs
    Strategy:
      • Build list of US/CA locations (from config or read the dropdown).
      • For each location: GET /jobs?location=<City>, snapshot all job links.
      • Open each job in a NEW TAB, parse details (title, locations, posted, description).
      • Return unified job dicts compatible with your pipeline.
    """

    def __init__(self, website_config: Dict[str, Any], global_config: Dict[str, Any]):
        self.cfg = website_config
        self.g = global_config
        self.logger = logging.getLogger("job_tracker")

        wp = self.cfg.get("playwright_options", {})
        self.sel_results = wp.get("results_section", "section[data-testid='mwp-jobs-table']")
        self.sel_row_link = wp.get("job_row_link", "a.sdsm-hyperlink[href*='/job?id=R']")

        self.sel_loc_toggle = wp.get("location_toggle",
            "button[aria-haspopup='listbox']:has-text('All locations')")
        self.sel_loc_listbox = wp.get("location_listbox", "div[role='listbox'].sdsm-dropdown")
        self.sel_loc_option = wp.get("location_option", "div[role='listbox'] button.sdsm-dropdown-item")

        self.sel_detail_title = wp.get("detail_title", "main h1, article h1, h1")
        self.sel_detail_loc_block = wp.get("detail_locations_block",
            "article section:has(p) .css-u413lb, article section p")
        self.sel_detail_posted = wp.get("detail_posted_text",
            "article section:has(span:has-text('Posted')) span")
        self.sel_detail_desc = wp.get("detail_description",
            "article[data-testid='content-body']")

        sc = self.cfg.get("scraping_config", {})
        self.max_pages = sc.get("max_pages", 3)
        self.max_jobs = sc.get("max_jobs", 400)
        self.us_ca_only = sc.get("us_ca_only", True)
        self.sleep_after_nav_ms = sc.get("sleep_after_nav_ms", 1200)
        self.sleep_after_open_ms = sc.get("sleep_after_open_ms", 700)
        self.scroll_passes = sc.get("scroll_passes", 10)

        # Optional explicit city lists (fallback to reading the dropdown)
        self.us_cities = set(sc.get("us_cities", []))
        self.ca_cities = set(sc.get("ca_cities", []))

        pg = self.g["playwright_global"]
        self.timeout = pg["timeout"]
        self.user_agent = pg["user_agent"]
        self.headless = self.g["global_settings"]["headless"]

        self.base_url = self.cfg["website_info"]["base_url"]
        self.scraped: List[Dict[str, Any]] = []
        self.seen_urls: Set[str] = set()
        # Canonical US/CA cities we consider valid for Snap (can extend anytime)
        self._CITY_ALLOW = set(
            (self.us_cities or {
                "Austin","Bellevue","Chicago","Los Angeles","New York",
                "Palo Alto","San Diego","San Francisco","Santa Monica",
                "Seattle","Washington"
            })
            | (self.ca_cities or {"Toronto","Vancouver","Ottawa","Montreal","Waterloo"})
        )


    # -------- entry --------
    async def scrape_jobs(self) -> List[Dict[str, Any]]:
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=self.headless,
                    args=["--no-sandbox", "--disable-dev-shm-usage"]
                )
                context = await browser.new_context(user_agent=self.user_agent)
                page = await context.new_page()
                page.set_default_timeout(self.timeout)

                # Build location list
                locs = await self._resolve_us_ca_locations(page)
                if not locs:
                    self.logger.warning("No US/CA locations detected—will scrape base page only.")
                    locs = [""]  # base listing

                total = 0
                for city in locs:
                    if total >= self.max_jobs:
                        break
                    url = self._location_url(city)
                    await page.goto(url)
                    await page.wait_for_load_state("domcontentloaded")
                    await page.wait_for_timeout(self.sleep_after_nav_ms)

                    # collect all job hrefs on this location page (no “next” observed; page groups by team)
                    hrefs = await self._snapshot_job_hrefs(page)
                    for href in hrefs:
                        if total >= self.max_jobs: break
                        if href in self.seen_urls: continue
                        job = await self._parse_detail_in_new_tab(page, href)
                        await page.wait_for_timeout(self.sleep_after_open_ms)
                        if job:
                            self.scraped.append(job)
                            self.seen_urls.add(job["url"])
                            total += 1
                            self.logger.info(f"✔ [{total}] {job['title']} ({job.get('location','')})")

                await context.close()
                await browser.close()

            self.logger.info(f"Snap scraping finished. Total jobs: {len(self.scraped)}")
            return self.scraped

        except Exception as e:
            self.logger.error(f"Snap scraping error: {e}")
            return self.scraped

    # -------- helpers --------
    async def _resolve_us_ca_locations(self, page: Page) -> List[str]:
        """Prefer config-provided city lists; else read the dropdown and filter."""
        if self.us_cities or self.ca_cities:
            return sorted(self.us_cities | self.ca_cities)

        try:
            await page.goto(self.base_url)
            await page.wait_for_load_state("domcontentloaded")
            # Try to open the locations dropdown
            if await page.locator(self.sel_loc_toggle).count():
                await page.click(self.sel_loc_toggle)
                await page.wait_for_selector(self.sel_loc_listbox, timeout=8000)

            options = page.locator(self.sel_loc_option)
            count = await options.count()
            cities: List[str] = []
            for i in range(count):
                btn = options.nth(i)
                # city label is in data-testid or visible text
                data_id = await btn.get_attribute("data-testid")
                label = (await btn.inner_text() or "").strip()
                name = (data_id or label).strip()
                if name and name.lower() not in ("all locations",):
                    cities.append(name)

            # Filter to US/CA sets we care about
            us_allow = {
                "Austin","Bellevue","Chicago","Los Angeles","New York",
                "Palo Alto","San Diego","San Francisco","Santa Monica",
                "Seattle","Washington"
            }
            ca_allow = {"Toronto"}

            selected = [c for c in cities if c in us_allow or c in ca_allow]
            # Close popover if it’s still open
            try:
                await page.keyboard.press("Escape")
            except:
                pass

            return sorted(set(selected))
        except Exception:
            # Fallback to a reasonable default if dropdown fails
            return sorted({
                "Austin","Bellevue","Chicago","Los Angeles","New York",
                "Palo Alto","San Diego","San Francisco","Santa Monica",
                "Seattle","Washington","Toronto"
            })

    def _location_url(self, city: str) -> str:
        if not city:
            return self.base_url
        qs = urlencode({"location": city})
        sep = "&" if "?" in self.base_url else "?"
        return f"{self.base_url}{sep}{qs}"

    async def _snapshot_job_hrefs(self, page: Page) -> List[Dict[str, str]]:
        # encourage lazy content to render
        for _ in range(self.scroll_passes):
            try:
                await page.mouse.wheel(0, 1200)
                await asyncio.sleep(0.08)
            except:
                break

        # WAIT FOR LINKS, not only wrapper
        try:
            await page.wait_for_selector(self.sel_row_link, timeout=self.timeout, state="attached")
        except:
            await page.wait_for_selector("a[href*='/job?id=R']", timeout=self.timeout, state="attached")

        links = page.locator(self.sel_row_link)
        n = await links.count()

        snapshot: List[Dict[str, str]] = []
        seen = set()
        for i in range(n):
            li = links.nth(i)
            href = await li.get_attribute("href")
            if not href:
                continue
            abs_url = self._abs(page, href)
            if abs_url in seen:
                continue

            # pull Team + Type from sibling <td>s in the same row
            # (Role | Team | Type | Location). We read td[1], td[2].
            try:
                team = await li.evaluate("el => (el.closest('tr')?.querySelectorAll('td')?.[1]?.innerText || '').trim()")
                typ  = await li.evaluate("el => (el.closest('tr')?.querySelectorAll('td')?.[2]?.innerText || '').trim()")
            except Exception:
                team, typ = "", ""

            snapshot.append({"href": abs_url, "team": team, "type": typ})
            seen.add(abs_url)

        return snapshot

    async def _parse_detail_in_new_tab(self, listing_page: Page, url: str, row_meta: Dict[str, str] | None = None) -> Optional[Dict[str, Any]]:
        ctx = listing_page.context
        p = await ctx.new_page()
        try:
            await p.goto(url)
            await p.wait_for_load_state("networkidle")

            title = await self._get_text(p, self.sel_detail_title) or self._title_from_url(url)
            locs = await self._extract_locations(p)
            location_field = "; ".join(locs)

            # --- employment type from the meta chips (e.g., "Full time")
            employment_type = await self._extract_employment_type(p)

            posted_date = await self._extract_posted(p)
            description = await self._extract_description(p)   # uses the improved extractor below

            job_id = self._job_id_from_url(url)
            if self.us_ca_only and not self._looks_us_ca(locs):
                return None

            can_url = self._canonical_url(url)

            # carry over Team/Type from listing row (if we got them)
            team = (row_meta or {}).get("Team") or ""
            typ  = (row_meta or {}).get("Type") or ""

            return {
                "jobId": job_id,
                "title": title,
                "company": "Snap",
                "location": location_field,
                "Employment type": employment_type or "",   # 👈 matches your desired key
                "Team": team,
                "Type": typ,
                "url": can_url,
                "description": description.strip(),
                "posted_date": (None if posted_date == "older_than_30_days" else posted_date),
                "source": "snap",
                "status": "active",
                "scraped_date": now_iso()
            }
        except Exception as e:
            self.logger.warning(f"Detail parse failed {url}: {e}")
            return None
        finally:
            await p.close()

    # ----- tiny utils -----
    async def _get_text(self, page: Page, selector: str) -> Optional[str]:
        try:
            el = await page.query_selector(selector)
            if not el: return None
            t = (await el.inner_text() or "").strip()
            return re.sub(r"\s+", " ", t)
        except:
            return None

    def _title_from_url(self, url: str) -> str:
        slug = urlparse(url).path.rstrip("/").split("/")[-1]
        slug = re.sub(r"^job$", "", slug)  # when URL ends with /job?id=Rxxxx
        return " ".join(re.sub(r"[-_]+", " ", slug).split()).title() or "Untitled"

    async def _extract_locations(self, page: Page) -> List[str]:
        """
        Pull only the city lines rendered near the top of the detail page.
        Stop scanning when we hit non-location content like 'Full time', 'Posted', 'R00...', 'Compensation', 'Zone', 'Life at Snap', etc.
        """
        # lines we should stop at / ignore
        NON_LOC_PATTERNS = re.compile(
            r"\b(Full\s*time|Part\s*time|Posted\b|R\d{6,}\b|Compensation|Zone\s+[ABC]|Life\s+at\s+Snap)\b",
            re.IGNORECASE,
        )

        # collect candidate <p> lines in the top meta block (your selector already points there)
        raw_lines: List[str] = []
        nodes = await page.query_selector_all(self.sel_detail_loc_block)
        for n in nodes:
            t = (await n.inner_text() or "").strip()
            t = re.sub(r"\s+", " ", t)
            if not t:
                continue
            # stop when we arrive at non-location content
            if NON_LOC_PATTERNS.search(t):
                break
            raw_lines.append(t)

        # split by common separators inside a line, but keep city tokens intact
        cand_locs: List[str] = []
        for line in raw_lines:
            # a line can be "Austin" OR "Austin; Chicago; Los Angeles"
            parts = re.split(r"[;,/•]\s*", line)
            for p in parts:
                p = p.strip()
                if p:
                    cand_locs.append(p)

        # filter to allowed city names only
        clean = [c for c in cand_locs if c in self._CITY_ALLOW]

        # de-dupe, preserve order
        seen, out = set(), []
        for c in clean:
            if c not in seen:
                seen.add(c)
                out.append(c)

        return out


    async def _extract_posted(self, page: Page) -> Optional[str]:
        """
        Returns ISO 'YYYY-MM-DD' for absolute/relative dates,
        or 'older_than_30_days' when applicable.
        """
        txt = await self._get_text(page, self.sel_detail_posted) or ""
        if not txt:
            return None

        # direct ISO
        m = re.search(r"\b(20\d{2})-(\d{2})-(\d{2})\b", txt)
        if m:
            return m.group(0)

        today = datetime.now().date()

        # 30+ days bucket
        if re.search(r"\b30\+\s*Days?\s*Ago\b", txt, re.I):
            return "older_than_30_days"

        # N days ago
        m = re.search(r"\b(\d+)\s*Days?\s*Ago\b", txt, re.I)
        if m:
            return (today - timedelta(days=int(m.group(1)))).strftime("%Y-%m-%d")

        # Yesterday / Today
        if re.search(r"\bYesterday\b", txt, re.I):
            return (today - timedelta(days=1)).strftime("%Y-%m-%d")
        if re.search(r"\bToday\b", txt, re.I):
            return today.strftime("%Y-%m-%d")

        # Month Day, Year
        m = re.search(r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\s+\d{1,2},\s+20\d{2}\b", txt, re.I)
        if m:
            raw = m.group(0).replace("Sept", "Sep")
            for fmt in ("%b %d, %Y", "%B %d, %Y"):
                try:
                    return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
                except:
                    pass

        return None


    def _job_id_from_url(self, url: str) -> str:
        # /job?id=R0041925 → SNAP_R0041925
        m = re.search(r"[?&]id=(R\d{6,})\b", url)
        token = m.group(1) if m else "UNKNOWN"
        return f"SNAP_{token}"

    def _canonical_url(self, url: str) -> str:
        # normalize to the /job?id=Rxxxx form without extra params/fragments
        p = urlparse(url)
        base = f"{p.scheme}://{p.netloc}/job"
        # keep only the id param
        m = re.search(r"[?&]id=(R\d{6,})\b", url)
        return f"{base}?id={m.group(1)}" if m else url

    def _abs(self, page: Page, href: str) -> str:
        if href.startswith("http"):
            return href
        p = urlparse(page.url)
        return f"{p.scheme}://{p.netloc}{href}"

    def _looks_us_ca(self, locs: List[str]) -> bool:
        """Return True if any location is an allowed US/CA city (or generic country/remote tokens)."""
        GENERIC_ALLOW = {"United States","USA","US","Canada","CA",
                        "Remote – US","Remote – Canada","Remote - US","Remote - Canada"}
        for l in locs:
            base = re.sub(r"\s+", " ", l).strip()
            if base in GENERIC_ALLOW or base in self._CITY_ALLOW:
                return True
        return False

    async def _dismiss_cookie_banner(self, page: Page):
        """Best-effort: close common cookie consent banners if present."""
        try:
            for sel in [
                "button:has-text('Accept All')",
                "button:has-text('Accept')",
                "button:has-text('I Agree')",
            ]:
                loc = page.locator(sel)
                if await loc.count():
                    await loc.first.click()
                    await page.wait_for_timeout(200)
                    break
        except:
            pass

    async def scrape_jobs(self) -> List[Dict[str, Any]]:
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=self.headless,
                    args=["--no-sandbox", "--disable-dev-shm-usage"]
                )
                context = await browser.new_context(user_agent=self.user_agent)
                page = await context.new_page()
                page.set_default_timeout(self.timeout)

                # Build location list (this function may do its own .goto on base_url)
                locs = await self._resolve_us_ca_locations(page)
                if not locs:
                    self.logger.warning("No US/CA locations detected—will scrape base page only.")
                    locs = [""]  # scrape without location filter

                total = 0
                for city in locs:
                    if total >= self.max_jobs:
                        break

                    url = self._location_url(city)
                    await page.goto(url)

                    # ⬇️ handle cookie banners first
                    await self._dismiss_cookie_banner(page)

                    # ⬇️ wait for the page to be ready using job links (more reliable than wrapper visibility)
                    await page.wait_for_load_state("domcontentloaded")
                    try:
                        await page.wait_for_selector(self.sel_row_link, timeout=self.timeout, state="attached")
                    except:
                        # fallback: either the results wrapper OR any job link
                        await page.wait_for_selector(
                            f"{self.sel_results}, {self.sel_row_link}",
                            timeout=self.timeout,
                            state="attached"
                        )
                    await page.wait_for_timeout(self.sleep_after_nav_ms)

                    # Collect and process links on this location page
                    items = await self._snapshot_job_hrefs(page)
                    for it in items:
                        if total >= self.max_jobs: break
                        if it["href"] in self.seen_urls: continue

                        job = await self._parse_detail_in_new_tab(page, it["href"], row_meta={"Team": it["team"], "Type": it["type"]})
                        await page.wait_for_timeout(self.sleep_after_open_ms)
                        if job:
                            self.scraped.append(job)
                            self.seen_urls.add(job["url"])
                            total += 1
                            self.logger.info(f"✔ [{total}] {job['title']} ({job.get('location','')})")


                await context.close()
                await browser.close()

            self.logger.info(f"Snap scraping finished. Total jobs: {len(self.scraped)}")
            return self.scraped

        except Exception as e:
            self.logger.error(f"Snap scraping error: {e}")
            return self.scraped

    async def _extract_description(self, page: Page) -> str:
        """
        Build a readable description from the body content.
        - Skips top meta chips (cities, Full time, Posted, JR id).
        - Skips boilerplate 'Life at Snap'.
        - Prefers longer, sentence-like paragraphs.
        """
        selectors = [
            "article[data-testid='content-body']",
            "article .sdsm-content-body",
            "main article"
        ]
        STOP_PAT = re.compile(r"^\s*Life\s+at\s+Snap\b", re.I)
        META_PAT = re.compile(r"\b(Full\s*time|Part\s*time|Posted\b|R\d{6,})\b", re.I)

        # a city token list to filter out lone city lines if they leak into content
        CITY_TOK = self._CITY_ALLOW

        for sel in selectors:
            root = await page.query_selector(sel)
            if not root:
                continue

            nodes = await root.query_selector_all("p, li")
            lines: list[str] = []
            for n in nodes:
                t = (await n.inner_text() or "").strip()
                t = re.sub(r"\s+", " ", t)
                if not t:
                    continue
                if STOP_PAT.search(t):
                    break
                # discard obvious meta chips and bare city names
                if META_PAT.search(t):
                    continue
                if t in CITY_TOK:
                    continue
                # keep paragraphs that look like real sentences or are sufficiently long
                if len(t) >= 80 or re.search(r"[.!?]\s", t):
                    lines.append(t)

            text = "\n".join(lines).strip()
            if text:
                return text

        return ""

    async def _extract_employment_type(self, page: Page) -> str | None:
        """
        Reads the small meta row under the title where 'Full time' appears.
        We ignore 'Posted ...' and job code 'R00....'
        """
        try:
            # scan the same meta block we used for locations, but keep only "Full time"/"Part time"/"Contract"
            nodes = await page.query_selector_all("article section p, article section span")
            for n in nodes:
                t = (await n.inner_text() or "").strip()
                t = re.sub(r"\s+", " ", t)
                if not t:
                    continue
                if re.search(r"\b(Full\s*time|Part\s*time|Contract)\b", t, re.I):
                    return re.sub(r".*\b(Full\s*time|Part\s*time|Contract)\b.*", r"\1", t, flags=re.I).title()
        except:
            pass
        return None

