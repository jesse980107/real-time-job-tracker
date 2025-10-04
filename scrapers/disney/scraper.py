# scrapers/disney/scraper.py

import logging
import re
import json
import asyncio
from datetime import datetime
from typing import Any, Dict, List, Optional, Set
from urllib.parse import urljoin, urlparse, urlunparse, quote, urlencode

from playwright.async_api import async_playwright, Page, TimeoutError as PlaywrightTimeoutError


def now_iso() -> str:
    return datetime.now().isoformat()


class DisneyScraper:
    """
    Disney Careers scraper for disneycareers.com
    """

    def __init__(self, website_config: Dict[str, Any], global_config: Dict[str, Any]):
        self.cfg = website_config
        self.g = global_config
        self.logger = logging.getLogger("job_tracker")

        sc = self.cfg.get("scraping_config", {})
        self.max_jobs = sc.get("max_jobs", 50)
        self.sleep_after_nav_ms = sc.get("sleep_after_nav_ms", 1000)
        self.sleep_after_open_ms = sc.get("sleep_after_open_ms", 500)
        self.apply_filters_via_ui = sc.get("apply_filters_via_ui", False)
        self.target_locations: List[str] = sc.get("locations", ["United States", "Canada"])

        wp = self.cfg.get("playwright_options", {})
        self.sel_results_link = wp.get("results_link_selector", "a[href*='/job/']")
        self.sel_show_more = wp.get("show_more_button", "[aria-label*='next' i], button:has-text('Next')")
        self.sel_detail_title = wp.get("detail_title", "h1")
        self.sel_detail_desc = wp.get("detail_description", ".job-description, [data-automation-id='jobDescription'], main")
        self.sel_job_card = wp.get("job_card_selector", "li, .job-item, [data-job-id]")
        self.sel_see_details = wp.get("see_details_button", "a[href*='/job/']")

        pg = self.g["playwright_global"]
        self.timeout = pg["timeout"]
        self.user_agent = pg["user_agent"]
        self.headless = self.g["global_settings"]["headless"]

        self.base_url = self.cfg["website_info"]["base_url"]

        self.scraped: List[Dict[str, Any]] = []
        self.seen_urls: Set[str] = set()

    async def scrape_jobs(self) -> List[Dict[str, Any]]:
        """Main entry point for scraping Disney jobs"""
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

                self.logger.info(f"Disney max_jobs = {self.max_jobs}")
                
                # Build URL with location filters
                url_with_filters = self._build_url_with_locations(self.base_url, self.target_locations)
                await self._open_list_page(page, url_with_filters)

                await self._harvest_and_parse(page)

                await context.close()
                await browser.close()

            end_time = datetime.now()
            duration = end_time - start_time
            duration_seconds = round(duration.total_seconds(), 2)
            
            self.logger.info(f"Disney scraping finished. Total jobs collected: {len(self.scraped)}")
            self.logger.info(f"Scraping duration: {duration_seconds} seconds")
            
            # Add duration info to the scraped data
            self.scraping_duration = duration_seconds
            
            return self.scraped

        except Exception as e:
            end_time = datetime.now()
            duration = end_time - start_time
            duration_seconds = round(duration.total_seconds(), 2)
            
            self.logger.error(f"Disney scraping error: {e}")
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
        
        # Disney might use different URL parameters for location filtering
        # This will need to be updated based on the actual URL structure
        # For now, return the base URL and we'll apply filters via UI if needed
        return base

    async def _harvest_and_parse(self, page: Page) -> None:
        """Harvest job links and parse details"""
        total = 0
        page_num = 1

        while total < self.max_jobs:
            self.logger.info(f"Processing page {page_num}")
            
            # Wait for page to load
            await page.wait_for_timeout(3000)
            
            # Check page title for debugging
            page_title = await page.title()
            self.logger.info(f"Page title: {page_title}")
            
            # Wait for job listings to load
            job_cards_found = False
            card_selectors = [
                "li:has(a[href*='/job/'])",
                "div:has(a[href*='/job/'])",
                ".job-item",
                "[data-job-id]",
                "a[href*='/job/']"
            ]
            
            for selector in card_selectors:
                try:
                    await page.wait_for_selector(selector, timeout=5000)
                    job_cards_found = True
                    self.logger.info(f"Found job cards with selector: {selector}")
                    break
                except PlaywrightTimeoutError:
                    continue
            
            if not job_cards_found:
                self.logger.warning("No job cards found with any selector")
                break

            # Collect job links from current page
            job_links = await self._collect_job_links(page, page_num)
            
            if not job_links:
                self.logger.info("No job links found on current page")
                break

            # Process each job on this page
            for job_url in job_links:
                if total >= self.max_jobs:
                    break
                    
                if job_url in self.seen_urls:
                    continue

                self.seen_urls.add(job_url)
                
                job_data = await self._parse_job_detail(page, job_url)
                if job_data:
                    self.scraped.append(job_data)
                    total += 1
                    self.logger.info(f"Scraped job {total}: {job_data.get('title', 'Unknown')}")

                await page.wait_for_timeout(self.sleep_after_open_ms)

            # After processing all jobs on this page, try to go to next page
            if total < self.max_jobs:
                if not await self._go_to_next_page(page):
                    break
                page_num += 1
            else:
                break

    async def _collect_job_links(self, page: Page, page_num: int = 1) -> List[str]:
        """Collect job URLs from current page"""
        job_links = []
        
        try:
            # Wait for content to load
            await page.wait_for_timeout(2000)
            
            # Look for job links
            link_selectors = [
                "a[href*='/job/']",
                "a[data-job-id]",
                "h2 a, h3 a",  # Job titles are often in headings
                ".job-title a"
            ]
            
            for selector in link_selectors:
                try:
                    links = await page.query_selector_all(selector)
                    if links:
                        for link in links:
                            href = await link.get_attribute("href")
                            if href and "/job/" in href:
                                # Convert relative URLs to absolute
                                if href.startswith("/"):
                                    full_url = f"https://www.disneycareers.com{href}"
                                else:
                                    full_url = href
                                
                                if full_url not in job_links:
                                    job_links.append(full_url)
                        
                        if job_links:
                            self.logger.info(f"Found {len(job_links)} job links with selector: {selector}")
                            break
                except Exception as e:
                    self.logger.debug(f"Error with selector {selector}: {e}")
                    continue

            return job_links[:self.max_jobs]  # Limit to max_jobs

        except Exception as e:
            self.logger.error(f"Error collecting job links: {e}")
            return []

    async def _parse_job_detail(self, listing_page: Page, job_url: str) -> Optional[Dict[str, Any]]:
        """Parse job details by navigating to job detail page"""
        
        try:
            context = listing_page.context
            detail_page = await context.new_page()
            await detail_page.goto(job_url)
            await detail_page.wait_for_load_state("networkidle")

            # Extract job details
            title = await self._extract_title(detail_page)
            location = await self._extract_location(detail_page)
            description = await self._extract_description(detail_page)
            
            # Extract metadata from the job details section
            metadata = await self._extract_metadata(detail_page)
            
            # Extract job ID from the page content (not URL)
            job_id = await self._extract_job_id_from_page(detail_page)

            job_data = {
                "jobId": f"Disney_{job_id}",
                "title": title or "Unknown Title",
                "company": "Disney",
                "location": location or "",
                "url": job_url,
                "description": description or "",
                "source": "Disney",
                "status": "active",
                "scraped_date": now_iso(),
            }

            # Add metadata fields
            if metadata:
                job_data.update(metadata)

            await detail_page.close()
            return job_data

        except Exception as e:
            self.logger.warning(f"Failed to parse job detail for {job_url}: {e}")
            if 'detail_page' in locals():
                await detail_page.close()
            return None

    async def _extract_title(self, page: Page) -> Optional[str]:
        """Extract job title from detail page"""
        try:
            title_selectors = [
                "h1",
                ".job-title",
                "[data-automation-id='jobTitle']",
                "h1.job-title",
                ".page-title"
            ]
            
            for selector in title_selectors:
                try:
                    title_el = await page.query_selector(selector)
                    if title_el:
                        title = await title_el.inner_text()
                        if title and title.strip():
                            return title.strip()
                except Exception:
                    continue
                    
        except Exception as e:
            self.logger.debug(f"Error extracting title: {e}")
        return None

    async def _extract_location(self, page: Page) -> Optional[str]:
        """Extract location from detail page"""
        try:
            location_selectors = [
                ".job-location",
                "[data-automation-id='jobLocation']",
                ".location",
                "p:has-text('Location')",
                "span:has-text('Location')"
            ]
            
            for selector in location_selectors:
                try:
                    location_el = await page.query_selector(selector)
                    if location_el:
                        location = await location_el.inner_text()
                        if location and location.strip():
                            # Clean up "Location " prefix if present
                            cleaned_location = location.strip()
                            if cleaned_location.startswith("Location "):
                                cleaned_location = cleaned_location[9:]  # Remove "Location " prefix
                            return cleaned_location
                except Exception:
                    continue

            # Fallback: look in the page text for location patterns
            page_text = await page.inner_text("body")
            location_patterns = [
                r'Location[:\s]+([^,\n]+(?:,\s*[^,\n]+)*)',
                r'([^,\n]+,\s*(?:United States|Canada|USA|US))',
                r'([^,\n]+,\s*[A-Z]{2})',  # City, State format
            ]
            
            for pattern in location_patterns:
                match = re.search(pattern, page_text, re.IGNORECASE)
                if match:
                    location = match.group(1).strip()
                    # Clean up "Location " prefix if present
                    if location.startswith("Location "):
                        location = location[9:]
                    return location

        except Exception as e:
            self.logger.debug(f"Error extracting location: {e}")
        return None

    async def _extract_description(self, page: Page) -> str:
        """Extract job description from Disney job detail page"""
        try:
            # Wait for content to load
            await page.wait_for_timeout(2000)
            
            # Disney-specific selectors for job descriptions based on the screenshots
            desc_selectors = [
                ".ats-description_content",  # Main content area from the screenshots
                ".job-description", 
                "[data-automation-id='jobDescription']",
                ".job-content",
                ".description",
                "section:has(h2:has-text('Job Summary'))",
                "section:has(h2:has-text('Description'))",
                "div:has(h3:has-text('Job Summary'))",
                "div:has(h3:has-text('Description'))",
                "div:has(h2:has-text('What You Will Do'))",
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
                    desc_el = await page.query_selector(selector)
                    if desc_el:
                        desc_text = await desc_el.inner_text()
                        # Look for substantial content
                        if desc_text and len(desc_text.strip()) > len(best_description):
                            best_description = desc_text.strip()
                            used_selector = selector
                            # If we found a substantial description, we can break
                            if len(best_description) > 200:
                                break
                except Exception as e:
                    continue
            
            if best_description:
                self.logger.debug(f"Using selector: {used_selector}")
                
                # Clean up the description
                lines = best_description.split('\n')
                cleaned_lines = []
                
                for line in lines:
                    line = line.strip()
                    if line and not any(skip in line.lower() for skip in [
                        'skip to main content',
                        'toggle navigation',
                        'search jobs',
                        'save job',
                        'apply now',
                        'share this job',
                        'links open in new tabs',
                        'candidate feedback'
                    ]):
                        cleaned_lines.append(line)
                
                final_description = '\n'.join(cleaned_lines)
                
                if len(final_description) > 50:
                    return final_description
            
            return "Job description not found on page"

        except Exception as e:
            self.logger.debug(f"Error extracting description: {e}")
            return f"Description extraction error: {str(e)}"

    async def _extract_metadata(self, page: Page) -> Dict[str, str]:
        """Extract metadata fields from Disney job detail page"""
        metadata = {}
        
        try:
            # Wait for page to load
            await page.wait_for_timeout(1000)
            
            # Disney-specific metadata fields based on the screenshots
            metadata_fields = [
                ("Date posted", "date_posted"),
                ("Job type", "job_type"),
                ("Employment type", "employment_type"),
                ("Experience level", "experience_level"),
                ("Department", "department"),
                ("Business", "business"),
                ("Salary", "salary"),
                ("Job level", "job_level"),
                ("Time type", "time_type")
            ]

            # Method 1: Extract from page text using patterns
            try:
                page_text = await page.inner_text("body")
                
                # Extract date posted with full format (Oct. 03, 2025)
                date_patterns = [
                    r'Date posted[:\s]+(\w{3}\.?\s+\d{1,2},?\s+\d{4})',
                    r'Posted[:\s]+(\w{3}\.?\s+\d{1,2},?\s+\d{4})',
                    r'(\w{3}\.?\s+\d{1,2},?\s+\d{4})'  # Match date formats like Oct. 03, 2025
                ]
                
                for pattern in date_patterns:
                    match = re.search(pattern, page_text, re.IGNORECASE)
                    if match and "date_posted" not in metadata:
                        date_value = match.group(1).strip()
                        # Ensure it includes the year
                        if "2025" in date_value or "2024" in date_value:
                            metadata["date_posted"] = date_value
                            break
                
                # Extract business from page text and clean it
                business_patterns = [
                    r'Business[:\s]+([^,\n]+)',
                    r'Business\s+([^,\n]+)'
                ]
                
                for pattern in business_patterns:
                    match = re.search(pattern, page_text, re.IGNORECASE)
                    if match and "business" not in metadata:
                        business_value = match.group(1).strip()
                        # Remove "Business " prefix if present
                        if business_value.startswith("Business "):
                            business_value = business_value[9:]
                        metadata["business"] = business_value
                        break
                
                # Extract salary from title or page text (like "$71,000 - $95,100")
                salary_patterns = [
                    r'\$[\d,]+ - \$[\d,]+',
                    r'\$[\d,]+/hour',
                    r'\$[\d,]+ per year'
                ]
                
                for pattern in salary_patterns:
                    match = re.search(pattern, page_text)
                    if match and "salary" not in metadata:
                        metadata["salary"] = match.group(0)
                        break
                        
            except Exception as e:
                self.logger.debug(f"Error in text-based extraction: {e}")

            # Method 2: Look for specific Disney page elements (but don't use location_detail)
            try:
                # Look for business (format: "Aulani, A Disney Resort & Spa")
                business_selectors = [
                    ".job-brand",
                    "[data-job-business]",
                    "span:has-text('Business')",
                    "p:has-text('Business')"
                ]
                
                for selector in business_selectors:
                    try:
                        business_el = await page.query_selector(selector)
                        if business_el and "business" not in metadata:
                            business_text = await business_el.inner_text()
                            if business_text and business_text.strip():
                                # Clean up "Business " prefix if present
                                cleaned_business = business_text.strip()
                                if cleaned_business.startswith("Business "):
                                    cleaned_business = cleaned_business[9:]
                                metadata["business"] = cleaned_business
                                break
                    except Exception:
                        continue
                        
            except Exception:
                pass

            # Clean up metadata values
            cleaned_metadata = {}
            for key, value in metadata.items():
                if value and isinstance(value, str):
                    cleaned_value = value.strip()
                    if cleaned_value and len(cleaned_value) > 0:
                        cleaned_metadata[key] = cleaned_value
            
            self.logger.debug(f"Extracted metadata: {list(cleaned_metadata.keys())}")
            return cleaned_metadata

        except Exception as e:
            self.logger.debug(f"Error extracting metadata: {e}")
            return {}

    async def _extract_job_id_from_page(self, page: Page) -> str:
        """Extract job ID from page content (not URL)"""
        try:
            # Wait for page to load
            await page.wait_for_timeout(1000)
            
            # Look for "Job ID" text pattern on the page
            page_text = await page.inner_text("body")
            job_id_match = re.search(r'Job ID[:\s]+(\d+)', page_text, re.IGNORECASE)
            if job_id_match:
                return job_id_match.group(1)
            
            # Alternative pattern
            job_id_match = re.search(r'Job ID:\s*(\d+)', page_text)
            if job_id_match:
                return job_id_match.group(1)
                
            # Fallback: extract from URL if page ID not found
            return self._extract_job_id_from_url(page.url)
            
        except Exception as e:
            self.logger.debug(f"Error extracting job ID from page: {e}")
            # Fallback to URL extraction
            return self._extract_job_id_from_url(page.url)

    def _extract_job_id_from_url(self, url: str) -> str:
        """Extract job ID from URL"""
        try:
            # URL format: /en/job/{location}/{job-title-slug}/{category_id}/{job_id}
            # Example: /en/job/kapolei/security-guest-service-manager-overnight-shift-71-000-95-100/391/86868325008
            match = re.search(r'/job/[^/]+/[^/]+/\d+/(\d+)', url)
            if match:
                return match.group(1)
            
            # Fallback: try to find any number at the end of the URL
            match = re.search(r'/(\d+)/?$', url)
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
            
            # Disney uses a specific pagination structure
            # Method 1: Try to find and use the page number input + GO button
            try:
                # Look for the current page input and increment it
                page_input = await page.query_selector("input[type='text'][value]:not([value=''])")
                if page_input:
                    current_page = await page_input.get_attribute("value")
                    if current_page and current_page.isdigit():
                        next_page_num = str(int(current_page) + 1)
                        
                        # Clear and fill the new page number
                        await page_input.fill(next_page_num)
                        await page.wait_for_timeout(500)
                        
                        # Look for GO button and click it
                        go_button = await page.query_selector("button:has-text('GO'), input[type='submit'][value*='GO' i]")
                        if go_button:
                            await go_button.click()
                            await page.wait_for_load_state("networkidle")
                            await page.wait_for_timeout(2000)
                            self.logger.info(f"Successfully navigated to page {next_page_num} using GO button")
                            return True
            except Exception as e:
                self.logger.debug(f"Error with page input + GO button navigation: {e}")
            
            # Method 2: Try to construct the URL manually
            try:
                current_url = page.url
                self.logger.debug(f"Current URL: {current_url}")
                
                # Parse current page number from URL or find it in the page
                page_match = re.search(r'[?&]p=(\d+)', current_url)
                if page_match:
                    current_page_num = int(page_match.group(1))
                    next_page_num = current_page_num + 1
                else:
                    # If no page parameter, this is page 1, so next is page 2
                    next_page_num = 2
                
                # Construct next page URL
                if '?' in current_url:
                    if 'p=' in current_url:
                        # Replace existing page parameter
                        next_url = re.sub(r'p=\d+', f'p={next_page_num}', current_url)
                    else:
                        # Add page parameter
                        next_url = f"{current_url}&p={next_page_num}"
                else:
                    # Add page parameter as first parameter
                    next_url = f"{current_url}?p={next_page_num}"
                
                self.logger.debug(f"Trying to navigate to: {next_url}")
                await page.goto(next_url)
                await page.wait_for_load_state("networkidle")
                await page.wait_for_timeout(2000)
                
                # Verify we actually moved to a new page
                new_url = page.url
                if new_url != current_url:
                    self.logger.info(f"Successfully navigated to page {next_page_num} via URL construction")
                    return True
                    
            except Exception as e:
                self.logger.debug(f"Error with URL construction navigation: {e}")
            
            # Method 3: Look for next arrow or pagination links
            next_selectors = [
                "a[href*='p=']:not([href*='p=1']):not(.disabled)",  # Pagination links with page numbers
                ".next[href]:not([disabled]):not(.disabled)",  # Next arrow link
                "a[rel='nofollow']:contains('Next')",  # Next link
                "button:has-text('Next'):not([disabled])",
                "[aria-label*='next' i]:not([disabled]):not(.disabled)",
                "a:has-text('Next'):not(.disabled)",
                "[title*='next' i]:not([disabled]):not(.disabled)",
                ".pagination .next:not(.disabled)",
                "nav a[href*='page']:last-child:not(.disabled)"  # Last pagination link
            ]

            for selector in next_selectors:
                try:
                    next_btn = await page.query_selector(selector)
                    if next_btn:
                        # Check if button/link is enabled
                        is_disabled = await next_btn.get_attribute("disabled")
                        class_name = await next_btn.get_attribute("class") or ""
                        
                        if is_disabled or "disabled" in class_name.lower():
                            continue
                            
                        # Check if it's a link or button and handle accordingly
                        href = await next_btn.get_attribute("href")
                        if href:
                            # It's a link, make sure it's a full URL
                            if href.startswith("/"):
                                full_url = f"https://www.disneycareers.com{href}"
                            elif href.startswith("http"):
                                full_url = href
                            else:
                                # Relative URL, combine with base
                                full_url = f"https://www.disneycareers.com/en/{href}"
                            
                            self.logger.debug(f"Trying to navigate to pagination link: {full_url}")
                            await page.goto(full_url)
                        else:
                            # It's a button, click it
                            await next_btn.click()
                        
                        await page.wait_for_load_state("networkidle")
                        await page.wait_for_timeout(2000)
                        
                        self.logger.info(f"Successfully navigated to next page using selector: {selector}")
                        return True
                        
                except Exception as e:
                    self.logger.debug(f"Error with next button selector {selector}: {e}")
                    continue

            self.logger.debug("No next page navigation options found")
            return False

        except Exception as e:
            self.logger.error(f"Error navigating to next page: {e}")
            return False