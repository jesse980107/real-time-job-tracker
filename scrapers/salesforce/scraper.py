import asyncio
import hashlib
import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Set
from urllib.parse import urlparse, urljoin

from playwright.async_api import async_playwright, Page, TimeoutError as PlaywrightTimeoutError

# If you want strict timezone-consistent timestamps, swap to your time util:
# from utils.timeUtil import get_current_timestamp_in_timezone
# def now_iso(): return get_current_timestamp_in_timezone("UTC")
def now_iso() -> str:
    return datetime.now().isoformat()

class SalesforceScraper:
    """
    Scrapes Salesforce Careers results filtered to US + Canada (URL params),
    opens each job detail page in a new tab, and returns a list[job].
    Output schema matches the unified structure your pipeline expects.
    Mirrors resilience patterns from WorkdayNvidiaScraper.  :contentReference[oaicite:1]{index=1}
    """

    def __init__(self, website_config: Dict[str, Any], global_config: Dict[str, Any]):
        self.cfg = website_config
        self.g = global_config
        self.logger = logging.getLogger("job_tracker")

        wp = self.cfg.get("playwright_options", {})
        self.sel_results = wp.get("results_container", "#js-job-search-results, .job-listing")
        self.sel_card = wp.get("card_selector", ".card.card-job")
        self.sel_card_link = wp.get("card_link_selector", "a.stretched-link.js-view-job")
        self.sel_detail_title = wp.get("detail_title", "h1.hero-heading, main h1, article h1, h1")
        self.sel_detail_meta_li = wp.get("detail_meta_list", "ul.list-unstyled.job-meta li")
        self.sel_detail_time = wp.get("detail_time", "time[datetime]")
        self.sel_detail_desc = wp.get("detail_description", "article.cms-content, article[class*='cms-content']")
        self.sel_next = wp.get("next_button", "a[rel='next']")

        sc = self.cfg.get("scraping_config", {})
        self.max_pages = sc.get("max_pages", 3)          # scrolling passes
        self.max_jobs = sc.get("max_jobs", 600)
        self.sleep_after_nav_ms = sc.get("sleep_after_nav_ms", 1200)
        self.sleep_after_open_ms = sc.get("sleep_after_open_ms", 700)

        pg = self.g["playwright_global"]
        self.timeout = pg["timeout"]
        self.user_agent = pg["user_agent"]
        self.headless = self.g["global_settings"]["headless"]

        self.base_url = self.cfg["website_info"]["base_url"]
        self.scraped: List[Dict[str, Any]] = []
        self.seen_urls: Set[str] = set()
        # __init__
        self.sel_detail_meta_li = wp.get(
            "detail_meta_list",
            "ul.list-unstyled.job-meta > li"  
        )


    # ---------------- entry ----------------
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

                await self._open_results(page)
                await self._scrape_listing(page)

                await context.close()
                await browser.close()

            self.logger.info(f"Salesforce scraping finished. Total jobs collected: {len(self.scraped)}")
            return self.scraped

        except Exception as e:
            self.logger.error(f"Salesforce scraping error: {e}")
            return self.scraped

    # -------------- navigation --------------
    async def _open_results(self, page: Page) -> None:
        self.logger.info(f"Opening: {self.base_url}")
        await page.goto(self.base_url)
        await page.wait_for_load_state("domcontentloaded")
        await page.wait_for_selector(self.sel_results, timeout=self.timeout)
        await page.wait_for_timeout(self.sleep_after_nav_ms)

    # -------------- listing --------------
    async def _scrape_listing(self, page: Page) -> None:
        page_pass = 1
        total = 0
        while page_pass <= self.max_pages:
            # coax lazy content to render
            await self._progressive_scroll(page)

            cards = page.locator(self.sel_card)
            count = await cards.count()
            if count == 0:
                # sometimes the cards are direct links under the container
                cards = page.locator(f"{self.sel_results} {self.sel_card}")
                count = await cards.count()

            self.logger.info(f"[Pass {page_pass}] Found {count} cards")
            if count == 0:
                break

            # snapshot links to avoid DOM churn issues
            snapshot: List[str] = []
            for i in range(count):
                a = cards.nth(i).locator(self.sel_card_link)
                if await a.count():
                    href = await a.first.get_attribute("href")
                    if href:
                        abs_url = self._abs(page, href)
                        if abs_url not in snapshot:
                            snapshot.append(abs_url)

            # process
            for href in snapshot:
                if total >= self.max_jobs: break
                if href in self.seen_urls: continue
                job = await self._parse_detail_in_new_tab(page, href)
                await page.wait_for_timeout(self.sleep_after_open_ms)
                if job:
                    self.scraped.append(job)
                    self.seen_urls.add(job["url"])
                    total += 1
                    self.logger.info(f"✔ [{total}] {job['title']} ({job.get('location','')})")

            if total >= self.max_jobs:
                break

            # try explicit "Next" if present; otherwise another scroll pass
            moved = await self._goto_next(page)
            if not moved:
                # Try URL-based paging
                target_page = page_pass + 1
                if target_page <= self.max_pages:
                    next_url = self._page_url(target_page)
                    self.logger.info(f"No rel=next; navigating to {next_url}")
                    await page.goto(next_url)
                    await page.wait_for_load_state("networkidle")
                    await page.wait_for_timeout(self.sleep_after_nav_ms)
                    page_pass += 1
                    continue
                else:
                    break
            else:
                page_pass += 1

    async def _progressive_scroll(self, page: Page) -> None:
        try:
            for _ in range(10):
                await page.mouse.wheel(0, 1400)
                await asyncio.sleep(0.12)
            await page.mouse.wheel(0, -800)
            await asyncio.sleep(0.1)
        except:
            pass

    async def _goto_next(self, page: Page) -> bool:
        try:
            btn = page.locator(self.sel_next)
            if await btn.count():
                first_before = await page.locator(self.sel_card_link).first.get_attribute("href")
                await btn.first.scroll_into_view_if_needed()
                await btn.first.click()
                await page.wait_for_load_state("networkidle")
                await page.wait_for_timeout(self.sleep_after_nav_ms)

                # confirm content changed
                for _ in range(25):
                    first_after = await page.locator(self.sel_card_link).first.get_attribute("href")
                    if first_after and first_after != first_before:
                        return True
                    await asyncio.sleep(0.2)
        except PlaywrightTimeoutError:
            return False
        except Exception:
            return False
        return False

    # -------------- detail parsing --------------
    async def _parse_detail_in_new_tab(self, listing_page: Page, url: str) -> Optional[Dict[str, Any]]:
        ctx = listing_page.context
        p = await ctx.new_page()
        try:
            # capture response so we can read HTTP status
            resp = await p.goto(url)
            await p.wait_for_load_state("networkidle")

            # --- Not-found guards ---
            # 1) HTTP status
            if resp and resp.status == 404:
                self.logger.info(f"Skip 404: {url}")
                return None

            # 2) Title/header says "Page Not Found"
            page_h1 = await self._get_text(p, self.sel_detail_title) or ""
            # sometimes the <title> is also useful
            try:
                doc_title = (await p.title()) or ""
            except Exception:
                doc_title = ""

            # 3) Body contains SF not-found copy
            try:
                body_text = (await p.inner_text("body") or "").strip()
            except Exception:
                body_text = ""

            not_found_markers = [
                "page not found",
                "the page you requested could not be found",
                "may have been moved, updated or deleted",
            ]
            if (
                re.search(r"page\s+not\s+found", page_h1, re.I)
                or re.search(r"page\s+not\s+found", doc_title, re.I)
                or any(m in body_text.lower() for m in not_found_markers)
            ):
                self.logger.info(f"Skip not-found content: {url}")
                return None

            # --- Normal extraction ---
            title = page_h1 or self._derive_title_from_url(url) or "Untitled"

            # meta list (category, locations, type, posted, JR id, salary…)
            metas = [t for t in (await self._all_text(p, self.sel_detail_meta_li)) if t]

            # Prefer explicit multi-location list
            loc_nodes = await p.query_selector_all("ul.multi-locations-list li")
            locs: List[str] = []
            if loc_nodes:
                for n in loc_nodes:
                    t = (await n.inner_text() or "").strip()
                    t = re.sub(r"\s+", " ", t)
                    if t:
                        locs.append(t)

            # Fallback from metas (avoid big concatenated outer <li>)
            if not locs:
                for t in metas:
                    if re.search(r"\b(Posted|JR\d+|Full[-\s]?time|Part[-\s]?time|Contract|Salary)\b", t, re.I):
                        continue
                    if " - " in t or re.search(r"\b[A-Za-z].*,\s*[A-Za-z]", t):
                        if t.count(" - ") > 1:
                            continue
                        locs.extend(self._split_locations(t))

            locs = self._dedupe_preserve_order(locs)
            location = ", ".join(locs)

            posted_date = await self._extract_posted_date(p, metas)
            description = await self._get_text(p, self.sel_detail_desc) or ""

            job_id = self._make_job_id(url, metas)

            return {
                "jobId": job_id,
                "title": title,
                "company": "Salesforce",
                "location": location,
                "url": url,
                "description": description.strip(),
                "posted_date": posted_date,
                "source": "salesforce",
                "status": "active",
                "scraped_date": now_iso(),
            }
        except Exception as e:
            self.logger.warning(f"Detail parse failed {url}: {e}")
            return None
        finally:
            await p.close()


    async def _get_text(self, page: Page, selector: str) -> Optional[str]:
        try:
            el = await page.query_selector(selector)
            if not el: return None
            txt = (await el.inner_text() or "").strip()
            return re.sub(r"\s+", " ", txt)
        except:
            return None

    async def _all_text(self, page: Page, selector: str) -> List[str]:
        items: List[str] = []
        try:
            nodes = await page.query_selector_all(selector)
            for n in nodes:
                t = (await n.inner_text() or "").strip()
                t = re.sub(r"\s+", " ", t)
                if t:
                    items.append(t)
        except:
            pass
        return items

    def _pick_location(self, metas: List[str]) -> str:
        """
        Collect all location-like entries from the meta list and return
        a single string joined by ', '.
        """
        locs: List[str] = []

        for t in metas:
            # skip non-location meta items
            if re.search(r"\bPosted\b", t, re.I):      continue
            if re.search(r"\bFull[-\s]?time|Part[-\s]?time|Contract\b", t, re.I): continue
            if re.search(r"\bJR\d+\b", t, re.I):       continue
            if re.search(r"\bSalary\b", t, re.I):      continue

            # heuristics: entries that look like locations:
            #   'California - San Francisco'  or contain a hyphenized city/state,
            #   or items that the page groups with other locations via '/'
            if " - " in t or re.search(r"\b[A-Za-z].*,\s*[A-Za-z]", t):
                locs.extend(self._split_locations(t))

        if not locs:
            # Fallback: first meta that isn't obviously non-location
            for t in metas:
                if not re.search(r"\b(Posted|JR\d+|Full|Part|Salary)\b", t, re.I):
                    locs = self._split_locations(t)
                    break

        locs = self._dedupe_preserve_order(locs)
        return ", ".join(locs)

    async def _extract_posted_date(self, page: Page, metas: List[str]) -> Optional[str]:
        # 1) <time datetime="YYYY-MM-DD">
        try:
            el = await page.query_selector(self.sel_detail_time)
            if el:
                dt = await el.get_attribute("datetime")
                if dt and re.match(r"^\d{4}-\d{2}-\d{2}", dt):
                    return dt[:10]
        except:
            pass
        # 2) Text like "Posted 08 September 2025"
        for t in metas:
            m = re.search(r"\bPosted\b\s+([A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4})", t)
            if m:
                raw = m.group(1).replace("Sept", "Sep")
                for fmt in ("%d %B %Y", "%B %d %Y", "%b %d %Y", "%d %b %Y", "%B %d, %Y", "%b %d, %Y"):
                    try:
                        return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
                    except:  # try next
                        pass
        return None

    def _make_job_id(self, url: str, metas: List[str]) -> str:
        # Prefer explicit JR code from metas; else derive from URL segment.
        for t in metas:
            m = re.search(r"\b(JR\d{5,})\b", t, re.I)
            if m:
                return f"SALESFORCE_{m.group(1).upper()}"
        # URL like /en/jobs/jr309729/principal-executive-assistant/
        path = urlparse(url).path.rstrip("/")
        parts = path.split("/")
        token = None
        if "jobs" in parts:
            i = parts.index("jobs")
            if i + 1 < len(parts):
                token = parts[i + 1]
        token = (token or "UNKNOWN").upper()
        return f"SALESFORCE_{token}"

    def _derive_title_from_url(self, url: str) -> str:
        slug = urlparse(url).path.rstrip("/").split("/")[-1]
        # clean dashes/underscores → spaces; fix doubled dashes
        t = slug.replace("---", " — ").replace("--", " – ")
        t = re.sub(r"[-_]+", " ", t)
        return re.sub(r"\s+", " ", t).strip().title()

    def _abs(self, page: Page, href: str) -> str:
        if href.startswith("http"): return href
        u = page.url
        base = f"{urlparse(u).scheme}://{urlparse(u).netloc}"
        return urljoin(base, href)

    def _dedupe_preserve_order(self, seq: List[str]) -> List[str]:
        seen = set()
        out = []
        for x in seq:
            if x not in seen:
                seen.add(x)
                out.append(x)
        return out

    def _split_locations(self, text: str) -> List[str]:
        """
        Salesforce sometimes shows multiple locations in one line, e.g.:
        'Indiana - Indianapolis / Washington - Seattle'
        Split on common separators but NOT on commas inside a single location.
        """
        parts = re.split(r"\s*[\/|•]\s*", text)  # split on "/", "|" or "•"
        out = []
        for p in parts:
            p = re.sub(r"\s+", " ", p).strip()
            if p:
                out.append(p)
        return out

    def _dedupe_preserve_order(self, seq: List[str]) -> List[str]:
        seen = set(); out = []
        for x in seq:
            if x not in seen:
                seen.add(x); out.append(x)
        return out

    def _split_locations(self, text: str) -> List[str]:
        # Split on "/", "|" or "•" but not on commas inside a single location
        parts = re.split(r"\s*[\/|•]\s*", text)
        return [re.sub(r"\s+", " ", p).strip() for p in parts if p and p.strip()]

    def _page_url(self, n: int) -> str:
        # Keep the #results anchor if present so the view jumps to the list
        frag = "#results" if self.base_url.endswith("#results") else ""
        base = self.base_url[:-8] if frag else self.base_url

        if re.search(r"[?&]page=\d+", base):
            base = re.sub(r"([?&]page=)\d+", r"\1" + str(n), base)
        else:
            sep = "&" if "?" in base else "?"
            base = f"{base}{sep}page={n}"

        return base + frag










