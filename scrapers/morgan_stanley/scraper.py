# scrapers/morgan_stanley/scraper.py
import logging
import re
import asyncio
from datetime import datetime
from typing import Any, Dict, List, Optional, Set
from urllib.parse import urljoin, urlparse, urlunparse, quote, urlencode

from playwright.async_api import async_playwright, Page, TimeoutError as PlaywrightTimeoutError


def now_iso() -> str:
    return datetime.now().isoformat()


class MorganStanleyScraper:
    """
    Morgan Stanley Careers scraper for www.morganstanley.com/careers
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
        self.result_limit = sc.get("result_limit", 10)

        wp = self.cfg.get("playwright_options", {})
        self.sel_results_link = wp.get("results_link_selector", "a[href*='/careers/career-opportunities/'], .job-link")
        self.sel_show_more = wp.get("show_more_button", "button:has-text('Load More'), button[aria-label*='Load more'], .load-more")
        self.sel_detail_title = wp.get("detail_title", "h1, .job-title")
        self.sel_detail_desc = wp.get("detail_description", ".job-description, .job-details, main")
        self.sel_job_card = wp.get("job_card_selector", ".job-card, .opportunity-card, [data-job-id]")
        self.sel_see_details = wp.get("see_details_button", "a[href*='/careers/career-opportunities/'], .view-details")

        pg = self.g["playwright_global"]
        self.timeout = pg["timeout"]
        self.user_agent = pg["user_agent"]
        self.headless = self.g["global_settings"]["headless"]

        self.base_url = self.cfg["website_info"]["base_url"]

        self.scraped: List[Dict[str, Any]] = []
        self.seen_urls: Set[str] = set()

    async def scrape_jobs(self) -> List[Dict[str, Any]]:
        """Main entry point for scraping Morgan Stanley jobs"""
        start_time = datetime.now()
        
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=self.headless,
                    args=["--no-sandbox", "--disable-dev-shm-usage"]
                )
                
                context = await browser.new_context(
                    user_agent=self.user_agent,
                    viewport={"width": 1920, "height": 1080}
                )
                
                page = await context.new_page()
                
                # Build the search URL with location filters
                search_url = self._build_url_with_locations(self.base_url, self.target_locations)
                
                await self._open_list_page(page, search_url)
                await self._harvest_and_parse(page)
                
                await browser.close()

            end_time = datetime.now()
            duration = end_time - start_time
            duration_seconds = round(duration.total_seconds(), 2)
            
            self.logger.info(f"Morgan Stanley scraping finished. Total jobs collected: {len(self.scraped)}")
            self.logger.info(f"Scraping duration: {duration_seconds} seconds")
            
            # Add duration info to the scraped data
            self.scraping_duration = duration_seconds
            
            return self.scraped

        except Exception as e:
            end_time = datetime.now()
            duration = end_time - start_time
            duration_seconds = round(duration.total_seconds(), 2)
            
            self.logger.error(f"Morgan Stanley scraping error: {e}")
            self.logger.info(f"Scraping duration (with error): {duration_seconds} seconds")
            
            # Add duration info even on error
            self.scraping_duration = duration_seconds
            
            return self.scraped

    async def _open_list_page(self, page: Page, url: str) -> None:
        """Open the job listings page and apply location filters"""
        self.logger.info(f"Opening: {url}")
        await page.goto(url)
        await page.wait_for_load_state("domcontentloaded")
        await page.wait_for_timeout(self.sleep_after_nav_ms)
        
        # Apply location filters if configured
        if self.apply_filters_via_ui and self.target_locations:
            await self._apply_location_filters(page)

    async def _apply_location_filters(self, page: Page) -> None:
        """Apply location filters through the UI following the specific flow"""
        try:
            self.logger.info("Applying location filters via UI")
            
            # Wait for the page to load completely
            await page.wait_for_timeout(2000)  # Reduced from 3000ms
            
            # The location filtering seems to work partially through URL params or default behavior
            # Let's try a simpler approach focusing on what works
            
            # Method 1: Try to find and use dropdowns (without clicking invisible elements)
            for location in self.target_locations:
                self.logger.info(f"Attempting to filter for: {location}")
                
                # Look for select dropdowns that might be present
                try:
                    # Check if there are any country/location selects available
                    selects = await page.query_selector_all("select")
                    for select in selects:
                        try:
                            # Get the options to see if our location is available
                            options = await select.query_selector_all("option")
                            for option in options:
                                option_text = await option.inner_text()
                                if location.lower() in option_text.lower():
                                    # Try to select this option
                                    await option.click()
                                    self.logger.info(f"Selected {location} from dropdown")
                                    await page.wait_for_timeout(800)  # Reduced from 1000ms
                                    break
                        except Exception:
                            continue
                except Exception:
                    continue
            
            # Method 2: Check if there are any location checkboxes or filters visible
            try:
                checkboxes = await page.query_selector_all("input[type='checkbox']")
                for checkbox in checkboxes:
                    try:
                        # Get the label or nearby text
                        checkbox_container = await checkbox.evaluate("el => el.closest('label') || el.parentElement")
                        if checkbox_container:
                            container_text = await checkbox_container.inner_text()
                            for location in self.target_locations:
                                if location.lower() in container_text.lower():
                                    await checkbox.click()
                                    self.logger.info(f"Clicked checkbox for {location}")
                                    await page.wait_for_timeout(300)  # Reduced from 500ms
                                    break
                    except Exception:
                        continue
            except Exception:
                pass
            
            # Method 3: Try URL-based filtering as fallback
            current_url = page.url
            if "?" in current_url:
                # Add location parameters if possible
                try:
                    if "location" not in current_url.lower():
                        # Try to add location filter via URL
                        separator = "&" if "?" in current_url else "?"
                        location_param = f"{separator}location=US,CA"  # US and Canada codes
                        new_url = current_url + location_param
                        await page.goto(new_url)
                        await page.wait_for_load_state("networkidle")
                        self.logger.info("Applied location filter via URL parameter")
                except Exception:
                    pass
            
            self.logger.info("Location filtering process completed")
                
        except Exception as e:
            self.logger.warning(f"Failed to apply location filters: {e}")
            # Continue anyway, default filtering seems to be working

    def _build_url_with_locations(self, base: str, locations: List[str]) -> str:
        """Build URL with location filters applied"""
        # Morgan Stanley uses a search page that requires UI interaction for filtering
        # We'll start with the base URL and apply filters via UI
        return f"{base}?opportunity=sg"

    def _build_url_with_page(self, base: str, page_num: int, locations: List[str]) -> str:
        """Build URL with location filters and specific page number"""
        # Morgan Stanley uses client-side pagination, so we'll handle this via UI interaction
        return f"{base}?opportunity=sg"

    async def _harvest_and_parse(self, page: Page) -> None:
        """Harvest job links and parse details"""
        total = 0
        page_num = 1

        while total < self.max_jobs:
            self.logger.info(f"Processing page {page_num}")
            
            # Wait for page to load and add some debugging
            await page.wait_for_timeout(2000)  # Reduced from 3000ms - Give time for dynamic content
            
            # Debug: Check what's actually on the page
            page_title = await page.title()
            self.logger.info(f"Page title: {page_title}")
            
            # Check for common elements
            body_text = await page.inner_text("body")
            if "results" in body_text.lower() or "opportunities" in body_text.lower():
                self.logger.info("Found results/opportunities text on page")
            
            # Wait for job listings to load with multiple selectors
            job_cards_found = False
            card_selectors = [
                ".cmp-jobcard",
                "[class*='jobcard']", 
                "[data-analytics-link*='jobcard']",
                ".cmp-opportunity-result_set .cmp-opportunity-result_set .expand",
                "div[class*='cmp-jobcard']"
            ]
            
            for selector in card_selectors:
                try:
                    cards = await page.query_selector_all(selector)
                    if cards:
                        self.logger.info(f"Found {len(cards)} job cards using selector: {selector}")
                        job_cards_found = True
                        break
                except Exception:
                    continue
            
            if not job_cards_found:
                self.logger.warning("No job cards found on page")
                # Try to debug what elements are actually present
                all_divs = await page.query_selector_all("div")
                self.logger.info(f"Total divs on page: {len(all_divs)}")
                
                # Check if this is a no-results page
                if "no results" in body_text.lower() or "no opportunities" in body_text.lower():
                    self.logger.info("No more results found")
                    break
                
                # If we can't find job cards but there should be content, log for debugging
                self.logger.warning("Could not identify job card structure - may need selector updates")
                break

            # Collect job links from current page
            job_links = await self._collect_job_links(page, page_num)
            
            if not job_links:
                self.logger.warning(f"No job links found on page {page_num}")
                # Try to go to next page anyway in case this is a loading issue
                if page_num == 1:
                    break
                # For subsequent pages, try next page in case this was a temporary issue
                page_num += 1
                continue

            # Process each job on this page
            for i, job_url in enumerate(job_links):
                if total >= self.max_jobs:
                    break
                
                if job_url in self.seen_urls:
                    self.logger.debug(f"Skipping duplicate job: {job_url}")
                    continue
                    
                self.seen_urls.add(job_url)
                
                self.logger.info(f"Processing job {total + 1}/{self.max_jobs}: {job_url}")
                
                # Parse job details with retry logic
                job_data = None
                max_retries = 2
                for attempt in range(max_retries + 1):
                    try:
                        job_data = await self._parse_job_detail(page, job_url)
                        if job_data:
                            break
                        elif attempt < max_retries:
                            self.logger.info(f"Retrying job {job_url} (attempt {attempt + 2}/{max_retries + 1})")
                            await page.wait_for_timeout(1500)  # Reduced from 2000ms - Wait before retry
                    except Exception as e:
                        self.logger.warning(f"Attempt {attempt + 1} failed for {job_url}: {e}")
                        if attempt < max_retries:
                            await page.wait_for_timeout(1500)  # Reduced from 2000ms - Wait before retry
                
                if job_data:
                    self.scraped.append(job_data)
                    total += 1
                    self.logger.info(f"Successfully scraped job: {job_data.get('title', 'Unknown')}")
                else:
                    self.logger.warning(f"Failed to scrape job from: {job_url}")
                
                # Add optimized delay between jobs
                await page.wait_for_timeout(self.sleep_after_open_ms)

            # After processing all jobs on this page, try to go to next page
            if total < self.max_jobs:
                next_page_success = await self._go_to_next_page(page)
                if not next_page_success:
                    self.logger.info("No more pages available or failed to navigate to next page")
                    break
                page_num += 1
            else:
                self.logger.info(f"Reached max jobs limit ({self.max_jobs})")
                break

    async def _collect_job_links(self, page: Page, page_num: int = 1) -> List[str]:
        """Collect job URLs from current page"""
        job_links = set()  # Use set to automatically handle duplicates
        
        try:
            # First, let's check what selectors actually exist on the page
            await page.wait_for_timeout(1500)  # Reduced from 2000ms - Give page time to load
            
            # Method 1: Look for APPLY NOW buttons to get job IDs
            apply_buttons = await page.query_selector_all("a:has-text('APPLY NOW')")
            self.logger.info(f"Found {len(apply_buttons)} APPLY NOW buttons")
            
            for button in apply_buttons:
                try:
                    href = await button.get_attribute("href")
                    if href:
                        if href.startswith("/"):
                            # Relative URL, make it absolute
                            href = f"https://morganstanley.tal.net{href}"
                        elif not href.startswith("http"):
                            href = urljoin("https://morganstanley.tal.net", href)
                        job_links.add(href)  # Use add() for set
                        self.logger.debug(f"Found job link from APPLY NOW: {href}")
                except Exception as e:
                    self.logger.debug(f"Error extracting href from APPLY NOW button: {e}")
            
            # Method 2: If no APPLY NOW links, try job card content links
            if not job_links:
                card_link_selectors = [
                    ".cmp-jobcard__content",
                    ".cmp-jobcard__link", 
                    "[data-analytics-link*='jobcard']",
                    ".cmp-jobcard a"
                ]
                
                for selector in card_link_selectors:
                    try:
                        links = await page.query_selector_all(selector)
                        if links:
                            self.logger.info(f"Found {len(links)} card links using selector: {selector}")
                            for link in links:
                                href = await link.get_attribute("href")
                                data_link = await link.get_attribute("data-analytics-link")
                                
                                # Try href first
                                if href:
                                    if href.startswith("/"):
                                        href = f"https://morganstanley.tal.net{href}"
                                    elif not href.startswith("http"):
                                        href = urljoin("https://morganstanley.tal.net", href)
                                    job_links.add(href)
                                # Try to extract job ID from data attributes
                                elif data_link and "jobcard" in data_link:
                                    # Extract job ID from analytics data
                                    job_id_match = re.search(r'job[_\s]*(\d+)', data_link, re.IGNORECASE)
                                    if job_id_match:
                                        job_id = job_id_match.group(1)
                                        job_url = f"https://morganstanley.tal.net/vx/candidate/apply/{job_id}"
                                        job_links.add(job_url)
                            break
                    except Exception as e:
                        self.logger.debug(f"Selector {selector} failed: {e}")
                        continue

            # Method 3: Look for job IDs in the page and construct URLs
            if not job_links:
                try:
                    # Extract job IDs from page content
                    page_content = await page.content()
                    job_id_pattern = r'Job #\s*(\d+)|job["\s]*[:\s]*["\s]*(\d+)'
                    job_ids = re.findall(job_id_pattern, page_content, re.IGNORECASE)
                    
                    for match in job_ids:
                        job_id = match[0] or match[1]  # Get the non-empty group
                        if job_id:
                            job_url = f"https://morganstanley.tal.net/vx/candidate/apply/{job_id}"
                            job_links.add(job_url)
                            self.logger.debug(f"Constructed job URL from ID: {job_url}")
                    
                except Exception as e:
                    self.logger.debug(f"Failed to extract job IDs from page content: {e}")

            # Convert set back to list and maintain order
            unique_links = list(job_links)

            self.logger.info(f"Found {len(unique_links)} unique job links on page")
            return unique_links

        except Exception as e:
            self.logger.error(f"Error collecting job links: {e}")
            return []

    async def _parse_job_detail(self, listing_page: Page, job_url: str) -> Optional[Dict[str, Any]]:
        """Parse job details by opening a new page"""
        
        detail_page = None
        try:
            # Create a new page for job details to avoid navigation issues
            detail_page = await listing_page.context.new_page()
            
            # Navigate to job detail page
            await detail_page.goto(job_url)
            await detail_page.wait_for_load_state("networkidle")

            # Extract job details
            title = await self._extract_title(detail_page)
            raw_location = await self._extract_raw_location(detail_page)
            
            # Filter by location BEFORE enhancement
            if not self._is_target_location(raw_location):
                self.logger.info(f"Skipping job not in target locations: {title} ({raw_location})")
                await detail_page.close()
                return None
            
            # Now enhance the location for valid jobs
            location = self._enhance_location(raw_location)
            description = await self._extract_description(detail_page)
            
            # Extract metadata from the job details section
            metadata = await self._extract_metadata(detail_page)
            
            job_id = self._extract_job_id_from_url(job_url)

            job_data = {
                "jobId": f"MorganStanley_{job_id}",
                "title": title or "Unknown Title",
                "company": "Morgan Stanley",
                "location": location or "",
                "url": job_url,
                "source": "Morgan Stanley",
                "status": "active",
                "scraped_date": now_iso(),
            }

            # Add description only if it exists and is meaningful
            if description:
                job_data["description"] = description

            # Add metadata fields (but only clean, valid ones)
            if metadata:
                for key, value in metadata.items():
                    if value:  # Only add non-empty values
                        job_data[key] = value

            # Close the detail page
            await detail_page.close()
            
            return job_data

        except Exception as e:
            self.logger.warning(f"Failed to parse job detail: {e}")
            if detail_page:
                try:
                    await detail_page.close()
                except Exception:
                    pass
            return None

    async def _extract_title(self, page: Page) -> Optional[str]:
        """Extract job title from detail page"""
        try:
            title_selectors = [
                "h1",
                ".job-title",
                "[data-testid='job-title']",
                ".opportunity-title",
                "h1.title",
                ".page-title",
                "h2"  # Morgan Stanley might use h2 for job titles
            ]
            
            for selector in title_selectors:
                try:
                    title_el = await page.query_selector(selector)
                    if title_el:
                        title = await title_el.inner_text()
                        if title and title.strip():
                            # Clean up title
                            title = title.strip()
                            # Remove common prefixes/suffixes
                            title = re.sub(r'^(Job Title:|Title:)\s*', '', title, flags=re.IGNORECASE)
                            return title
                except Exception:
                    continue
                    
        except Exception as e:
            self.logger.debug(f"Error extracting title: {e}")
        return None

    async def _extract_location(self, page: Page) -> Optional[str]:
        """Extract location from detail page"""
        try:
            # Method 1: Look for structured location fields (based on screenshot)
            location_selectors = [
                "td:has-text('City') + td",
                "tr:has(td:has-text('City')) td:nth-child(2)",
                ".location",
                ".job-location",
                "[data-testid='location']",
                ".opportunity-location"
            ]
            
            city = None
            for selector in location_selectors:
                try:
                    location_el = await page.query_selector(selector)
                    if location_el:
                        location = await location_el.inner_text()
                        if location and location.strip():
                            # Clean up location text
                            city = location.replace("Location:", "").replace("City:", "").strip()
                            break
                except Exception:
                    continue

            # Method 2: Look for location in structured table format
            if not city:
                try:
                    # Look for table rows with location data
                    rows = await page.query_selector_all("tr")
                    for row in rows:
                        row_text = await row.inner_text()
                        if "city" in row_text.lower():
                            cells = await row.query_selector_all("td")
                            if len(cells) >= 2:
                                city = await cells[1].inner_text()
                                if city and city.strip():
                                    city = city.strip()
                                    break
                except Exception:
                    pass

            # Method 3: Look for location patterns in page text
            if not city:
                page_text = await page.inner_text("body")
                location_patterns = [
                    r'City[:\s]+([^,\n\t]+)',
                    r'Location[:\s]+([^,\n\t]+,\s*(?:United States|Canada|USA|US))',
                    r'([^,\n\t]+,\s*(?:United States|Canada|USA|US))',
                    r'([A-Za-z\s]+,\s*[A-Z]{2}(?:\s+\d{5})?)'  # City, State format
                ]
                
                for pattern in location_patterns:
                    match = re.search(pattern, page_text, re.IGNORECASE)
                    if match:
                        location = match.group(1).strip()
                        if len(location) > 2:  # Ensure it's not just initials
                            city = location
                            break

            # Now format the location properly
            if city:
                city = city.strip()
                # If it's just a city name, add country based on the target locations
                if "," not in city:
                    # Check if this job is in US or Canada based on common city names
                    us_cities = ["new york", "alpharetta", "atlanta", "chicago", "san francisco", "los angeles", "boston", "seattle"]
                    canadian_cities = ["toronto", "vancouver", "montreal", "calgary", "ottawa"]
                    
                    city_lower = city.lower()
                    if any(us_city in city_lower for us_city in us_cities):
                        return f"{city}, United States of America"
                    elif any(can_city in city_lower for can_city in canadian_cities):
                        return f"{city}, Canada"
                    else:
                        # Default to US for Morgan Stanley
                        return f"{city}, United States of America"
                else:
                    # Already has state/country info, enhance if needed
                    if "united states" not in city.lower() and "canada" not in city.lower():
                        if any(state in city.lower() for state in ["ga", "ny", "ca", "tx", "fl"]):
                            return f"{city}, United States of America"
                        else:
                            return f"{city}, United States of America"
                    return city

        except Exception as e:
            self.logger.debug(f"Error extracting location: {e}")
        
        return None

    async def _extract_raw_location(self, page: Page) -> Optional[str]:
        """Extract raw location/city name without enhancement"""
        try:
            city = None

            # Method 1: Look for location in page metadata or specific selectors
            location_selectors = [
                "td:has-text('City') + td",
                "tr:has(td:has-text('City')) td:nth-child(2)",
                ".location",
                ".job-location",
                "[data-testid='location']",
                ".opportunity-location"
            ]

            for selector in location_selectors:
                try:
                    element = await page.query_selector(selector)
                    if element:
                        location = await element.inner_text()
                        if location and location.strip():
                            # Clean up location text
                            city = location.replace("Location:", "").replace("City:", "").strip()
                            break
                except Exception:
                    continue

            # Method 2: Look for location in structured table format
            if not city:
                try:
                    # Look for table rows with location data
                    rows = await page.query_selector_all("tr")
                    for row in rows:
                        row_text = await row.inner_text()
                        if "city" in row_text.lower():
                            cells = await row.query_selector_all("td")
                            if len(cells) >= 2:
                                city = await cells[1].inner_text()
                                if city and city.strip():
                                    city = city.strip()
                                    break
                except Exception:
                    pass

            # Method 3: Look for location patterns in page text
            if not city:
                page_text = await page.inner_text("body")
                location_patterns = [
                    r'City[:\s]+([^,\n\t]+)',
                    r'Location[:\s]+([^,\n\t]+)',
                    r'([A-Za-z\s]+,\s*(?:United States|Canada|USA|US))',
                    r'([A-Za-z\s]+,\s*[A-Z]{2}(?:\s+\d{5})?)'  # City, State format
                ]
                
                for pattern in location_patterns:
                    match = re.search(pattern, page_text, re.IGNORECASE)
                    if match:
                        location = match.group(1).strip()
                        if len(location) > 2:  # Ensure it's not just initials
                            city = location
                            break

            return city.strip() if city else None

        except Exception as e:
            self.logger.debug(f"Error extracting raw location: {e}")
        
        return None

    def _is_target_location(self, raw_location: str) -> bool:
        """Check if raw location is in target countries/cities"""
        if not raw_location:
            return False
            
        location_lower = raw_location.lower()
        
        # Define known US and Canadian cities
        us_cities = [
            "new york", "alpharetta", "atlanta", "chicago", "san francisco", "los angeles", 
            "boston", "seattle", "minneapolis", "dallas", "denver", "miami", "philadelphia",
            "washington", "austin", "charlotte", "phoenix", "san diego", "detroit", "tampa"
        ]
        
        canadian_cities = [
            "toronto", "vancouver", "montreal", "calgary", "ottawa", "edmonton", 
            "winnipeg", "halifax", "quebec city", "victoria"
        ]
        
        # Check for explicit country mentions
        if any(country in location_lower for country in ["united states", "canada", "usa"]):
            return True
            
        # Check for known US cities
        if any(city in location_lower for city in us_cities):
            return True
            
        # Check for known Canadian cities
        if any(city in location_lower for city in canadian_cities):
            return True
            
        # Check for US state abbreviations or names
        us_states = ["ny", "ga", "ca", "tx", "fl", "il", "ma", "wa", "mn", "co", "nc", "az"]
        if any(f", {state}" in location_lower for state in us_states):
            return True
            
        # If none of the above match, it's likely not US/Canada
        return False

    def _enhance_location(self, raw_location: str) -> str:
        """Enhance location with proper country formatting for valid locations"""
        if not raw_location:
            return ""
            
        city = raw_location.strip()
        
        # If it's just a city name, add country based on known cities
        if "," not in city:
            us_cities = ["new york", "alpharetta", "atlanta", "chicago", "san francisco", "los angeles", "boston", "seattle", "minneapolis"]
            canadian_cities = ["toronto", "vancouver", "montreal", "calgary", "ottawa"]
            
            city_lower = city.lower()
            if any(us_city in city_lower for us_city in us_cities):
                return f"{city}, United States of America"
            elif any(can_city in city_lower for can_city in canadian_cities):
                return f"{city}, Canada"
            else:
                # For other valid cities, default to US (since this method is only called for valid locations)
                return f"{city}, United States of America"
        else:
            # Already has state/country info, enhance if needed
            if "united states" not in city.lower() and "canada" not in city.lower():
                if any(state in city.lower() for state in ["ga", "ny", "ca", "tx", "fl"]):
                    return f"{city}, United States of America"
                else:
                    return f"{city}, United States of America"
            return city

    async def _extract_description(self, page: Page) -> Optional[str]:
        """Extract job description from Morgan Stanley job detail page"""
        try:
            # Wait for content to load
            await page.wait_for_timeout(1500)  # Reduced from 2000ms
            
            # Morgan Stanley-specific selectors for job descriptions (based on screenshot analysis)
            desc_selectors = [
                "td:has-text('Job description') + td",
                "tr:has(td:has-text('Job description')) td:nth-child(2)",
                ".job-description",
                ".description_section",
                "[data-testid='job-description']",
                ".description",
                ".job-content",
                ".opportunity-content",
                "section:has(h2:has-text('Description'))",
                "div:has(h3:has-text('Description'))",
                "div:has(h2:has-text('Job Description'))",
                ".content-area",
                "main .content",
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
                        if desc_text and len(desc_text.strip()) > len(best_description):
                            best_description = desc_text.strip()
                            used_selector = selector
                except Exception:
                    continue
            
            # Method 2: Look for description in table structure
            if not best_description:
                try:
                    rows = await page.query_selector_all("tr")
                    for row in rows:
                        row_text = await row.inner_text()
                        if "job description" in row_text.lower():
                            cells = await row.query_selector_all("td")
                            if len(cells) >= 2:
                                desc_text = await cells[1].inner_text()
                                if desc_text and len(desc_text.strip()) > 50:
                                    best_description = desc_text.strip()
                                    used_selector = "table structure"
                                    break
                except Exception:
                    pass
            
            if best_description:
                self.logger.debug(f"Found description using selector: {used_selector}")
                # Clean up the description
                cleaned_description = self._clean_description(best_description)
                return cleaned_description
            
            # Fallback: try to get content from main content area
            try:
                main_content = await page.query_selector("main")
                if main_content:
                    content = await main_content.inner_text()
                    if content and len(content.strip()) > 100:
                        cleaned_content = self._clean_description(content)
                        return cleaned_content
            except Exception:
                pass
            
            return None  # Return None instead of error message

        except Exception as e:
            self.logger.debug(f"Error extracting description: {e}")
            return None

    def _clean_description(self, description: str) -> str:
        """Clean up the job description"""
        # Remove excessive whitespace
        description = re.sub(r'\s+', ' ', description)
        
        # Remove common Morgan Stanley header/footer content
        unwanted_patterns = [
            r'Morgan Stanley.*careers.*',
            r'Equal Opportunity Employer.*',
            r'Privacy Policy.*Terms.*',
            r'Apply Now.*',
            r'Share this job.*',
            r'Back to search.*',
            r'Related jobs.*',
            r'Morgan Stanley is an equal opportunity employer.*'
        ]
        
        for pattern in unwanted_patterns:
            description = re.sub(pattern, '', description, flags=re.IGNORECASE | re.DOTALL)
        
        # Remove leading/trailing whitespace
        description = description.strip()
        
        # If description is too short after cleaning, return a placeholder
        if len(description) < 50:
            return "Job description not fully available"
        
        return description

    async def _extract_metadata(self, page: Page) -> Dict[str, str]:
        """Extract metadata fields from Morgan Stanley job detail page"""
        metadata = {}
        
        try:
            # Morgan Stanley-specific metadata fields (based on screenshot analysis)
            metadata_fields = [
                ("Education Level", "education_level"),
                ("Business Unit", "business_unit"),
                ("City", "city"),
                ("Job description", "job_description"),
                ("Program Type", "program_type"),
                ("Job type", "job_type"),
                ("Experience level", "experience_level"),
                ("Employment type", "employment_type")
            ]

            # Method 1: Extract from table structure (td elements)
            for field_name, field_key in metadata_fields:
                try:
                    # Look for table rows with the field name
                    rows = await page.query_selector_all("tr")
                    for row in rows:
                        row_text = await row.inner_text()
                        if field_name.lower() in row_text.lower():
                            cells = await row.query_selector_all("td")
                            if len(cells) >= 2:
                                value = await cells[1].inner_text()
                                if value and value.strip() and field_name.lower() not in value.lower():
                                    # Clean the value to remove CSS artifacts
                                    cleaned_value = self._clean_metadata_value(value.strip())
                                    if cleaned_value:
                                        metadata[field_key] = cleaned_value
                                        break
                except Exception:
                    continue

            # Method 2: Look for structured data in definition lists (dt/dd)
            for field_name, field_key in metadata_fields:
                if field_key not in metadata:
                    try:
                        dt_elements = await page.query_selector_all("dt")
                        for dt in dt_elements:
                            dt_text = await dt.inner_text()
                            if field_name.lower() in dt_text.lower():
                                dd = await dt.query_selector("xpath=following-sibling::dd[1]")
                                if dd:
                                    value = await dd.inner_text()
                                    if value and value.strip():
                                        cleaned_value = self._clean_metadata_value(value.strip())
                                        if cleaned_value:
                                            metadata[field_key] = cleaned_value
                                            break
                    except Exception:
                        continue
            
            # Method 3: Look for label-value pairs in divs/spans
            for field_name, field_key in metadata_fields:
                if field_key not in metadata:
                    try:
                        # Look for labels with the field name
                        label_selectors = [
                            f"label:has-text('{field_name}')",
                            f"span:has-text('{field_name}')",
                            f"div:has-text('{field_name}')",
                            f"strong:has-text('{field_name}')",
                            f"b:has-text('{field_name}')"
                        ]
                        
                        for label_selector in label_selectors:
                            try:
                                label_el = await page.query_selector(label_selector)
                                if label_el:
                                    # Try to find the value in adjacent elements
                                    siblings = await page.query_selector_all(f"{label_selector} + *")
                                    for sibling in siblings:
                                        value = await sibling.inner_text()
                                        if value and value.strip() and field_name.lower() not in value.lower():
                                            cleaned_value = self._clean_metadata_value(value.strip())
                                            if cleaned_value:
                                                metadata[field_key] = cleaned_value
                                                break
                                    
                                    if field_key in metadata:
                                        break
                            except Exception:
                                continue
                    except Exception:
                        continue

            # Method 4: Extract specific Morgan Stanley fields from page text
            try:
                page_text = await page.inner_text("body")
                
                # Extract program type
                program_patterns = [
                    r'Program Type[:\s]+([^,\n]+)',
                    r'Type[:\s]+(Internship|Full-time|Part-time|Contract)',
                ]
                
                for pattern in program_patterns:
                    match = re.search(pattern, page_text, re.IGNORECASE)
                    if match and 'program_type' not in metadata:
                        cleaned_value = self._clean_metadata_value(match.group(1).strip())
                        if cleaned_value:
                            metadata['program_type'] = cleaned_value
                        break
                        
            except Exception:
                pass

        except Exception as e:
            self.logger.debug(f"Error extracting metadata: {e}")

        return metadata

    def _extract_job_id_from_url(self, url: str) -> str:
        """Extract job ID from URL"""
        try:
            # Morgan Stanley URL format: https://morganstanley.tal.net/vx/candidate/apply/20203
            match = re.search(r'/apply/(\d+)/?$', url)
            if match:
                return match.group(1)
            
            # Try alternative patterns
            match = re.search(r'[?&]id=([^&]+)', url)
            if match:
                return match.group(1)
                
            # Try to extract any number from the URL
            match = re.search(r'/(\d+)/?$', url)
            if match:
                return match.group(1)
                
            # Fallback: use last part of URL
            return url.split('/')[-1] or "UNKNOWN"
        except Exception:
            pass
        return "UNKNOWN"

    async def _go_to_next_page(self, page: Page) -> bool:
        """Navigate to next page"""
        try:
            # Wait for any existing navigation to complete
            await page.wait_for_load_state("domcontentloaded")
            await page.wait_for_timeout(1500)  # Reduced from 2000ms
            
            # First, try to find pagination elements
            self.logger.info("Looking for pagination elements...")
            
            # Check if pagination exists at all
            pagination_sections = await page.query_selector_all(".pagination, .opportunity.pagination, [class*='pagination'], [class*='paging']")
            if not pagination_sections:
                self.logger.info("No pagination section found on page")
                return False
            
            # Check if we're on the last page by looking for disabled next buttons or last page indicators
            disabled_next_selectors = [
                ".arrow.next.disabled",
                ".arrow.next[disabled]", 
                ".arrow.next[aria-disabled='true']",
                "a.arrow.next.disabled",
                ".pagination .next.disabled",
                ".pagination .arrow.next.disabled"
            ]
            
            for disabled_selector in disabled_next_selectors:
                try:
                    disabled_btn = await page.query_selector(disabled_selector)
                    if disabled_btn:
                        self.logger.info(f"Found disabled next button: {disabled_selector} - we're on the last page")
                        return False
                except Exception:
                    pass
            
            # Check if current page is the last page number in pagination
            try:
                # Get all page numbers
                page_numbers = await page.query_selector_all(".pagination a")
                if page_numbers:
                    max_page = 0
                    current_page = 0
                    
                    for page_link in page_numbers:
                        try:
                            page_text = await page_link.inner_text()
                            if page_text.isdigit():
                                page_num = int(page_text)
                                max_page = max(max_page, page_num)
                                
                                # Check if this is the active/current page
                                class_attr = await page_link.get_attribute("class") or ""
                                if "active" in class_attr.lower() or "current" in class_attr.lower():
                                    current_page = page_num
                        except Exception:
                            continue
                    
                    if current_page > 0 and max_page > 0 and current_page >= max_page:
                        self.logger.info(f"Currently on last page {current_page} of {max_page} - stopping pagination")
                        return False
                        
                    self.logger.info(f"Current page: {current_page}, Max page: {max_page}")
            except Exception as e:
                self.logger.debug(f"Failed to check page numbers: {e}")
            
            # Look for next page selectors with optimized timeout
            next_selectors = [
                ".arrow.next",  
                "a.arrow.next",
                ".pagination .arrow.next",
                ".opportunity.pagination .arrow.next",
                "a[aria-label*='Next']",
                ".pagination a:has-text('Next')",
                "button:has-text('Next')",
                "a:has-text('Next')",
                ".pagination .next:not(.disabled)",
                ".pagination-next:not(.disabled)"
            ]
            
            for selector in next_selectors:
                try:
                    # Use optimized timeout for each selector
                    next_btn = await page.wait_for_selector(selector, timeout=2500, state="visible")  # Reduced from 3000ms
                    if next_btn:
                        # Check if button is enabled
                        is_disabled = await next_btn.get_attribute("disabled")
                        aria_disabled = await next_btn.get_attribute("aria-disabled")
                        class_attr = await next_btn.get_attribute("class") or ""
                        
                        # Check if it's disabled by class name
                        is_disabled_by_class = "disabled" in class_attr.lower()
                        
                        if not is_disabled and aria_disabled != "true" and not is_disabled_by_class:
                            self.logger.info(f"Found clickable next button: {selector}")
                            
                            # Store current page content to check if navigation actually happened
                            current_url = page.url
                            current_page_content = await page.content()
                            
                            await next_btn.click()
                            
                            # Wait for navigation with optimized timeout
                            try:
                                await page.wait_for_load_state("domcontentloaded", timeout=8000)  # Reduced from 10000ms
                                await page.wait_for_timeout(self.sleep_after_nav_ms)
                                
                                # Check if we actually navigated to a new page
                                new_url = page.url
                                new_page_content = await page.content()
                                
                                # If URL didn't change and content is the same, we might be on the last page
                                if new_url == current_url and new_page_content == current_page_content:
                                    self.logger.info("Page content unchanged after clicking next - likely on last page")
                                    return False
                                
                                # Check if we have job cards on the new page
                                try:
                                    job_cards = await page.query_selector_all(self.job_card_selector)
                                    if not job_cards:
                                        self.logger.info("No job cards found after clicking next - likely on last page")
                                        return False
                                except Exception:
                                    pass
                                
                                self.logger.info("Successfully navigated to next page")
                                return True
                            except Exception as nav_e:
                                self.logger.warning(f"Navigation after click failed: {nav_e}")
                                # Continue to try other selectors
                                continue
                        else:
                            self.logger.info(f"Next button found but disabled: {selector}")
                except Exception as e:
                    self.logger.debug(f"Selector {selector} not found or failed: {e}")
                    continue
            
            # Try numbered pagination - look for the next number
            try:
                self.logger.info("Trying numbered pagination...")
                # Get current page number
                current_page_elements = await page.query_selector_all(".pagination a.current, .pagination a.active, .pagination .active, .pagination .current")
                if current_page_elements:
                    current_page_text = await current_page_elements[0].inner_text()
                    try:
                        current_page_num = int(current_page_text.strip())
                        next_page_num = current_page_num + 1
                        
                        # First check if next page number exists in pagination
                        next_page_exists = False
                        all_page_links = await page.query_selector_all(".pagination a")
                        for page_link in all_page_links:
                            try:
                                page_text = await page_link.inner_text()
                                if page_text.strip() == str(next_page_num):
                                    next_page_exists = True
                                    break
                            except Exception:
                                continue
                        
                        if not next_page_exists:
                            self.logger.info(f"Next page {next_page_num} does not exist in pagination - we're on the last page")
                            return False
                        
                        # Look for next page number link
                        next_page_selectors = [
                            f".pagination a:has-text('{next_page_num}')",
                            f".pagination button:has-text('{next_page_num}')",
                            f"[data-page='{next_page_num}']"
                        ]
                        
                        for page_selector in next_page_selectors:
                            try:
                                next_page_link = await page.wait_for_selector(page_selector, timeout=1800, state="visible")  # Reduced from 2000ms
                                if next_page_link:
                                    self.logger.info(f"Clicking page number: {next_page_num}")
                                    await next_page_link.click()
                                    await page.wait_for_load_state("domcontentloaded", timeout=8000)  # Reduced from 10000ms
                                    await page.wait_for_timeout(self.sleep_after_nav_ms)
                                    return True
                            except Exception:
                                continue
                    except ValueError:
                        pass
            except Exception as e:
                self.logger.debug(f"Numbered pagination failed: {e}")
            
            # Try URL-based pagination as last resort
            try:
                current_url = page.url
                self.logger.info(f"Trying URL-based pagination from: {current_url}")
                
                # Look for page parameter in URL
                if "page=" in current_url:
                    # Extract current page and increment
                    import re
                    match = re.search(r'page=(\d+)', current_url)
                    if match:
                        current_page = int(match.group(1))
                        next_page = current_page + 1
                        new_url = re.sub(r'page=\d+', f'page={next_page}', current_url)
                        self.logger.info(f"Navigating to: {new_url}")
                        await page.goto(new_url)
                        await page.wait_for_load_state("domcontentloaded")
                        await page.wait_for_timeout(self.sleep_after_nav_ms)
                        return True
                else:
                    # Try adding page parameter
                    separator = "&" if "?" in current_url else "?"
                    new_url = f"{current_url}{separator}page=2"
                    self.logger.info(f"Trying page parameter: {new_url}")
                    await page.goto(new_url)
                    await page.wait_for_load_state("domcontentloaded")
                    await page.wait_for_timeout(self.sleep_after_nav_ms)
                    return True
                    
            except Exception as e:
                self.logger.debug(f"URL-based pagination failed: {e}")
            
            self.logger.info("No working pagination method found")
            return False

        except Exception as e:
            self.logger.error(f"Error navigating to next page: {e}")
            return False

    def _clean_metadata_value(self, value: str) -> Optional[str]:
        """Clean metadata values to remove CSS artifacts and unwanted content"""
        if not value:
            return None
            
        # Remove CSS-related artifacts
        css_patterns = [
            r'/\*[^*]*\*+(?:[^/*][^*]*\*+)*/',  # More precise CSS comment removal
            r'generated inline style',  # Remove this specific text
            r'style\s*=\s*["\'][^"\']*["\']',  # Remove style attributes
            r'class\s*=\s*["\'][^"\']*["\']',  # Remove class attributes
            r'<[^>]*>',  # Remove any HTML tags
            r'/\*.*?\*/',  # Remove /* ... */ comments (more aggressive)
            r'generated.*?inline.*?style',  # Remove variations of generated inline style
        ]
        
        cleaned = value
        for pattern in css_patterns:
            cleaned = re.sub(pattern, '', cleaned, flags=re.IGNORECASE | re.DOTALL)
        
        # Clean up whitespace
        cleaned = re.sub(r'\s+', ' ', cleaned).strip()
        
        # Additional checks for CSS artifacts
        if '/*' in cleaned or '*/' in cleaned or 'generated' in cleaned.lower():
            return None
        
        # Return None if the value is too short or contains only special characters
        if len(cleaned) < 2 or not re.search(r'[a-zA-Z0-9]', cleaned):
            return None
            
        return cleaned