import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Set
from urllib.parse import urljoin, urlparse, urlunparse, quote, urlencode

from playwright.async_api import async_playwright, Page, TimeoutError as PlaywrightTimeoutError


def now_iso() -> str:
    return datetime.now().isoformat()


class JpmorganScraper:
    """
    JPMorgan Chase Careers scraper for jpmc.fa.oraclecloud.com
    """

    def __init__(self, website_config: Dict[str, Any], global_config: Dict[str, Any]):
        self.cfg = website_config
        self.g = global_config
        self.logger = logging.getLogger("job_tracker")

        sc = self.cfg.get("scraping_config", {})
        self.max_jobs = sc.get("max_jobs", 100)
        self.sleep_after_nav_ms = sc.get("sleep_after_nav_ms", 2000)
        self.sleep_after_open_ms = sc.get("sleep_after_open_ms", 1000)
        self.apply_filters_via_ui = sc.get("apply_filters_via_ui", False)
        self.target_locations: List[str] = sc.get("locations", ["United States", "Canada"])
        self.result_limit = sc.get("result_limit", 20)
        self.location_ids = sc.get("location_ids", {
            "United States": "300000000289738",
            "Canada": "300000000289162"
        })

        wp = self.cfg.get("playwright_options", {})
        self.sel_results_link = wp.get("results_link_selector", "a.job-grid-item__link, a[href*='/preview/']")
        self.sel_show_more = wp.get("show_more_button", "button[aria-label*='Next'], button:has-text('Next')")
        self.sel_detail_title = wp.get("detail_title", "h1.heading.job-details__title, h1")
        self.sel_detail_desc = wp.get("detail_description", "div:has-text('JOB DESCRIPTION')")
        self.sel_job_card = wp.get("job_card_selector", "div.job-grid-item, .job-tile")
        self.sel_see_details = wp.get("see_details_button", "a.job-grid-item__link")
        self.sel_job_info_section = wp.get("job_info_section", "div:has-text('JOB INFORMATION')")
        self.sel_job_meta_item = wp.get("job_meta_item", "li.job-meta__item")

        pg = self.g["playwright_global"]
        self.timeout = pg["timeout"]
        self.user_agent = pg["user_agent"]
        self.headless = self.g["global_settings"]["headless"]

        self.base_url = self.cfg["website_info"]["base_url"]

        self.scraped: List[Dict[str, Any]] = []
        self.seen_urls: Set[str] = set()

    async def scrape_jobs(self) -> List[Dict[str, Any]]:
        """Main entry point for scraping JPMorgan jobs"""
        start_time = datetime.now()
        
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=self.headless)
                context = await browser.new_context(
                    user_agent=self.user_agent,
                    viewport={"width": 1920, "height": 1080}
                )
                page = await context.new_page()
                page.set_default_timeout(self.timeout)

                # Scrape jobs from each location separately
                # Get max_jobs from each location (not split between them)
                
                for location in self.target_locations:
                    self.logger.info(f"Starting to scrape jobs for location: {location}")
                    
                    # Build URL for specific location
                    url = self._build_url_with_locations(self.base_url, [location])
                    await self._open_list_page(page, url)
                    
                    # Get full max_jobs count from this location
                    await self._harvest_and_parse(page)
                    
                    self.logger.info(f"Completed scraping for {location}. Total jobs so far: {len(self.scraped)}")

                await browser.close()

            end_time = datetime.now()
            duration = end_time - start_time
            duration_seconds = round(duration.total_seconds(), 2)
            
            self.logger.info(f"JPMorgan scraping finished. Total jobs collected: {len(self.scraped)}")
            self.logger.info(f"Scraping duration: {duration_seconds} seconds")
            
            # Add duration info to the scraped data
            self.scraping_duration = duration_seconds
            
            return self.scraped

        except Exception as e:
            end_time = datetime.now()
            duration = end_time - start_time
            duration_seconds = round(duration.total_seconds(), 2)
            
            self.logger.error(f"JPMorgan scraping error: {e}")
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
            return f"{base}?mode=location"
        
        # JPMorgan uses specific location and locationId parameters
        # Default to first location if multiple specified
        location = locations[0] if locations else "United States"
        location_id = self.location_ids.get(location, self.location_ids["United States"])
        
        params = [
            f"location={quote(location)}",
            f"locationId={location_id}",
            "locationLevel=country",
            "mode=location"
        ]
        
        query_string = "&".join(params)
        return f"{base}?{query_string}"

    def _build_url_with_page(self, base: str, page_num: int, locations: List[str]) -> str:
        """Build URL with location filters and pagination"""
        if not locations:
            locations = ["United States"]
        
        # JPMorgan pagination seems to work differently - may need to handle via UI
        location = locations[0] if locations else "United States"
        location_id = self.location_ids.get(location, self.location_ids["United States"])
        
        params = [
            f"location={quote(location)}",
            f"locationId={location_id}",
            "locationLevel=country",
            "mode=location"
        ]
        
        # Add pagination if supported (may need adjustment based on testing)
        if page_num > 1:
            params.append(f"page={page_num}")
        
        query_string = "&".join(params)
        return f"{base}?{query_string}"

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
            if "Jobs" in body_text or "results" in body_text:
                self.logger.info("Found job listings on page")
            
            # Wait for job listings to load with multiple selectors
            job_cards_found = False
            card_selectors = [
                "div.job-grid-item",
                ".job-tile",
                ".job-grid-item.search-results",
                "div[class*='job-grid-item']",
                "div[class*='job']"
            ]
            
            for selector in card_selectors:
                try:
                    await page.wait_for_selector(selector, timeout=5000)
                    cards = await page.query_selector_all(selector)
                    if cards:
                        self.logger.info(f"Found {len(cards)} job cards using selector: {selector}")
                        job_cards_found = True
                        break
                except:
                    continue
            
            if not job_cards_found:
                self.logger.warning("No job cards found on page")
                # Try to save page content for debugging
                try:
                    content = await page.content()
                    with open(f"debug/jpmorgan_page_{page_num}.html", "w", encoding="utf-8") as f:
                        f.write(content)
                    self.logger.info(f"Saved page content to debug/jpmorgan_page_{page_num}.html")
                except:
                    pass
                break

            # Collect job links from current page
            job_links = await self._collect_job_links(page, page_num)
            
            if not job_links:
                self.logger.warning(f"No job links found on page {page_num}")
                if page_num == 1:
                    self.logger.error("No job links found on first page - check selectors")
                break

            # Process each job on this page
            for i, job_url in enumerate(job_links):
                if total >= self.max_jobs:
                    break
                
                if job_url in self.seen_urls:
                    self.logger.debug(f"Skipping duplicate URL: {job_url}")
                    continue
                
                self.seen_urls.add(job_url)
                self.logger.info(f"Processing job {total + 1}/{self.max_jobs}: {job_url}")
                
                job_data = await self._parse_job_detail(page, job_url)
                if job_data:
                    self.scraped.append(job_data)
                    total += 1
                    self.logger.info(f"Successfully scraped job: {job_data.get('title', 'Unknown')}")
                else:
                    self.logger.warning(f"Failed to parse job: {job_url}")
                
                # Small delay between job detail requests
                await page.wait_for_timeout(self.sleep_after_open_ms)

            # After processing all jobs on this page, try to go to next page
            if total < self.max_jobs:
                if not await self._go_to_next_page(page):
                    self.logger.info("No more pages available")
                    break
                page_num += 1
            else:
                self.logger.info(f"Reached max jobs limit ({self.max_jobs})")
                break

    async def _collect_job_links(self, page: Page, page_num: int = 1) -> List[str]:
        """Collect job URLs from current page"""
        job_links = []
        
        try:
            # First, let's check what selectors actually exist on the page
            await page.wait_for_timeout(2000)  # Give page time to load
            
            # Method 1: Look for job cards using multiple possible selectors
            card_selectors = [
                "div.job-grid-item",
                ".job-tile",
                ".job-grid-item.search-results",
                "div[class*='job-grid-item']",
                "div[class*='job']"
            ]
            
            cards = []
            for selector in card_selectors:
                try:
                    cards = await page.query_selector_all(selector)
                    if cards:
                        self.logger.info(f"Found {len(cards)} job cards using selector: {selector}")
                        break
                except:
                    continue
            
            if not cards:
                self.logger.warning("No job cards found")
                return []
            
            for i, card in enumerate(cards):
                try:
                    # Look for job links within each card
                    link_selectors = [
                        "a.job-grid-item__link",
                        "a[href*='/preview/']",
                        "a[href*='/jobs/']",
                        "a[data-bind*='click']",
                        "h3 a",
                        "h2 a",
                        "a"
                    ]
                    
                    job_link = None
                    for link_sel in link_selectors:
                        try:
                            link_elem = await card.query_selector(link_sel)
                            if link_elem:
                                href = await link_elem.get_attribute("href")
                                if href and ("/preview/" in href or "/jobs/" in href):
                                    if href.startswith("/"):
                                        parsed_url = urlparse(self.base_url)
                                        job_link = f"{parsed_url.scheme}://{parsed_url.netloc}{href}"
                                    else:
                                        job_link = href
                                    break
                        except:
                            continue
                    
                    if job_link:
                        job_links.append(job_link)
                        self.logger.debug(f"Found job link {i+1}: {job_link}")
                    else:
                        self.logger.debug(f"No valid job link found in card {i+1}")
                        
                except Exception as e:
                    self.logger.debug(f"Error processing card {i+1}: {e}")
                    continue

            # Method 2: If still no links, try direct link extraction
            if not job_links:
                direct_link_selectors = [
                    "a.job-grid-item__link",
                    "a[href*='/preview/']",
                    "a[href*='/jobs/']"
                ]
                
                for selector in direct_link_selectors:
                    try:
                        links = await page.query_selector_all(selector)
                        for link in links:
                            href = await link.get_attribute("href")
                            if href:
                                if href.startswith("/"):
                                    parsed_url = urlparse(self.base_url)
                                    full_url = f"{parsed_url.scheme}://{parsed_url.netloc}{href}"
                                else:
                                    full_url = href
                                job_links.append(full_url)
                        if job_links:
                            break
                    except:
                        continue

            self.logger.info(f"Found {len(job_links)} job links on page")
            return job_links  # Return all job links found on this page

        except Exception as e:
            self.logger.error(f"Error collecting job links: {e}")
            return []

    async def _parse_job_detail(self, listing_page: Page, job_url: str) -> Optional[Dict[str, Any]]:
        """Parse job details by navigating to job detail page"""
        
        try:
            # Store current URL to navigate back
            current_url = listing_page.url
            
            # Navigate to job detail page
            await listing_page.goto(job_url)
            await listing_page.wait_for_load_state("networkidle")

            # Extract job details
            title = await self._extract_title(listing_page)
            location = await self._extract_location(listing_page)
            description = await self._extract_description(listing_page)
            
            # Extract metadata from the job details section
            metadata = await self._extract_metadata(listing_page)
            
            job_id = self._extract_job_id_from_url(job_url)

            job_data = {
                "jobId": f"JPMorgan_{job_id}",
                "title": title or "Unknown Title",
                "company": "JPMorgan Chase",
                "location": location or "",
                "url": job_url,
                "description": description or "",
                "source": "JPMorgan",
                "status": "active",
                "scraped_date": now_iso(),
            }

            # Add metadata fields
            if metadata:
                # Clean metadata before adding to job_data
                cleaned_metadata = {}
                for key, value in metadata.items():
                    # Skip unwanted fields and malformed entries
                    if (key not in ["job_identification", "base_pay"] and 
                        not any(char in key for char in ['\n', '_09', '_06', '_20']) and
                        len(key) < 50 and
                        not key.startswith('posting_date\n') and
                        not (key == "base_pay" and value == "/Salary")):
                        cleaned_metadata[key] = value
                
                job_data.update(cleaned_metadata)

            # Navigate back to listing page
            await listing_page.goto(current_url)
            await listing_page.wait_for_load_state("networkidle")
            
            return job_data

        except Exception as e:
            self.logger.warning(f"Failed to parse job detail: {e}")
            # Try to navigate back to listing page
            try:
                await listing_page.goto(current_url)
                await listing_page.wait_for_load_state("networkidle")
            except:
                pass
            return None

    async def _extract_title(self, page: Page) -> Optional[str]:
        """Extract job title from detail page"""
        try:
            title_selectors = [
                "h1.heading.job-details__title",
                "h1[class*='job-details']",
                "h1",
                ".job-details__title",
                "h2[class*='title']"
            ]
            
            for selector in title_selectors:
                try:
                    title_el = await page.query_selector(selector)
                    if title_el:
                        title = await title_el.inner_text()
                        title = title.strip()
                        if title and len(title) > 2:
                            return title
                except:
                    continue
                    
        except Exception as e:
            self.logger.debug(f"Error extracting title: {e}")
        return None

    async def _extract_location(self, page: Page) -> Optional[str]:
        """Extract all locations from detail page, separated by semicolons"""
        try:
            all_locations = []
            
            # Method 1: Look for the "Locations" field in the job information section
            try:
                job_info_text = await page.inner_text("body")
                
                # Find the "Locations" section and extract all location lines
                locations_match = re.search(r'Locations\s*(.*?)(?=\n\s*[A-Z][a-z\s]+\s*:|Job Schedule|Job Shift|Base Pay|$)', job_info_text, re.DOTALL | re.IGNORECASE)
                if locations_match:
                    locations_text = locations_match.group(1).strip()
                    
                    # Split by lines and extract each location
                    location_lines = [line.strip() for line in locations_text.split('\n') if line.strip()]
                    
                    for line in location_lines:
                        # Clean up location marker symbols and extract address
                        clean_line = re.sub(r'^[📍🎯📌]\s*', '', line).strip()
                        
                        # Only accept lines that look like addresses (have numbers, commas, state codes)
                        if re.match(r'\d+.*,.*[A-Z]{2}.*\d{5}.*[A-Z]{2}$', clean_line):
                            all_locations.append(clean_line)
                        elif re.match(r'\d+\s+[^,]+,\s*[^,]+,\s*[A-Z]{2},?\s*\d{5},?\s*[A-Z]{2}$', clean_line):
                            all_locations.append(clean_line)
                
                if all_locations:
                    # Remove duplicates while preserving order
                    unique_locations = []
                    for loc in all_locations:
                        if loc not in unique_locations:
                            unique_locations.append(loc)
                    return '; '.join(unique_locations)
                    
            except Exception as e:
                self.logger.debug(f"Error in method 1: {e}")

            # Method 2: JPMorgan shows location right under the title
            location_selectors = [
                ".job-details__subtitle",
                "div[class*='subtitle']",
                "p:nth-child(2)",  # Usually second element after title
                "span[data-bind*='primaryLocation']"
            ]
            
            for selector in location_selectors:
                try:
                    location_el = await page.query_selector(selector)
                    if location_el:
                        location = await location_el.inner_text()
                        location = location.strip()
                        if location and len(location) > 2 and ("," in location or "Canada" in location or "United States" in location):
                            return location
                except:
                    continue

            # Method 3: Fallback - look for structured address patterns only
            try:
                # Look for address patterns: street number + street, city, state, zip, country
                address_patterns = [
                    r'(\d+\s+[^,\n]+,\s*[^,\n]+,\s*[A-Z]{2},?\s*\d{5},?\s*[A-Z]{2})',  # Full address
                    r'([^,\n]+,\s*[A-Z]{2},?\s*(?:Canada|United States))',  # City, State, Country
                    r'([^,\n]+,\s*(?:ON|NY|CA|BC|AB|SK|MB|NB|NS|PE|NL|QC|NT|NU|YT),?\s*Canada)',  # Canadian provinces
                    r'([^,\n]+,\s*[A-Z]{2},?\s*United States)'  # US states
                ]
                
                for pattern in address_patterns:
                    matches = re.findall(pattern, job_info_text, re.IGNORECASE)
                    if matches:
                        valid_locations = []
                        for match in matches:
                            location = match.strip() if isinstance(match, str) else match[0].strip()
                            # Additional filtering to avoid picking up job descriptions
                            if (len(location) < 100 and  # Reasonable length
                                not any(word in location.lower() for word in ['role', 'position', 'team', 'experience', 'skills', 'responsibilities', 'join']) and
                                ',' in location):  # Must have comma structure
                                valid_locations.append(location)
                        
                        if valid_locations:
                            # Remove duplicates while preserving order
                            unique_locations = []
                            for loc in valid_locations:
                                if loc not in unique_locations:
                                    unique_locations.append(loc)
                            return '; '.join(unique_locations[:5])  # Limit to first 5 unique locations
                            
            except Exception as e:
                self.logger.debug(f"Error in method 3: {e}")

        except Exception as e:
            self.logger.debug(f"Error extracting location: {e}")
        return None

    async def _extract_description(self, page: Page) -> str:
        """Extract job description from JPMorgan job detail page"""
        try:
            # Wait for content to load
            await page.wait_for_timeout(3000)
            
            desc_sections = []
            
            # Method 1: Look for "JOB DESCRIPTION" section with better targeting
            try:
                # Try to find the section after "JOB DESCRIPTION" heading
                elements = await page.query_selector_all("*")
                found_job_desc = False
                
                for element in elements:
                    try:
                        text = await element.inner_text()
                        if "JOB DESCRIPTION" in text and len(text.strip()) < 50:
                            found_job_desc = True
                            continue
                        
                        if found_job_desc and len(text.strip()) > 100:
                            # This might be the description content
                            cleaned_text = self._clean_description(text)
                            if len(cleaned_text) > 200:
                                desc_sections.append(cleaned_text)
                                break
                    except:
                        continue
            except:
                pass
            
            # Method 2: Look for Organization Description section
            try:
                # Find text that comes after "Organization Description"
                page_content = await page.inner_text("body")
                org_desc_match = re.search(r'Organization Description\s*(.*?)(?:JOB DESCRIPTION|$)', page_content, re.DOTALL | re.IGNORECASE)
                if org_desc_match:
                    org_desc = org_desc_match.group(1).strip()
                    if len(org_desc) > 100:
                        desc_sections.append(self._clean_description(org_desc))
            except:
                pass
            
            # Method 3: Look for the main job content area
            try:
                # Try to get content from the main modal/dialog area
                modal_selectors = [
                    "[role='dialog']",
                    ".modal-content",
                    "[class*='modal']",
                    "[class*='dialog']"
                ]
                
                for selector in modal_selectors:
                    try:
                        modal = await page.query_selector(selector)
                        if modal:
                            modal_text = await modal.inner_text()
                            
                            # Extract meaningful content after the job info section
                            job_desc_match = re.search(r'Job Schedule.*?Full time.*?(.*)', modal_text, re.DOTALL | re.IGNORECASE)
                            if job_desc_match:
                                desc_content = job_desc_match.group(1).strip()
                                cleaned_desc = self._clean_description(desc_content)
                                if len(cleaned_desc) > 200:
                                    desc_sections.append(cleaned_desc)
                                    break
                    except:
                        continue
            except:
                pass
            
            # Method 4: Try to get all text and extract the description part
            if not desc_sections:
                try:
                    full_text = await page.inner_text("body")
                    
                    # Look for patterns that indicate job description content
                    desc_patterns = [
                        r'Organization Description\s*(.*?)(?=JOB INFORMATION|$)',
                        r'JOB DESCRIPTION\s*(.*?)(?=Apply Now|$)',
                        r'Job Schedule\s*Full time\s*(.*?)(?=Apply Now|$)',
                        r'(?:Day|Full time)\s*((?:.*?\n.*?){10,})'  # Look for substantial text after basic info
                    ]
                    
                    for pattern in desc_patterns:
                        match = re.search(pattern, full_text, re.DOTALL | re.IGNORECASE)
                        if match:
                            desc_content = match.group(1).strip()
                            cleaned_desc = self._clean_description(desc_content)
                            if len(cleaned_desc) > 200:
                                desc_sections.append(cleaned_desc)
                                break
                except:
                    pass
            
            # Combine all sections
            if desc_sections:
                combined_desc = "\n\n".join(desc_sections[:2])  # Limit to first 2 sections
                return self._clean_description(combined_desc)
            
            return "Job description not found on page"

        except Exception as e:
            self.logger.debug(f"Error extracting description: {e}")
            return f"Description extraction error: {str(e)}"

    def _clean_description(self, description: str) -> str:
        """Clean up the job description"""
        if not description:
            return ""
            
        # Remove excessive whitespace
        description = re.sub(r'\s+', ' ', description)
        
        # Remove common unwanted content
        unwanted_patterns = [
            r'Skip to main content\.',
            r'View More Jobs',
            r'Apply now.*',
            r'Share this job.*',
            r'Print this job.*',
            r'Save this job.*',
            r'Similar jobs.*',
            r'Related jobs.*',
            r'JOB INFORMATION.*?Job Shift\s*Day',
            r'Job Identification.*?Job Schedule\s*Full time',
            r'^.*?United States\s*',  # Remove location header
        ]
        
        for pattern in unwanted_patterns:
            description = re.sub(pattern, '', description, flags=re.IGNORECASE | re.DOTALL)
        
        # Remove leading/trailing whitespace
        description = description.strip()
        
        # If description is too short, return placeholder
        if len(description) < 50:
            return "Job description content not fully available"
        
        return description

    async def _extract_metadata(self, page: Page) -> Dict[str, str]:
        """Extract metadata fields from JPMorgan job detail page"""
        metadata = {}
        
        try:
            # JPMorgan has structured job information in the modal
            # Look for the JOB INFORMATION section
            
            # Method 1: Extract from job meta items (li.job-meta__item)
            try:
                meta_items = await page.query_selector_all("li.job-meta__item")
                for item in meta_items:
                    try:
                        item_text = await item.inner_text()
                        # Parse structured data like "Job Category: Account Service"
                        if ":" in item_text:
                            key, value = item_text.split(":", 1)
                            key = key.strip().lower().replace(" ", "_")
                            value = value.strip()
                            
                            # Skip unwanted fields and malformed entries
                            if (key not in ["job_identification"] and 
                                not any(char in key for char in ['\n', '_09', '_06']) and
                                len(key) < 50):
                                metadata[key] = value
                    except:
                        continue
            except:
                pass
            
            # Method 2: Extract specific fields we want (excluding unwanted ones)
            field_mapping = {
                "Job Category": "job_category", 
                "Business Unit": "business_unit",
                "Posting Date": "posting_date",
                "Job Schedule": "job_schedule",
                "Job Shift": "job_shift",
                "Salary Range": "salary_range",
                "Compensation": "compensation",
                "Pay Range": "pay_range"
            }
            
            page_text = await page.inner_text("body")
            
            for field_name, field_key in field_mapping.items():
                try:
                    # Look for patterns like "Job Category Account Service"
                    pattern = rf'{re.escape(field_name)}\s*([^\n]+)'
                    match = re.search(pattern, page_text, re.IGNORECASE)
                    if match:
                        value = match.group(1).strip()
                        # Clean up the value and check for reasonable length
                        if value and len(value) < 100:
                            metadata[field_key] = value
                except:
                    continue
            
            # Method 3: Look for salary/compensation information in various formats
            try:
                salary_patterns = [
                    r'Base Pay/Salary[:\s]*([^,\n]+,\s*[A-Z]{2})[;\s]*\$?([\d,]+\.?\d*)\s*-\s*\$?([\d,]+\.?\d*)',
                    r'Salary[:\s]*([^,\n]+,\s*[A-Z]{2})[;\s]*\$?([\d,]+\.?\d*)\s*-\s*\$?([\d,]+\.?\d*)',
                    r'([A-Za-z\s]+,\s*[A-Z]{2})[;\s]*\$?([\d,]+\.?\d*)\s*-\s*\$?([\d,]+\.?\d*)',
                    r'\$?([\d,]+\.?\d*)\s*-\s*\$?([\d,]+\.?\d*)'
                ]
                
                for pattern in salary_patterns:
                    match = re.search(pattern, page_text, re.IGNORECASE)
                    if match:
                        groups = match.groups()
                        if len(groups) >= 3:  # Location + salary range
                            location = groups[0]
                            min_sal = groups[1]
                            max_sal = groups[2]
                            metadata['salary'] = f"{location} ${min_sal} - ${max_sal}"
                        elif len(groups) == 2:  # Just salary range
                            min_sal = groups[0]
                            max_sal = groups[1]
                            metadata['salary'] = f"${min_sal} - ${max_sal}"
                        break
            except:
                pass
            
            # Method 4: Extract from structured sections (exclude job_identification)
            try:
                # Look for "JOB INFORMATION" section specifically
                job_info_section = await page.query_selector("div:has-text('JOB INFORMATION')")
                if job_info_section:
                    # Get the parent container
                    parent = await job_info_section.query_selector("xpath=..")
                    if parent:
                        info_text = await parent.inner_text()
                        
                        # Parse the structured information, excluding job identification
                        lines = info_text.split('\n')
                        for line in lines:
                            line = line.strip()
                            if not line or "Job Identification" in line:
                                continue
                                
                            # Check if this is a field label
                            for field_name, field_key in field_mapping.items():
                                if field_name in line:
                                    # Extract the value part
                                    value = line.replace(field_name, "").strip()
                                    if value:
                                        metadata[field_key] = value
                                    break
            except:
                pass

        except Exception as e:
            self.logger.debug(f"Error extracting metadata: {e}")

        return metadata

    def _extract_job_id_from_url(self, url: str) -> str:
        """Extract job ID from URL"""
        try:
            # JPMorgan URLs look like: /jobs/preview/210672360/
            patterns = [
                r'/preview/([0-9]+)',
                r'/jobs/([0-9]+)',
                r'jobId=([0-9]+)',
                r'/([0-9]{8,})'  # 8+ digit numbers
            ]
            
            for pattern in patterns:
                match = re.search(pattern, url)
                if match:
                    return match.group(1)
                    
        except Exception:
            pass
        return "UNKNOWN"

    async def _go_to_next_page(self, page: Page) -> bool:
        """Navigate to next page"""
        try:
            # JPMorgan likely uses infinite scroll or load more buttons
            # Look for common pagination patterns
            
            # Method 1: Look for Load More or Show More buttons
            load_more_selectors = [
                "button:has-text('Load More')",
                "button:has-text('Show More')",
                "button[aria-label*='Load more']",
                "button[aria-label*='Show more']",
                ".load-more",
                ".show-more"
            ]
            
            for selector in load_more_selectors:
                try:
                    load_btn = await page.query_selector(selector)
                    if load_btn:
                        # Check if button is enabled
                        is_disabled = await load_btn.is_disabled()
                        is_visible = await load_btn.is_visible()
                        if not is_disabled and is_visible:
                            await load_btn.click()
                            await page.wait_for_load_state("networkidle")
                            await page.wait_for_timeout(self.sleep_after_nav_ms)
                            return True
                except:
                    continue
            
            # Method 2: Look for traditional Next page buttons
            next_selectors = [
                "button[aria-label*='Next']",
                "button:has-text('Next')",
                "a[aria-label*='Next']",
                "a:has-text('Next')",
                "[class*='next']:not([disabled])",
                "[class*='pagination'] button:not([disabled]):last-child"
            ]
            
            for selector in next_selectors:
                try:
                    next_btn = await page.query_selector(selector)
                    if next_btn:
                        # Check if button is enabled
                        is_disabled = await next_btn.is_disabled()
                        is_visible = await next_btn.is_visible()
                        if not is_disabled and is_visible:
                            await next_btn.click()
                            await page.wait_for_load_state("networkidle")
                            await page.wait_for_timeout(self.sleep_after_nav_ms)
                            return True
                except:
                    continue
            
            # Method 3: Try infinite scroll
            try:
                # Scroll to bottom to trigger more content loading
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await page.wait_for_timeout(2000)
                
                # Check if new content appeared
                new_cards = await page.query_selector_all("div.job-grid-item")
                if len(new_cards) > 0:
                    # Wait a bit more for content to stabilize
                    await page.wait_for_timeout(3000)
                    return True
                    
            except Exception as e:
                self.logger.debug(f"Infinite scroll failed: {e}")
            
            return False

        except Exception as e:
            self.logger.debug(f"Error navigating to next page: {e}")
            return False