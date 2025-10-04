import asyncio
import html
import logging
import json
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Set
from urllib.parse import urlencode, quote, urlparse, urljoin, parse_qs

from playwright.async_api import async_playwright, Page, TimeoutError as PlaywrightTimeoutError

REQ_RE = re.compile(r"\bID\s*[:\-]?\s*(\d{5,7})\b", re.I)
REQ_JSON_RE = re.compile(r'"(req(?:uisition)?(?:_?id)?)"\s*:\s*"?(?P<num>\d{5,7})"?', re.I)

def now_iso() -> str:
    return datetime.now().isoformat()

def _stable_md5(text: str) -> str:
    import hashlib
    return hashlib.md5(text.encode('utf-8')).hexdigest()

class LyftScraper:

    def __init__(self, website_config: Dict[str, Any], global_config: Dict[str, Any]):
        self.cfg = website_config
        self.g = global_config
        self.logger = logging.getLogger("job_tracker")

        sc = self.cfg.get("scraping_config", {})
        self.locations: List[str] = [str(x).strip() for x in sc.get("locations", [])]
        self.max_jobs = sc.get("max_jobs", 500)
        self.sleep_after_nav_ms = sc.get("sleep_after_nav_ms", 1200)
        self.sleep_after_open_ms = sc.get("sleep_after_open_ms", 700)

        wp = self.cfg.get("playwright_options", {})
        self.sel_results = wp.get("results_container", "[data-testid='core-ui-layout-container']")
        self.sel_card_anchor = wp.get(
            "card_anchor_selector",
            "a[target='_blank'][href*='app.careerpuck.com/job-board/lyft/job']"
        )

        self.sel_detail_title = wp.get("detail_title", "h1")
        self.sel_detail_location = wp.get("detail_location", "[data-testing*='Location']")
        self.sel_detail_req_id = wp.get("detail_req_id", "[data-testing*='Requisition']")
        self.sel_detail_desc = wp.get("detail_description", "[data-testing='job-description']")
        self.sel_detail_time = wp.get("detail_time", "time[datetime]")

        pg = self.g["playwright_global"]
        self.timeout = pg["timeout"]
        self.user_agent = pg["user_agent"]
        self.headless = self.g["global_settings"]["headless"]

        self.base_url = self.cfg["website_info"]["base_url"]
        self.scraped, self.seen_urls = [], set()
        self.detail_timeout = max(self.timeout, 60000)
        self.detail_retries = 2

    async def scrape_jobs(self) -> List[Dict[str, Any]]:
        start_time = datetime.now()
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=self.headless,
                    args=["--no-sandbox", "--disable-dev-shm-usage"]
                )
                context = await browser.new_context(user_agent=self.user_agent)
                page = await context.new_page()
                page.set_default_timeout(self.timeout)

                total = 0
                for human_loc in self.locations:
                    if total >= self.max_jobs: break
                    ok = await self._open_listing_for_location(page, human_loc)
                    if not ok:
                        continue

                    hrefs = await self._snapshot_job_hrefs(page)
                    self.logger.info(f"[{human_loc}] found {len(hrefs)} jobs")

                    for href in hrefs:
                        if total >= self.max_jobs: break
                        if href in self.seen_urls:
                            continue
                        job = await self._parse_detail_in_new_tab(page, href, location_hint=human_loc)
                        await page.wait_for_timeout(self.sleep_after_open_ms)
                        if job:
                            self.scraped.append(job)
                            self.seen_urls.add(job["url"])
                            total += 1
                            self.logger.info(f"✔ [{total}] {job['title']} ({job.get('location','')})")

                await context.close()
                await browser.close()

            end_time = datetime.now()
            duration = (end_time - start_time).total_seconds()
            self.scraping_duration = duration
            self.logger.info(f"Scraping duration: {duration:.2f} seconds")
            self.logger.info(f"Lyft scraping finished. Total jobs collected: {len(self.scraped)}")
            return self.scraped

        except Exception as e:
            self.logger.error(f"Lyft scraping error: {e}")
            return self.scraped

    async def _open_listing_for_location(self, page: Page, human_location: str) -> bool:
        try:
            self.logger.info(f"Opening: {self.base_url}")
            await page.goto(self.base_url)
            await page.wait_for_load_state("domcontentloaded")
            await page.wait_for_selector(self.sel_results, timeout=self.timeout)
            await page.wait_for_timeout(self.sleep_after_nav_ms)

            try:
                select = page.locator("select[aria-label*='All location' i]").first
                if await select.count():
                    self.logger.info(f"Selecting location via native <select>: {human_location}")
                    try:
                        # Try exact visible label first
                        await select.select_option(label=human_location)
                    except Exception:
                        # Fallback: choose the first option whose TEXT contains the city (handles combined options)
                        opts = select.locator("option")
                        n = await opts.count()
                        chosen_value = None
                        city_only = human_location.split(",")[0].strip().lower()
                        target_lc = human_location.lower()

                        for i in range(n):
                            o = opts.nth(i)
                            txt = (await o.inner_text() or "").strip()
                            txt_lc = txt.lower()
                            if target_lc in txt_lc or city_only in txt_lc:
                                chosen_value = await o.get_attribute("value")
                                break

                        if chosen_value:
                            await select.select_option(value=chosen_value)
                        else:
                            # Native <select> didn’t have a matching label/value — use combobox contains()
                            self.logger.info(f"No native option for '{human_location}', switching to combobox contains()")
                            await self._select_location_via_combobox_contains(page, human_location)


                    await page.wait_for_load_state("networkidle")
                    await page.wait_for_timeout(self.sleep_after_nav_ms)

                else:
                    self.logger.info(f"Selecting location via custom combobox: {human_location}")
                    cb = page.locator("div[role='combobox']:not([aria-label*='Language' i])")
                    if await page.get_by_text("All locations", exact=False).count():
                        after_label = page.get_by_text("All locations", exact=False).first.locator(
                            "xpath=following::div[@role='combobox'][1]"
                        )
                        if await after_label.count():
                            cb = after_label

                    if not await cb.count():
                        raise PlaywrightTimeoutError("Location combobox not found")

                    await cb.first.click()
                    try:
                        await cb.first.type(human_location, delay=40)
                    except Exception:
                        pass

                    options = page.locator("ul[role='listbox'] li[role='option']")
                    if await options.filter(has_text=human_location).count():
                        await options.filter(has_text=human_location).first.click()
                    else:
                        await options.filter(has_text=human_location.split(',')[0]).first.click()

                    await page.wait_for_load_state("networkidle")
                    await page.wait_for_timeout(self.sleep_after_nav_ms)

            except PlaywrightTimeoutError as e:
                self.logger.warning(f"Timeout locating/using location filter: {e}")
                return False
            except Exception as e:
                self.logger.warning(f"Filter interaction failed: {e}")
                return False

            await self._expand_all_sections(page)

            if await page.locator(self.sel_card_anchor).count() > 0:
                return True

            await self._progressive_scroll(page)
            await self._expand_all_sections(page)
            return (await page.locator(self.sel_card_anchor).count()) > 0

        except PlaywrightTimeoutError:
            self.logger.warning(f"Timeout applying filter for '{human_location}'")
            return False
        except Exception as e:
            self.logger.warning(f"Filter/navigation error for '{human_location}': {e}")
            return False
        

    async def _snapshot_job_hrefs(self, page: Page) -> List[str]:
        anchors = page.locator(self.sel_card_anchor)
        if await anchors.count() == 0:
            anchors = page.locator("a[href*='app.careerpuck.com/job-board/lyft/job']")
        count = await anchors.count()

        if count == 0:
            await self._expand_all_sections(page)
            await self._progressive_scroll(page)
            anchors = page.locator("a[href*='app.careerpuck.com/job-board/lyft/job']")
            count = await anchors.count()

        hrefs: List[str] = []
        for i in range(count):
            href = await anchors.nth(i).get_attribute("href")
            if href and href.startswith("http"):
                hrefs.append(href)

        seen, out = set(), []
        for u in hrefs:
            if u not in seen:
                seen.add(u); out.append(u)
        return out

    async def _parse_detail_in_new_tab(self, listing_page: Page, url: str, location_hint: str = "") -> Optional[Dict[str, Any]]:
        gh_jid = self._gh_job_id_from_url(url)
        if gh_jid:
            api_rec = await self._fetch_greenhouse_job(listing_page, gh_jid)
            if api_rec:
                if not api_rec.get("location") and location_hint:
                    api_rec["location"] = location_hint
                    api_rec["id"] = _stable_md5(f"Lyft|{api_rec['title']}|{api_rec['location']}|{api_rec['url']}")
                return api_rec

        ctx = listing_page.context
        for attempt in range(self.detail_retries + 1):
            p = await ctx.new_page()
            try:
                p.set_default_timeout(self.detail_timeout)
                await p.goto(url, wait_until="domcontentloaded", timeout=self.detail_timeout)

                for sel in (
                    "#onetrust-accept-btn-handler",
                    "button#onetrust-accept-btn-handler",
                    "button:has-text('Accept All')",
                    "button:has-text('Accept')",
                    "button:has-text('Allow all')",
                ):
                    btn = p.locator(sel)
                    if await btn.count():
                        try:
                            await btn.first.click(); await asyncio.sleep(0.2); break
                        except:
                            pass

                await p.wait_for_selector(self.sel_detail_desc + ", " + self.sel_detail_title, timeout=self.detail_timeout)
                await p.wait_for_load_state("networkidle")
                await p.wait_for_timeout(250)

                title = await self._get_text(p, self.sel_detail_title) or self._derive_title_from_url(url)
                location = await self._extract_location(p) or location_hint or ""
                description = await self._get_text(p, self.sel_detail_desc) or ""
                posted_date = await self._extract_posted_date(p)

                req_id = await self._extract_req_id(p) or self._slug_token(url)
                return {
                    "jobId": f"LYFT_{req_id}",
                    "title": title,
                    "company": "Lyft",
                    "location": location,
                    "url": url,
                    "description": description.strip(),
                    "posted_date": posted_date,
                    "source": "lyft",
                    "status": "active",
                    "id": _stable_md5(f"Lyft|{title}|{location}|{url}"),
                    "scraped_date": now_iso(),
                }

            except Exception as e:
                self.logger.warning(f"[Lyft detail attempt {attempt+1}/{self.detail_retries+1}] {url} -> {e}")
                try: await p.close()
                except: pass
                if attempt < self.detail_retries:
                    await asyncio.sleep(0.6)
                    continue
                return None
            finally:
                try: await p.close()
                except: pass

    async def _get_text(self, page: Page, selector: str) -> Optional[str]:
        try:
            el = await page.query_selector(selector)
            if not el:
                return None
            txt = (await el.inner_text() or "").strip()
            return re.sub("\\s+", " ", txt)
        except:
            return None

    async def _extract_posted_date(self, page: Page) -> Optional[str]:
    # 1) <time datetime="YYYY-MM-DD">
        try:
            el = await page.query_selector(self.sel_detail_time)
            if el:
                dt = await el.get_attribute("datetime")
                if dt and re.match(r"^\d{4}-\d{2}-\d{2}", dt):
                    return dt[:10]
        except:
            pass
        # 2) Text like "Posted September 5, 2025" or "Posted on ..."
        txt = await self._get_text(page, "[data-testing*='Posted'], .posted-date, .job-posted, header, main")
        if txt:
            m = re.search(r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\s+\d{1,2},\s+20\d{2}", txt, re.I)
            if m:
                raw = m.group(0).replace("Sept", "Sep")
                for fmt in ("%b %d, %Y", "%B %d, %Y"):
                    try:
                        return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
                    except:
                        pass
        return None

    async def _extract_req_id(self, page: Page) -> Optional[str]:
        txt = await self._get_text(page, self.sel_detail_req_id)
        if txt:
            m = re.search("\\bID\\s*:\\s*(\\d{4,10})\\b", txt, re.I)
            if m: return m.group(1)

        header_txt = await self._get_text(page, "[class*='HeaderSubtitle'], [class*='HeaderContainer'], header, .header")
        if header_txt:
            m = re.search("\\bID\\s*:\\s*(\\d{4,10})\\b", header_txt, re.I)
            if m: return m.group(1)
        return None

    async def _extract_location(self, page: Page) -> Optional[str]:
        for sel in [
            self.sel_detail_location,
            "[data-testing*='Location'] h3, [data-testing*='Location']",
            "div[class*='Location'] h3, div[class*='Location']",
            "div:has(> h3:has-text(',')) h3",
        ]:
            txt = await self._get_text(page, sel)
            if txt and re.search(r",\s*[A-Za-z]{2}\b|Canada", txt):
                return re.sub(r"\s+", " ", re.sub(r"^Location\s*:\s*", "", txt, flags=re.I)).strip()

        txt = await self._get_text(page, "h1 + div, h1 + p, h1 ~ p")
        if txt and re.search(r",\s*[A-Za-z]{2}\b|Canada", txt):
            return re.sub(r"\s+", " ", txt).strip()

        body = await self._get_text(page, "main, article, body")
        if body:
            m = re.search(r"\b([A-Za-z][A-Za-z .'-]+),\s*([A-Z]{2}|Canada)\b", body)
            if m:
                return f"{m.group(1).strip()}, {m.group(2).strip()}"

        return None

    def _slug_token(self, url: str) -> str:
        p = urlparse(url)
        path_last = p.path.rstrip("/").split("/")[-1]
        m = re.search(r"(\d{6,12})", path_last)
        if m: return m.group(1)
        q = p.query or ""
        m = re.search(r"gh_jid=(\d{6,12})", q)
        return m.group(1) if m else (path_last or "UNKNOWN")

    def _derive_title_from_url(self, url: str) -> str:
        slug = urlparse(url).path.rstrip("/").split("/")[-1]
        t = slug.replace("---", " — ").replace("--", " – ")
        t = re.sub("[-_]+", " ", t)
        return re.sub("\\s+", " ", t).strip().title()

    async def _expand_all_sections(self, page: Page) -> None:
        try:
            toggles = page.locator("button[aria-controls][aria-expanded='false']")
            count = await toggles.count()
            for i in range(count):
                btn = toggles.nth(i)
                try:
                    await btn.scroll_into_view_if_needed()
                    await btn.click()
                    await asyncio.sleep(0.15)
                except:
                    pass
            await page.wait_for_timeout(400)
        except:
            pass

    def _gh_job_id_from_url(self, url: str) -> str:
        q = parse_qs(urlparse(url).query or "")
        if "gh_jid" in q and len(q["gh_jid"]) > 0:
            return q["gh_jid"][0]
        m = re.search(r"(\d{6,12})", urlparse(url).path)
        return m.group(1) if m else ""

    async def _fetch_greenhouse_job(self, page: Page, gh_jid: str) -> Optional[Dict[str, Any]]:
        try:
            api = f"https://boards-api.greenhouse.io/v1/boards/lyft/jobs/{gh_jid}"
            resp = await page.request.get(api)
            if not resp.ok:
                self.logger.debug(f"[GH API] {api} -> {resp.status}")
                return None
            payload = await resp.json()

            title = (payload.get("title") or "").strip()
            loc_obj = payload.get("location") or {}
            location = (loc_obj.get("name") or "").strip()

            raw_html = payload.get("content") or ""
            desc_text = self._html_to_text(raw_html)

            updated = payload.get("updated_at") or ""
            posted_date = None
            m = re.match(r"^(\d{4}-\d{2}-\d{2})", updated)
            if m:
                posted_date = m.group(1)

            absolute = (payload.get("absolute_url") or "").strip()
            req_id = ""

            for key in ("internal_job_id", "requisition_id"):
                v = str(payload.get(key) or "").strip()
                if v.isdigit() and 5 <= len(v) <= 7:
                    req_id = v
                    break

            if not req_id:
                for item in (payload.get("metadata") or []):
                    name = (str(item.get("name") or "")).lower()
                    val  = str(item.get("value") or "").strip()
                    if ("req" in name or "id" in name) and val.isdigit() and 5 <= len(val) <= 7:
                        req_id = val
                        break

            if not req_id:
                cp_url = (payload.get("absolute_url") or "").strip()
                if not cp_url:
                    cp_url = f"https://app.careerpuck.com/job-board/lyft/job/{gh_jid}?gh_jid={gh_jid}"
                req_id = await self._fetch_req_id_via_http(page, cp_url)

            if req_id and not (5 <= len(req_id) <= 7):
                req_id = ""

            if not req_id:
                req_id = str(gh_jid)

            record = {
                "jobId": f"LYFT_{req_id}",
                "gh_jid": str(gh_jid),
                "title": title,
                "company": "Lyft",
                "location": location,
                "url": absolute or f"https://app.careerpuck.com/job-board/lyft/job/{gh_jid}?gh_jid={gh_jid}",
                "description": desc_text,
                "posted_date": posted_date,
                "source": "lyft",
                "status": "active",
                "scraped_date": now_iso(),
            }
            return record
        except Exception as e:
            self.logger.debug(f"[GH API] fetch error for {gh_jid}: {e}")
            return None


    def _html_to_text(self, html_str: str) -> str:
        if not html_str:
            return ""
        s = html.unescape(html_str)
        s = re.sub(r"(?i)</p\s*>", "\n\n", s)
        s = re.sub(r"(?i)<br\s*/?>", "\n", s)
        s = re.sub(r"<[^>]+>", " ", s)
        s = re.sub(r"\s+\n", "\n", s)
        s = re.sub(r"\n{3,}", "\n\n", s)
        s = re.sub(r"[ \t]{2,}", " ", s)
        return s.strip()

    def _gh_job_id_from_url(self, url: str) -> str:
        q = parse_qs(urlparse(url).query or "")
        if "gh_jid" in q and q["gh_jid"]:
            return q["gh_jid"][0]
        m = re.search(r"(\d{6,12})", urlparse(url).path or "")
        return m.group(1) if m else ""

    
    async def _fetch_req_id_via_http(self, page: Page, url: str) -> str:
        try:
            resp = await page.request.get(url, timeout=30000)
            if resp.ok:
                raw = await resp.text()
                txt = html.unescape(raw)
                plain = re.sub(r"<[^>]+>", " ", txt)
                plain = re.sub(r"\s+", " ", plain).strip()
                m = REQ_RE.search(plain)
                if m:
                    return m.group(1)
                m = REQ_JSON_RE.search(txt)
                if m:
                    return m.group("num")
            ctx = page.context
            tmp = await ctx.new_page()
            try:
                tmp.set_default_timeout(8000)
                await tmp.goto(url)
                body_text = (await tmp.locator("body").inner_text()).strip()
                m = REQ_RE.search(body_text)
                if m:
                    return m.group(1)
            finally:
                await tmp.close()
        except Exception:
            pass
        return ""
        
    async def _select_location_via_combobox_contains(self, page: Page, human_location: str) -> None:
        city_only = human_location.split(",")[0].strip()
        cb = page.locator("div[role='combobox']:not([aria-label*='Language' i])")
        if await page.get_by_text("All locations", exact=False).count():
            after_label = page.get_by_text("All locations", exact=False).first.locator(
                "xpath=following::div[@role='combobox'][1]"
            )
            if await after_label.count():
                cb = after_label
        if not await cb.count():
            raise PlaywrightTimeoutError("Location combobox not found")

        await cb.first.click()
        try:
            await cb.first.type(human_location, delay=40)
        except Exception:
            pass

        options = page.locator("ul[role='listbox'] li[role='option']")
        if await options.filter(has_text=human_location).count():
            await options.filter(has_text=human_location).first.click()
        else:
            await options.filter(has_text=city_only).first.click()

        await page.wait_for_load_state("networkidle")
        await page.wait_for_timeout(self.sleep_after_nav_ms)



