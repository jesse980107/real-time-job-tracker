# scrapers/meta/scraper.py

import asyncio
import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Set
from urllib.parse import urljoin, urlparse, urlunparse, quote, urlencode

from playwright.async_api import async_playwright, Page, TimeoutError as PlaywrightTimeoutError


def now_iso() -> str:
    return datetime.now().isoformat()


class TeslaScraper:
    """
    Tesla Careers scraper for www.tesla.com/careers
    """

    def __init__(self, website_config: Dict[str, Any], global_config: Dict[str, Any]):
        self.cfg = website_config
        self.g = global_config
        self.logger = logging.getLogger("job_tracker")

        sc = self.cfg.get("scraping_config", {})
        self.max_jobs = sc.get("max_jobs", 400)
        self.sleep_after_nav_ms = sc.get("sleep_after_nav_ms", 2000)
        self.sleep_after_open_ms = sc.get("sleep_after_open_ms", 1000)
        self.apply_filters_via_ui = sc.get("apply_filters_via_ui", False)

        wp = self.cfg.get("playwright_options", {})
        self.sel_results_link = wp.get("results_link_selector", "a[data-testid='job-item-link']")
        self.sel_show_more = wp.get("show_more_button", "button[aria-label*='Next']")
        self.sel_detail_title = wp.get("detail_title", "h1")
        self.sel_detail_desc = wp.get("detail_description", "[data-testid='job-description']")
        self.sel_job_card = wp.get("job_card_selector", "[data-testid='job-item']")
        self.sel_see_details = wp.get("see_details_button", "a[data-testid='job-item-link']")
        self.sel_pagination = wp.get("pagination_button", "button[aria-label*='Next']")
        self.sel_job_summary = wp.get("job_summary_selector", "[data-testid='job-description']")
        self.sel_job_qualifications = wp.get("job_qualifications_selector", ".qualifications")

        pg = self.g["playwright_global"]
        self.timeout = pg["timeout"]
        self.user_agent = pg["user_agent"]
        self.headless = self.g["global_settings"]["headless"]

        self.base_url = self.cfg["website_info"]["base_url"]

        self.scraped: List[Dict[str, Any]] = []
        self.seen_urls: Set[str] = set()

    async def scrape_jobs(self) -> List[Dict[str, Any]]:
        """Main entry point for scraping Tesla jobs"""
        start_time = datetime.now()
        
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=self.headless,
                    args=['--disable-blink-features=AutomationControlled']
                )
                context = await browser.new_context(
                    user_agent=self.user_agent,
                    viewport={'width': 1920, 'height': 1080}
                )
                page = await context.new_page()
                
                # Set default timeout
                page.set_default_timeout(self.timeout)
                
                # Navigate to the main page and start scraping
                await self._open_list_page(page, self.base_url)
                await self._harvest_and_parse(page)
                
                await browser.close()

            end_time = datetime.now()
            duration = end_time - start_time
            duration_seconds = round(duration.total_seconds(), 2)
            
            self.logger.info(f"Tesla scraping finished. Total jobs collected: {len(self.scraped)}")
            self.logger.info(f"Scraping duration: {duration_seconds} seconds")
            
            # Add duration info to the scraped data
            self.scraping_duration = duration_seconds
            
            return self.scraped

        except Exception as e:
            end_time = datetime.now()
            duration = end_time - start_time
            duration_seconds = round(duration.total_seconds(), 2)
            
            self.logger.error(f"Tesla scraping error: {e}")
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

    async def _harvest_and_parse(self, page: Page) -> None:
        """Harvest job links and parse details"""
        total = 0
        page_num = 1

        while total < self.max_jobs:
            self.logger.info(f"Processing page {page_num}")
            
            # Wait for page to load
            await page.wait_for_load_state("domcontentloaded")
            await page.wait_for_timeout(self.sleep_after_nav_ms)
            
            # Collect job links from current page
            job_links = await self._collect_job_links(page, page_num)
            self.logger.info(f"Found {len(job_links)} job links on page {page_num}")
            
            if not job_links:
                self.logger.info("No job links found, stopping pagination")
                break
            
            # Process each job link
            for idx, job_url in enumerate(job_links):
                if total >= self.max_jobs:
                    break
                    
                if job_url in self.seen_urls:
                    self.logger.debug(f"Already seen URL: {job_url}")
                    continue
                
                self.seen_urls.add(job_url)
                
                self.logger.info(f"Processing job {total + 1}/{self.max_jobs}: {job_url}")
                
                # Create new page for job details to avoid conflicts
                new_page = await page.context.new_page()
                new_page.set_default_timeout(self.timeout)
                
                try:
                    job_data = await self._parse_job_detail(new_page, job_url)
                    if job_data:
                        self.scraped.append(job_data)
                        total += 1
                        self.logger.info(f"Successfully parsed job: {job_data.get('title', 'Unknown')}")
                    else:
                        self.logger.warning(f"Failed to parse job details from: {job_url}")
                finally:
                    await new_page.close()
                
                # Small delay between jobs
                await page.wait_for_timeout(500)
            
            # Try to go to next page
            if total < self.max_jobs:
                has_next = await self._go_to_next_page(page)
                if not has_next:
                    self.logger.info("No more pages available")
                    break
                page_num += 1
            else:
                break

    async def _collect_job_links(self, page: Page, page_num: int = 1) -> List[str]:
        """Collect job URLs from current page"""
        job_links = []
        
        try:
            # Wait for page to fully load
            await page.wait_for_load_state("networkidle", timeout=15000)
            await page.wait_for_timeout(2000)  # Additional wait
            
            # Tesla often has job links in tables or lists
            link_selectors = [
                "a[href*='/careers/']",  # Tesla career links
                "table a",  # Links in tables
                ".job-link",
                "a[href*='job']",  # Any job-related link
                "tr a",  # Table row links
            ]
            
            for selector in link_selectors:
                try:
                    link_elements = await page.query_selector_all(selector)
                    self.logger.info(f"Selector '{selector}' found {len(link_elements)} elements")
                    
                    for element in link_elements:
                        try:
                            href = await element.get_attribute('href')
                            text_content = await element.inner_text()
                            
                            if href:
                                # Convert relative URLs to absolute
                                if href.startswith('/'):
                                    full_url = urljoin('https://www.tesla.com', href)
                                else:
                                    full_url = href
                                
                                # Look for Tesla career URLs
                                if 'tesla.com' in full_url and ('/careers/' in full_url or '/job' in full_url):
                                    if full_url not in job_links:
                                        job_links.append(full_url)
                                        self.logger.debug(f"Found job URL: {full_url}")
                                        
                        except Exception as e:
                            continue
                    
                    # If we found job links with this selector, we can continue
                    if job_links:
                        self.logger.info(f"Found {len(job_links)} job links with selector: {selector}")
                        break
                        
                except Exception as e:
                    self.logger.debug(f"Selector {selector} failed: {e}")
                    continue
            
            self.logger.info(f"Collected {len(job_links)} unique job links from page {page_num}")
            return job_links

        except Exception as e:
            self.logger.error(f"Error collecting job links from page {page_num}: {e}")
            return []

    async def _parse_job_detail(self, page: Page, job_url: str) -> Optional[Dict[str, Any]]:
        """Parse job details by navigating to job detail page"""
        
        try:
            self.logger.debug(f"Navigating to job detail: {job_url}")
            await page.goto(job_url, wait_until="domcontentloaded")
            await page.wait_for_timeout(self.sleep_after_open_ms)
            
            # Extract job details
            title = await self._extract_title(page)
            if not title:
                self.logger.warning(f"No title found for job: {job_url}")
                return None
            
            location = await self._extract_location(page)
            description = await self._extract_description(page)
            metadata = await self._extract_metadata(page)
            
            # Extract job ID from URL
            job_id = self._extract_job_id_from_url(job_url)
            
            job_data = {
                "id": f"Tesla_{job_id}",
                "title": title,
                "company": "Tesla",
                "location": location or "Not specified",
                "description": description,
                "url": job_url,
                "scraped_at": now_iso(),
                "source": "Tesla Careers",
                **metadata
            }
            
            return job_data

        except Exception as e:
            self.logger.error(f"Error parsing job detail from {job_url}: {e}")
            return None

    async def _extract_title(self, page: Page) -> Optional[str]:
        """Extract job title from detail page"""
        try:
            title_element = await page.wait_for_selector(self.sel_detail_title, timeout=10000)
            title = await title_element.inner_text()
            return title.strip() if title else None
        except Exception as e:
            self.logger.debug(f"Error extracting title: {e}")
        return None

    async def _extract_location(self, page: Page) -> Optional[str]:
        """Extract location from detail page"""
        try:
            # Look for specific location patterns common on Tesla careers
            location_patterns = [
                r'(Fremont, CA)',
                r'(Austin, TX)',
                r'(Palo Alto, CA)',
                r'(Gigafactory)',
                r'(Buffalo, NY)',
                r'(Sparks, NV)',
                r'(Berlin, Germany)',
                r'(Shanghai, China)',
                r'(Remote)',
                r'([A-Za-z\s]+,\s*[A-Z]{2})',  # "City, State" pattern
                r'([A-Za-z\s]+,\s*[A-Za-z\s]+)'  # "City, Country" pattern
            ]
            
            page_content = await page.content()
            
            for pattern in location_patterns:
                matches = re.findall(pattern, page_content)
                if matches:
                    # Return the first clean match
                    location = matches[0]
                    if len(location) > 2:  # Basic validation
                        return location.strip()
            
            # Try CSS selectors as fallback
            location_selectors = [
                ".location",
                ".job-location", 
                "[data-field='location']",
                "td:contains('Location')",
                "span:has-text('Remote')"
            ]
            
            for selector in location_selectors:
                try:
                    location_element = await page.query_selector(selector)
                    if location_element:
                        location_text = await location_element.inner_text()
                        if location_text and location_text.strip():
                            return location_text.strip()
                except Exception:
                    continue

        except Exception as e:
            self.logger.debug(f"Error extracting location: {e}")
        return None

    async def _extract_description(self, page: Page) -> str:
        """Extract job description from Tesla job detail page"""
        try:
            # Look for the main description content
            desc_element = await page.query_selector(self.sel_detail_desc)
            
            if desc_element:
                description = await desc_element.inner_text()
                if description and description.strip():
                    return description.strip()
            
            # Fallback selectors for description
            fallback_selectors = [
                ".job-description",
                ".description",
                ".job-details",
                ".content",
                "main div",
                "article"
            ]
            
            for selector in fallback_selectors:
                try:
                    element = await page.query_selector(selector)
                    if element:
                        text = await element.inner_text()
                        if text and len(text.strip()) > 100:  # Reasonable length check
                            return text.strip()
                except Exception:
                    continue
            
            # If no specific description found, get main content
            main_content = await page.query_selector("main")
            if main_content:
                content = await main_content.inner_text()
                if content and content.strip():
                    return content.strip()

        except Exception as e:
            self.logger.debug(f"Error extracting description: {e}")
        
        return "Description not available"

    async def _extract_metadata(self, page: Page) -> Dict[str, str]:
        """Extract metadata fields from Tesla job detail page"""
        metadata = {}
        
        try:
            page_content = await page.content()
            
            # Extract job type (Full time, Part time, etc.)
            job_type_patterns = [
                r'(Full.time|Part.time|Contract|Internship)',
                r'(Full-time|Part-time)',
                r'Employment Type[:\s]*([^<\n]+)',
                r'Job Type[:\s]*([^<\n]+)'
            ]
            
            for pattern in job_type_patterns:
                matches = re.findall(pattern, page_content, re.IGNORECASE)
                for match in matches:
                    clean_type = match.strip()
                    if clean_type and len(clean_type) > 2:
                        metadata["job_type"] = clean_type
                        break
                if "job_type" in metadata:
                    break
            
            # Extract department/category
            dept_patterns = [
                r'Team[:\s]*([^<\n]+)',
                r'Department[:\s]*([^<\n]+)',
                r'(Engineering|Manufacturing|Sales|Service|Energy|Autopilot|AI|Software|Hardware)',
                r'Business Unit[:\s]*([^<\n]+)'
            ]
            
            for pattern in dept_patterns:
                matches = re.findall(pattern, page_content, re.IGNORECASE)
                for match in matches:
                    clean_dept = match.strip()
                    if clean_dept and len(clean_dept) > 2:
                        metadata["department"] = clean_dept
                        break
                if "department" in metadata:
                    break
            
            # Extract remote work status
            if 'Remote' in page_content or 'remote' in page_content.lower():
                if 'Remote eligible' in page_content or 'Remote work' in page_content:
                    metadata["remote_work"] = "Yes"
                elif 'No remote' in page_content or 'On-site' in page_content:
                    metadata["remote_work"] = "No"
                elif 'Remote' in page_content:
                    metadata["remote_work"] = "Remote"

        except Exception as e:
            self.logger.debug(f"Error extracting metadata: {e}")
        
        return metadata

    def _extract_job_id_from_url(self, url: str) -> str:
        """Extract job ID from URL"""
        try:
            # Extract from URL patterns common in Tesla URLs
            # Example: https://www.tesla.com/careers/search/job/123456
            match = re.search(r'/job/(\d+)', url)
            if match:
                return match.group(1)
            
            # Alternative patterns
            match = re.search(r'/careers/([^/]+)', url)
            if match:
                return match.group(1)
                
            # Extract any numeric ID from URL
            match = re.search(r'(\d{4,})', url)
            if match:
                return match.group(1)
                
        except Exception:
            pass
        
        # Fallback: create ID from URL hash
        import hashlib
        return hashlib.md5(url.encode()).hexdigest()[:8]

    async def _go_to_next_page(self, page: Page) -> bool:
        """Navigate to next page"""
        try:
            # Look for pagination buttons
            next_selectors = [
                self.sel_pagination,
                "button[aria-label*='Next']",
                "button[aria-label*='next']",
                ".pagination-next",
                "a[aria-label*='Next']",
                "button:has-text('Next')",
                "button:has-text('Load more')",
                ".load-more-button"
            ]
            
            for selector in next_selectors:
                try:
                    next_button = await page.query_selector(selector)
                    if next_button:
                        # Check if button is enabled/clickable
                        is_disabled = await next_button.get_attribute('disabled')
                        aria_disabled = await next_button.get_attribute('aria-disabled')
                        
                        if not is_disabled and aria_disabled != 'true':
                            self.logger.info("Clicking next page button")
                            await next_button.click()
                            await page.wait_for_load_state("domcontentloaded")
                            await page.wait_for_timeout(self.sleep_after_nav_ms)
                            return True
                except Exception as e:
                    self.logger.debug(f"Error with selector {selector}: {e}")
                    continue
            
            self.logger.info("No next page button found or next page unavailable")
            return False

        except Exception as e:
            self.logger.error(f"Error navigating to next page: {e}")
            return False