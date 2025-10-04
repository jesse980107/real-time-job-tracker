# scrapers/microsoft/scraper.py

import asyncio
import json
import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Set
from urllib.parse import urljoin, urlparse, urlunparse, quote, urlencode

from playwright.async_api import async_playwright, Page, TimeoutError as PlaywrightTimeoutError


def now_iso() -> str:
    return datetime.now().isoformat()


class MicrosoftScraper:
    """
    Microsoft Careers scraper for jobs.careers.microsoft.com
    """

    def __init__(self, website_config: Dict[str, Any], global_config: Dict[str, Any]):
        self.cfg = website_config
        self.g = global_config
        self.logger = logging.getLogger("job_tracker")

        sc = self.cfg.get("scraping_config", {})
        self.max_jobs = sc.get("max_jobs", 400)
        self.sleep_after_nav_ms = sc.get("sleep_after_nav_ms", 1000)
        self.sleep_after_open_ms = sc.get("sleep_after_open_ms", 600)
        self.apply_filters_via_ui = sc.get("apply_filters_via_ui", False)
        self.target_locations: List[str] = sc.get("locations", ["United States", "Canada"])

        wp = self.cfg.get("playwright_options", {})
        self.sel_results_link = wp.get("results_link_selector", "button[aria-label*='Click to see details'], a[href*='/job/']")
        self.sel_show_more = wp.get("show_more_button", "button:has-text('Next'), [aria-label*='next' i]")
        self.sel_detail_title = wp.get("detail_title", "h1")
        self.sel_detail_desc = wp.get("detail_description", "[data-automation-id='jobDescription'], .job-description, main")
        self.sel_job_card = wp.get("job_card_selector", "div[role='listitem'][class*='ms-list-cell']")
        self.sel_see_details = wp.get("see_details_button", "button[aria-label*='Click to see details']")

        pg = self.g["playwright_global"]
        self.timeout = pg["timeout"]
        self.user_agent = pg["user_agent"]
        self.headless = self.g["global_settings"]["headless"]

        self.base_url = self.cfg["website_info"]["base_url"]

        self.scraped: List[Dict[str, Any]] = []
        self.seen_urls: Set[str] = set()

    async def scrape_jobs(self) -> List[Dict[str, Any]]:
        """Main entry point for scraping Microsoft jobs"""
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

                self.logger.info(f"Microsoft max_jobs = {self.max_jobs}")
                
                # Build URL with location filters
                url_with_filters = self._build_url_with_locations(self.base_url, self.target_locations)
                await self._open_list_page(page, url_with_filters)

                await self._harvest_and_parse(page)

                await context.close()
                await browser.close()

            end_time = datetime.now()
            duration = end_time - start_time
            duration_seconds = round(duration.total_seconds(), 2)
            
            self.logger.info(f"Microsoft scraping finished. Total jobs collected: {len(self.scraped)}")
            self.logger.info(f"Scraping duration: {duration_seconds} seconds")
            
            # Add duration info to the scraped data
            self.scraping_duration = duration_seconds
            
            return self.scraped

        except Exception as e:
            end_time = datetime.now()
            duration = end_time - start_time
            duration_seconds = round(duration.total_seconds(), 2)
            
            self.logger.error(f"Microsoft scraping error: {e}")
            self.logger.info(f"Scraping duration (with error): {duration_seconds} seconds")
            
            # Add duration info even on error
            self.scraping_duration = duration_seconds
            
            return self.scraped

    async def _open_list_page(self, page: Page, url: str) -> None:
        """Open the job listings page"""
        self.logger.info(f"Opening: {url}")
        await page.goto(url)
        await page.wait_for_load_state("domcontentloaded")
        await page.wait_for_timeout(self.sleep_after_nav_ms)

    def _build_url_with_locations(self, base: str, locations: List[str]) -> str:
        """Build URL with location filters applied"""
        if not locations:
            return base
        
        # Microsoft uses lc= parameters for location filtering
        params = []
        for loc in locations:
            params.append(f"lc={quote(loc)}")
        
        # Add default parameters
        params.extend([
            "l=en_us",
            "pg=1", 
            "pgSz=20",
            "o=Relevance",
            "flt=true"
        ])
        
        query_string = "&".join(params)
        separator = "&" if "?" in base else "?"
        return f"{base}{separator}{query_string}"

    def _build_url_with_page(self, base: str, page_num: int, locations: List[str]) -> str:
        """Build URL with location filters and specific page number"""
        if not locations:
            locations = []
        
        # Microsoft uses lc= parameters for location filtering
        params = []
        for loc in locations:
            params.append(f"lc={quote(loc)}")
        
        # Add default parameters with specific page
        params.extend([
            "l=en_us",
            f"pg={page_num}", 
            "pgSz=20",
            "o=Relevance",
            "flt=true"
        ])
        
        query_string = "&".join(params)
        separator = "&" if "?" in base else "?"
        return f"{base}{separator}{query_string}"

    async def _harvest_and_parse(self, page: Page) -> None:
        """Harvest job links and parse details"""
        total = 0
        page_num = 1

        while total < self.max_jobs:
            self.logger.info(f"Processing page {page_num}")
            
            # Wait for page to load and add some debugging
            await page.wait_for_timeout(3000)  # Give more time for dynamic content
            
            # Debug: Check what's actually on the page
            page_title = await page.title()
            self.logger.info(f"Page title: {page_title}")
            
            # Check for common elements
            body_text = await page.inner_text("body")
            if "Showing" in body_text:
                showing_match = re.search(r'Showing \d+-\d+ of \d+ results', body_text)
                if showing_match:
                    self.logger.info(f"Found results indicator: {showing_match.group()}")
            
            # Wait for job listings to load with multiple selectors
            job_cards_found = False
            card_selectors = [
                "div[data-automation-id='listCell']",
                "div[role='listitem']", 
                ".ms-list-cell",
                "div[class*='ms-list-cell']",
                "div[class*='listCell']"
            ]
            
            for selector in card_selectors:
                try:
                    await page.wait_for_selector(selector, timeout=5000)
                    count = await page.locator(selector).count()
                    if count > 0:
                        self.logger.info(f"Found {count} job cards with selector: {selector}")
                        job_cards_found = True
                        break
                except PlaywrightTimeoutError:
                    continue
            
            if not job_cards_found:
                self.logger.warning("No job cards found with any selector. Checking page content...")
                
                # Debug: check if there are any buttons or links that might indicate jobs
                buttons = await page.locator("button").count()
                links = await page.locator("a").count()
                self.logger.info(f"Found {buttons} buttons and {links} links on page")
                
                # Check for error messages or redirects
                if "Access Denied" in body_text or "403" in body_text:
                    self.logger.error("Access denied - may need different user agent or headers")
                elif "No results found" in body_text or "0 results" in body_text:
                    self.logger.info("No jobs available for the selected criteria")
                else:
                    self.logger.debug(f"Page content preview: {body_text[:500]}...")
                break

            # Collect job links from current page
            job_links = await self._collect_job_links(page, page_num)
            
            if not job_links:
                self.logger.info("No job links found on current page")
                # Try to go to next page even if no jobs found (might be a temporary issue)
                if total < self.max_jobs:
                    if not await self._go_to_next_page(page):
                        self.logger.info("No more pages available")
                        break
                    page_num += 1
                    continue
                break

            # Process each job on this page
            for i, job_url in enumerate(job_links):
                if total >= self.max_jobs:
                    break
                    
                if job_url in self.seen_urls:
                    continue

                # For testing: try to extract basic info from listing page first
                if job_url.startswith("CARD_INDEX_"):
                    # Parse the new format: CARD_INDEX_{page_num}_{card_index}
                    parts = job_url.split("_")
                    if len(parts) >= 3:
                        card_index = int(parts[-1])  # Get the last part as card index
                    try:
                        # Try to extract basic info from the card on listing page
                        cards = await page.query_selector_all("div[role='listitem']")
                        if card_index < len(cards):
                            card = cards[card_index]
                            
                            # Debug: Log the card's HTML structure (truncated)
                            try:
                                card_text = await card.inner_text()
                                self.logger.debug(f"Card {card_index} text: {card_text[:200]}...")
                            except Exception:
                                pass
                            
                            # Extract title from card - try multiple selectors
                            title = "Unknown Title"
                            title_selectors = [
                                "h2", "h3", "h4",
                                "[class*='title']", 
                                "[data-automation-id*='title']",
                                "a", "button[aria-label*='Click to see details']"
                            ]
                            
                            for sel in title_selectors:
                                try:
                                    title_el = await card.query_selector(sel)
                                    if title_el:
                                        if sel == "button[aria-label*='Click to see details']":
                                            aria_label = await title_el.get_attribute("aria-label")
                                            if aria_label and "Click to see details for" in aria_label:
                                                title = aria_label.replace("Click to see details for", "").strip()
                                                self.logger.debug(f"Extracted title from aria-label: {title}")
                                                break
                                        else:
                                            title_text = await title_el.inner_text()
                                            if title_text and len(title_text.strip()) > 3:
                                                title = title_text.strip()
                                                self.logger.debug(f"Extracted title from {sel}: {title}")
                                                break
                                except Exception as e:
                                    self.logger.debug(f"Failed to extract title with {sel}: {e}")
                                    continue
                            
                            # Extract location from card if available
                            location = ""
                            location_selectors = [
                                "p:has-text('United States')", 
                                "p:has-text('Canada')", 
                                "[class*='location']",
                                "p", "div p", "span"
                            ]
                            
                            # First try specific selectors
                            for sel in location_selectors:
                                try:
                                    location_el = await card.query_selector(sel)
                                    if location_el:
                                        loc_text = await location_el.inner_text()
                                        if loc_text and ("United States" in loc_text or "Canada" in loc_text or "," in loc_text):
                                            location = loc_text.strip()
                                            self.logger.debug(f"Extracted location from {sel}: {location}")
                                            break
                                except Exception as e:
                                    self.logger.debug(f"Failed to extract location with {sel}: {e}")
                                    continue
                            
                            # If no location found, extract from card text using patterns
                            if not location:
                                try:
                                    card_text = await card.inner_text()
                                    # Look for location patterns in the text
                                    location_patterns = [
                                        r'([^,\n]+,\s*(?:United States|Canada))',
                                        r'(Multiple Locations,\s*(?:United States|Canada))',
                                        r'([A-Za-z\s]+,\s*[A-Za-z\s]+,\s*(?:United States|Canada))'
                                    ]
                                    
                                    for pattern in location_patterns:
                                        match = re.search(pattern, card_text)
                                        if match:
                                            location = match.group(1).strip()
                                            self.logger.debug(f"Extracted location from text pattern: {location}")
                                            break
                                except Exception as e:
                                    self.logger.debug(f"Failed to extract location from text: {e}")
                            
                            # If we couldn't extract title properly, use the full card text as fallback
                            if title == "Unknown Title":
                                try:
                                    card_text = await card.inner_text()
                                    lines = [line.strip() for line in card_text.split('\n') if line.strip()]
                                    if lines:
                                        # Take the first substantial line as title
                                        for line in lines:
                                            if len(line) > 5 and not line.lower().startswith(('today', 'posted', 'see details')):
                                                title = line[:100]  # Limit length
                                                break
                                except Exception:
                                    pass
                            
                            # Try to get the real job URL by finding and clicking the "See details" button
                            real_job_url = f"https://jobs.careers.microsoft.com/job/temp_{total + 1}"  # Default fallback
                            description = "Description extraction failed"
                            metadata = {}
                            
                            try:
                                # Try multiple selectors to find the button/link to job details
                                button_selectors = [
                                    "button[aria-label*='Click to see details']",
                                    "button:has-text('See details')",
                                    "a:has-text('See details')",
                                    "button[class*='seeDetails']",
                                    "button[class*='see-details']",
                                    ".ms-Button:has-text('See details')",
                                    "[role='button']:has-text('See details')"
                                ]
                                
                                see_details_element = None
                                for selector in button_selectors:
                                    try:
                                        element = await card.query_selector(selector)
                                        if element:
                                            see_details_element = element
                                            self.logger.debug(f"Found see details element with selector: {selector}")
                                            break
                                    except Exception:
                                        continue
                                
                                if see_details_element:
                                    # Get the aria-label to extract job info
                                    aria_label = await see_details_element.get_attribute("aria-label")
                                    if aria_label:
                                        self.logger.debug(f"Found see details element with aria-label: {aria_label}")
                                    
                                    # Navigate to job detail page
                                    try:
                                        # Remember current URL and scroll position
                                        current_url = page.url
                                        
                                        # Scroll the element into view and click
                                        await see_details_element.scroll_into_view_if_needed()
                                        await page.wait_for_timeout(500)  # Let it settle
                                        
                                        # Click the element
                                        await see_details_element.click()
                                        
                                        # Wait for navigation to job detail page
                                        await page.wait_for_url(lambda url: url != current_url and '/job/' in url, timeout=10000)
                                        
                                        # We're now on the job detail page
                                        real_job_url = page.url
                                        self.logger.info(f"Successfully navigated to job page: {real_job_url}")
                                        
                                        # Extract complete job details from this page
                                        description = await self._extract_description(page)
                                        metadata = await self._extract_metadata(page)
                                        
                                        # Extract real job ID from URL
                                        job_id = self._extract_job_id_from_url(real_job_url)
                                        
                                        # Wait a bit for any dynamic content to load
                                        await page.wait_for_timeout(1000)
                                        
                                        # Navigate back to listing page
                                        await page.go_back()
                                        await page.wait_for_load_state("networkidle")
                                        
                                        # Verify we're back on the listing page
                                        if page.url == current_url:
                                            self.logger.debug("Successfully returned to listing page")
                                        else:
                                            self.logger.warning("May not have returned to correct listing page")
                                        
                                    except Exception as e:
                                        self.logger.debug(f"Navigation to job detail failed: {e}")
                                        description = f"Navigation failed: {str(e)}"
                                        metadata = {}
                                        
                                        # Try to get back to listing page if we got lost
                                        try:
                                            if '/job/' in page.url:
                                                await page.go_back()
                                                await page.wait_for_load_state("networkidle")
                                        except Exception:
                                            pass
                                        
                                else:
                                    self.logger.debug("No 'see details' element found with any selector")
                                    
                                    # Alternative approach: try to find any clickable element in the card that might lead to job details
                                    try:
                                        # Look for any links or buttons in the card
                                        clickable_elements = await card.query_selector_all("a, button, [role='button']")
                                        self.logger.debug(f"Found {len(clickable_elements)} clickable elements in card")
                                        
                                        for element in clickable_elements:
                                            try:
                                                element_text = await element.inner_text()
                                                href = await element.get_attribute("href")
                                                aria_label = await element.get_attribute("aria-label")
                                                
                                                self.logger.debug(f"Clickable element: text='{element_text[:50]}...', href='{href}', aria-label='{aria_label}'")
                                                
                                                # Check if this looks like a job detail link
                                                if (href and '/job/' in href) or \
                                                   (aria_label and 'detail' in aria_label.lower()) or \
                                                   (element_text and 'detail' in element_text.lower()):
                                                    
                                                    if href and '/job/' in href:
                                                        # Direct link to job page
                                                        real_job_url = href if href.startswith('http') else f"https://jobs.careers.microsoft.com{href}"
                                                        
                                                        # Open job page in new tab to extract details
                                                        context = page.context
                                                        job_page = await context.new_page()
                                                        try:
                                                            await job_page.goto(real_job_url)
                                                            await job_page.wait_for_load_state("networkidle")
                                                            
                                                            description = await self._extract_description(job_page)
                                                            metadata = await self._extract_metadata(job_page)
                                                            job_id = self._extract_job_id_from_url(real_job_url)
                                                            
                                                            self.logger.info(f"Successfully extracted details from job page: {real_job_url}")
                                                            
                                                        finally:
                                                            await job_page.close()
                                                        break
                                                        
                                            except Exception as e:
                                                self.logger.debug(f"Error checking clickable element: {e}")
                                                continue
                                                
                                    except Exception as e:
                                        self.logger.debug(f"Error in alternative approach: {e}")
                                    
                            except Exception as e:
                                self.logger.debug(f"Error processing job detail extraction: {e}")
                            
                            # Create a complete job record
                            job_data = {
                                "jobId": f"Microsoft_{job_id if 'job_id' in locals() and job_id != 'UNKNOWN' else f'TEMP_{total + 1}'}",
                                "title": title,
                                "company": "Microsoft",
                                "location": location,
                                "url": real_job_url,
                                "description": description,
                                "source": "Microsoft",
                                "status": "active",
                                "scraped_date": now_iso(),
                            }
                            
                            # Add metadata if available
                            if metadata:
                                job_data.update(metadata)
                            
                            self.scraped.append(job_data)
                            self.seen_urls.add(job_url)
                            total += 1
                            self.logger.info(f"✔ [{total}] {job_data['title']} ({job_data.get('location', '')})")
                                
                    except Exception as e:
                        self.logger.error(f"Error extracting card info for card {card_index}: {e}")
                        import traceback
                        self.logger.debug(f"Full traceback: {traceback.format_exc()}")
                else:
                    # Handle direct URLs normally
                    job = await self._parse_job_detail(page, job_url)
                    if job:
                        self.scraped.append(job)
                        self.seen_urls.add(job_url)
                        total += 1
                        self.logger.info(f"✔ [{total}] {job['title']} ({job.get('location', '')})")

                await page.wait_for_timeout(self.sleep_after_open_ms)

            # After processing all jobs on this page, try to go to next page
            if total < self.max_jobs:
                if not await self._go_to_next_page(page):
                    self.logger.info("No more pages available")
                    break
                page_num += 1
            else:
                # We've reached max_jobs, exit the loop
                break

    async def _collect_job_links(self, page: Page, page_num: int = 1) -> List[str]:
        """Collect job URLs from current page"""
        job_links = []
        
        try:
            # First, let's check what selectors actually exist on the page
            await page.wait_for_timeout(2000)  # Give page time to load
            
            # Method 1: Look for job cards using multiple possible selectors
            card_selectors = [
                "div[data-automation-id='listCell']",
                "div[role='listitem']", 
                ".ms-list-cell",
                "div[class*='ms-list-cell']",
                "div[class*='listCell']"
            ]
            
            cards = []
            for selector in card_selectors:
                try:
                    found_cards = await page.query_selector_all(selector)
                    if found_cards:
                        self.logger.info(f"Found {len(found_cards)} cards with selector: {selector}")
                        cards = found_cards
                        break
                except Exception:
                    continue
            
            if not cards:
                # Fallback: look for any elements containing "See details"
                self.logger.info("No cards found with standard selectors, trying fallback...")
                cards = await page.query_selector_all("*:has(button[aria-label*='Click to see details'])")
            
            for i, card in enumerate(cards):
                try:
                    # Look for "See details" button within the card
                    see_details_selectors = [
                        "button[class*='seeDetailsLink']",
                        "button[aria-label*='Click to see details']",
                        "button:has-text('See details')",
                        "a:has-text('See details')"
                    ]
                    
                    see_details_btn = None
                    for btn_selector in see_details_selectors:
                        try:
                            see_details_btn = await card.query_selector(btn_selector)
                            if see_details_btn:
                                break
                        except Exception:
                            continue
                    
                    if see_details_btn:
                        # Store card index for later processing - include page number to avoid conflicts
                        job_links.append(f"CARD_INDEX_{page_num}_{i}")
                    else:
                        # Try to find direct job links
                        job_link = await card.query_selector("a[href*='/job/']")
                        if job_link:
                            href = await job_link.get_attribute("href")
                            if href:
                                full_url = urljoin(page.url, href)
                                job_links.append(full_url)

                except Exception as e:
                    self.logger.debug(f"Error processing job card {i}: {e}")
                    continue

            # Method 2: If still no links, try direct link extraction
            if not job_links:
                self.logger.info("No job links found in cards, trying direct link extraction...")
                links = await page.query_selector_all("a[href*='/job/']")
                for link in links:
                    href = await link.get_attribute("href")
                    if href:
                        full_url = urljoin(page.url, href)
                        job_links.append(full_url)

            self.logger.info(f"Found {len(job_links)} job links on page")
            return job_links  # Return all job links found on this page

        except Exception as e:
            self.logger.error(f"Error collecting job links: {e}")
            return []

    def _extract_title_from_aria_label(self, aria_label: str) -> Optional[str]:
        """Extract job title from aria-label text"""
        # aria-label format: "Click to see details for {job_title}"
        match = re.search(r"Click to see details for (.+)", aria_label)
        if match:
            return match.group(1).strip()
        return None

    async def _parse_job_detail(self, listing_page: Page, job_ref: str) -> Optional[Dict[str, Any]]:
        """Parse job details by navigating to job detail page"""
        
        try:
            job_url = None
            
            # Handle different job reference types
            if job_ref.startswith("CARD_INDEX_"):
                # This is a card index, we need to click the button
                card_index = int(job_ref.split("_")[-1])
                
                # Find cards again (they might have changed due to page updates)
                card_selectors = [
                    "div[data-automation-id='listCell']",
                    "div[role='listitem']", 
                    ".ms-list-cell",
                    "div[class*='ms-list-cell']",
                    "div[class*='listCell']"
                ]
                
                cards = []
                for selector in card_selectors:
                    try:
                        cards = await listing_page.query_selector_all(selector)
                        if cards:
                            break
                    except Exception:
                        continue
                
                if card_index < len(cards):
                    card = cards[card_index]
                    
                    # First, try to extract the job title to construct the URL
                    title_element = await card.query_selector("h2, h3, [class*='title'], [data-automation-id*='title']")
                    job_title = ""
                    if title_element:
                        job_title = await title_element.inner_text()
                        job_title = job_title.strip()
                    
                    # Find the "See details" button
                    see_details_selectors = [
                        "button[class*='seeDetailsLink']",
                        "button[aria-label*='Click to see details']",
                        "button:has-text('See details')",
                        "a:has-text('See details')"
                    ]
                    
                    see_details_btn = None
                    for btn_selector in see_details_selectors:
                        try:
                            see_details_btn = await card.query_selector(btn_selector)
                            if see_details_btn:
                                break
                        except Exception:
                            continue
                    
                    if see_details_btn:
                        # Try a different approach: extract job URL from the button's behavior
                        context = listing_page.context
                        detail_page = await context.new_page()
                        
                        try:
                            # Method 1: Try to get URL from aria-label or data attributes
                            aria_label = await see_details_btn.get_attribute("aria-label")
                            if aria_label and "Click to see details for" in aria_label:
                                # Extract the job title and try to construct URL
                                title_from_aria = aria_label.replace("Click to see details for", "").strip()
                                self.logger.debug(f"Extracted title from aria-label: {title_from_aria}")
                            
                            # Method 2: Click and wait for new page/navigation
                            try:
                                # Start waiting for a new page to open
                                new_page_promise = context.wait_for_event("page", timeout=5000)
                                await see_details_btn.click()
                                
                                # Check if a new page opened
                                try:
                                    new_page = await new_page_promise
                                    job_url = new_page.url
                                    await new_page.close()
                                    self.logger.debug(f"Got job URL from new page: {job_url}")
                                except Exception:
                                    # No new page opened, check if current page navigated
                                    await listing_page.wait_for_timeout(2000)
                                    current_url = listing_page.url
                                    if "/job/" in current_url:
                                        job_url = current_url
                                        # Navigate back to listing
                                        await listing_page.go_back()
                                        await listing_page.wait_for_load_state("networkidle")
                                        self.logger.debug(f"Got job URL from navigation: {job_url}")
                            
                            except Exception as e:
                                self.logger.debug(f"Click navigation failed: {e}")
                                # Method 3: Try to construct URL from job title
                                if job_title:
                                    # Microsoft job URLs are like: /global/en/job/{id}/{title-slug}
                                    # We'll try to search for this job by title
                                    pass
                            
                            # If we have a job URL, open it in the detail page
                            if job_url:
                                await detail_page.goto(job_url)
                            else:
                                await detail_page.close()
                                return None
                            
                        except Exception as e:
                            self.logger.debug(f"Error with button processing: {e}")
                            await detail_page.close()
                            return None
                    else:
                        return None
                else:
                    return None
            else:
                # Direct URL
                job_url = job_ref
                context = listing_page.context
                detail_page = await context.new_page()
                await detail_page.goto(job_url)

            if not job_url:
                return None

            await detail_page.wait_for_load_state("networkidle")

            # Extract job details
            title = await self._extract_title(detail_page)
            location = await self._extract_location(detail_page)
            description = await self._extract_description(detail_page)
            
            # Extract metadata from the job details section
            metadata = await self._extract_metadata(detail_page)
            
            job_id = self._extract_job_id_from_url(job_url)

            job_data = {
                "jobId": f"Microsoft_{job_id}",
                "title": title or "Unknown Title",
                "company": "Microsoft",
                "location": location or "",
                "url": job_url,
                "description": description or "",
                "source": "Microsoft",
                "status": "active",
                "scraped_date": now_iso(),
            }

            # Add metadata fields
            if metadata:
                job_data.update(metadata)

            await detail_page.close()
            return job_data

        except Exception as e:
            self.logger.warning(f"Failed to parse job detail: {e}")
            if 'detail_page' in locals():
                await detail_page.close()
            return None

    async def _extract_title(self, page: Page) -> Optional[str]:
        """Extract job title from detail page"""
        try:
            title_el = await page.query_selector(self.sel_detail_title)
            if title_el:
                title = await title_el.inner_text()
                return title.strip()
        except Exception as e:
            self.logger.debug(f"Error extracting title: {e}")
        return None

    async def _extract_location(self, page: Page) -> Optional[str]:
        """Extract location from detail page"""
        try:
            # Look for location in the metadata section or near the title
            location_selectors = [
                "p:has-text('Multiple Locations')",
                "[class*='location']",
                "p:text-matches(r'.*,.*United States')",
                "p:text-matches(r'.*,.*Canada')"
            ]
            
            for selector in location_selectors:
                try:
                    el = await page.query_selector(selector)
                    if el:
                        text = await el.inner_text()
                        if text and ("United States" in text or "Canada" in text):
                            return text.strip()
                except Exception:
                    continue

            # Fallback: look in the page text for location patterns
            page_text = await page.inner_text("body")
            location_match = re.search(r'([^,\n]+,\s*(?:United States|Canada))', page_text)
            if location_match:
                return location_match.group(1).strip()

        except Exception as e:
            self.logger.debug(f"Error extracting location: {e}")
        return None

    async def _extract_description(self, page: Page) -> str:
        """Extract job description from Microsoft job detail page"""
        try:
            # Wait for content to load
            await page.wait_for_timeout(2000)
            
            # Microsoft-specific selectors for job descriptions
            desc_selectors = [
                # Try specific content sections first
                "section:has(h2:has-text('Overview'))",
                "div:has(h3:has-text('Overview'))",
                "div:has(h2:has-text('Overview'))",
                "[data-automation-id='jobDescription']",
                ".job-description",
                "section[aria-label*='job description' i]",
                "div[class*='jobDescription']",
                "div[class*='overview']",
                "div[class*='description']",
                # Broader selectors
                "[role='main'] section",
                "main section",
                "main article", 
                "main div",
                "[role='main']",
                "main"
            ]
            
            best_description = ""
            used_selector = ""
            
            # Try each selector to find the best description
            for selector in desc_selectors:
                try:
                    elements = await page.query_selector_all(selector)
                    for element in elements:
                        desc_text = await element.inner_text()
                        if desc_text and len(desc_text.strip()) > len(best_description):
                            # Check if this looks like a job description (not navigation/header text)
                            lower_text = desc_text.lower()
                            if any(keyword in lower_text for keyword in [
                                'responsibilities', 'qualifications', 'requirements', 
                                'experience', 'skills', 'job', 'role', 'position',
                                'we are looking', 'you will', 'the ideal candidate',
                                'azure', 'microsoft', 'team', 'work with', 'develop'
                            ]):
                                best_description = desc_text.strip()
                                used_selector = selector
                                self.logger.debug(f"Found description with selector {selector}: {len(desc_text)} chars")
                                break
                except Exception as e:
                    self.logger.debug(f"Error with selector {selector}: {e}")
                    continue
            
            if best_description:
                self.logger.debug(f"Using selector: {used_selector}")
                
                # Clean up the description with less aggressive filtering
                lines = [line.strip() for line in best_description.split('\n') if line.strip()]
                
                # Find the start of the actual job content
                content_start_idx = 0
                for i, line in enumerate(lines):
                    line_lower = line.lower()
                    # Look for indicators of job content start
                    if any(indicator in line_lower for indicator in [
                        'overview', 'responsibilities', 'qualifications', 'requirements',
                        'we are looking', 'about this role', 'the azure', 'microsoft is',
                        'join our team', 'the ', 'this position'
                    ]):
                        content_start_idx = i
                        break
                
                # Extract content from the start point
                content_lines = lines[content_start_idx:]
                
                # Remove unwanted navigation/metadata lines but be less aggressive
                filtered_lines = []
                skip_phrases = [
                    'apply now', 'save job', 'share this job', 'back to search',
                    'sign in', 'create account', 'job alert', 'similar jobs',
                    'view all jobs', 'search jobs', 'microsoft careers',
                    'cookie preferences', 'privacy policy', 'terms of use',
                    'apply', 'save', 'share job',
                    # Microsoft footer content that should be excluded
                    'english | fr - canada', 'accessibility', 'microsoft data privacy notice',
                    'legal policies', 'contractor roles', 'your privacy choices',
                    '© microsoft', 'privacy and cookies', 'sitemap'
                ]
                
                # Also check for common footer patterns
                footer_patterns = [
                    r'english\s*\|\s*fr\s*-\s*canada',
                    r'©\s*microsoft\s*\d{4}',
                    r'microsoft data privacy notice',
                    r'legal policies',
                    r'contractor roles',
                    r'your privacy choices',
                    r'accessibility'
                ]
                
                for line in content_lines:
                    line_lower = line.lower()
                    
                    # Skip obvious navigation/action elements
                    if any(skip in line_lower for skip in skip_phrases):
                        continue
                    
                    # Skip footer patterns using regex
                    if any(re.search(pattern, line_lower) for pattern in footer_patterns):
                        continue
                        
                    # Skip very short lines (likely navigation) unless they're section headers
                    if len(line) < 4:
                        continue
                    
                    # Keep substantial content
                    if len(line) > 10 or line.endswith(':'):  # Keep section headers
                        filtered_lines.append(line)
                
                # Clean up the final description - remove footer content at the end
                final_text = '\n'.join(filtered_lines[:100])  # First 100 lines
                
                # Post-process to remove any footer content that might have slipped through
                # Split by common footer indicators and take only the first part
                footer_splits = [
                    'English | FR - Canada',
                    'Accessibility\n',
                    'Microsoft Data Privacy Notice',
                    '© Microsoft 20',
                    'Legal policies',
                    'Your Privacy Choices'
                ]
                
                for footer_indicator in footer_splits:
                    if footer_indicator in final_text:
                        final_text = final_text.split(footer_indicator)[0].strip()
                        self.logger.debug(f"Removed footer content after: {footer_indicator}")
                        break
                
                final_description = final_text
                
                if len(final_description) > 100:  # Ensure we have substantial content
                    return final_description
            
            # Fallback: try to get content from specific job sections
            try:
                # Look for the main job content area more specifically
                content_areas = await page.query_selector_all("main, [role='main']")
                for area in content_areas:
                    area_text = await area.inner_text()
                    if len(area_text) > 300:  # Ensure substantial content
                        lines = [line.strip() for line in area_text.split('\n') if line.strip()]
                        
                        # Look for overview or main content
                        start_idx = 0
                        for i, line in enumerate(lines):
                            if any(keyword in line.lower() for keyword in ['overview', 'the azure', 'microsoft', 'responsibilities']):
                                start_idx = i
                                break
                        
                        content_lines = lines[start_idx:start_idx+50]  # Take next 50 lines
                        filtered = [line for line in content_lines if len(line) > 10]
                        
                        # Apply footer filtering to fallback content as well
                        fallback_text = '\n'.join(filtered)
                        
                        # Remove footer content from fallback text
                        footer_splits = [
                            'English | FR - Canada',
                            'Accessibility\n',
                            'Microsoft Data Privacy Notice',
                            '© Microsoft 20',
                            'Legal policies',
                            'Your Privacy Choices'
                        ]
                        
                        for footer_indicator in footer_splits:
                            if footer_indicator in fallback_text:
                                fallback_text = fallback_text.split(footer_indicator)[0].strip()
                                break
                        
                        if len(filtered) > 5 and fallback_text:  # Ensure we have multiple lines of content
                            return fallback_text
            except Exception:
                pass
            
            return "Job description not found on page"

        except Exception as e:
            self.logger.debug(f"Error extracting description: {e}")
            return f"Description extraction error: {str(e)}"

    async def _extract_metadata(self, page: Page) -> Dict[str, str]:
        """Extract metadata fields from Microsoft job detail page"""
        metadata = {}
        
        try:
            # Microsoft-specific metadata fields
            metadata_fields = [
                ("Date posted", "date_posted"),
                ("Work site", "work_site"),
                ("Travel", "travel"),
                ("Role type", "role_type"),
                ("Profession", "profession"),
                ("Discipline", "discipline"),
                ("Employment type", "employment_type"),
                ("Experience level", "experience_level"),
                ("Team", "team"),
                ("Division", "division"),
                ("Location", "location_detail"),
                ("Business group", "business_group")
            ]

            # Method 1: Look for structured data in definition lists (dt/dd)
            for field_name, field_key in metadata_fields:
                try:
                    # Try dt/dd structure first
                    dt_element = await page.query_selector(f"dt:has-text('{field_name}')")
                    if dt_element:
                        dd_element = await dt_element.query_selector("xpath=following-sibling::dd[1]")
                        if dd_element:
                            value = await dd_element.inner_text()
                            if value and value.strip() and len(value.strip()) < 200:  # Ensure it's not the entire page
                                metadata[field_key] = value.strip()
                                self.logger.debug(f"Found {field_key} via dt/dd: {value.strip()}")
                                continue
                except Exception:
                    pass
            
            # Method 2: Look for label-value pairs in divs/spans
            for field_name, field_key in metadata_fields:
                if field_key in metadata:  # Skip if already found
                    continue
                    
                try:
                    # Look for elements containing the field name
                    field_elements = await page.query_selector_all(f"*:has-text('{field_name}')")
                    
                    for field_el in field_elements[:5]:  # Limit to first 5 matches to avoid performance issues
                        try:
                            element_text = await field_el.inner_text()
                            
                            # Skip if this element contains too much text (likely not metadata)
                            if len(element_text) > 500:
                                continue
                            
                            # Case 1: Field and value in same element "Field: Value"
                            if ":" in element_text and len(element_text) < 200:
                                parts = element_text.split(":", 1)
                                if len(parts) == 2 and field_name.lower() in parts[0].lower():
                                    value = parts[1].strip()
                                    if value and len(value) < 100:  # Reasonable length for metadata
                                        metadata[field_key] = value
                                        self.logger.debug(f"Found {field_key} via colon split: {value}")
                                        break
                            
                            # Case 2: Look for value in next sibling
                            parent = await field_el.query_selector("xpath=..")
                            if parent:
                                next_sibling = await parent.query_selector("xpath=following-sibling::*[1]")
                                if next_sibling:
                                    value = await next_sibling.inner_text()
                                    if value and value.strip() and len(value.strip()) < 100:
                                        metadata[field_key] = value.strip()
                                        self.logger.debug(f"Found {field_key} via sibling: {value.strip()}")
                                        break
                                        
                        except Exception:
                            continue
                            
                except Exception:
                    continue

            # Method 3: Extract from page structure - look for Microsoft's specific layout
            try:
                # Try to find structured info sections with more specific selectors
                info_sections = await page.query_selector_all("section[aria-label*='Job details'], div[data-automation-id*='jobDetails'], .job-info, .job-details, div:has(dt)")
                
                for section in info_sections:
                    try:
                        section_text = await section.inner_text()
                        
                        # Skip sections that are too large (likely not metadata sections)
                        if len(section_text) > 1000:
                            continue
                            
                        lines = section_text.split('\n')
                        
                        for i, line in enumerate(lines):
                            line = line.strip()
                            if ':' in line and len(line) < 150:  # Reasonable line length
                                parts = line.split(':', 1)
                                if len(parts) == 2:
                                    label = parts[0].strip().lower()
                                    value = parts[1].strip()
                                    
                                    # Map common labels to our field keys
                                    label_mapping = {
                                        'date posted': 'date_posted',
                                        'work site': 'work_site',
                                        'travel': 'travel',
                                        'role type': 'role_type',
                                        'profession': 'profession',
                                        'employment type': 'employment_type',
                                        'experience level': 'experience_level',
                                        'team': 'team',
                                        'division': 'division',
                                        'business group': 'business_group'
                                    }
                                    
                                    if label in label_mapping and value and len(value) < 100:
                                        field_key = label_mapping[label]
                                        if field_key not in metadata:  # Don't overwrite existing values
                                            metadata[field_key] = value
                                            self.logger.debug(f"Found {field_key} via section parsing: {value}")
                                        
                    except Exception:
                        continue
                        
            except Exception:
                pass

            # Method 4: Microsoft-specific metadata extraction
            try:
                page_content = await page.inner_text("body")
                
                # Look for date posted in specific patterns
                if 'date_posted' not in metadata:
                    date_patterns = [
                        r'Date posted[:\s]+([A-Z][a-z]{2}\s+\d{1,2},\s+\d{4})',
                        r'Posted[:\s]+([A-Z][a-z]{2}\s+\d{1,2},\s+\d{4})',
                        r'(\w{3}\s+\d{1,2},\s+\d{4})'  # General date pattern
                    ]
                    
                    for pattern in date_patterns:
                        match = re.search(pattern, page_content)
                        if match:
                            date_str = match.group(1) if len(match.groups()) > 0 else match.group(0)
                            if len(date_str) < 20:  # Reasonable date length
                                metadata['date_posted'] = date_str
                                self.logger.debug(f"Found date_posted via regex: {date_str}")
                                break
                
                # Extract specific Microsoft job fields using targeted patterns
                microsoft_field_patterns = {
                    'travel': [
                        r'Travel[:\s]*([0-9]+-[0-9]+\s*%)',
                        r'Travel[:\s]*([0-9]+\s*%)',
                        r'Travel[:\s]*([0-9]+-[0-9]+\s*%\s*)',
                    ],
                    'profession': [
                        r'Profession[:\s]*\n\s*([A-Za-z][A-Za-z\s&]{3,50}?)(?:\n|$)',
                        r'Profession[:\s]*([A-Za-z][A-Za-z\s&]{3,50}?)(?:\n|Discipline|Role type|Employment)',
                    ],
                    'employment_type': [
                        r'Employment type[:\s]*\n\s*([A-Za-z][A-Za-z\s-]{3,30}?)(?:\n|$)',
                        r'Employment type[:\s]*([A-Za-z][A-Za-z\s-]{3,30}?)(?:\n|Benefits|Responsibilities)',
                    ],
                    'discipline': [
                        r'Discipline[:\s]*\n\s*([A-Za-z][A-Za-z\s&]{3,50}?)(?:\n|$)',
                        r'Discipline[:\s]*([A-Za-z][A-Za-z\s&]{3,50}?)(?:\n|Role type|Employment)',
                    ],
                    'role_type': [
                        r'Role type[:\s]*\n\s*([A-Za-z][A-Za-z\s]{3,40}?)(?:\n|$)',
                        r'Role type[:\s]*([A-Za-z][A-Za-z\s]{3,40}?)(?:\n|Employment|Profession)',
                    ]
                }
                
                for field_key, patterns in microsoft_field_patterns.items():
                    if field_key not in metadata:
                        for pattern in patterns:
                            match = re.search(pattern, page_content, re.IGNORECASE | re.MULTILINE)
                            if match:
                                value = match.group(1).strip()
                                # Additional cleaning
                                value = re.sub(r'\s+', ' ', value)  # Normalize whitespace
                                
                                if len(value) > 3 and len(value) < 80:  # Reasonable length
                                    metadata[field_key] = value
                                    self.logger.debug(f"Found {field_key} via pattern: {value}")
                                    break
                
                # Look for salary information in the page - DISABLED
                # if 'salary' not in metadata:
                #     salary_patterns = [
                #         r'\$[\d,]+\s*-\s*\$[\d,]+(?:\s*per\s*year)?',
                #         r'USD\s+\$[\d,]+\s*-\s*\$[\d,]+(?:\s*per\s*year)?',
                #         r'The typical base pay range.*?\$[\d,]+\s*-\s*\$[\d,]+'
                #     ]
                #     
                #     for pattern in salary_patterns:
                #         match = re.search(pattern, page_content, re.IGNORECASE)
                #         if match:
                #             salary_text = match.group(0)
                #             # Extract just the salary range
                #             range_match = re.search(r'\$[\d,]+\s*-\s*\$[\d,]+', salary_text)
                #             if range_match:
                #                 metadata['salary'] = range_match.group(0)
                #                 self.logger.debug(f"Found salary via regex: {range_match.group(0)}")
                #                 break
                
                # Extract work arrangement
                if 'work_arrangement' not in metadata:
                    work_patterns = [
                        r'(\d+\s+days?\s*/\s*week\s+in[-\s]office\s*-\s*remote)',
                        r'(\d+\s+days?\s*/\s*week\s+in[-\s]office)',
                        r'(100%\s*remote)',
                        r'(fully\s*remote)',
                        r'(hybrid)',
                        r'(on[-\s]site)',
                        r'(remote\s*work)',
                        r'(work\s*from\s*home)'
                    ]
                    
                    for pattern in work_patterns:
                        match = re.search(pattern, page_content, re.IGNORECASE)
                        if match:
                            work_arr = match.group(1)
                            if len(work_arr) < 50:  # Reasonable length
                                metadata['work_arrangement'] = work_arr
                                self.logger.debug(f"Found work_arrangement via regex: {work_arr}")
                                break
                                
            except Exception as e:
                self.logger.debug(f"Error in Microsoft-specific extraction: {e}")
                pass

            # Method 5: Enhanced Microsoft page structure parsing
            try:
                # Look for job details in common Microsoft page structures
                detail_selectors = [
                    "div[data-automation-id*='jobDetails']",
                    "section[aria-labelledby*='jobDetails']", 
                    "div:has(dt, dd)",
                    ".job-details",
                    ".job-info",
                    "dl"  # Definition lists
                ]
                
                for selector in detail_selectors:
                    try:
                        detail_containers = await page.query_selector_all(selector)
                        for container in detail_containers:
                            # Look for definition list structure (dt/dd pairs)
                            dt_elements = await container.query_selector_all("dt")
                            for dt in dt_elements:
                                try:
                                    dt_text = await dt.inner_text()
                                    dt_text_clean = dt_text.strip().lower()
                                    
                                    # Find corresponding dd element
                                    dd = await dt.query_selector("xpath=following-sibling::dd[1]")
                                    if dd:
                                        dd_text = await dd.inner_text()
                                        dd_value = dd_text.strip()
                                        
                                        if len(dd_value) < 80 and dd_value:  # Reasonable metadata length
                                            # Map the field names
                                            field_mappings = {
                                                'travel': ['travel'],
                                                'profession': ['profession'],
                                                'employment type': ['employment_type'],
                                                'discipline': ['discipline'],
                                                'role type': ['role_type'],
                                                'work site': ['work_site'],
                                                'experience level': ['experience_level'],
                                                'team': ['team'],
                                                'division': ['division']
                                            }
                                            
                                            for field_name, field_keys in field_mappings.items():
                                                if field_name in dt_text_clean:
                                                    field_key = field_keys[0]
                                                    if field_key not in metadata:
                                                        metadata[field_key] = dd_value
                                                        self.logger.debug(f"Found {field_key} via dt/dd: {dd_value}")
                                                    break
                                except Exception:
                                    continue
                    except Exception:
                        continue
                        
            except Exception as e:
                self.logger.debug(f"Error in enhanced structure parsing: {e}")
                pass
                
            # Final fallback for missing fields
            except Exception as e:
                self.logger.debug(f"Error in Microsoft-specific extraction: {e}")
                pass

            # Clean up metadata values
            cleaned_metadata = {}
            for key, value in metadata.items():
                if value and isinstance(value, str):
                    # Remove extra whitespace and newlines
                    cleaned_value = ' '.join(value.split())
                    if cleaned_value and len(cleaned_value.strip()) > 0:
                        cleaned_metadata[key] = cleaned_value.strip()
            
            self.logger.debug(f"Extracted metadata: {list(cleaned_metadata.keys())}")
            return cleaned_metadata

        except Exception as e:
            self.logger.debug(f"Error extracting metadata: {e}")
            return {}

    def _extract_job_id_from_url(self, url: str) -> str:
        """Extract job ID from URL"""
        try:
            # URL format: /global/en/job/{job_id}/{job-title-slug}
            match = re.search(r'/job/(\d+)/', url)
            if match:
                return match.group(1)
        except Exception:
            pass
        return "UNKNOWN"

    async def _go_to_next_page(self, page: Page) -> bool:
        """Navigate to next page"""
        try:
            # Add a small delay before attempting navigation
            await page.wait_for_timeout(1000)
            
            # Look for Next button or pagination
            next_selectors = [
                "button:has-text('Next')",
                "[aria-label*='next' i]",
                "a:has-text('Next')",
                "[title*='next' i]",
                "button[aria-label*='Go to next page' i]"
            ]

            for selector in next_selectors:
                try:
                    next_btn = await page.query_selector(selector)
                    if next_btn:
                        # Check if button is enabled
                        is_disabled = await next_btn.get_attribute("disabled")
                        aria_disabled = await next_btn.get_attribute("aria-disabled")
                        
                        if not is_disabled and aria_disabled != "true":
                            await next_btn.click()
                            await page.wait_for_load_state("networkidle", timeout=15000)
                            await page.wait_for_timeout(self.sleep_after_nav_ms)
                            self.logger.debug(f"Successfully navigated using selector: {selector}")
                            return True
                        else:
                            self.logger.debug(f"Next button found but disabled: {selector}")
                except Exception as e:
                    self.logger.debug(f"Error with selector {selector}: {e}")
                    continue

            # Try pagination numbers (look for current page + 1)
            try:
                current_page_el = await page.query_selector("[aria-current='page'], .current-page")
                if current_page_el:
                    current_page_text = await current_page_el.inner_text()
                    current_page_num = int(current_page_text)
                    next_page_num = current_page_num + 1
                    
                    next_page_link = await page.query_selector(f"a:has-text('{next_page_num}')")
                    if next_page_link:
                        await next_page_link.click()
                        await page.wait_for_load_state("networkidle", timeout=15000)
                        await page.wait_for_timeout(self.sleep_after_nav_ms)
                        self.logger.debug(f"Successfully navigated to page {next_page_num}")
                        return True
            except Exception as e:
                self.logger.debug(f"Error with pagination numbers: {e}")

            self.logger.debug("No next page navigation options found")
            return False

        except Exception as e:
            self.logger.error(f"Error navigating to next page: {e}")
            return False