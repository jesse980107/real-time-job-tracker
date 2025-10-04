# scrapers/netflix/scraper.py

import asyncio
import json
import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Set
from urllib.parse import urljoin, urlparse, urlunparse, quote, urlencode
from contextlib import asynccontextmanager

from playwright.async_api import async_playwright, Page, TimeoutError as PlaywrightTimeoutError


def now_iso() -> str:
    return datetime.now().isoformat()


class NetflixScraper:
    """
    Netflix Careers scraper for explore.jobs.netflix.net
    """

    def __init__(self, website_config: Dict[str, Any], global_config: Dict[str, Any]):
        self.cfg = website_config
        self.g = global_config
        self.logger = logging.getLogger("job_tracker")

        sc = self.cfg.get("scraping_config", {})
        self.max_jobs = sc.get("max_jobs", 400)
        self.scroll_passes = sc.get("scroll_passes", 40)

        # Filter settings
        filters = self.cfg.get("filters", {})
        self.us_ca_only = filters.get("us_ca_only", True)

        wp = self.cfg.get("playwright_options", {})
        self.timeout = wp.get("timeout", 30000)

        pg = self.g.get("playwright_global", {})
        self.user_agent = pg.get("user_agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
        self.headless = self.g.get("global_settings", {}).get("headless", True)

        self.base_url = self.cfg["website_info"]["base_url"]

        self.scraped: List[Dict[str, Any]] = []
        self.seen_urls: Set[str] = set()

    async def scrape_jobs(self) -> List[Dict[str, Any]]:
        """Main entry point for scraping Netflix jobs"""
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

                self.logger.info(f"Netflix max_jobs = {self.max_jobs}")
                
                await self._open_list_page(page)
                await self._harvest_and_parse(page)

                await context.close()
                await browser.close()

            end_time = datetime.now()
            duration = (end_time - start_time).total_seconds()
            self.scraping_duration = duration
            self.logger.info(f"Scraping duration: {duration:.2f} seconds")
            self.logger.info(f"Netflix scraping finished. Total jobs collected: {len(self.scraped)}")
            return self.scraped

        except Exception as e:
            self.logger.error(f"Netflix scraping error: {e}")
            return self.scraped

    async def _open_list_page(self, page: Page) -> None:
        """Open the Netflix careers page"""
        self.logger.info(f"Opening: {self.base_url}")
        await page.goto(self.base_url, wait_until="domcontentloaded", timeout=self.timeout)
        await self._close_popups(page)
        await page.wait_for_selector(
            "[role='list'] .card, [role='button'][data-test-id^='position-card-']",
            timeout=self.timeout,
        )

    async def _close_popups(self, page: Page) -> None:
        """Close any popups that might appear"""
        # Resume modal ("SKIP")
        try:
            await page.get_by_role("button", name=re.compile(r"^skip$", re.I)).click(timeout=4000)
        except Exception:
            pass

        # Cookie banner (OK / ACCEPT / etc.)
        for name in ("OK", "ACCEPT", "I AGREE", "Got it"):
            try:
                await page.get_by_role("button", name=re.compile(fr"^{name}$", re.I)).click(timeout=2000)
                break
            except Exception:
                continue

        # Fallback close icon
        for sel in ("button[aria-label='Close']", "button[aria-label='close']", "button:has(svg)"):
            try:
                await page.locator(sel).first.click(timeout=1000)
                break
            except Exception:
                pass

    async def _harvest_and_parse(self, page: Page) -> None:
        """Harvest job cards and parse details"""
        seen_cards = set()
        stale_passes = 0

        while stale_passes < 5 and len(self.scraped) < self.max_jobs:
            cards = page.locator("[role='list'] [role='button'][data-test-id^='position-card-']")
            total = await cards.count()
            self.logger.info(f"Netflix: {total} cards currently loaded")

            grew_this_pass = False
            for i in range(total):
                if len(self.scraped) >= self.max_jobs:
                    break
                if i in seen_cards:
                    continue
                seen_cards.add(i)

                card = cards.nth(i)
                try:
                    await card.scroll_into_view_if_needed()
                    await card.click()
                    await page.wait_for_selector(".position-title h1, h1[aria-level]", timeout=8000)
                    
                    job_data = await self._parse_job_detail(page)
                    if job_data:
                        # Apply US/CA filter if enabled
                        if self.us_ca_only and not self._is_us_ca(job_data.get("location", "")):
                            self.logger.debug(f"Filtered out non-US/CA job: {job_data['title']} - {job_data['location']}")
                            continue
                        
                        self.scraped.append(job_data)
                        self.logger.info(f"✔ [{len(self.scraped)}] {job_data['title']} — {job_data['location']}")
                        grew_this_pass = True
                except Exception as e:
                    self.logger.debug(f"Netflix: parse failed at card {i}: {e}")

            if len(self.scraped) >= self.max_jobs:
                break

            # Try to load more jobs
            loaded_more = await self._load_more_jobs(page)

            # If we didn't grow by parsing OR by loading more, count a stale pass
            if not grew_this_pass and not loaded_more:
                stale_passes += 1
            else:
                stale_passes = 0

    async def _load_more_jobs(self, page: Page) -> bool:
        """Try to load more jobs by scrolling and clicking 'Show More' button"""
        before_count = await self._count_cards(page)
        
        # 1. Scroll window to bottom
        await self._scroll_window_to_bottom(page)
        
        # 2. Scroll left rail
        await self._scroll_left_rail(page)
        
        # 3. Try to click "Show More" button
        if await self._try_click_show_more(page):
            return True
        
        # 4. Try lazy-load scrolling on left rail
        await self._scroll_left_rail(page, passes=8)
        try:
            await page.wait_for_load_state("networkidle", timeout=2000)
        except Exception:
            pass
        
        after_count = await self._count_cards(page)
        return after_count > before_count

    async def _count_cards(self, page: Page) -> int:
        """Count the number of job cards currently visible"""
        return await page.locator(
            "[role='list'] [role='button'][data-test-id^='position-card-']"
        ).count()

    async def _scroll_window_to_bottom(self, page: Page, max_secs: int = 6) -> None:
        """Scroll the outer document to the bottom"""
        try:
            start = await page.evaluate("document.scrollingElement.scrollHeight")
            last = start
            elapsed = 0
            while elapsed < max_secs * 1000:
                await page.evaluate("""
                    const se = document.scrollingElement || document.documentElement;
                    se.scrollTop = se.scrollHeight;
                """)
                await page.wait_for_timeout(250)
                cur = await page.evaluate("document.scrollingElement.scrollHeight")
                if cur == last:
                    break
                last = cur
                elapsed += 250
            try:
                await page.wait_for_load_state("networkidle", timeout=2000)
            except Exception:
                pass
        except Exception:
            pass

    async def _scroll_left_rail(self, page: Page, passes: int = 10) -> None:
        """Scroll the left rail job list"""
        scroller = page.locator("div.position-sidebar-scroll-handler").first
        if not await scroller.count():
            scroller = page.locator("[role='list']").first  # fallback
        for _ in range(max(1, passes)):
            try:
                await scroller.evaluate("(el) => el.scrollBy(0, Math.max(700, el.clientHeight*0.95))")
            except Exception:
                pass
            await page.wait_for_timeout(140)

    async def _try_click_show_more(self, page: Page) -> bool:
        """Try to click the 'Show More Positions' button"""
        before = await self._count_cards(page)
        btn = self._get_show_more_button(page)

        if not await btn.count():
            return False

        try:
            await btn.scroll_into_view_if_needed()
            await btn.click()
        except Exception:
            try:
                await btn.click(force=True)
            except Exception:
                return False

        try:
            await page.wait_for_load_state("networkidle", timeout=3000)
        except Exception:
            pass

        # Wait for more cards to appear
        for _ in range(16):  # ~8s
            await page.wait_for_timeout(500)
            after = await self._count_cards(page)
            if after > before:
                return True
        return False

    def _get_show_more_button(self, page: Page):
        """Get the 'Show More' button locator"""
        selectors = [
            "button.show-more-positions",
            "button.btn.btn-sm.btn-secondary.show-more-positions",
            "div.iframe-button-wrapper >> button",
            "footer button",
        ]
        
        for sel in selectors:
            loc = page.locator(sel).first
            if loc:
                return loc
        
        # Fallback by text
        return page.locator("button").filter(has_text=re.compile(r"show\s*more", re.I)).first

    async def _parse_job_detail(self, page: Page) -> Optional[Dict[str, Any]]:
        """Parse job details from the detail panel"""
        try:
            detail = page.locator("div.position-full-card").first
            
            # Title
            title_el = detail.locator(".position-title h1, h1[aria-level]").first
            if not await title_el.count():
                title_el = page.locator(".position-title h1, h1[aria-level]").first
            title = (await title_el.inner_text()).strip()

            # Location
            location = await self._extract_location(page)

            # Job Posting Date
            posted_date = await self._extract_posted_date(page)

            # Req ID
            req_id = await self._extract_req_id(page)

            # Team (department)
            department = await self._extract_department(page)

            # Work Type
            job_type = await self._extract_job_type(page)

            # Description
            description = await self._extract_description(page)

            # Create job ID
            slug = re.sub(r"\W+", "_", title)[:30]
            job_id = f"NETFLIX_{req_id or slug}"

            return {
                "jobId": job_id,
                "title": title,
                "company": "Netflix",
                "location": location,
                "url": page.url,
                "description": description,
                "posted_date": posted_date,
                "scraped_date": now_iso(),
                "source": "netflix",
                "status": "active",
                "department": department or None,
                "job_type": job_type or None,
            }

        except Exception as e:
            self.logger.debug(f"Netflix: failed to parse job detail: {e}")
            return None

    async def _extract_location(self, page: Page) -> str:
        """Extract job location with expansion of 'more' links"""
        detail = page.locator("div.position-full-card").first

        # Expand '+ N more' if present
        await self._expand_location_more(page)

        # Try hero paragraph first
        hero_p = detail.locator("div[data-testid='sdsm-hero-text'] p")
        if await hero_p.count():
            tokens = []
            for i in range(await hero_p.count()):
                t = (await hero_p.nth(i).inner_text() or "").strip()
                if ("," in t and len(t) <= 80) or "remote" in t.lower():
                    tokens.append(re.sub(r"\s+", " ", t))
            if tokens:
                return self._clean_location_text(" • ".join(dict.fromkeys(tokens)))

        # Fallback to position-location block
        for sel in ("p.position-location span", "p.position-location"):
            el = detail.locator(sel)
            if await el.count():
                raw = (await el.first.inner_text() or "").strip()
                return self._clean_location_text(raw)

        return ""

    async def _expand_location_more(self, page: Page) -> None:
        """Click the '+ N more' link in location if present"""
        detail = page.locator("div.position-full-card").first
        link = detail.locator("p.position-location a.link-blue")

        if not await link.count():
            return

        for _ in range(2):  # safety cap
            txt = ((await link.first.inner_text()) or "").strip().lower()
            if "more" in txt or "+" in txt:
                await link.first.click()
                await page.wait_for_timeout(150)
            else:
                break

    def _clean_location_text(self, raw: str) -> str:
        """Clean and normalize location text"""
        # Remove UI tokens
        raw = re.sub(r"\+\s*\d+\s*more", "", raw, flags=re.I)
        raw = re.sub(r"\bview\s+less\b", "", raw, flags=re.I)

        # Split by newlines/bullets, trim and dedupe
        parts = re.split(r"[\n\r•]+", raw)
        parts = [re.sub(r"\s+", " ", p).strip(" .,-") for p in parts if p and p.strip()]
        if len(parts) > 1:
            parts = [p for p in parts if not re.search(r"\bUSA\s*-\s*Remote\b", p, re.I)]

        seen, uniq = set(), []
        for p in parts:
            if p not in seen:
                seen.add(p)
                uniq.append(p)
        return " • ".join(uniq)

    async def _extract_posted_date(self, page: Page) -> Optional[str]:
        """Extract job posting date"""
        try:
            node = page.locator("//div[h4[contains(.,'Job Posting Date')]]/div").first
            raw = (await node.inner_text()).strip()  # e.g., "06-10-2025"
            try:
                posted_date = datetime.strptime(raw, "%d-%m-%Y").strftime("%Y-%m-%d")
                return posted_date
            except ValueError:
                return raw or None
        except Exception:
            return None

    async def _extract_req_id(self, page: Page) -> str:
        """Extract job requisition ID"""
        try:
            node = page.locator("//div[h4[contains(.,'Job Requisition ID')]]/div").first
            return (await node.inner_text()).strip()
        except Exception:
            return ""

    async def _extract_department(self, page: Page) -> str:
        """Extract team/department"""
        try:
            node = page.locator("//div[h4[contains(.,'Teams')]]/div").first
            return (await node.inner_text()).strip()
        except Exception:
            return ""

    async def _extract_job_type(self, page: Page) -> str:
        """Extract work type"""
        try:
            node = page.locator("//div[h4[contains(.,'Work Type')]]/div").first
            job_type = (await node.inner_text()).strip()
            if job_type:
                low = job_type.lower()
                if "remote" in low:
                    return "Remote"
                elif "hybrid" in low:
                    return "Hybrid"
                elif "onsite" in low or "on-site" in low:
                    return "Onsite"
            return job_type
        except Exception:
            return ""

    async def _extract_description(self, page: Page) -> str:
        """Extract job description"""
        desc_nodes = page.locator(".position-job-description p span, .position-job-description p")
        parts: List[str] = []
        cnt = await desc_nodes.count()
        for i in range(cnt):
            t = (await desc_nodes.nth(i).inner_text()).strip()
            if t:
                parts.append(t)
        return "\n\n".join(parts)

    def _is_us_ca(self, location: str) -> bool:
        """Check if location is in US or Canada"""
        loc = (location or "").lower()
        keywords = [
            # US
            "usa", "united states", "u.s.", "us -", "california", "los gatos",
            "remote - us", "remote - usa", "remote - united states",
            # CA
            "canada", "toronto", "vancouver", "montreal", "bc", "british columbia",
            "ontario", "remote - canada",
        ]
        return any(k in loc for k in keywords)