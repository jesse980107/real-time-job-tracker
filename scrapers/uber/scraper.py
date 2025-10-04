import asyncio
import json
import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urljoin, urlparse, urlunparse, quote, urlencode

from playwright.async_api import async_playwright, Page, TimeoutError as PlaywrightTimeoutError


# --- Regions & helpers --------------------------------------------------------

US_STATE_FULL = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho",
    "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi",
    "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico",
    "NY": "New York", "NC": "North Carolina", "ND": "North Dakota",
    "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania",
    "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont",
    "VA": "Virginia", "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin",
    "WY": "Wyoming", "DC": "District of Columbia"
}
US_STATE_NAMES = set(US_STATE_FULL.values())  # full names incl. DC

BAD_CITY_TOKENS = {
    "operations", "backend", "engineering", "legal", "finance", "product",
    "sales", "design", "security", "marketing", "data", "cloud", "android",
    "ios", "platform", "support", "global", "enterprise", "retail", "airport",
    "venue", "curb"
}

CITY_STATE_FINDALL = re.compile(r"([A-Za-z0-9 .’'&()/-]+,\s*[A-Za-z .’'&()/-]+)")

def now_iso() -> str:
    return datetime.now().isoformat()


# --- Scraper ------------------------------------------------------------------

class UberScraper:

    def __init__(self, website_config: Dict[str, Any], global_config: Dict[str, Any]):
        self.cfg = website_config
        self.g = global_config
        self.logger = logging.getLogger("job_tracker")

        sc = self.cfg.get("scraping_config", {})
        self.max_jobs = sc.get("max_jobs", 400)
        self.sleep_after_nav_ms = sc.get("sleep_after_nav_ms", 900)
        self.sleep_after_open_ms = sc.get("sleep_after_open_ms", 500)
        self.apply_filters_via_ui = sc.get("apply_filters_via_ui", True)
        self.target_locations: List[str] = sc.get("locations", [])

        wp = self.cfg.get("playwright_options", {})
        self.sel_results_link = wp.get("results_link_selector", "a[href^='/careers/list/']")
        self.sel_show_more = wp.get("show_more_button", "button:has-text('Show more openings')")
        self.sel_detail_title = wp.get("detail_title", "main h1, h1")
        self.sel_detail_desc = wp.get("detail_description", "article, main article")
        self.locations_filter_label = wp.get("locations_filter_label", "Locations")

        pg = self.g["playwright_global"]
        self.timeout = pg["timeout"]
        self.user_agent = pg["user_agent"]
        self.headless = self.g["global_settings"]["headless"]

        self.base_url = self.cfg["website_info"]["base_url"]

        self.scraped: List[Dict[str, Any]] = []
        self.seen_urls: Set[str] = set()

    # ---------------- entry ----------------
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

                self.logger.info(f"Uber max_jobs = {self.max_jobs}")
                await self._open_list_page(page)

                applied = False
                if self.apply_filters_via_ui:
                    applied = await self._apply_locations_via_ui(page, self.target_locations)

                if not applied:
                    url_with_params = self._build_url_with_locations(self.base_url, self.target_locations)
                    await page.goto(url_with_params)
                    await page.wait_for_load_state("domcontentloaded")
                    await page.wait_for_timeout(self.sleep_after_nav_ms)

                await self._harvest_and_parse(page)

                await context.close()
                await browser.close()

            end_time = datetime.now()
            duration = (end_time - start_time).total_seconds()
            self.scraping_duration = duration
            self.logger.info(f"Scraping duration: {duration:.2f} seconds")
            self.logger.info(f"Uber scraping finished. Total jobs collected: {len(self.scraped)}")
            return self.scraped

        except Exception as e:
            self.logger.error(f"Uber scraping error: {e}")
            return self.scraped

    # -------------- open page --------------
    async def _open_list_page(self, page: Page) -> None:
        self.logger.info(f"Opening: {self.base_url}")
        await page.goto(self.base_url)
        await page.wait_for_load_state("domcontentloaded")
        await page.wait_for_timeout(self.sleep_after_nav_ms)

    # -------------- filtering --------------
    async def _apply_locations_via_ui(self, page: Page, locations: List[str]) -> bool:
        if not locations:
            return True
        try:
            await page.get_by_text(re.compile(rf"^{re.escape(self.locations_filter_label)}\b", re.I)).first.scroll_into_view_if_needed()
            await page.wait_for_timeout(100)

            sel = page.locator("div[data-baseweb='select']").first
            await sel.click()
            await page.wait_for_timeout(150)

            combo = sel.locator("input[role='combobox']").first

            for loc in locations:
                try:
                    await combo.fill("")
                    await combo.type(loc, delay=20)
                    await page.wait_for_timeout(180)

                    opt = page.get_by_role("option", name=re.compile(rf"^{re.escape(loc)}$", re.I))
                    if await opt.count():
                        await opt.first.click()
                    else:
                        txt = page.locator(f"div[role='listbox'] >> text={loc}")
                        if await txt.count():
                            await txt.first.click()
                        else:
                            self.logger.debug(f"Could not select location by typing: {loc}")

                    await page.wait_for_timeout(120)
                except Exception:
                    self.logger.debug(f"Typing-select failed for: {loc}")

            applied = False
            for btn_name in ("View Jobs", "Apply", "Done", "Close"):
                btn = page.get_by_role("button", name=re.compile(btn_name, re.I))
                if await btn.count():
                    try:
                        await btn.first.click()
                        applied = True
                        break
                    except Exception:
                        pass
            if not applied:
                try:
                    await page.keyboard.press("Enter")
                except Exception:
                    pass

            await page.wait_for_load_state("networkidle")
            await page.wait_for_timeout(self.sleep_after_nav_ms)

            # If at least one chip exists, consider it success
            chip_any = page.get_by_role("button", name=re.compile(r".+,\s*(?:[A-Za-z ]+)$", re.I))
            if await chip_any.count():
                self.logger.info("Location filter applied via UI (chips detected).")
                return True

            self.logger.info("Location chips not detected; UI filter may have failed.")
            return False

        except PlaywrightTimeoutError:
            self.logger.warning("Filter UI timed out.")
            return False
        except Exception as e:
            self.logger.warning(f"Filter UI failed: {e}")
            return False

    def _build_url_with_locations(self, base: str, locations: List[str]) -> str:
        p = urlparse(base)

        def enc(s: str) -> str:
            return quote(s, safe="")

        def to_token(loc: str) -> str:
            loc = (loc or "").strip()
            if not loc:
                return ""
            if "," in loc:
                city, right = [x.strip() for x in loc.split(",", 1)]
                if right.lower() == "canada":
                    if city == "Toronto":
                        return f"CAN-{enc('Ontario')}-{enc(city)}"
                    return f"Canada-{enc(city)}"
                return f"USA-{enc(right)}-{enc(city)}"
            return enc(loc)

        tokens = [t for t in (to_token(l) for l in locations) if t]
        existing_q = p.query
        loc_q = "&".join([f"location={t}" for t in tokens])
        new_query = loc_q if not existing_q else f"{existing_q}&{loc_q}"
        return urlunparse((p.scheme, p.netloc, p.path, "", new_query, ""))

    # -------------- harvesting + pagination --------------
    async def _harvest_and_parse(self, page: Page) -> None:
        total = 0
        all_hrefs_seen: Set[str] = set()
        page_pass = 1

        while True:
            # Seed with what's visible
            await self._progressive_scroll(page)
            seed = await self._collect_visible_job_links(page)
            for href, *_ in seed:
                all_hrefs_seen.add(href)

            # Pre-expand aggressively: keep opening ~10-per-click until we have enough
            for _ in range(10):
                if len(all_hrefs_seen) >= self.max_jobs:
                    break
                await self._dismiss_overlays(page)
                moved = await self._click_show_more_until_new_href(page, all_hrefs_seen)
                if not moved:
                    break

            # After pre-expansion, collect the current visible snapshot to process
            snapshot = await self._collect_visible_job_links(page)
            self.logger.info(
                f"[Pass {page_pass}] Visible links: {len(snapshot)} | Total unique seen: {len(all_hrefs_seen)}"
            )

            # Process the visible ones (skip already processed)
            for href, title_text, dept_hint, loc_hint in snapshot:
                if total >= self.max_jobs:
                    break
                if href in self.seen_urls:
                    continue
                job = await self._parse_detail_in_new_tab(
                    page, href, title_hint=title_text, dept_hint=dept_hint, loc_hint=loc_hint
                )
                await page.wait_for_timeout(self.sleep_after_open_ms)
                if job:
                    self.scraped.append(job)
                    self.seen_urls.add(job["url"])
                    total += 1
                    self.logger.info(f"✔ [{total}] {job['title']} ({job.get('location','')})")

            if total >= self.max_jobs:
                self.logger.info("Reached max_jobs; stopping.")
                break

            # Try to move to next batch (post-processing pagination)
            await self._dismiss_overlays(page)
            moved = await self._click_show_more_until_new_href(page, all_hrefs_seen)
            if moved:
                page_pass += 1
                continue

            self.logger.info("No further new links after 'Show more'; stopping pagination.")
            break

    async def _click_show_more_until_new_href(self, page: Page, known: Set[str]) -> bool:
        try:
            old = len(known)

            # Try several attempts in case overlays block or virtualization is slow
            for attempt in range(5):
                await self._progressive_scroll(page)
                await self._dismiss_overlays(page)

                btn = page.locator(
                    "button:has-text('Show more openings'), button:has-text('Show more')"
                )
                if await btn.count():
                    await btn.first.scroll_into_view_if_needed()
                    try:
                        await btn.first.click(timeout=5000)
                    except Exception:
                        try:
                            await btn.first.evaluate("b => b.click()")
                        except Exception:
                            pass

                # Poll for newly materialized links (virtualized list renders in chunks)
                for _ in range(30):
                    await self._progressive_scroll(page)
                    for href, *_ in await self._collect_visible_job_links(page):
                        known.add(href)
                    if len(known) > old:
                        self.logger.info(f"Show more: unique hrefs {old} → {len(known)}")
                        return True
                    await asyncio.sleep(0.25)

            return False
        except Exception as e:
            self.logger.debug(f"Show more check failed: {e}")
            return False
   

    async def _progressive_scroll(self, page: Page) -> None:
        try:
            for _ in range(10):
                await page.mouse.wheel(0, 1400)
                await asyncio.sleep(0.10)
            await page.mouse.wheel(0, -800)
            await asyncio.sleep(0.08)
        except Exception:
            pass

    # -------------- detail parsing --------------
    async def _parse_detail_in_new_tab(
        self,
        listing_page: Page,
        url: str,
        title_hint: Optional[str] = None,
        dept_hint: Optional[str] = None,
        loc_hint: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        ctx = listing_page.context
        p = await ctx.new_page()
        try:
            await p.goto(url)
            await p.wait_for_load_state("networkidle")

            title = await self._get_text(p, self.sel_detail_title) or title_hint or "Untitled"

            # --- IMPORTANT: Prefer the list-page department ("Sub-Team") ---
            department = dept_hint or await self._extract_department(p)

            # --- locations ---
            locs = await self._extract_locations(p, dept_exclude=department)
            if not locs and loc_hint:
                loc_text = re.sub(r"\s+", " ", loc_hint).strip()
                if self._looks_valid_city_state(loc_text):
                    locs = [self._normalize_city_state(loc_text)]
            location_field = ", ".join(locs) if locs else ""

            description = await self._extract_description(p)

            job_id = self._make_job_id_from_url(url)

            return {
                "jobId": job_id,
                "title": title,
                "company": "Uber",
                "department": department or "",
                "location": location_field,
                "url": url,
                "description": description.strip(),
                "source": "Uber",
                "status": "active",
                "scraped_date": now_iso(),
            }
        except Exception as e:
            self.logger.warning(f"Detail parse failed {url}: {e}")
            return None
        finally:
            await p.close()

    # -------- department / locations / description --------
    async def _extract_department(self, page: Page) -> Optional[str]:
        import re

        def clean(x: str) -> str:
            return re.sub(r"\s+", " ", (x or "").strip())

        LABELS = ["Team", "Sub-Team", "Department", "Organization", "Org"]

        # Try multiple selectors for the department section
        try:
            selectors = [
                "main h1 + div",
                "h1 + div", 
                ".css-eBnvrI",
                "div[class*='eBnvrI']",
                "main h1 ~ div:first-of-type"
            ]
            
            for selector in selectors:
                host = await page.query_selector(selector)
                if host:
                    raw = (await host.inner_text() or "").strip()
                    lines = [re.sub(r"\s+", " ", ln.strip()) for ln in raw.splitlines() if ln.strip()]
                    for ln in lines:
                        if "|" in ln:
                            continue
                        if self._looks_valid_city_state(ln):
                            continue
                        if "," in ln and 2 <= len(ln) <= 160:
                            city_state_pattern = r'\b[A-Za-z\s]+,\s*[A-Z]{2}\b|\b[A-Za-z\s]+,\s*Canada\b'
                            if not re.search(city_state_pattern, ln):
                                return ln
                    for ln in lines:
                        if not self._looks_valid_city_state(ln) and len(ln.strip()) > 2:
                            return ln.strip()
        except Exception:
            pass

        # Labeled KV rows
        found: Dict[str, str] = {}
        for label in LABELS:
            try:
                dd = await page.query_selector(f"dl:has(dt:has-text('{label}')) dd")
                if dd:
                    t = clean(await dd.inner_text())
                    if t:
                        found[label] = t
            except Exception:
                pass
        for label in LABELS:
            try:
                row = page.locator(f"xpath=//*[normalize-space(text())='{label}']/following::*[1]")
                if await row.count():
                    t = clean(await row.first.inner_text())
                    if t:
                        found.setdefault(label, t)
            except Exception:
                pass
        if "Team" in found and "Sub-Team" in found:
            return clean(f"{found['Team']}, {found['Sub-Team']}")
        for k in ("Sub-Team", "Team", "Department", "Organization", "Org"):
            if k in found and found[k]:
                return found[k]

        # Near-title fallback
        try:
            nodes = await page.query_selector_all("main h1 ~ div, h1 ~ div")
            for n in nodes[:4]:
                txt = clean(await n.inner_text())
                if not txt or "|" in txt:
                    continue
                if "," in txt and 2 <= len(txt) <= 160 and not self._looks_valid_city_state(txt):
                    return txt
            span = await page.query_selector("main h1 + div span, h1 + div span")
            if span:
                txt = clean(await span.inner_text())
                if "," in txt and 2 <= len(txt) <= 160 and not self._looks_valid_city_state(txt):
                    return txt
        except Exception:
            pass

        return None

    async def _extract_locations(self, page: Page, dept_exclude: Optional[str] = None) -> List[str]:
        locs: List[str] = []

        def find_pairs(text: str) -> List[str]:
            if not text:
                return []
            if dept_exclude:
                text = text.replace(dept_exclude, "")
            pairs = [m.strip() for m in CITY_STATE_FINDALL.findall(text)]
            out = []
            for p in pairs:
                if self._looks_valid_city_state(p):
                    out.append(self._normalize_city_state(p))
            return out

        # Header lines
        try:
            headers = await page.query_selector_all("main h1 ~ div")
            for h in headers[:5]:
                txt = (await h.inner_text() or "").strip()
                locs.extend(find_pairs(txt))
                if locs:
                    break
        except Exception:
            pass

        # Chips
        if not locs:
            try:
                chips = await page.query_selector_all("main h1 ~ div div, main h1 ~ div span, h1 ~ div div, h1 ~ div span")
                for n in chips:
                    t = (await n.inner_text() or "").strip()
                    locs.extend(find_pairs(t))
            except Exception:
                pass

        # Labeled row
        if not locs:
            try:
                dd = await page.query_selector("dl:has(dt:has-text('Location')) dd")
                if dd:
                    txt = (await dd.inner_text() or "").strip()
                    locs.extend(find_pairs(txt))
            except Exception:
                pass

        # JSON-LD
        if not locs:
            try:
                scripts = await page.query_selector_all("script[type='application/ld+json']")
                for s in scripts:
                    raw = (await s.inner_text() or "").strip()
                    if not raw:
                        continue
                    data = json.loads(raw)
                    items = data if isinstance(data, list) else [data]
                    for d in items:
                        jl = d.get("jobLocation")
                        if not jl:
                            continue
                        jl_list = jl if isinstance(jl, list) else [jl]
                        for j in jl_list:
                            addr = j.get("address") or {}
                            city = (addr.get("addressLocality") or "").strip()
                            region = (addr.get("addressRegion") or "").strip()
                            country = (addr.get("addressCountry") or "").strip()
                            if city:
                                reg = self._expand_region(region, country) if region else ""
                                candidate = f"{city}, {reg}" if country.upper() == "US" and reg else (
                                    f"{city}, Canada" if country.upper() in ("CA", "CANADA") else ""
                                )
                                if candidate and self._looks_valid_city_state(candidate):
                                    locs.append(candidate)
            except Exception:
                pass

        seen, out = set(), []
        for x in locs:
            if x and x not in seen:
                seen.add(x)
                out.append(x)
        return out

    def _expand_region(self, region: str, country: str) -> str:
        if not region:
            return region
        region = region.strip()
        if country.upper() == "US":
            return US_STATE_FULL.get(region.upper(), region)
        if country.upper() in ("CA", "CANADA"):
            return "Canada"
        return region

    async def _extract_description(self, page: Page) -> str:
        import json
        import re

        # JSON-LD (often contains HTML)
        try:
            scripts = await page.query_selector_all("script[type='application/ld+json']")
            best = ""
            for s in scripts:
                raw = (await s.inner_text() or "").strip()
                if not raw:
                    continue
                data = json.loads(raw)
                items = data if isinstance(data, list) else [data]
                for d in items:
                    desc = d.get("description")
                    if not desc:
                        continue
                    text = self._html_to_text(str(desc))
                    if len(text) > len(best):
                        best = text
            if best:
                return best
        except Exception:
            pass

        # DOM: try section starting from "About the Role"
        try:
            about = page.get_by_text(re.compile(r"\bAbout the Role\b", re.I))
            if await about.count():
                node = about.first
                section = node.locator("xpath=ancestor::*[self::section or self::article][1]")
                el = section if await section.count() else page.locator("main")
                html = await el.inner_html()
                return self._html_to_text(html)
        except Exception:
            pass

        # General DOM fallbacks
        for sel in ("main article", "article", "main"):
            try:
                el = await page.query_selector(sel)
                if el:
                    html = await el.inner_html()
                    return self._html_to_text(html)
            except Exception:
                pass

        return ""


    # -------------- helpers --------------
    async def _get_text(self, page: Page, selector: str) -> Optional[str]:
        try:
            el = await page.query_selector(selector)
            if not el:
                return None
            txt = (await el.inner_text() or "").strip()
            return re.sub(r"\s+", " ", txt)
        except Exception:
            return None

    def _abs(self, page: Page, href: str) -> str:
        if href.startswith("http"):
            return href
        u = page.url
        parsed = urlparse(u)
        return urljoin(f"{parsed.scheme}://{parsed.netloc}", href)

    def _make_job_id_from_url(self, url: str) -> str:
        try:
            slug = urlparse(url).path.rstrip("/").split("/")[-1]
            if slug.isdigit():
                return f"Uber_{slug}"
            return f"Uber_{slug or 'UNKNOWN'}"
        except Exception:
            return "Uber_UNKNOWN"

    # --- location validators/normalizers ---
    def _normalize_city_state(self, text: str) -> str:
        t = re.sub(r"\s+", " ", (text or "")).strip()
        m = re.match(r"^([A-Za-z .’'&()/-]+),\s*([A-Za-z]{2})$", t)
        if m:
            city, st = m.group(1), m.group(2).upper()
            return f"{city}, {US_STATE_FULL.get(st, st)}"
        return t

    def _region_is_allowed(self, region: str) -> bool:
        r = re.sub(r"\s+", " ", (region or "")).strip()
        if not r:
            return False
        if r in US_STATE_NAMES:
            return True
        if r.lower() == "canada":
            return True
        return False

    def _looks_valid_city_state(self, text: str) -> bool:
        t = re.sub(r"\s+", " ", (text or "")).strip()
        if not t or len(t) > 80:
            return False
        m = re.match(r"^([A-Za-z0-9 .’'&()/-]+),\s*([A-Za-z0-9 .’'&()/-]+)$", t)
        if not m:
            return False
        city = m.group(1).strip().lower()
        region = m.group(2).strip()
        if any(tok in city.split() for tok in BAD_CITY_TOKENS):
            return False
        return self._region_is_allowed(region)

    async def _extract_row_meta_from_list(self, page: Page, link_el) -> Tuple[Optional[str], Optional[str]]:
        """
        From the list row containing the given job link, extract:
        dept_hint: the value under the 'Sub-Team' label (fallback to 'Team')
        loc_hint:  the value under the 'Location' label, normalized to 'City, State' pairs
        """
        import re

        dept_hint: Optional[str] = None
        loc_hint: Optional[str] = None

        def clean(x: str) -> str:
            x = (x or "").strip()
            x = re.sub(r"\s+", " ", x)
            return "" if x in {"—", "-", "— —"} else x

        # 1) Find the enclosing *data row* for this link (avoid header rows)
        try:
            row = link_el.locator("xpath=ancestor::*[@role='row' and not(.//*[@role='columnheader'])][1]")
            if not await row.count():
                # fallback: nearest block with the link but not a header
                row = link_el.locator("xpath=ancestor::*[.//a[@href] and not(.//*[@role='columnheader'])][1]")
        except Exception:
            row = link_el

        # Helper: pick the text that’s in the cell *after* a label inside this row
        async def value_after_label(label: str) -> Optional[str]:
            try:
                lab = row.locator(f"xpath=.//*[normalize-space(text())='{label}']").first
                if not await lab.count():
                    return None
                # in Uber’s markup the value is in the immediate following sibling block
                val_block = lab.locator("xpath=following-sibling::*[1]")
                if not await val_block.count():
                    return None
                txt = clean(await val_block.first.inner_text())
                return txt or None
            except Exception:
                return None

        # 2) Department from Sub-Team first, then Team (but never a location)
        cand = await value_after_label("Sub-Team")
        if not cand:
            # tolerate slight label variants
            for alt in ("Subteam", "Sub team"):
                cand = await value_after_label(alt)
                if cand:
                    break
        if not cand:
            cand = await value_after_label("Team")

        if cand and "Multiple Locations" not in cand and "|" not in cand and not self._looks_valid_city_state(cand):
            dept_hint = cand

        # 3) Location from 'Location' cell; normalize to 'City, State' and join with ' | '
        loc_raw = await value_after_label("Location")
        if loc_raw and "Multiple Locations" not in loc_raw:
            pairs = [m.strip() for m in CITY_STATE_FINDALL.findall(loc_raw)]
            norm: List[str] = []
            for p in pairs:
                if self._looks_valid_city_state(p):
                    norm.append(self._normalize_city_state(p))
            # de-dupe, preserve order
            seen, out = set(), []
            for x in norm:
                if x and x not in seen:
                    seen.add(x)
                    out.append(x)
            if out:
                loc_hint = " | ".join(out)

        return dept_hint, loc_hint

    def _html_to_text(self, html_str: str) -> str:
        import re
        import html as ihtml

        if not html_str:
            return ""

        s = ihtml.unescape(html_str)

        s = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", s)
        s = re.sub(r"(?i)<br\s*/?>", "\n", s)
        s = re.sub(r"(?i)</(p|div|section|article|h\d|li|ul|ol|blockquote)>", "\n", s)
        s = re.sub(r"(?i)<li[^>]*>", "• ", s)
        s = re.sub(r"<[^>]+>", " ", s)

        s = s.replace("\r", "")
        s = re.sub(r"[ \t]+\n", "\n", s)
        s = re.sub(r"\n{2,}", "\n", s)
        s = re.sub(r"[ \t]{2,}", " ", s)

        s = re.split(
            r"\n(?:For US[- ]?based roles|Benefits|EEO|Equal Opportunity|Accommodations)\b",
            s, maxsplit=1, flags=re.I
        )[0]

        return s.strip()

    async def _collect_visible_job_links(self, page: Page):
        links = page.locator(self.sel_results_link)
        cnt = await links.count()
        out = []
        seen_local = set()
        for i in range(cnt):
            el = links.nth(i)
            href = await el.get_attribute("href")
            if not href:
                continue
            href = self._abs(page, href)
            if href in seen_local:
                continue
            seen_local.add(href)

            title_text = (await el.inner_text() or "").strip()
            dept_hint, loc_hint = await self._extract_row_meta_from_list(page, el)
            out.append((href, title_text, dept_hint, loc_hint))
        return out

    async def _dismiss_overlays(self, page: Page) -> None:
        for sel in [
            "button[aria-label='Close']",
            "button[aria-label*='close' i]",
            "button:has-text('×')",
            "div[role='dialog'] button:has-text('Close')",
            "div[aria-label*='feedback' i] button",
        ]:
            try:
                btn = page.locator(sel)
                if await btn.count():
                    await btn.first.click(timeout=1500)
                    await page.wait_for_timeout(150)
            except Exception:
                pass
        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass

        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass

