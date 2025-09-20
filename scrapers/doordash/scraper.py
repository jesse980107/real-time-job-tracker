import asyncio
import hashlib
import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Set
from urllib.parse import urlencode, urlparse, urlunparse, parse_qsl

from playwright.async_api import async_playwright, Page, TimeoutError as PlaywrightTimeoutError


def now_iso() -> str:
    return datetime.now().isoformat()


class DoordashScraper:
    """
    Scrapes DoorDash Careers for two countries (United States, Canada).
    - Applies filter via URL (keyword blank, intern=0, location=<country>).
    - Paginates by spage=1..N.
    - Opens EACH job in a new tab and extracts details.
    Output schema matches your other scrapers.
    """

    def __init__(self, website_config: Dict[str, Any], global_config: Dict[str, Any]):
        self.cfg = website_config
        self.g = global_config
        self.logger = logging.getLogger("job_tracker")

        wp = self.cfg.get("playwright_options", {})
        self.sel_results = wp.get("results_container", "div#postings, .holder--showing")
        self.sel_card = wp.get("card_selector", "div.job-item")
        self.sel_card_link = wp.get("card_link_selector", ".title-container a[href*='/jobs/']")
        self.sel_next = wp.get("next_button", "a[aria-label*='Next']")

        self.sel_detail_title = wp.get("detail_title", "h1")
        self.sel_detail_jobid = wp.get("detail_jobid", "p:has-text('Job ID:')")
        self.sel_detail_company = wp.get("detail_company", "p:has-text('DoorDash')")
        self.sel_detail_loc_block = wp.get("detail_location_block", ".location-container .value-secondary")
        self.sel_detail_desc = wp.get("detail_description", "section#content, .content, article, main")

        sc = self.cfg.get("scraping_config", {})
        self.countries: List[str] = sc.get("countries", ["United states", "canada"])
        self.max_pages = sc.get("max_pages", 10)
        self.max_jobs_per_country = sc.get("max_jobs_per_country", 400)
        self.sleep_after_nav_ms = sc.get("sleep_after_nav_ms", 1200)
        self.sleep_after_open_ms = sc.get("sleep_after_open_ms", 700)

        pg = self.g["playwright_global"]
        self.timeout = pg["timeout"]
        self.user_agent = pg["user_agent"]
        self.headless = self.g["global_settings"]["headless"]

        self.base_url = self.cfg["website_info"]["base_url"]
        self.scraped: List[Dict[str, Any]] = []
        self.seen_urls: Set[str] = set()

    # --------------- entry ---------------
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

                for country in self.countries:
                    await self._scrape_country(page, country)

                await context.close()
                await browser.close()

            self.logger.info(f"DoorDash scraping finished. Total jobs collected: {len(self.scraped)}")
            return self.scraped

        except Exception as e:
            self.logger.error(f"DoorDash scraping error: {e}")
            return self.scraped

    # --------------- per-country flow ---------------
    async def _scrape_country(self, page: Page, country: str) -> None:
        total_for_country = 0
        page_idx = 1

        while page_idx <= self.max_pages and total_for_country < self.max_jobs_per_country:
            url = self._build_search_url(country=country, spage=page_idx)
            self.logger.info(f"[{country}] Opening results page {page_idx}: {url}")
            await page.goto(url)
            await page.wait_for_load_state("domcontentloaded")
            # Wait for either results container or at least one card link to appear
            try:
                await page.wait_for_selector(self.sel_results, timeout=self.timeout)
            except:
                pass
            await page.wait_for_timeout(self.sleep_after_nav_ms)
            await self._progressive_scroll(page)

            # Prefer card wrapper; fall back to scanning links directly
            cards = page.locator(self.sel_card)
            card_count = await cards.count()
            if card_count == 0:
                # Some pages render items without the wrapper class; fallback to link scan
                links = page.locator(self.sel_card_link)
                if await links.count() == 0:
                    self.logger.info(f"[{country}] No jobs found on page {page_idx}. Stopping.")
                    break

            # Snapshot: (href, dept, func) for each card
            snapshot: List[tuple[str, Optional[str], Optional[str]]] = []
            if card_count > 0:
                for i in range(card_count):
                    card = cards.nth(i)
                    a = card.locator(self.sel_card_link).first
                    if not await a.count():
                        continue
                    href = await a.get_attribute("href")
                    if not href:
                        continue
                    abs_url = self._absolute(page, href)

                    # Pull Department & Function from the card (right-hand columns)
                    # DOM (from your screenshots):
                    #   div.department-container .value-secondary
                    #   div.function-container  .value-secondary
                    dept = None
                    func = None
                    try:
                        dep_el = card.locator(".department-container .value-secondary")
                        if await dep_el.count():
                            dept = (await dep_el.first.inner_text() or "").strip()
                    except:
                        pass
                    try:
                        fun_el = card.locator(".function-container .value-secondary")
                        if await fun_el.count():
                            func = (await fun_el.first.inner_text() or "").strip()
                    except:
                        pass

                    snapshot.append((abs_url, dept, func))
            else:
                # Link-only fallback
                links = page.locator(self.sel_card_link)
                link_count = await links.count()
                for i in range(link_count):
                    a = links.nth(i)
                    href = await a.get_attribute("href")
                    if href:
                        snapshot.append((self._absolute(page, href), None, None))

            self.logger.info(f"[{country}] Prepared {len(snapshot)} job links on page {page_idx}")

            # Process each detail page
            for abs_url, dept, func in snapshot:
                if total_for_country >= self.max_jobs_per_country:
                    break
                if abs_url in self.seen_urls:
                    continue

                job = await self._parse_detail_in_new_tab(page, abs_url, country=country, dept_hint=dept, func_hint=func)
                await page.wait_for_timeout(self.sleep_after_open_ms)
                if job:
                    self.scraped.append(job)
                    self.seen_urls.add(job["url"])
                    total_for_country += 1
                    self.logger.info(f"✔ [{country}] {total_for_country} :: {job['title']} ({job.get('location','')})")

            # Next page
            page_idx += 1


    # --------------- detail parsing ---------------
    async def _parse_detail_in_new_tab(
    self,
    listing_page: Page,
    url: str,
    country: str,
    dept_hint: Optional[str] = None,
    func_hint: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        ctx = listing_page.context
        p = await ctx.new_page()
        try:
            await p.goto(url)
            await p.wait_for_load_state("networkidle")

            title = await self._get_text(p, self.sel_detail_title) or self._title_from_url(url) or "Untitled"

            # Company (best-effort)
            company = await self._get_first_that_exists_text(p, [self.sel_detail_company]) or "DoorDash, Inc."

            # Job ID (from explicit text or URL)
            job_id = await self._extract_job_id(p) or self._job_id_from_url(url)

            # -------- Robust LOCATION extraction --------
            loc_text = await self._get_text(p, self.sel_detail_loc_block) or ""
            locations = self._normalize_locations(loc_text)

            if (not locations) or (len(locations) == 1 and locations[0].lower().startswith("doordash")):
                try:
                    header_ps = await p.query_selector_all("div.bg-primary.posting-header p.text-white")
                    header_vals: List[str] = []
                    for n in header_ps:
                        t = (await n.inner_text() or "").strip()
                        t = re.sub(r"\s+", " ", t)
                        if not t:
                            continue
                        if re.search(r"\bJob ID\b", t, re.I):
                            continue
                        if re.search(r"\bDoorDash\b", t, re.I):
                            continue
                        header_vals.append(t)
                    if header_vals:
                        locations = self._normalize_locations("; ".join(header_vals))
                except:
                    pass

            location = ", ".join(locations)

            # Description
            description = await self._get_text(p, self.sel_detail_desc) or ""

            # Department / Function
            department = dept_hint
            function = func_hint
            if not department:
                try:
                    dep = await p.query_selector(".department-container .value-secondary")
                    if dep:
                        department = (await dep.inner_text() or "").strip()
                except:
                    pass
            if not function:
                try:
                    fun = await p.query_selector(".function-container .value-secondary")
                    if fun:
                        function = (await fun.inner_text() or "").strip()
                except:
                    pass

            return {
                "jobId": job_id,
                "title": title,
                "company": company,
                "location": location,
                "department": department or "",
                "function": function or "",
                "url": url,
                "description": description.strip(),
                "source": "doordash",
                "status": "active",
                "scraped_date": now_iso(),
            }
        except Exception as e:
            self.logger.warning(f"Detail parse failed {url}: {e}")
            return None
        finally:
            await p.close()


    # --------------- helpers ---------------
    async def _progressive_scroll(self, page: Page) -> None:
        try:
            for _ in range(8):
                await page.mouse.wheel(0, 1400)
                await asyncio.sleep(0.12)
            await page.mouse.wheel(0, -700)
            await asyncio.sleep(0.08)
        except:
            pass

    def _absolute(self, page: Page, href: str) -> str:
        if href.startswith("http"):
            return href
        u = urlparse(page.url)
        return f"{u.scheme}://{u.netloc}{href}"

    def _build_search_url(self, country: str, spage: int) -> str:
        """
        Builds URL like:
        https://careersatdoordash.com/job-search/?intern=0&keyword=&location=canada&spage=1
        """
        parsed = urlparse(self.base_url)
        q = dict(parse_qsl(parsed.query))
        q["intern"] = "0"
        q["keyword"] = q.get("keyword", "")
        q["location"] = country
        q["spage"] = str(spage)
        new_qs = urlencode(q)
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", new_qs, ""))

    async def _get_text(self, page: Page, selector: str) -> Optional[str]:
        try:
            el = await page.query_selector(selector)
            if not el:
                return None
            txt = (await el.inner_text() or "").strip()
            return re.sub(r"\s+", " ", txt)
        except:
            return None

    async def _get_first_that_exists_text(self, page: Page, selectors: List[str]) -> Optional[str]:
        for sel in selectors:
            t = await self._get_text(page, sel)
            if t:
                return t
        return None

    def _normalize_locations(self, text: str) -> List[str]:
        """
        DoorDash location blocks look like:
        'Phoenix, AZ; Seattle, WA; Los Angeles, CA; ...; United States - Remote'
        We split on ';' and '•', trim, and keep order.
        """
        out: List[str] = []
        for part in re.split(r"[;•]+", text or ""):
            val = re.sub(r"\s+", " ", (part or "").strip())
            if val:
                out.append(val)
        # de-dupe preserving order
        seen = set()
        uniq = []
        for x in out:
            if x not in seen:
                seen.add(x)
                uniq.append(x)
        return uniq

    def _title_from_url(self, url: str) -> Optional[str]:
        slug = urlparse(url).path.rstrip("/").split("/")[-2:]  # [..., <title-slug>, <numeric-id>]
        if not slug:
            return None
        title_slug = slug[0]
        t = title_slug.replace("---", " — ").replace("--", " – ")
        t = re.sub(r"[-_]+", " ", t)
        return re.sub(r"\s+", " ", t).strip().title()

    async def _extract_job_id(self, page: Page) -> Optional[str]:
        try:
            el = await page.query_selector(self.sel_detail_jobid)
            if not el:
                return None
            t = (await el.inner_text() or "").strip()
            m = re.search(r"Job ID:\s*(\d+)", t, re.I)
            if m:
                return f"DOORDASH_{m.group(1)}"
        except:
            pass
        return None

    def _job_id_from_url(self, url: str) -> str:
        # /jobs/sr-associate-drive---growth-strategy-and-operations/7239009/
        m = re.search(r"/jobs/[^/]+/(\d+)/?", url)
        if m:
            return f"DOORDASH_{m.group(1)}"
        return "DOORDASH_UNKNOWN"
