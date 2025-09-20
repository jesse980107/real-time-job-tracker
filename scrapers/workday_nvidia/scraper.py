"""
NVIDIA (Workday) scraper
- Filters to United States & Canada
- Opens each job card (side panel) and extracts fields
- jobId = "NVIDIA_" + <last URL slug from the card link>
"""

import asyncio
from email.utils import unquote
from urllib.parse import urlparse, urljoin, urlunparse, unquote
import hashlib
import logging
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Set
from urllib.parse import urlparse
from playwright.async_api import async_playwright, Page
from urllib.parse import urlparse
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
COUNTRY_KEEP = {"US", "CA"}  # country codes to keep

class WorkdayNvidiaScraper:
    def _canonicalize_url(self, url: str) -> str:
        """
        Return a canonical version of the URL (strip query params for deduplication).
        """
        return url.split('?')[0] if url else url
    def __init__(self, website_config: Dict[str, Any], global_config: Dict[str, Any]):
        # selectors (keep these near your __init__)
        self.sel_results = "section[data-automation-id='jobResults']"
        self.sel_card_link = "a[data-automation-id='jobTitle']"
        self.sel_detail_panel = "section[data-automation-id='jobDetails']"
        self.sel_locations = "div[data-automation-id='locations'] dd"
        self.sel_posted = "div[data-automation-id='postedOn']"
        self.sel_desc = "div[data-automation-id='jobPostingDescription']"
        self.sel_next_btn = "button[aria-label*='Next' i], [data-automation-id='nextPageButton']"

        self.cfg = website_config
        self.g = global_config
        self.logger = logging.getLogger("job_tracker")

        self.base_url = self.cfg["website_info"]["base_url"]

        sc = self.cfg.get("scraping_config", {})
        self.max_pages = sc.get("max_pages", 10)
        self.max_jobs = sc.get("max_jobs", 500)
        self.us_ca_only = sc.get("us_ca_only", True)
        self.sleep_after_nav_ms = sc.get("sleep_after_nav_ms", 1200)
        self.sleep_after_open_ms = sc.get("sleep_after_open_ms", 500)

        # filter UI selectors
        self.sel_filter_button = "button[data-automation-id='distanceLocation']"
        self.sel_filter_panel = "div[data-automation-id='filterMenu']"  # container of the popover
        self.sel_filter_view_jobs = "button:has-text('View Jobs')"

        # pagination (best-effort)
        self.sel_next_btn = "button[aria-label*='Next' i], [data-automation-id='nextPageButton']"

        pg = self.g["playwright_global"]
        self.timeout = pg["timeout"]
        self.user_agent = pg["user_agent"]
        self.headless = self.g["global_settings"]["headless"]

        self.scraped: List[Dict[str, Any]] = []
        self.seen_urls: Set[str] = set()
        # pagination / load more (add these in __init__)
        self.next_selectors = [
            "[data-automation-id='pagination'] button[aria-label*='Next' i]",
            "nav[aria-label*='pagination' i] button[aria-label*='Next' i]",
            "button[data-automation-id='nextPageButton']",
            "button[aria-label='Next Page']",
            "button:has-text('Next')"
        ]
        self.load_more_selector = "button[data-automation-id='showMoreJobs'], button:has-text('Load more')"


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

                await self._open_and_apply_filters(page)
                await self._scrape_all_pages(page)

                await context.close()
                await browser.close()

            self.logger.info(f"NVIDIA scraping finished. Total jobs collected: {len(self.scraped)}")
            return self.scraped

        except Exception as e:
            self.logger.error(f"NVIDIA scraping error: {e}")
            # return what we have so far rather than []
            return self.scraped

    # -------------- navigation & filters --------------

    async def _open_and_apply_filters(self, page: Page) -> None:
        self.logger.info(f"Opening: {self.base_url}")
        await page.goto(self.base_url)
        await page.wait_for_load_state("domcontentloaded")

        # Ensure results container exists before filtering
        await page.wait_for_selector(self.sel_results, timeout=self.timeout)
        await page.wait_for_timeout(400)

        # If chips already visible, skip re-applying
        chips_have_us = await page.get_by_role("button", name=re.compile(r"United States", re.I)).count()
        chips_have_ca = await page.get_by_role("button", name=re.compile(r"Canada", re.I)).count()
        if chips_have_us and chips_have_ca:
            self.logger.info("US + Canada chips already active; skipping filter UI.")
            return

        try:
            # Open "Location" filter popover
            await page.click(self.sel_filter_button, timeout=8000)
            await page.wait_for_selector(self.sel_filter_panel, timeout=10000)

            # The popover has a search field and groups (Location Type / Locations)
            # Check 'United States' and 'Canada' (checkboxes preferred, fallback to button)
            for country in ("United States", "Canada"):
                # First try a checkbox by accessible name
                cb = page.get_by_role("checkbox", name=re.compile(country, re.I))
                if await cb.count():
                    if not await cb.first.is_checked():
                        await cb.first.check()
                        await page.wait_for_timeout(120)
                else:
                    # Some themes render as buttons/options
                    opt = page.get_by_role("button", name=re.compile(f"^{country}\\b", re.I))
                    if await opt.count():
                        await opt.first.click()
                        await page.wait_for_timeout(120)

            # Apply / View Jobs
            if await page.locator(self.sel_filter_view_jobs).count():
                await page.click(self.sel_filter_view_jobs)
            else:
                # fallback: press Enter in the popover to apply
                await page.keyboard.press("Enter")

            # Wait for results to refresh and chips to appear below the search box
            await page.wait_for_load_state("networkidle")
            await page.wait_for_timeout(self.sleep_after_nav_ms)

            # Verify chips are visible (the little pills that read "United States" and "Canada")
            await page.get_by_role("button", name=re.compile(r"United States", re.I)).first.wait_for(timeout=8000)
            await page.get_by_role("button", name=re.compile(r"Canada", re.I)).first.wait_for(timeout=8000)

            # Final guard: ensure results container is still present
            await page.wait_for_selector(self.sel_results, timeout=self.timeout)
            self.logger.info("Location filter applied: United States + Canada")

        except PlaywrightTimeoutError:
            self.logger.warning("Filter UI timed out; continuing and filtering in code as a fallback.")
        except Exception as e:
            self.logger.warning(f"Could not apply location filters via UI (will filter in code): {e}")

    # -------------- list paging --------------

    async def _scrape_all_pages(self, page: Page) -> None:
        """
        Robust paging:
        - For each page, snapshot all job hrefs & titles first.
        - Open each href in a NEW TAB and parse full page (no side panel).
        - Use change-aware pagination to advance pages.
        """
        page_idx = 1
        total = 0

        while page_idx <= self.max_pages:
            self.logger.info(f"Scraping result page {page_idx}")

            # Ensure results shown and render lazy content
            await page.wait_for_selector(self.sel_results, timeout=self.timeout)
            await self._progressive_scroll(page)
            try:
                await page.mouse.wheel(0, 1600)
                await asyncio.sleep(0.1)
            except:
                pass

            # ----- SNAPSHOT HREFS & TITLES for THIS PAGE -----
            links = page.locator(self.sel_card_link)
            count = await links.count()
            self.logger.info(f"Found {count} job title links on page {page_idx}")

            if count == 0:
                await page.wait_for_timeout(1200)
                await self._progressive_scroll(page)
                count = await links.count()
                self.logger.info(f"Retry count of job title links: {count}")
                if count == 0:
                    self.logger.info("No links found after retry—stopping.")
                    break

            # capture fingerprint to verify page change later
            first_href = await links.first.get_attribute("href") or ""

            # snapshot hrefs + titles now, so later DOM changes don't affect us
            snapshot: list[tuple[str, str]] = []
            for i in range(count):
                li = links.nth(i)
                href = await li.get_attribute("href")
                title_text = (await li.inner_text() or "").strip()
                if href:
                    snapshot.append((href, title_text))

            # compute how many we’re allowed to process this page
            remaining = max(0, self.max_jobs - total)
            to_process = count if remaining <= 0 else min(count, remaining)

            # ----- PROCESS EACH HREF IN NEW TAB -----
            for i in range(to_process):
                if total >= self.max_jobs:
                    self.logger.info("Reached max_jobs; stopping.")
                    break

                href, title_text = snapshot[i]
                abs_url = await self._absolute_url_from_href(page, href)

                # open in a fresh tab to avoid re-render of the list
                ctx = page.context
                newp = await ctx.new_page()
                try:
                    await newp.goto(abs_url)
                    await newp.wait_for_load_state("networkidle")
                    job = await self._extract_from_full_page(newp, abs_url, title_text)
                except Exception as e:
                    self.logger.warning(f"Failed parsing {abs_url}: {e}")
                    job = None
                finally:
                    await newp.close()

                if job:
                    if not self.us_ca_only or self._has_us_ca_location(job.get("location", "")):
                        if job["url"] not in self.seen_urls:
                            self.scraped.append(job)
                            self.seen_urls.add(job["url"])
                            total += 1
                            self.logger.info(f"✔ [{total}] {job['title']} ({job['location']})")
                        if self.us_ca_only and not self._has_us_ca_location(job.get("location","")):
                            self.logger.debug(f"Skip: no US/CA location after normalization {abs_url} -> {job.get('location')}")
                            continue
                        if job["url"] in self.seen_urls:
                            self.logger.debug(f"Skip: duplicate url {job['url']}")
                            continue

                if job is None:
                    self.logger.debug(f"Skip: parse returned None for {abs_url}")

            # stop if we've hit the job cap
            if total >= self.max_jobs:
                self.logger.info("Reached max_jobs; stopping.")
                break

            # ----- PAGINATION: only increment if content actually changes -----
            moved = await self._goto_next_results_page(
                page,
                prev_first_href=first_href,
                prev_count=count
            )
            if moved:
                page_idx += 1
                if page_idx > self.max_pages:
                    self.logger.info(f"Reached max_pages={self.max_pages}. Stopping.")
                    break
            else:
                self.logger.info("Stopping pagination (no movement detected).")
                break

    async def _progressive_scroll(self, page: Page) -> None:
        # Scroll a few times to coax lazy content to render
        try:
            for _ in range(8):
                await page.mouse.wheel(0, 1200)
                await asyncio.sleep(0.12)
            # small jump up to re-trigger observers
            await page.mouse.wheel(0, -600)
            await asyncio.sleep(0.1)
        except:
            pass

    # -------------- detail extraction --------------

    async def _extract_from_side_panel(self, page: Page, href: Optional[str], title_text: str):
        try:
            url = await self._absolute_url_from_href(page, href)
            job_id = self._make_job_id_from_url(url)

            # title (prefer panel header, then robust extractor)
            header = await page.query_selector(f"{self.sel_detail_panel} h1, {self.sel_detail_panel} h2") \
                    or await page.query_selector("h1, h2")
            title = await self._extract_title(page, title_hint=title_text, url=url)
            if header:
                t = (await header.inner_text() or "").strip()
                if t:
                    title = t

            # locations (robust)
            locs = await self._extract_locations_anywhere(page, url)
            location_field = " ;".join(locs)

            # posted date + description
            posted_date = await self._extract_posted(page)
            desc_el = await page.query_selector(self.sel_desc)
            description = await self._extract_description(page)

            if self.us_ca_only and not location_field:
                return None

            return {
                "jobId": job_id,
                "title": title or "Untitled",
                "company": "NVIDIA",
                "location": location_field,
                "url": url,
                "description": description.strip(),
                "posted_date": posted_date,
                "source": "workday",
                "status": "active",
                "id": self._stable_md5(f"NVIDIA|{title}|{location_field}|{self._canonicalize_url(url)}"),
                "scraped_date": datetime.now().isoformat(),
            }
        except Exception as e:
            self.logger.error(f"Panel parse failed: {e}")
            return None

    async def _extract_posted(self, page: Page) -> str | None:
        """
        Return:
        - 'YYYY-MM-DD' for absolute or relative dates,
        - 'older_than_30_days' for 'Posted 30+ Days Ago',
        - None if nothing parsable is found.
        """
        # Helper inside the function so it's always in scope
        def _fmt_date(d: datetime.date) -> str:
            return d.strftime("%Y-%m-%d")

        el = await page.query_selector(self.sel_posted)
        if not el:
            try:
                el = await page.get_by_text(re.compile(r"\bPosted\b", re.I)).first
            except Exception:
                el = None

        text = (await el.inner_text() if el else None) or ""
        text = text.strip()

        # 1) Direct ISO date like 2025-09-06
        m = re.search(r"\b(20\d{2})-(\d{2})-(\d{2})\b", text)
        if m:
            return m.group(0)

        # 2) Month Day, Year (short or long month names)
        m = re.search(r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\s+\d{1,2},\s+20\d{2}\b", text, re.I)
        if m:
            raw = m.group(0).replace("Sept", "Sep")
            for fmt in ("%b %d, %Y", "%B %d, %Y"):
                try:
                    return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
                except Exception:
                    pass

        today = datetime.now().date()

        # 3) Special: 30+ Days
        if re.search(r"\b30\+\s*Days?\s*Ago\b", text, re.I):
            return "older_than_30_days"

        # 4) N Days Ago
        m = re.search(r"\b(\d+)\s*Days?\s*Ago\b", text, re.I)
        if m:
            n = int(m.group(1))
            return _fmt_date(today - timedelta(days=n))

        # 5) Yesterday / Today
        if re.search(r"\bYesterday\b", text, re.I):
            return _fmt_date(today - timedelta(days=1))
        if re.search(r"\bToday\b", text, re.I):
            return _fmt_date(today)

        return None

    async def _extract_from_full_page(self, page: Page, url: str, title_hint: str):
        title = await self._extract_title(page, title_hint=title_hint, url=url)

        # locations (robust)
        locs = await self._extract_locations_anywhere(page, url)
        location_field = " ;".join(locs)

        posted_date = await self._extract_posted(page)
        desc_el = await page.query_selector(self.sel_desc)
        # ----- Description -----
        description = await self._extract_description(page)


        # only skip if us_ca_only AND we positively know it’s not US/CA after all fallbacks
        if self.us_ca_only and not location_field:
            return None

        return {
            "jobId": self._make_job_id_from_url(url),
            "title": title or "Untitled",
            "company": "NVIDIA",
            "location": location_field,
            "url": url,
            "description": description.strip(),
            "posted_date": posted_date,
            "source": "workday",
            "status": "active",
            "id": self._stable_md5(f"NVIDIA|{title}|{location_field}|{self._canonicalize_url(url)}"),
            "scraped_date": datetime.now().isoformat(),
        }
    # -------------- helpers --------------

    def _make_job_id_from_url(self, url: str) -> str:
        try:
            slug = urlparse(url).path.rstrip("/").split("/")[-1]
            return f"NVIDIA_{slug or 'UNKNOWN'}"
        except Exception:
            return "NVIDIA_UNKNOWN"

    def _stable_md5(self, s: str) -> str:
        return hashlib.md5(s.encode("utf-8")).hexdigest()

    def _normalize_locations(self, items: List[str]) -> List[str]:
        out: List[str] = []
        for raw in items:
            # split on newlines/semicolons/bullets
            for part in re.split(r"[;\n•]+", raw):
                val = " ".join(part.strip().split())
                if val:
                    out.append(val)
        return out

    def _has_us_ca_location(self, loc: str) -> bool:
        # NVIDIA uses formats like "US, CA, Santa Clara" or "US, Remote"
        m = re.match(r"\s*([A-Z]{2})\b", loc)
        return bool(m and m.group(1) in COUNTRY_KEEP)

    def _dedupe_preserve_order(self, seq: List[str]) -> List[str]:
        seen = set()
        out = []
        for x in seq:
            if x not in seen:
                seen.add(x)
                out.append(x)
        return out

    async def _absolute_url_from_href(self, page: Page, href: Optional[str]) -> str:
        if href:
            if href.startswith("/"):
                parts = urlparse(self.base_url)
                return f"{parts.scheme}://{parts.netloc}{href}"
            return href
        return self.base_url
    
    async def _goto_next_results_page(self, page: Page, prev_first_href: str, prev_count: int) -> bool:
        """Advance results to the next page (or load more). Returns True only if content changed."""
        # expose controls
        try:
            for _ in range(4):
                await page.mouse.wheel(0, 1500)
                await asyncio.sleep(0.12)
        except:
            pass

        # --- LOAD MORE path: link count should increase ---
        try:
            load_more = page.locator(self.load_more_selector)
            if await load_more.count():
                await load_more.first.scroll_into_view_if_needed()
                await load_more.first.click()
                await page.wait_for_load_state("networkidle")
                await page.wait_for_timeout(self.sleep_after_nav_ms)

                # wait until link count increases (up to ~5s)
                for _ in range(25):
                    new_count = await page.locator(self.sel_card_link).count()
                    if new_count > prev_count:
                        self.logger.info("Pagination: Load more -> count increased")
                        return True
                    await asyncio.sleep(0.2)
                self.logger.info("Load more clicked but count did not change")
                return False
        except Exception as e:
            self.logger.debug(f"Load more not usable: {e}")

        # --- NEXT page path: first href should change ---
        for sel in self.next_selectors:
            try:
                btn = page.locator(sel)
                if not await btn.count():
                    continue

                await btn.first.scroll_into_view_if_needed()
                await btn.first.click()
                await page.wait_for_load_state("networkidle")
                await page.wait_for_timeout(self.sleep_after_nav_ms)

                # wait until the first href is different (up to ~6s)
                for _ in range(30):
                    links = page.locator(self.sel_card_link)
                    if await links.count():
                        new_first = await links.first.get_attribute("href")
                        if new_first and new_first != prev_first_href:
                            self.logger.info(f"Pagination: Next via {sel}")
                            return True
                    await asyncio.sleep(0.2)

                self.logger.debug(f"Clicked {sel} but first href did not change")
            except Exception as e:
                self.logger.debug(f"Next selector failed ({sel}): {e}")

        self.logger.info("No working pagination control (Next/Load more) found.")
        return False

    def _looks_generic_title(self, t: str) -> bool:
        return bool(re.search(r"\bCAREERS\s+AT\s+NVIDIA\b", t, re.I)) or len((t or "").strip()) < 4

    async def _extract_title(self, page, title_hint: str, url: str) -> str:
        """
        Robust job title:
        1) Prefer job-specific headers.
        2) If generic/missing, derive from URL slug (cleaned).
        3) Else fallback to the title link we clicked.
        """
        try_selectors = [
            "[data-automation-id='jobPostingHeader'] h1",
            "h1[data-automation-id='jobTitle']",
            "section[data-automation-id='jobDetails'] h1",
            "main h1",
            "article h1",
            "h1",
        ]

        text = None
        for sel in try_selectors:
            el = await page.query_selector(sel)
            if el:
                cand = (await el.inner_text() or "").strip()
                if cand:
                    text = cand
                    break

        # If header is generic or missing, derive from URL; else use the header.
        if not text or self._looks_generic_title(text):
            # derive from URL slug
            slug = urlparse(url).path.rstrip("/").split("/")[-1]
            slug = unquote(slug)
            # drop trailing _JR... token
            slug = re.sub(r"_JR\d+[A-Z0-9-]*$", "", slug)

            # convert separators
            derived = slug.replace("---", " — ").replace("--", " – ")
            derived = re.sub(r"[-_]+", " ", derived)
            # fixes
            derived = re.sub(r"\bI O\b", "I/O", derived, flags=re.I)
            derived = re.sub(r"\bA I\b", "AI", derived, flags=re.I)
            derived = re.sub(r"\s+", " ", derived).strip()

            if derived and not self._looks_generic_title(derived):
                text = derived

        # final fallback: link text
        if not text or self._looks_generic_title(text):
            text = (title_hint or "Untitled").strip()

        return text

    async def _extract_locations_anywhere(self, page: Page, url: str) -> list[str]:
        """
        Collect location strings from multiple possible Workday layouts.
        Falls back to parse from URL (e.g., /US-CA-Santa-Clara/...) if needed.
        """
        loc_texts: list[str] = []

        # 1) Standard job details grid (panel or full page)
        sels = [
            # typical details grid
            "section[data-automation-id='jobDetails'] dl[data-automation-id='jobDetail'] dd",
            # row where the dt explicitly says "locations"
            "section[data-automation-id='jobDetails'] dt:has-text('locations') ~ dd",
            # sometimes Workday drops data-automation-id attributes:
            "dl:has(dt:has-text('locations')) dd",
            # simplified panel body
            f"{self.sel_detail_panel} dl dd",
        ]
        for sel in sels:
            nodes = await page.query_selector_all(sel)
            for n in nodes:
                t = (await n.inner_text() or "").strip()
                if t:
                    loc_texts.append(t)

        # 2) If nothing yet, try list-pane subtitle lines (the icon list under a card)
        if not loc_texts:
            nodes = await page.query_selector_all("ul[data-automation-id='subtitle'] li")
            for n in nodes:
                t = (await n.inner_text() or "").strip()
                if t and re.search(r"\bUS\b|\bCanada\b|\bCA\b|,?\s*Remote\b", t, re.I):
                    loc_texts.append(t)

        # 3) Still nothing? derive from URL path:  /US-CA-Santa-Clara/<title>_JR...
        if not loc_texts:
            path = urlparse(url).path
            m = re.search(r"/(US|Canada)-([A-Z]{2})(?:-([A-Za-z][A-Za-z-]*))?/", path)
            if m:
                country_code = m.group(1)
                state = m.group(2)
                city = (m.group(3) or "").replace("-", " ").title().strip()
                if country_code == "US":
                    if city:
                        loc_texts.append(f"US, {state}, {city}")
                    else:
                        loc_texts.append(f"US, {state}")
                elif country_code.lower() == "canada":
                    # we don’t know province from the URL reliably; leave as country-level
                    loc_texts.append("Canada")

        # Normalize + US/CA filter
        locs = self._normalize_locations(loc_texts)
        if self.us_ca_only:
            locs = [l for l in locs if self._has_us_ca_location(l)]

        # De-dupe preserving order
        return self._dedupe_preserve_order(locs)

    def _canonicalize_url(self, url: str) -> str:
        """
        Normalize Workday job URLs for stable IDs & duplicate detection.
        - Drop querystring (?locationHierarchy=...)
        - Normalize /details/ → /job/ (both show the same posting)
        - Remove trailing slash
        """
        try:
            p = urlparse(url)
            # normalize the path between the two Workday styles
            path = p.path.replace("/details/", "/job/").rstrip("/")
            return urlunparse((p.scheme, p.netloc, path, "", "", ""))
        except Exception:
            # if anything odd happens, just return the original
            return url

    async def _extract_description(self, page: Page) -> str:
        """
        Robust description extractor for Workday:
        - Expand 'Show more' if present
        - Try several known description containers
        - Fallback to job details section text
        """
        try:
            # 1) Expand 'Show more' / 'More' if present (panel or full page)
            for sel in [
                "button[aria-label*='Show more' i]",
                "button:has-text('Show more')",
                "button[aria-label*='More' i]"
            ]:
                btn = await page.query_selector(sel)
                if btn:
                    try:
                        await btn.click()
                        # brief wait for expansion
                        await page.wait_for_timeout(200)
                    except Exception:
                        pass

            # 2) Common WD description containers (panel + full page)
            candidates = [
                # panel rich text
                "section[data-automation-id='jobDetails'] [data-automation-id='richTextSection'],section[data-automation-id='jobDetails'] [data-automation-id='richTextDescription']",
                # full page rich text
                "[data-automation-id='richTextSection'], [data-automation-id='richTextDescription']",
                # job posting description container
                "div[data-automation-id='jobPostingDescription']",
                "section[data-automation-id='jobDescription']",
                # generic article/main fallbacks
                "article",
                "main"
            ]

            # 3) Wait briefly for any of these to appear
            try:
                await page.wait_for_selector(", ".join(candidates), timeout=3000)
            except Exception:
                pass  # continue to try-get

            # 4) Try in order
            for sel in candidates:
                el = await page.query_selector(sel)
                if el:
                    txt = (await el.inner_text() or "").strip()
                    if txt and len(txt) > 30:
                        return txt

            # 5) Last-chance fallback: the job details section (panel), or whole body
            for sel in [
                "section[data-automation-id='jobDetails']",
                "body"
            ]:
                el = await page.query_selector(sel)
                if el:
                    txt = (await el.inner_text() or "").strip()
                    if txt and len(txt) > 30:
                        return txt

            return ""
        except Exception:
            return ""


