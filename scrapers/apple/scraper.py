# scrapers/apple/scraper.py

import logging
import re
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Set
from urllib.parse import urljoin, urlparse, urlunparse, quote, urlencode

from playwright.async_api import async_playwright, Page, TimeoutError as PlaywrightTimeoutError


def now_iso() -> str:
    return datetime.now().isoformat()


class AppleScraper:
    """
    Apple Careers scraper for jobs.apple.com
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
        self.sel_results_link = wp.get("results_link_selector", "a[href*='/details/']")
        self.sel_show_more = wp.get("show_more_button", "button:has-text('Next'), [aria-label*='next' i]")
        self.sel_detail_title = wp.get("detail_title", "h1")
        self.sel_detail_desc = wp.get("detail_description", ".jd-info, .job-description, main section")
        self.sel_job_card = wp.get("job_card_selector", "h3 a[href*='/details/'], .searchresult-container a[href*='/details/']")
        self.sel_see_details = wp.get("see_details_button", "a[href*='/details/']")

        pg = self.g["playwright_global"]
        self.timeout = pg["timeout"]
        self.user_agent = pg["user_agent"]
        self.headless = self.g["global_settings"]["headless"]

        self.base_url = self.cfg["website_info"]["base_url"]

        self.scraped: List[Dict[str, Any]] = []
        self.seen_urls: Set[str] = set()

    async def scrape_jobs(self) -> List[Dict[str, Any]]:
        """Main entry point for scraping Apple jobs"""
        start_time = datetime.now()
        
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=self.headless,
                    args=["--no-sandbox", "--disable-dev-shm-usage"]
                )
                context = await browser.new_context(user_agent=self.user_agent)
                page = await context.new_page()

                # Build URL with location filters
                url = self._build_url_with_locations(self.base_url, self.target_locations)
                await self._open_list_page(page, url)
                await self._harvest_and_parse(page)

                await browser.close()

            end_time = datetime.now()
            duration = end_time - start_time
            duration_seconds = round(duration.total_seconds(), 2)
            
            self.logger.info(f"Apple scraping finished. Total jobs collected: {len(self.scraped)}")
            self.logger.info(f"Scraping duration: {duration_seconds} seconds")
            
            # Add duration info to the scraped data
            self.scraping_duration = duration_seconds
            
            return self.scraped

        except Exception as e:
            end_time = datetime.now()
            duration = end_time - start_time
            duration_seconds = round(duration.total_seconds(), 2)
            
            self.logger.error(f"Apple scraping error: {e}")
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
        
        # Apple uses location= parameter for location filtering
        # Format: location=united-states-USA+canada-CANC for both countries
        location_params = []
        for loc in locations:
            if loc.lower() == "united states":
                location_params.append("united-states-USA")
            elif loc.lower() == "canada":
                location_params.append("canada-CANC")
        
        if location_params:
            # Join multiple locations with +
            location_string = "+".join(location_params)
            separator = "&" if "?" in base else "?"
            return f"{base}{separator}location={location_string}"
        
        return base

    def _build_url_with_page(self, base: str, page_num: int, locations: List[str]) -> str:
        """Build URL with location filters and specific page number"""
        # Apple pagination with location filtering
        location_params = []
        for loc in locations:
            if loc.lower() == "united states":
                location_params.append("united-states-USA")
            elif loc.lower() == "canada":
                location_params.append("canada-CANC")
        
        params = []
        if location_params:
            location_string = "+".join(location_params)
            params.append(f"location={location_string}")
        
        params.append(f"page={page_num}")
        
        query_string = "&".join(params)
        separator = "&" if "?" in base else "?"
        return f"{base}{separator}{query_string}"

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
                "h3:has(a[href*='/details/'])",
                "a[href*='/details/']",
                ".searchresult-container",
                ".search-result-item",
                "[data-automation-id='jobTitle']"
            ]
            
            for selector in card_selectors:
                try:
                    await page.wait_for_selector(selector, timeout=5000)
                    job_cards_found = True
                    self.logger.info(f"Found job cards using selector: {selector}")
                    break
                except:
                    continue
            
            if not job_cards_found:
                self.logger.warning("No job cards found on this page")
                break

            # Collect job links from current page
            job_links = await self._collect_job_links(page, page_num)
            
            if not job_links:
                self.logger.info("No job links found on this page")
                break

            # Process each job on this page
            for job_url in job_links:
                if total >= self.max_jobs:
                    break
                    
                if job_url in self.seen_urls:
                    self.logger.debug(f"Already processed: {job_url}")
                    continue
                
                self.seen_urls.add(job_url)
                
                job_data = await self._parse_job_detail(page, job_url)
                if job_data:
                    self.scraped.append(job_data)
                    total += 1
                    self.logger.info(f"Scraped job {total}: {job_data.get('title', 'Unknown')}")
                
                # Small delay between jobs
                await page.wait_for_timeout(self.sleep_after_open_ms)

            # After processing all jobs on this page, try to go to next page
            if total < self.max_jobs:
                if not await self._go_to_next_page(page):
                    self.logger.info("No more pages to process")
                    break
                page_num += 1
            else:
                self.logger.info("Reached max jobs limit")
                break

    async def _collect_job_links(self, page: Page, page_num: int = 1) -> List[str]:
        """Collect job URLs from current page"""
        job_links = []
        
        try:
            # Wait for content to load
            await page.wait_for_timeout(2000)
            
            # Look for job links using multiple selectors
            link_selectors = [
                "h3 a[href*='/details/']",
                "a[href*='/details/'][data-automation-id='jobTitle']",
                "a[href*='/details/']"
            ]
            
            for selector in link_selectors:
                try:
                    links = await page.query_selector_all(selector)
                    if links:
                        self.logger.info(f"Found {len(links)} job links using selector: {selector}")
                        for link in links:
                            href = await link.get_attribute("href")
                            if href:
                                # Convert relative URLs to absolute
                                if href.startswith("/"):
                                    href = f"https://jobs.apple.com{href}"
                                job_links.append(href)
                        break
                except Exception as e:
                    self.logger.debug(f"Error with selector {selector}: {e}")
                    continue

            # Remove duplicates while preserving order
            seen = set()
            unique_links = []
            for link in job_links:
                if link not in seen:
                    seen.add(link)
                    unique_links.append(link)

            self.logger.info(f"Found {len(unique_links)} unique job links on page {page_num}")
            return unique_links[:self.max_jobs]  # Limit to max_jobs

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
            
            # Filter by target locations - only process US and Canada jobs
            if location and self.target_locations:
                location_lower = location.lower()
                is_target_location = (
                    "united states" in location_lower or 
                    "usa" in location_lower or 
                    "us" in location_lower or
                    "california" in location_lower or
                    "washington" in location_lower or
                    "texas" in location_lower or
                    "new york" in location_lower or
                    "canada" in location_lower or
                    "ontario" in location_lower or
                    "quebec" in location_lower or
                    "british columbia" in location_lower
                )
                if not is_target_location:
                    self.logger.debug(f"Skipping job outside target locations: {location}")
                    await detail_page.close()
                    return None
            
            description = await self._extract_description(detail_page)
            
            # Extract metadata from the job details section
            metadata = await self._extract_metadata(detail_page)
            
            # Extract job ID from URL
            job_id = self._extract_job_id_from_url(job_url)

            job_data = {
                "jobId": f"Apple_{job_id}",
                "title": title or "Unknown Title",
                "company": "Apple",
                "location": location or "",
                "url": job_url,
                "description": description or "",
                "source": "Apple",
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
                "h1.job-title"
            ]
            
            for selector in title_selectors:
                try:
                    title_el = await page.query_selector(selector)
                    if title_el:
                        title = await title_el.inner_text()
                        return title.strip()
                except:
                    continue
                    
        except Exception as e:
            self.logger.debug(f"Error extracting title: {e}")
        return None

    async def _extract_location(self, page: Page) -> Optional[str]:
        """Extract location from detail page"""
        try:
            # Look for location in the page content
            page_text = await page.inner_text("body")
            
            # Apple shows location in multiple places - try different patterns
            location_patterns = [
                # Pattern 1: Specific city, state, country format
                r'([^,\n]+,\s*California,\s*United States)',
                r'([^,\n]+,\s*Washington,\s*United States)',
                r'([^,\n]+,\s*Texas,\s*United States)',
                r'([^,\n]+,\s*New York,\s*United States)',
                r'([^,\n]+,\s*Ontario,\s*Canada)',
                r'([^,\n]+,\s*Quebec,\s*Canada)',
                r'([^,\n]+,\s*British Columbia,\s*Canada)',
                
                # Pattern 2: More general city, state, country
                r'([^,\n]+,\s*[^,\n]+,\s*United States)(?:\s*Software|\s*Apple|\s*Hardware|\s*Machine Learning|\s*Corporate|\s*$)',
                r'([^,\n]+,\s*[^,\n]+,\s*Canada)(?:\s*Software|\s*Apple|\s*Hardware|\s*Machine Learning|\s*Corporate|\s*$)',
                
                # Pattern 3: "Various Locations within" pattern
                r'Various Locations within\s+([^,\n]+(?:,\s*[^,\n]+)*)',
                
                # Pattern 4: Country only patterns for jobs with broad locations
                r'(?:US - Specialist|CA-Specialist).*?\s+(United States)(?:\s*Apple|\s*$)',
                r'(?:US - Specialist|CA-Specialist).*?\s+(Canada)(?:\s*Apple|\s*$)',
                
                # Pattern 5: Extract from job title context - for specialist roles
                r'US - Specialist.*?Various Locations within\s+(United States)',
                r'CA-Specialist.*?(?:Canada)\s+(Canada)',
                
                # Pattern 6: Direct country match when job is country-wide
                r'^.*?(United States)(?:\s*Apple\s*Retail|\s*$)',
                r'^.*?(Canada)(?:\s*Apple\s*Retail|\s*$)',
            ]
            
            for pattern in location_patterns:
                match = re.search(pattern, page_text, re.IGNORECASE | re.MULTILINE)
                if match:
                    location = match.group(1).strip()
                    # Clean up common issues
                    location = re.sub(r'\s+', ' ', location)
                    # Don't include team names in location
                    if "Apple" not in location and "Software" not in location and "Hardware" not in location:
                        self.logger.debug(f"Found location using pattern: {location}")
                        return location

            # Fallback for specialist roles - check if it's a US or Canada specialist role
            if "US - Specialist" in page_text:
                return "United States"
            elif "CA-Specialist" in page_text or "CA - Specialist" in page_text:
                return "Canada"

            # Fallback: look for specific location elements in HTML
            location_selectors = [
                ".job-location",
                "[data-automation-id='jobLocation']",
                ".location",
                "h1 + p",  # Sometimes location is in a paragraph after the title
                "h1 + div"
            ]
            
            for selector in location_selectors:
                try:
                    location_el = await page.query_selector(selector)
                    if location_el:
                        location = await location_el.inner_text()
                        location = location.strip()
                        # Clean up and validate
                        if location and ("United States" in location or "Canada" in location):
                            # Remove team names if they got included
                            location = re.sub(r'\s*Apple.*$', '', location)
                            location = re.sub(r'\s*Software.*$', '', location)
                            location = re.sub(r'\s*Hardware.*$', '', location)
                            return location.strip()
                except:
                    continue

            # Last resort: Extract from URL if it contains location info
            url = page.url
            if "cupertino" in url.lower():
                return "Cupertino, California, United States"
            elif "seattle" in url.lower():
                return "Seattle, Washington, United States"
            elif "san-diego" in url.lower():
                return "San Diego, California, United States"

        except Exception as e:
            self.logger.debug(f"Error extracting location: {e}")
        return None

    async def _extract_description(self, page: Page) -> str:
        """Extract job description from Apple job detail page"""
        try:
            # Wait for content to load
            await page.wait_for_timeout(2000)
            
            # Apple-specific selectors for job descriptions
            desc_selectors = [
                ".jd-info",  # Main job description container
                "section:has(h2:has-text('Summary'))",
                "section:has(h2:has-text('Description'))", 
                "section:has(h2:has-text('Minimum Qualifications'))",
                "section:has(h2:has-text('Preferred Qualifications'))",
                ".job-description",
                "[data-automation-id='jobDescription']",
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
                        desc_text = desc_text.strip()
                        
                        # Quality check: prefer longer descriptions
                        if len(desc_text) > len(best_description):
                            best_description = desc_text
                            used_selector = selector
                            
                except Exception as e:
                    self.logger.debug(f"Error with selector {selector}: {e}")
                    continue
            
            if best_description:
                self.logger.debug(f"Used selector '{used_selector}' for description ({len(best_description)} chars)")
                
                # Clean up the description by finding the "Summary" section start
                description = best_description
                
                # Remove common Apple job page navigation elements first
                nav_patterns = [
                    r'^Back to search results\s*',
                    r'^.*?Submit Resume\s*',
                    r'^.*?Where we\'re hiring\s*',
                    r'^.*?Apply\s*',
                ]
                
                for pattern in nav_patterns:
                    description = re.sub(pattern, '', description, flags=re.IGNORECASE | re.MULTILINE)
                
                # Remove Posted: and Role Number: from the beginning
                description = re.sub(r'^Posted:\s*[^\n]*\s*', '', description, flags=re.IGNORECASE | re.MULTILINE)
                description = re.sub(r'^Role Number:\s*[^\n]*\s*', '', description, flags=re.IGNORECASE | re.MULTILINE)
                description = re.sub(r'^Weekly Hours:\s*[^\n]*\s*', '', description, flags=re.IGNORECASE | re.MULTILINE)
                
                # Find the "Summary" section start or main job description content
                summary_patterns = [
                    r'(Summary\s+.*)',              # Include "Summary" in the description
                    r'(Apple\s+\w+.*)',             # "Apple Retail is..." or similar
                    r'(The\s+\w+.*team.*)',         # "The Audio QA team..."
                    r'(At Apple.*)',                # "At Apple, our goal..."
                    r'(Imagine what you could do here.*)',  # Apple's common intro
                ]
                
                description_found = False
                for pattern in summary_patterns:
                    match = re.search(pattern, description, re.DOTALL | re.IGNORECASE)
                    if match:
                        description = match.group(1).strip()
                        description_found = True
                        break
                
                # If no summary pattern found, just clean up what we have
                if not description_found and description:
                    # Remove any remaining metadata patterns
                    description = re.sub(r'^[^a-zA-Z]*', '', description)
                
                # Remove extra whitespace and normalize line breaks
                description = re.sub(r'\n\s*\n', '\n\n', description)
                description = re.sub(r'\s+', ' ', description)
                description = description.strip()
                
                return description
            
            return "Job description not found on page"

        except Exception as e:
            self.logger.debug(f"Error extracting description: {e}")
            return f"Description extraction error: {str(e)}"

    async def _extract_metadata(self, page: Page) -> Dict[str, str]:
        """Extract metadata fields from Apple job detail page"""
        metadata = {}
        
        try:
            # Wait for page to load
            await page.wait_for_timeout(1000)
            
            # Apple-specific metadata fields
            page_text = await page.inner_text("body")
            
            # Extract posting date
            posted_match = re.search(r'Posted:\s*([^\n]+)', page_text)
            if posted_match:
                metadata["date_posted"] = posted_match.group(1).strip()
            
            # Extract weekly hours
            hours_match = re.search(r'Weekly Hours:\s*(\d+)', page_text)
            if hours_match:
                metadata["weekly_hours"] = hours_match.group(1).strip()
            
            # Extract role number (Job ID)
            role_match = re.search(r'Role Number:\s*([^\s\n]+)', page_text)
            if role_match:
                metadata["role_number"] = role_match.group(1).strip()
            
            # Extract team information from page content (human-readable format)
            # Look for the team name that appears near the location/title
            team_patterns = [
                r'(Apple Retail)',
                r'(Software and Services)',
                r'(Machine Learning and AI)', 
                r'(Hardware)',
                r'(Corporate Functions)',
                r'(Operations)',
                r'(Marketing)',
                r'(Sales)',
                r'(Customer Support)',
                r'(AppleCare)',
                r'(Finance)',
                r'(Legal)',
                r'(People)',
                r'(Information Systems)',
                r'(Security)',
            ]
            
            for pattern in team_patterns:
                match = re.search(pattern, page_text, re.IGNORECASE)
                if match:
                    metadata["team"] = match.group(1)
                    break
            
            # If no human-readable team found, fall back to URL team parameter
            if "team" not in metadata:
                team_match = re.search(r'team=([A-Z]+)', page.url)
                if team_match:
                    # Map common team codes to readable names
                    team_codes = {
                        "APPST": "Apple Retail",
                        "SFTWR": "Software and Services",
                        "MLAI": "Machine Learning and AI",
                        "HRDWR": "Hardware",
                        "CORSV": "Corporate Functions",
                        "MKTG": "Marketing",
                        "SALES": "Sales",
                        "CUSTSUPP": "Customer Support",
                        "APPLECARE": "AppleCare",
                        "FINANCE": "Finance",
                        "LEGAL": "Legal",
                        "PEOPLE": "People",
                        "INFOSYS": "Information Systems",
                        "SECURITY": "Security"
                    }
                    team_code = team_match.group(1)
                    metadata["team"] = team_codes.get(team_code, team_code)
            
            # Note: Intentionally excluding salary_range from metadata extraction
            
            self.logger.debug(f"Extracted metadata: {list(metadata.keys())}")
            return metadata

        except Exception as e:
            self.logger.debug(f"Error extracting metadata: {e}")
            return {}

    def _extract_job_id_from_url(self, url: str) -> str:
        """Extract job ID from URL"""
        try:
            # Apple URL format: /en-us/details/{job_id}-{suffix}/job-title
            # Example: /en-us/details/200624060-0836/software-engineer-watchos-widgets-live-activities
            match = re.search(r'/details/([^/]+)/', url)
            if match:
                job_id = match.group(1)
                return job_id
            
            # Fallback: try to find any number pattern
            match = re.search(r'(\d{9,})', url)
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
            
            # Method 1: Look for pagination controls
            pagination_selectors = [
                "button:has-text('Next')",
                ".pagination-next",
                "[aria-label*='next' i]",
                "a[href*='page=']"
            ]
            
            for selector in pagination_selectors:
                try:
                    next_button = await page.query_selector(selector)
                    if next_button:
                        # Check if button is enabled
                        is_disabled = await next_button.get_attribute("disabled")
                        if is_disabled:
                            self.logger.info("Next button is disabled")
                            return False
                        
                        self.logger.info(f"Clicking next page button: {selector}")
                        await next_button.click()
                        await page.wait_for_load_state("domcontentloaded")
                        await page.wait_for_timeout(2000)
                        return True
                except Exception as e:
                    self.logger.debug(f"Error with pagination selector {selector}: {e}")
                    continue
            
            # Method 2: Check for page numbers in URL and increment
            current_url = page.url
            if "page=" in current_url:
                try:
                    page_match = re.search(r'page=(\d+)', current_url)
                    if page_match:
                        current_page = int(page_match.group(1))
                        next_page = current_page + 1
                        next_url = re.sub(r'page=\d+', f'page={next_page}', current_url)
                        
                        self.logger.info(f"Navigating to next page URL: {next_url}")
                        await page.goto(next_url)
                        await page.wait_for_load_state("domcontentloaded")
                        await page.wait_for_timeout(2000)
                        return True
                except Exception as e:
                    self.logger.debug(f"Error incrementing page number: {e}")
            
            # Method 3: Add page parameter to URL
            else:
                try:
                    separator = "&" if "?" in current_url else "?"
                    next_url = f"{current_url}{separator}page=2"
                    
                    self.logger.info(f"Adding page parameter: {next_url}")
                    await page.goto(next_url)
                    await page.wait_for_load_state("domcontentloaded")
                    await page.wait_for_timeout(2000)
                    return True
                except Exception as e:
                    self.logger.debug(f"Error adding page parameter: {e}")

            self.logger.info("No pagination method worked")
            return False

        except Exception as e:
            self.logger.error(f"Error navigating to next page: {e}")
            return False