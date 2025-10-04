# scrapers/ibm/scraper.py

import asyncio
import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Set
from urllib.parse import urlparse, urlunparse, parse_qs, urlencode, urljoin

from playwright.async_api import async_playwright, Page


def now_iso() -> str:
    return datetime.now().isoformat()


class IbmScraper:
    """
    IBM careers scraper (US & Canada).
    - Opens filtered list page
    - Collects JobDetail links (multi-selector + DOM sweep)
    - Visits each job and extracts:
        title, location, department, job type, panel fields, description
    - Paginates via "Next" or ?p=N
    NOTE: No posted_date in output by design.
    """

    def __init__(self, website_config: Dict[str, Any], global_config: Dict[str, Any]):
        self.cfg = website_config
        self.g = global_config
        self.logger = logging.getLogger("job_tracker")

        # Pagination
        self.sel_next = "a[aria-label='Next'], a[aria-label*='Next']"

        # Detail chips / panel
        self.sel_detail_loc_chip = ".card-item.card-item-location, [class*='card-item-location']"
        self.sel_detail_dept_chip = ".card-item.card-item-department, [class*='card-item-department']"
        self.sel_detail_type_chip = ".card-item.card-item-type, [class*='card-item-type']"
        self.sel_detail_panel = "article.article, .article, .article--details"
        self.sel_detail_desc = "article.article, main article, article, main"

        # Cookie/consent
        self.cookie_selectors = [
            "#onetrust-accept-btn-handler",
            "button:has-text('Accept all')",
            "button:has-text('Accept All')",
            "button:has-text('Accept')",
            "button:has-text('Agree')",
            "button:has-text('I agree')",
            "button:has-text('Got it')",
        ]

        # Scraping config
        sc = self.cfg.get("scraping_config", {})
        self.max_pages = sc.get("max_pages", 3)
        self.max_jobs = sc.get("max_jobs", 150)
        self.us_ca_only = sc.get("us_ca_only", True)
        self.sleep_after_nav_ms = sc.get("sleep_after_nav_ms", 1200)
        self.sleep_after_open_ms = sc.get("sleep_after_open_ms", 700)
        self.detail_timeout_ms = sc.get("detail_timeout_ms")  # fallback to global if None

        # Global Playwright
        pg = self.g["playwright_global"]
        self.timeout = pg["timeout"]
        self.user_agent = pg["user_agent"]
        self.headless = self.g["global_settings"]["headless"]

        self.base_url = self.cfg["website_info"]["base_url"]

        # State
        self.scraped: List[Dict[str, Any]] = []
        self.seen_urls: Set[str] = set()

    async def scrape_jobs(self) -> List[Dict[str, Any]]:
        start_time = datetime.now()
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=self.headless,
                    args=["--no-sandbox", "--disable-dev-shm-usage"],
                )
                context = await browser.new_context(user_agent=self.user_agent)
                page = await context.new_page()
                page.set_default_timeout(self.timeout)

                await self._open_results(page)
                await self._scrape_all_pages(page)

                await context.close()
                await browser.close()

            end_time = datetime.now()
            duration = (end_time - start_time).total_seconds()
            self.scraping_duration = duration
            self.logger.info(f"Scraping duration: {duration:.2f} seconds")
            self.logger.info(f"IBM scraping finished. Total jobs: {len(self.scraped)}")
            return self.scraped
        except Exception as e:
            self.logger.error(f"IBM scraping error: {e}")
            return self.scraped

    # ---------- listing ----------
    async def _open_results(self, page: Page) -> None:
        self.logger.info(f"Opening: {self.base_url}")
        await page.goto(self.base_url, wait_until="domcontentloaded")
        await self._consent_sweeper(page)
        try:
            await page.wait_for_selector(
                "text=Items per page:, .bx--card-group__cards__row--condensed, a[href*='JobDetail']",
                timeout=max(15000, self.timeout // 2),
            )
        except Exception:
            pass
        await page.wait_for_timeout(self.sleep_after_nav_ms)

    async def _scrape_all_pages(self, page: Page) -> None:
        total = 0
        page_idx = 1
        while page_idx <= self.max_pages:
            self.logger.info(f"Scraping results page {page_idx}")

            links = await self._collect_links_on_page(page)
            self.logger.info(f"Found {len(links)} job links on page {page_idx}")

            if not links:
                await self._progressive_scroll(page)
                await self._consent_sweeper(page)
                links = await self._collect_links_on_page(page)
                self.logger.info(f"After retry, found {len(links)} links")
                if not links:
                    break

            first_before = links[0]

            for href in links:
                if total >= self.max_jobs:
                    break
                if href in self.seen_urls:
                    continue
                job = await self._parse_detail_in_new_tab(page, href)
                await page.wait_for_timeout(self.sleep_after_open_ms)
                if job:
                    if not self.us_ca_only or self._is_us_ca(job.get("location", "")):
                        self.scraped.append(job)
                        self.seen_urls.add(job["url"])
                        total += 1
                        self.logger.info(f"✔ [{total}] {job['title']} ({job.get('location','')})")

            if total >= self.max_jobs:
                self.logger.info("Reached max_jobs; stopping.")
                break

            moved = await self._goto_next(page, first_before)
            if not moved:
                next_url = self._page_url(page.url, page_idx + 1)
                if next_url and (page_idx + 1) <= self.max_pages:
                    self.logger.info(f"No clickable 'Next' -> navigating to {next_url}")
                    await page.goto(next_url, wait_until="domcontentloaded")
                    await page.wait_for_timeout(self.sleep_after_nav_ms)
                    page_idx += 1
                else:
                    break
            else:
                page_idx += 1

    async def _collect_links_on_page(self, page: Page) -> List[str]:
        await self._progressive_scroll(page)
        selectors = [
            "a[href*='ibmglobal.avature.net'][href*='JobDetail']",
            "a.bx-card__cta[href*='JobDetail']",
            "a[data-autoid='bx-card__cta'][href*='JobDetail']",
            "a[href*='/careers/JobDetail']",
        ]
        hrefs: Set[str] = set()

        # standard locators
        for sel in selectors:
            try:
                loc = page.locator(sel)
                cnt = await loc.count()
                for i in range(min(cnt, 400)):
                    href = await loc.nth(i).get_attribute("href")
                    if href:
                        hrefs.add(self._abs(page, href))
            except Exception:
                continue

        # DOM sweep fallback
        if not hrefs:
            try:
                raw = await page.evaluate(
                    "() => Array.from(document.querySelectorAll('a'))"
                    ".map(a => a.getAttribute('href') || '')"
                    ".filter(h => h && h.includes('JobDetail'))"
                )
                for h in raw:
                    hrefs.add(self._abs(page, h))
            except Exception:
                pass

        return list(hrefs)

    async def _goto_next(self, page: Page, first_before: str) -> bool:
        try:
            btn = page.locator(self.sel_next)
            if await btn.count():
                await btn.first.scroll_into_view_if_needed()
                await btn.first.click()
                await page.wait_for_load_state("domcontentloaded")
                await page.wait_for_timeout(self.sleep_after_nav_ms)
                for _ in range(25):
                    try:
                        first_after = await page.locator("a[href*='JobDetail']").first.get_attribute("href")
                    except Exception:
                        first_after = None
                    if first_after and first_after != first_before:
                        return True
                    await asyncio.sleep(0.2)
        except Exception:
            return False
        return False

    async def _progressive_scroll(self, page: Page) -> None:
        try:
            for _ in range(10):
                await page.mouse.wheel(0, 1400)
                await asyncio.sleep(0.12)
            await page.mouse.wheel(0, -800)
            await asyncio.sleep(0.1)
        except Exception:
            pass

    # ---------- detail ----------
    async def _parse_detail_in_new_tab(self, listing_page: Page, url: str) -> Optional[Dict[str, Any]]:
        ctx = listing_page.context
        p = await ctx.new_page()
        try:
            dt = self.detail_timeout_ms or self.timeout
            p.set_default_timeout(dt)

            await p.goto(url, wait_until="domcontentloaded", timeout=dt)
            await self._consent_sweeper(p)
            await self._ensure_panel_visible_and_loaded(p)

            # basic 'not found' guard
            try:
                doc_title = (await p.title()) or ""
            except Exception:
                doc_title = ""
            try:
                body_text = (await p.inner_text("body") or "")
            except Exception:
                body_text = ""
            if (re.search(r"page\s+not\s+found|no longer available", doc_title, re.I) or
                re.search(r"page\s+not\s+found|no longer available", body_text, re.I)):
                self.logger.info(f"Skip not-found: {url}")
                return None

            title = await self._extract_title_ibm(p, url)
            dept_chip = await self._get_text(p, self.sel_detail_dept_chip) or ""
            type_chip = await self._get_text(p, self.sel_detail_type_chip) or ""
            location_chip = await self._get_text(p, self.sel_detail_loc_chip) or ""

            panel_fields = await self._extract_panel_fields_mapped(p)
            location = location_chip or self._build_location_from_fields(panel_fields) or ""

            description = await self._extract_description(p)
            job_id = self._job_id_from_url(url)

            job: Dict[str, Any] = {
                "jobId": f"IBM_{job_id}" if job_id else "IBM_UNKNOWN",
                "title": title,
                "company": "IBM",
                "location": location,
                "url": self._canonical(url),
                "description": (description or "").strip(),
                "source": "IBM",
                "status": "active",
                "scraped_date": now_iso(),
            }

            if dept_chip:
                job["Department"] = dept_chip
            if type_chip:
                job["Job type"] = type_chip

            # panel fields you want to keep
            for k in [
                "Work arrangement",
                "Area of work",
                "Employment type",
                "Contract type",
                "Projected Minimum Salary per year",
                "Projected Maximum Salary per year",
                "Position type",
                "Travel required",
                "Company",
                "Shift",
            ]:
                v = panel_fields.get(k)
                if v:
                    job[k] = v

            if self.us_ca_only and not self._is_us_ca(job.get("location", "")):
                self.logger.debug(f"Skip non-US/CA or empty location: '{job.get('location','')}' -> {url}")
                return None

            return job

        except Exception as e:
            self.logger.warning(f"Detail parse failed {url}: {e}")
            return None
        finally:
            await p.close()

    # ---------- consent / readiness ----------
    async def _accept_cookies_if_any(self, page: Page) -> None:
        for sel in self.cookie_selectors:
            try:
                loc = page.locator(sel)
                if await loc.count():
                    await loc.first.click()
                    await page.wait_for_timeout(200)
                    return
            except Exception:
                pass

    async def _consent_sweeper(self, page: Page) -> None:
        await self._accept_cookies_if_any(page)
        try:
            for frame in page.frames:
                try:
                    for sel in self.cookie_selectors:
                        loc = frame.locator(sel)
                        if await loc.count():
                            await loc.first.click()
                            await page.wait_for_timeout(200)
                            return
                except Exception:
                    continue
        except Exception:
            pass

    async def _ensure_panel_visible_and_loaded(self, page: Page) -> None:
        try:
            details = page.locator("details.article, details.article--details, details.article--collapsible")
            for i in range(await details.count()):
                d = details.nth(i)
                try:
                    if not await d.get_attribute("open"):
                        await d.click()
                        await page.wait_for_timeout(150)
                except Exception:
                    pass
            for _ in range(8):
                await page.mouse.wheel(0, 1200)
                await asyncio.sleep(0.12)
            await page.mouse.wheel(0, -600)
            await asyncio.sleep(0.1)
        except Exception:
            pass

    # ---------- extraction helpers ----------
    async def _get_text(self, page: Page, selector: str) -> Optional[str]:
        try:
            el = await page.query_selector(selector)
            if not el:
                return None
            t = (await el.inner_text() or "").strip()
            return re.sub(r"\s+", " ", t)
        except Exception:
            return None

    async def _extract_title_ibm(self, page: Page, url: str) -> str:
        for sel in [
            ".banner_text_content h1",
            ".banner_text_content h2",
            "header .banner_text_content h1",
            "header .banner_text_content h2",
            "main h1",
            "main h2",
            "h1",
            "h2",
        ]:
            t = await self._get_text(page, sel)
            if t and not re.fullmatch(r"IBM", t, re.I):
                return t
        fields = await self._extract_panel_fields_mapped(page)
        if fields.get("Job Title"):
            return fields["Job Title"]
        derived = self._derive_title_from_url(url)
        return "Untitled" if derived.lower() == "jobdetail" else derived

    async def _extract_description(self, page: Page) -> str:
        try:
            for sel in ["button[aria-label*='Show more' i]", "button:has-text('Show more')", "button:has-text('More')"]:
                if await page.locator(sel).count():
                    try:
                        await page.click(sel)
                        await page.wait_for_timeout(200)
                    except Exception:
                        pass
            for sel in [self.sel_detail_desc, "article", "main"]:
                el = await page.query_selector(sel)
                if el:
                    txt = (await el.inner_text() or "").strip()
                    if txt and len(txt) > 30:
                        return re.sub(r"\s+", " ", txt)
        except Exception:
            pass
        return ""

    def _canonicalize_label(self, raw: str) -> str:
        if not raw:
            return ""
        lab = re.sub(r"\s+", " ", raw).strip(" :\t\r\n")
        key = lab.lower().replace("–", "-")
        mapping = {
            "job title": "Job Title",
            "work arrangement": "Work arrangement",
            "area of work": "Area of work",
            "employment type": "Employment type",
            "contract type": "Contract type",
            "projected minimum salary per year": "Projected Minimum Salary per year",
            "projected maximum salary per year": "Projected Maximum Salary per year",
            "position type": "Position type",
            "travel required": "Travel required",
            "company": "Company",
            "shift": "Shift",
            "city / township / village": "City / Township / Village",
            "city": "City",
            "state / province": "State / Province",
            "state": "State",
            "province": "Province",
            "country": "Country",
        }
        return mapping.get(key, lab)

    async def _extract_panel_fields_mapped(self, page: Page) -> Dict[str, str]:
        await self._ensure_panel_visible_and_loaded(page)
        out: Dict[str, str] = {}
        try:
            rows = page.locator(".article_content_view_field")
            n = await rows.count()
            if n == 0:
                for _ in range(4):
                    await page.mouse.wheel(0, 1000)
                    await asyncio.sleep(0.1)
                n = await rows.count()
            for i in range(n):
                row = rows.nth(i)
                lab_el = await row.query_selector(".article_content_view_field_label")
                val_el = await row.query_selector(".article_content_view_field_value")
                raw_lab = (await lab_el.inner_text() if lab_el else "") or ""
                raw_val = (await val_el.inner_text() if val_el else "") or ""
                lab = self._canonicalize_label(raw_lab)
                val = re.sub(r"\s+", " ", raw_val).strip()
                if lab and val:
                    out[lab] = val
        except Exception:
            pass
        return out

    def _build_location_from_fields(self, fields: Dict[str, str]) -> Optional[str]:
        city = fields.get("City / Township / Village") or fields.get("City")
        state = fields.get("State / Province") or fields.get("State") or fields.get("Province")
        country = fields.get("Country")
        parts = [p for p in [city, state, country] if p]
        return ", ".join(parts) if parts else None

    # ---------- URL helpers ----------
    def _page_url(self, current: str, n: int) -> Optional[str]:
        try:
            u = urlparse(current)
            q = parse_qs(u.query)
            q["p"] = [str(n)]
            new_q = urlencode({k: v[0] if isinstance(v, list) and len(v) == 1 else v for k, v in q.items()}, doseq=True)
            return urlunparse((u.scheme, u.netloc, u.path, u.params, new_q, u.fragment))
        except Exception:
            return None

    def _abs(self, page: Page, href: str) -> str:
        if href.startswith("http"):
            return href
        u = page.url
        base = f"{urlparse(u).scheme}://{urlparse(u).netloc}"
        return urljoin(base, href)

    def _canonical(self, url: str) -> str:
        """Drop tracking params; keep jobId."""
        try:
            u = urlparse(url)
            q = parse_qs(u.query)
            keep = {"jobId": q["jobId"]} if "jobId" in q else {}
            return urlunparse((u.scheme, u.netloc, u.path.rstrip("/"), u.params, urlencode(keep, doseq=True), ""))
        except Exception:
            return url

    def _job_id_from_url(self, url: str) -> Optional[str]:
        try:
            q = parse_qs(urlparse(url).query)
            if "jobId" in q and len(q["jobId"]) > 0:
                return q["jobId"][0]
        except Exception:
            pass
        return None

    def _derive_title_from_url(self, url: str) -> str:
        seg = urlparse(url).path.rstrip("/").split("/")[-1]
        if seg.lower() == "jobdetail":
            return "Untitled"
        t = re.sub(r"[-_]+", " ", seg)
        return re.sub(r"\s+", " ", t).strip().title()

    def _is_us_ca(self, loc: str) -> bool:
        return bool(re.search(r"\bUnited States\b|\bCanada\b", loc, re.I))
