# scrapers/humana/scraper.py

import asyncio
import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Set
from urllib.parse import urljoin, urlparse, urlunparse, quote, urlencode

from playwright.async_api import async_playwright, Page, TimeoutError as PlaywrightTimeoutError


def now_iso() -> str:
    return datetime.now().isoformat()


class HumanaScraper:
    """
    Humana Careers scraper for careers.humana.com
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
        self.sel_results_link = wp.get("results_link_selector", "h3 a")
        self.sel_show_more = wp.get("show_more_button", "button:has-text(Next)")
        self.sel_detail_title = wp.get("detail_title", "h1")
        self.sel_detail_desc = wp.get("detail_description", ".phw-job-description")
        self.sel_job_card = wp.get("job_card_selector", "li")
        self.sel_see_details = wp.get("see_details_button", "h3 a")
        self.sel_pagination = wp.get("pagination_button", "a[aria-label=Next]")
        self.sel_job_summary = wp.get("job_summary_selector", ".phw-job-description")
        self.sel_job_qualifications = wp.get("job_qualifications_selector", ".qualifications")

        pg = self.g["playwright_global"]
        self.timeout = pg["timeout"]
        self.user_agent = pg["user_agent"]
        self.headless = self.g["global_settings"]["headless"]

        self.base_url = self.cfg["website_info"]["base_url"]

        self.scraped: List[Dict[str, Any]] = []
        self.seen_urls: Set[str] = set()

    async def scrape_jobs(self) -> List[Dict[str, Any]]:
        """Main entry point for scraping Humana jobs"""
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
            
            self.logger.info(f"Humana scraping finished. Total jobs collected: {len(self.scraped)}")
            self.logger.info(f"Scraping duration: {duration_seconds} seconds")
            
            # Add duration info to the scraped data
            self.scraping_duration = duration_seconds
            
            return self.scraped

        except Exception as e:
            end_time = datetime.now()
            duration = end_time - start_time
            duration_seconds = round(duration.total_seconds(), 2)
            
            self.logger.error(f"Humana scraping error: {e}")
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
            # Wait for job listings to load
            await page.wait_for_selector(self.sel_results_link, timeout=15000)
            
            # Get all job links
            link_elements = await page.query_selector_all(self.sel_results_link)
            
            for element in link_elements:
                try:
                    href = await element.get_attribute('href')
                    if href:
                        # Convert relative URLs to absolute
                        if href.startswith('/'):
                            full_url = urljoin(self.base_url, href)
                        else:
                            full_url = href
                        
                        # Validate that this is a job URL
                        if '/job/' in full_url and full_url not in job_links:
                            job_links.append(full_url)
                            
                except Exception as e:
                    self.logger.debug(f"Error processing link element: {e}")
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
                "id": f"Humana_{job_id}",
                "title": title,
                "company": "Humana",
                "location": location or "Not specified",
                "description": description,
                "url": job_url,
                "scraped_at": now_iso(),
                "source": "Humana Careers",
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
            # Look for specific location patterns based on the screenshots
            # The location appears as clean text like "San Diego, California"
            location_patterns = [
                r'(San Diego, California)',
                r'(Henderson, Nevada)', 
                r'(Los Angeles, California)',
                r'(Cottonwood, Arizona)',
                r'(Tucson, Arizona)',
                r'(Prescott, Arizona)',
                r'(Las Vegas, Nevada)',
                r'([A-Za-z\s]+,\s*[A-Za-z]{2,})'  # General "City, State" pattern
            ]
            
            page_content = await page.content()
            
            for pattern in location_patterns:
                matches = re.findall(pattern, page_content)
                if matches:
                    # Return the first clean match
                    location = matches[0]
                    if len(location) > 5 and ',' in location:  # Basic validation
                        return location.strip()
            
            # Try CSS selectors as fallback
            location_selectors = [
                "span:has-text('San Diego, California')",
                "span:has-text('Henderson, Nevada')",
                "span:has-text('Los Angeles, California')"
            ]
            
            for selector in location_selectors:
                try:
                    location_element = await page.query_selector(selector)
                    if location_element:
                        location_text = await location_element.inner_text()
                        if location_text and location_text.strip() and ',' in location_text:
                            return location_text.strip()
                except Exception:
                    continue

        except Exception as e:
            self.logger.debug(f"Error extracting location: {e}")
        return None

    async def _extract_description(self, page: Page) -> str:
        """Extract job description from Humana job detail page"""
        try:
            # Look for the main description content based on the screenshots
            # The description starts with "Become a part of our caring community..."
            page_content = await page.content()
            
            # Extract description content starting from key phrases
            desc_patterns = [
                r'<h2[^>]*>Description</h2>(.*?)(?=<h2|<section|$)',
                r'Become a part of our caring community and help us put health first(.*?)(?=Use your skills to make an impact|Required Experience|Essential Functions)',
                r'<div[^>]*class="[^"]*job-description[^"]*"[^>]*>(.*?)</div>',
            ]
            
            for pattern in desc_patterns:
                match = re.search(pattern, page_content, re.DOTALL | re.IGNORECASE)
                if match:
                    # Clean up HTML and extract text
                    raw_desc = match.group(1) if len(match.groups()) > 0 else match.group(0)
                    # Remove HTML tags
                    clean_desc = re.sub(r'<[^>]+>', ' ', raw_desc)
                    # Clean up whitespace
                    clean_desc = re.sub(r'\s+', ' ', clean_desc)
                    clean_desc = clean_desc.strip()
                    
                    if len(clean_desc) > 100:  # Reasonable length check
                        return "Become a part of our caring community and help us put health first. " + clean_desc
            
            # Fallback: try to find description section by looking for specific content
            try:
                # Wait for any description element
                desc_element = await page.query_selector("h2:has-text('Description') + *")
                if desc_element:
                    description = await desc_element.inner_text()
                    if description and len(description.strip()) > 100:
                        return description.strip()
                        
                # Try main content area
                main_element = await page.query_selector("main")
                if main_element:
                    # Get all paragraphs and text content
                    content = await main_element.inner_text()
                    if content:
                        # Find description starting point
                        lines = content.split('\n')
                        desc_lines = []
                        capture = False
                        
                        for line in lines:
                            line = line.strip()
                            if not line:
                                continue
                                
                            # Start capturing at description section
                            if 'become a part of our caring community' in line.lower():
                                capture = True
                            
                            # Stop at certain sections
                            if capture and ('use your skills to make an impact' in line.lower() or 
                                          'required experience' in line.lower() or
                                          'essential functions' in line.lower()):
                                break
                                
                            if capture:
                                desc_lines.append(line)
                        
                        if desc_lines:
                            description = ' '.join(desc_lines)
                            if len(description) > 100:
                                return description

            except Exception:
                pass

        except Exception as e:
            self.logger.debug(f"Error extracting description: {e}")
        
        return "Description not available"

    async def _extract_metadata(self, page: Page) -> Dict[str, str]:
        """Extract metadata fields from Humana job detail page"""
        metadata = {}
        
        try:
            page_content = await page.content()
            
            # Extract job type (Remote, Full time, etc.) - clean extraction
            job_type_patterns = [
                r'widget:\s*([^<\n]+)',
                r'(Full time|Part time|Per diem|PRN)'
            ]
            
            job_types = []
            for pattern in job_type_patterns:
                matches = re.findall(pattern, page_content, re.IGNORECASE)
                for match in matches:
                    clean_type = match.strip()
                    if clean_type and clean_type.lower() not in ['widget:', 'no', '']:
                        # Keep only the first valid job type found
                        if clean_type not in job_types:
                            job_types.append(clean_type)
            
            # Take only the first clean job type to avoid duplicates
            if job_types:
                metadata["job_type"] = job_types[0]
            
            # Extract department/category - clean extraction
            dept_patterns = [
                r'Category:\s*([^<\n]+)',
                r'undefined:\s*([^<\n]+)',
                r'(Clinical Support|CenterWell Home Health|Speech Language Pathology|Nursing|Social Worker)'
            ]
            
            for pattern in dept_patterns:
                matches = re.findall(pattern, page_content, re.IGNORECASE)
                for match in matches:
                    clean_dept = match.strip()
                    if clean_dept and clean_dept not in ['Category:', 'undefined:', '']:
                        metadata["department"] = clean_dept
                        break
            
            # Extract remote work status
            if 'Remote Job: No' in page_content:
                metadata["remote_work"] = "No"
            elif 'Remote Job: Yes' in page_content:
                metadata["remote_work"] = "Yes"

        except Exception as e:
            self.logger.debug(f"Error extracting metadata: {e}")
        
        return metadata

    def _extract_job_id_from_url(self, url: str) -> str:
        """Extract job ID from URL"""
        try:
            # Extract from URL pattern like /job/R-391902/Branch-Director-Home-Health
            match = re.search(r'/job/(R-\d+)/', url)
            if match:
                return match.group(1)
            
            # Fallback: extract any R-XXXXXX pattern from URL
            match = re.search(r'R-\d+', url)
            if match:
                return match.group(0)
                
        except Exception:
            pass
        return "UNKNOWN"

    async def _go_to_next_page(self, page: Page) -> bool:
        """Navigate to next page"""
        try:
            # Look for pagination buttons
            next_selectors = [
                self.sel_pagination,
                "a[aria-label*='Next']",
                "button:has-text('Next')",
                "a:has-text('Next')",
                ".phw-pagination-next-link",
                "a[data-ph-at-id*='pagination-next']"
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
            
            # Check for numbered pagination
            try:
                current_page_elem = await page.query_selector(".phw-pagination-current, .current, [aria-current='page']")
                if current_page_elem:
                    current_page_text = await current_page_elem.inner_text()
                    try:
                        current_page_num = int(current_page_text.strip())
                        next_page_num = current_page_num + 1
                        
                        # Look for next page number link
                        next_page_link = await page.query_selector(f"a:has-text('{next_page_num}')")
                        if next_page_link:
                            await next_page_link.click()
                            await page.wait_for_load_state("domcontentloaded")
                            await page.wait_for_timeout(self.sleep_after_nav_ms)
                            return True
                    except ValueError:
                        pass
            except Exception:
                pass
            
            self.logger.info("No next page button found or next page unavailable")
            return False

        except Exception as e:
            self.logger.error(f"Error navigating to next page: {e}")
            return False