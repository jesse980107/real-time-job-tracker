# scrapers/tiktok/scraper.py

import asyncio
import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Set
from urllib.parse import urljoin, urlparse, urlunparse, quote, urlencode, parse_qs

from playwright.async_api import async_playwright, Page, TimeoutError as PlaywrightTimeoutError


def now_iso() -> str:
    return datetime.now().isoformat()


class TiktokScraper:
    """
    TikTok Careers scraper for lifeattiktok.com
    """

    def __init__(self, website_config: Dict[str, Any], global_config: Dict[str, Any]):
        self.cfg = website_config
        self.g = global_config
        self.logger = logging.getLogger("job_tracker")

        sc = self.cfg.get("scraping_config", {})
        self.max_jobs = sc.get("max_jobs", 400)
        self.sleep_after_nav_ms = sc.get("sleep_after_nav_ms", 2000)
        self.sleep_after_open_ms = sc.get("sleep_after_open_ms", 1000)
        self.apply_filters_via_ui = sc.get("apply_filters_via_ui", True)
        self.usa_locations: List[str] = sc.get("usa_locations", [])
        self.canada_locations: List[str] = sc.get("canada_locations", [])
        self.target_locations = self.usa_locations + self.canada_locations

        wp = self.cfg.get("playwright_options", {})
        self.sel_results_link = wp.get("results_link_selector", "a[href*='/search/']")
        self.sel_show_more = wp.get("show_more_button", "button:has-text('2'), button:has-text('3'), button:has-text('Next')")
        self.sel_detail_title = wp.get("detail_title", "h1")
        self.sel_detail_desc = wp.get("detail_description", "div:has-text('Responsibilities'), main")
        self.sel_job_card = wp.get("job_card_selector", "div:has(h3), section:has(h3)")
        self.sel_see_details = wp.get("see_details_button", "a[href*='/search/']")
        self.sel_job_title_link = wp.get("job_title_link", "h3 a, a:has(h3)")
        self.sel_pagination = wp.get("pagination_numbers", "button:has-text('2'), button:has-text('3'), button:has-text('4')")
        self.sel_location_input = wp.get("location_input", "input[placeholder*='location' i]")

        pg = self.g["playwright_global"]
        self.timeout = pg["timeout"]
        self.user_agent = pg["user_agent"]
        self.headless = self.g["global_settings"]["headless"]

        self.base_url = self.cfg["website_info"]["base_url"]

        self.scraped: List[Dict[str, Any]] = []
        self.seen_urls: Set[str] = set()

    async def scrape_jobs(self) -> List[Dict[str, Any]]:
        """Main entry point for scraping TikTok jobs"""
        start_time = datetime.now()
        
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=self.headless,
                    args=['--no-sandbox', '--disable-dev-shm-usage']
                )
                context = await browser.new_context(
                    user_agent=self.user_agent,
                    viewport={'width': 1920, 'height': 1080}
                )
                page = await context.new_page()
                page.set_default_timeout(self.timeout)

                # Use UI-based filtering for TikTok to select specific US/Canada locations
                await self._open_list_page(page, self.base_url)
                await self._apply_location_filters_via_ui(page)
                await self._harvest_and_parse(page)

                await browser.close()

            end_time = datetime.now()
            duration = end_time - start_time
            duration_seconds = round(duration.total_seconds(), 2)
            
            self.logger.info(f"TikTok scraping finished. Total jobs collected: {len(self.scraped)}")
            self.logger.info(f"Scraping duration: {duration_seconds} seconds")
            
            # Add duration info to the scraped data
            self.scraping_duration = duration_seconds
            
            return self.scraped

        except Exception as e:
            end_time = datetime.now()
            duration = end_time - start_time
            duration_seconds = round(duration.total_seconds(), 2)
            
            self.logger.error(f"TikTok scraping error: {e}")
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

    async def _apply_location_filters_via_ui(self, page: Page) -> None:
        """Apply location filters by using the exact filtered URL"""
        try:
            self.logger.info("Applying location filters using direct URL navigation...")
            
            # Use the exact URL with location codes provided by the user
            filtered_url = (
                "https://lifeattiktok.com/search?"
                "recruitment_id_list=&job_category_id_list=&subject_id_list=&"
                "location_code_list=CT_233%2CCT_1000001%2CCT_247%2CCT_101443%2CCT_221%2CCT_94%2CCT_243%2CCT_114%2CCT_104%2CCT_75%2CCT_1103355%2CCT_1103348%2CCT_157&"
                "keyword=&limit=12&offset=0"
            )
            
            self.logger.info(f"Navigating to filtered URL: {filtered_url}")
            await page.goto(filtered_url)
            
            # Wait for the page to load completely
            await page.wait_for_load_state('networkidle')
            await page.wait_for_timeout(3000)
            
            # Verify we have job results
            job_cards = await page.query_selector_all("a[href*='/search/'][href*='7']")
            self.logger.info(f"Found {len(job_cards)} job cards after applying location filter")
            
            # Log current URL to confirm filtering
            current_url = page.url
            self.logger.info(f"Current URL: {current_url}")
            
            if "location_code_list" in current_url:
                self.logger.info("✓ Successfully applied location filters via URL")
            else:
                self.logger.warning("Location parameters not found in URL")
            
        except Exception as e:
            self.logger.error(f"Error applying location filters via URL: {e}")
            # Take a screenshot for debugging
            try:
                await page.screenshot(path="debug/tiktok_location_filter_error.png")
                self.logger.info("Screenshot saved to debug/tiktok_location_filter_error.png")
            except:
                pass

    async def _apply_url_based_location_filter(self, page: Page) -> None:
        """Fallback method: apply location filter via URL"""
        try:
            # Build URL with location parameters (you'd need to find the correct location codes)
            # For now, we'll try to construct a URL with common US location parameters
            current_url = page.url
            
            # Example URL structure from TikTok: 
            # https://lifeattiktok.com/search?job_category_id_list=&location_code_list=CT_XXX&...
            
            # Since we don't have the exact location codes, we'll try a different approach
            # by using the base search URL and hoping it defaults to reasonable results
            base_search_url = "https://lifeattiktok.com/search?recruitment_id_list=&job_category_id_list=&subject_id_list=&location_code_list=&keyword=&limit=12&offset=0"
            
            await page.goto(base_search_url)
            await page.wait_for_load_state("networkidle")
            await page.wait_for_timeout(3000)
            
            self.logger.info("Applied URL-based location filtering")
            
        except Exception as e:
            self.logger.error(f"Error in URL-based location filtering: {e}")

    def _build_url_with_locations(self, base: str, locations: List[str]) -> str:
        """Build URL with location filters applied - TikTok specific"""
        if not locations:
            return base
        
        # TikTok uses different URL structure, often the search is handled via UI
        # This is a fallback method in case UI filtering fails
        params = []
        
        # Try common URL parameter patterns
        for loc in locations[:3]:  # Limit to avoid too long URLs
            params.append(f"location={quote(loc)}")
        
        if params:
            query_string = "&".join(params)
            separator = "&" if "?" in base else "?"
            return f"{base}{separator}{query_string}"
        
        return base

    def _build_url_with_page(self, base: str, page_num: int, locations: List[str]) -> str:
        """Build URL with location filters and specific page number"""
        # TikTok pagination might be handled differently
        # This method may need adjustment based on actual behavior
        url = self._build_url_with_locations(base, locations)
        
        # Add page parameter if not page 1
        if page_num > 1:
            separator = "&" if "?" in url else "?"
            url = f"{url}{separator}page={page_num}"
        
        return url

    async def _harvest_and_parse(self, page: Page) -> None:
        """Harvest job links and parse details"""
        total = 0
        page_num = 1
        
        # Initialize job URL tracking for pagination verification
        self._last_page_job_urls = []

        while total < self.max_jobs:
            self.logger.info(f"Processing page {page_num}")
            
            # Wait for page to load and add some debugging
            await page.wait_for_timeout(3000)  # Give more time for dynamic content
            
            # Debug: Check what's actually on the page
            page_title = await page.title()
            self.logger.info(f"Page title: {page_title}")
            
            # Check for common elements
            body_text = await page.inner_text("body")
            if "job" in body_text.lower() or "position" in body_text.lower():
                self.logger.info(f"Found job-related content on page")
            
            # Wait for job listings to load with multiple selectors
            job_cards_found = False
            card_selectors = [
                ".job-card",
                "[data-job-id]",
                ".job-item",
                ".position-card",
                "div[class*='job']",
                "div[class*='position']"
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
                # Log page content for debugging
                page_content = await page.content()
                with open(f"debug/tiktok_page_{page_num}.html", "w", encoding="utf-8") as f:
                    f.write(page_content)
                self.logger.info(f"Page content saved to debug/tiktok_page_{page_num}.html")
                break

            # Collect job links from current page
            job_links = await self._collect_job_links(page, page_num)
            
            if not job_links:
                self.logger.warning(f"No job links found on page {page_num}")
                # Save page for debugging
                page_content = await page.content()
                with open(f"debug/tiktok_no_links_{page_num}.html", "w", encoding="utf-8") as f:
                    f.write(page_content)
                break

            # Process each job on this page
            for i, job_url in enumerate(job_links):
                if total >= self.max_jobs:
                    break
                
                self.logger.info(f"Processing job {total + 1}/{self.max_jobs}: {job_url}")
                
                if job_url in self.seen_urls:
                    self.logger.info(f"Skipping duplicate job: {job_url}")
                    continue
                
                self.seen_urls.add(job_url)
                
                try:
                    job_data = await self._parse_job_detail(page, job_url)
                    if job_data:
                        self.scraped.append(job_data)
                        total += 1
                        self.logger.info(f"Successfully scraped job: {job_data.get('title', 'Unknown')}")
                    else:
                        self.logger.warning(f"Failed to parse job data for: {job_url}")
                except Exception as e:
                    self.logger.error(f"Error processing job {job_url}: {e}")
                    continue

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
        """Collect job URLs from current page - TikTok specific"""
        job_links = []
        
        try:
            # Give page time to load
            await page.wait_for_timeout(3000)
            
            # Method 1: Look for job title links (based on screenshots)
            # TikTok seems to have job titles as clickable links
            job_title_selectors = [
                "h3 a[href*='/search/']",
                "a[href*='/search/']:has(h3)",
                "h2 a[href*='/search/']", 
                "a[href*='/search/']:has(h2)",
                "a[href*='/search/'][href*='7']",  # Based on URL pattern seen
                "a[href*='lifeattiktok.com/search/']"
            ]
            
            for selector in job_title_selectors:
                try:
                    links = await page.query_selector_all(selector)
                    if links:
                        self.logger.info(f"Found {len(links)} job links using selector: {selector}")
                        for link in links:
                            href = await link.get_attribute("href")
                            if href:
                                # Convert relative URLs to absolute
                                if href.startswith("/"):
                                    href = urljoin("https://lifeattiktok.com", href)
                                elif not href.startswith("http"):
                                    href = urljoin(page.url, href)
                                
                                # Validate it's a job detail URL (contains numeric ID)
                                if "/search/" in href and any(c.isdigit() for c in href):
                                    job_links.append(href)
                        break
                except Exception as e:
                    self.logger.debug(f"Error with selector {selector}: {e}")
                    continue

            # Method 2: Look for any links with numeric job IDs in /search/ path
            if not job_links:
                try:
                    all_links = await page.query_selector_all("a[href*='/search/']")
                    for link in all_links:
                        href = await link.get_attribute("href")
                        if href and "/search/" in href:
                            # Check if URL contains a numeric ID (like 7550763298204731666)
                            url_parts = href.split("/search/")
                            if len(url_parts) > 1 and url_parts[1].isdigit():
                                if href.startswith("/"):
                                    href = urljoin("https://lifeattiktok.com", href)
                                job_links.append(href)
                except Exception as e:
                    self.logger.debug(f"Error in method 2: {e}")

            # Method 3: Check for job sections and extract URLs from onclick or data attributes
            if not job_links:
                try:
                    # Look for job container sections
                    job_sections = await page.query_selector_all("div:has(h3), section:has(h3), div:has(h2)")
                    for section in job_sections:
                        # Check if section contains job-related text
                        section_text = await section.inner_text()
                        if any(keyword in section_text.lower() for keyword in ["engineer", "manager", "analyst", "lead", "developer", "coordinator"]):
                            # Look for any links within this section
                            section_links = await section.query_selector_all("a[href*='/search/']")
                            for link in section_links:
                                href = await link.get_attribute("href")
                                if href and "/search/" in href:
                                    if href.startswith("/"):
                                        href = urljoin("https://lifeattiktok.com", href)
                                    job_links.append(href)
                except Exception as e:
                    self.logger.debug(f"Error in method 3: {e}")

            # Remove duplicates while preserving order
            seen = set()
            unique_links = []
            for link in job_links:
                if link not in seen:
                    seen.add(link)
                    unique_links.append(link)

            self.logger.info(f"Found {len(unique_links)} unique job links on page")
            
            # Log first few URLs for debugging
            for i, url in enumerate(unique_links[:3]):
                self.logger.debug(f"Job URL {i+1}: {url}")
                
            return unique_links

        except Exception as e:
            self.logger.error(f"Error collecting job links: {e}")
            return []

    async def _parse_job_detail(self, listing_page: Page, job_url: str) -> Optional[Dict[str, Any]]:
        """Parse job details by navigating to job detail page"""
        
        try:
            # Open job detail page in new tab
            detail_page = await listing_page.context.new_page()
            detail_page.set_default_timeout(self.timeout)
            
            await detail_page.goto(job_url)
            await detail_page.wait_for_load_state("networkidle")

            # Extract job details
            title = await self._extract_title(detail_page)
            location = await self._extract_location(detail_page)
            description = await self._extract_description(detail_page)
            
            # Extract metadata from the job details section
            metadata = await self._extract_metadata(detail_page)
            
            # Extract job code from page content (preferred) or fall back to URL
            job_code = await self._extract_job_code_from_page(detail_page)
            if not job_code:
                job_code = self._extract_job_id_from_url(job_url)

            job_data = {
                "jobId": f"TikTok_{job_code}",
                "title": title or "Unknown Title",
                "company": "TikTok",
                "location": location or "",
                "url": job_url,
                "description": description or "",
                "source": "TikTok",
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
        """Extract job title from TikTok detail page"""
        try:
            title_selectors = [
                "h1",
                "h2",
                ".job-title",
                "[data-testid='job-title']"
            ]
            
            for selector in title_selectors:
                title_el = await page.query_selector(selector)
                if title_el:
                    title = await title_el.inner_text()
                    if title and title.strip():
                        # Clean up title
                        title = title.strip()
                        # Remove any trailing organization info
                        if " - " in title and "organization" in title.lower():
                            title = title.split(" - ")[0].strip()
                        return title
                        
        except Exception as e:
            self.logger.debug(f"Error extracting title: {e}")
        return None

    async def _extract_location(self, page: Page) -> Optional[str]:
        """Extract location from TikTok detail page"""
        try:
            # Look for location information near employment details
            location_patterns = [
                "Location:",
                "Employment Type:",
                "Job Code:"
            ]
            
            # Method 1: Look for structured location information
            location_selectors = [
                "p:has-text('Location:') + p",
                "div:has-text('Location:') + div",
                "span:has-text('Location:')",
                ".location",
                "[data-testid='location']"
            ]
            
            for selector in location_selectors:
                location_el = await page.query_selector(selector)
                if location_el:
                    location = await location_el.inner_text()
                    if location and location.strip() and "location:" not in location.lower():
                        return location.strip()

            # Method 2: Extract from the metadata section near employment type
            try:
                page_text = await page.inner_text("body")
                
                # Look for location patterns in text (like "San Jose" from the screenshot)
                # Match common city patterns
                location_matches = [
                    r'Location:\s*([^,\n]+)',
                    r'(San Jose|Singapore|Jakarta|Tokyo|New York|San Francisco|Seattle|Austin|Los Angeles|Toronto|Vancouver|Montreal)',
                    r'Employment Type:.*?\n.*?([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)'
                ]
                
                for pattern in location_matches:
                    match = re.search(pattern, page_text, re.IGNORECASE)
                    if match:
                        location = match.group(1).strip()
                        # Validate it looks like a real location
                        if len(location) > 2 and not location.lower() in ['regular', 'full', 'time', 'type']:
                            return location
                            
            except Exception:
                pass

            # Method 3: Look in structured data
            try:
                # Look for location in the job details section
                location_elements = await page.query_selector_all("p")
                for el in location_elements:
                    text = await el.inner_text()
                    # Check if this paragraph contains location info
                    if "san jose" in text.lower() or "singapore" in text.lower() or "jakarta" in text.lower():
                        # Extract the location part
                        lines = text.split('\n')
                        for line in lines:
                            line = line.strip()
                            if any(city in line.lower() for city in ['san jose', 'singapore', 'jakarta', 'tokyo', 'new york']):
                                return line
            except Exception:
                pass

        except Exception as e:
            self.logger.debug(f"Error extracting location: {e}")
        return None

    async def _extract_description(self, page: Page) -> str:
        """Extract job description from TikTok job detail page"""
        try:
            # Wait for content to load
            await page.wait_for_timeout(3000)
            
            # Method 1: Look for specific content sections like "Responsibilities"
            content_sections = []
            
            # Target specific sections that contain job content
            section_selectors = [
                "h2:has-text('Responsibilities') + div",
                "h3:has-text('Responsibilities') + div", 
                "h2:has-text('What you will do') + div",
                "h3:has-text('What you will do') + div",
                "h2:has-text('Job Description') + div",
                "h3:has-text('Job Description') + div",
                "*:has-text('Responsibilities')",
                "*:has-text('What you will do')",
                "*:has-text('Job Description')",
                "*:has-text('About the role')"
            ]
            
            for selector in section_selectors:
                try:
                    elements = await page.query_selector_all(selector)
                    for element in elements:
                        text = await element.inner_text()
                        if text and len(text.strip()) > 100:
                            content_sections.append(text.strip())
                            self.logger.debug(f"Found content via selector {selector}: {len(text)} chars")
                except Exception as e:
                    self.logger.debug(f"Selector {selector} failed: {e}")
                    continue
            
            # Method 2: Extract content between specific headings
            try:
                page_html = await page.content()
                
                # Look for content between Responsibilities and other sections
                resp_patterns = [
                    r'Responsibilities</h[23]>(.*?)(?:<h[23]|Apply to this job|Share this listing|$)',
                    r'>Responsibilities</(.*?)(?:<h[23]|Apply to this job|Share this listing|$)',
                    r'What you will do</h[23]>(.*?)(?:<h[23]|Apply to this job|Share this listing|$)',
                    r'>What you will do</(.*?)(?:<h[23]|Apply to this job|Share this listing|$)'
                ]
                
                for pattern in resp_patterns:
                    match = re.search(pattern, page_html, re.DOTALL | re.IGNORECASE)
                    if match:
                        # Extract text from HTML content
                        html_content = match.group(1)
                        # Remove HTML tags and get text
                        text_content = re.sub(r'<[^>]+>', ' ', html_content)
                        text_content = re.sub(r'\s+', ' ', text_content).strip()
                        
                        if len(text_content) > 100:
                            content_sections.append(text_content)
                            self.logger.debug(f"Found content via HTML pattern: {len(text_content)} chars")
                            break
                            
            except Exception as e:
                self.logger.debug(f"HTML pattern extraction failed: {e}")
            
            # Method 3: Get all text and use improved cleaning
            try:
                all_text = await page.inner_text("body")
                self.logger.debug(f"Page content length: {len(all_text)}")
                
                if all_text and len(all_text.strip()) > 300:
                    # Use improved cleaning that looks for job content patterns
                    cleaned_text = self._clean_description_advanced(all_text)
                    if len(cleaned_text) > 100:
                        content_sections.append(cleaned_text)
                        self.logger.debug(f"Found description via advanced cleaning: {len(cleaned_text)} chars")
                    
            except Exception as e:
                self.logger.debug(f"Body text extraction failed: {e}")
            
            # Method 4: Try specific TikTok job page structure
            try:
                # Look for main content area
                main_selectors = [
                    "main div[class*='content']",
                    "main section", 
                    "div[class*='job-detail']",
                    "div[class*='job-content']",
                    "main"
                ]
                
                for selector in main_selectors:
                    try:
                        element = await page.query_selector(selector)
                        if element:
                            text = await element.inner_text()
                            # Look for content after job title and metadata
                            lines = text.split('\n')
                            content_started = False
                            content_lines = []
                            
                            for line in lines:
                                line = line.strip()
                                if not line:
                                    continue
                                    
                                # Skip header content
                                if any(skip in line for skip in ['Job Code:', 'Employment Type:', 'Location:', 'Apply to this job', 'Share this listing']):
                                    continue
                                
                                # Look for content sections
                                if any(section in line for section in ['Responsibilities', 'What you will do', 'Job Description', 'About the role']):
                                    content_started = True
                                    content_lines.append(line)
                                    continue
                                
                                # If we've started collecting content, add relevant lines
                                if content_started and len(line) > 20:
                                    content_lines.append(line)
                            
                            if content_lines:
                                combined_content = '\n'.join(content_lines)
                                if len(combined_content) > 100:
                                    content_sections.append(combined_content)
                                    self.logger.debug(f"Found content via main selector {selector}: {len(combined_content)} chars")
                                    break
                                    
                    except Exception as e:
                        self.logger.debug(f"Main selector {selector} failed: {e}")
                        continue
                        
            except Exception as e:
                self.logger.debug(f"Main content extraction failed: {e}")
            
            # Return the best content found
            if content_sections:
                # Find the longest meaningful content
                best_content = max(content_sections, key=len)
                
                # Try light cleaning first to preserve content
                lightly_cleaned = self._light_clean_description(best_content)
                if len(lightly_cleaned) > 200:
                    self.logger.debug(f"Returning lightly cleaned content: {len(lightly_cleaned)} chars")
                    return lightly_cleaned
                
                # If light cleaning removes too much, try advanced cleaning
                final_content = self._clean_description(best_content)
                
                if len(final_content) > 50:
                    self.logger.debug(f"Returning advanced cleaned content: {len(final_content)} chars")
                    return final_content
                
                # If all cleaning fails, return the raw best content (trimmed)
                if len(best_content) > 500:
                    # Just remove obvious navigation elements but keep the core content
                    simple_clean = re.sub(r'^.*?(Responsibilities|What you will do|Job Description|About the role|Team Introduction)', r'\1', best_content, flags=re.IGNORECASE | re.DOTALL)
                    simple_clean = re.sub(r'(Apply to this job|Share this listing).*$', '', simple_clean, flags=re.IGNORECASE | re.DOTALL)
                    simple_clean = simple_clean.strip()
                    
                    if len(simple_clean) > 200:
                        self.logger.debug(f"Returning simply cleaned content: {len(simple_clean)} chars")
                        return simple_clean
                
                # Last resort: return raw content if it's substantial
                if len(best_content) > 1000:
                    self.logger.debug(f"Returning raw content as last resort: {len(best_content)} chars")
                    return best_content[:2000] + "..." if len(best_content) > 2000 else best_content
            
            return "Job description content not accessible"

        except Exception as e:
            self.logger.debug(f"Error extracting description: {e}")
            return f"Description extraction error: {str(e)}"

    def _light_clean_description(self, description: str) -> str:
        """Light cleaning that preserves more content"""
        if not description:
            return "No description available"
        
        # Remove excessive whitespace
        description = re.sub(r'\s+', ' ', description)
        
        # Remove only the most obvious navigation/header elements
        light_unwanted_patterns = [
            r'^.*?Apply Company About TikTok.*?English\s*',
            r'Apply to this job.*?Share this listing.*?$',
            r'Share this listing:.*?$',
            r'Back to top\s*$',
            r'©.*TikTok.*$'
        ]
        
        for pattern in light_unwanted_patterns:
            description = re.sub(pattern, '', description, flags=re.IGNORECASE | re.DOTALL)
        
        # Try to start from meaningful content sections
        content_start_indicators = [
            'Responsibilities',
            'What you will do', 
            'Job Description',
            'About the role',
            'Team Introduction',
            'We\'re looking'
        ]
        
        for indicator in content_start_indicators:
            if indicator in description:
                # Find the position and start from there
                pos = description.lower().find(indicator.lower())
                if pos >= 0:
                    description = description[pos:]
                    break
        
        # Remove content after common end indicators
        end_indicators = [
            'Apply to this job',
            'Share this listing',
            'Back to top',
            'Cookie Policy',
            'Privacy Policy',
            'Contact Us'
        ]
        
        for indicator in end_indicators:
            pos = description.lower().find(indicator.lower())
            if pos >= 0:
                description = description[:pos]
                break
        
        return description.strip()

    def _clean_description_advanced(self, description: str) -> str:
        """Advanced cleaning for TikTok job descriptions with better pattern recognition"""
        if not description:
            return "No description available"
        
        # Remove excessive whitespace first
        description = re.sub(r'\s+', ' ', description)
        
        # Look for content starting from "Responsibilities" or similar headings
        content_start_patterns = [
            r'(Responsibilities.*?)(?:Apply to this job|Share this listing|Back to top|Cookie Policy|Privacy Policy|©|Contact|$)',
            r'(What you will do.*?)(?:Apply to this job|Share this listing|Back to top|Cookie Policy|Privacy Policy|©|Contact|$)',
            r'(Job Description.*?)(?:Apply to this job|Share this listing|Back to top|Cookie Policy|Privacy Policy|©|Contact|$)',
            r'(About the role.*?)(?:Apply to this job|Share this listing|Back to top|Cookie Policy|Privacy Policy|©|Contact|$)',
            r'(Role Description.*?)(?:Apply to this job|Share this listing|Back to top|Cookie Policy|Privacy Policy|©|Contact|$)',
            r'(Team Introduction.*?)(?:Apply to this job|Share this listing|Back to top|Cookie Policy|Privacy Policy|©|Contact|$)'
        ]
        
        extracted_content = None
        for pattern in content_start_patterns:
            match = re.search(pattern, description, re.DOTALL | re.IGNORECASE)
            if match:
                content = match.group(1).strip()
                if len(content) > 200:  # Ensure meaningful content
                    extracted_content = content
                    self.logger.debug(f"Found description via pattern matching: {len(content)} chars")
                    break
        
        # If pattern matching worked, use that content
        if extracted_content:
            description = extracted_content
        else:
            # Fallback: try to clean the full text more intelligently
            
            # Remove common TikTok header/footer content and navigation
            unwanted_patterns = [
                r'^.*?Apply Company About TikTok.*?English\s*',
                r'^.*?(Life at TikTok|Newsroom|Contact|Programs).*?English\s*',
                r'Life at TikTok.*?Apply.*?',
                r'Apply to this job.*?Share this listing.*?',
                r'©.*TikTok.*',
                r'Cookie Policy.*Privacy Policy.*',
                r'Contact Us.*Help Center.*',
                r'Share this listing:.*',
                r'Apply to this job\s*$',
                r'Share this listing:\s*$',
                # Remove specific TikTok navigation/header elements
                r'^.*?Location:\s*[^.]+\s*Employment Type:\s*[^.]+\s*Job Code:\s*[^\s]+\s*',
                r'^.*?(Home|Jobs|Search|Filter|Apply|Company|About TikTok|Newsroom|Contact|Programs|TikTok for Good|TikTok for Developers|Effect House|Advertise on TikTok|TikTok Rewards|TikTok Browse|TikTok Embeds|Resources|Help center|Safety Center|Creator Portal|Community Guidelines|Transparency|Accessibility|Legal|Privacy Policy|Candidate Privacy Policy|Terms of Service|English)\s*',
                r'Back to top\s*',
                r'Save this job\s*',
                r'Apply now\s*'
            ]
            
            for pattern in unwanted_patterns:
                description = re.sub(pattern, '', description, flags=re.IGNORECASE | re.DOTALL)
            
            # Remove remaining department/category prefixes
            description = re.sub(r'^(Security|Technology|Client|Global Operations|Algorithm|Machine learning|Multimedia|Backend|Corporate Functions)\s+', '', description)
            
            self.logger.debug(f"Found description via text cleaning: {len(description)} chars")
        
        # Final cleanup
        description = description.strip()
        description = re.sub(r'\n\s*\n', '\n\n', description)  # Normalize paragraph breaks
        
        # Remove any remaining navigation elements at the start
        lines = description.split('\n')
        content_lines = []
        found_content = False
        
        for line in lines:
            line = line.strip()
            if not line:
                continue
            
            # Skip navigation/header lines
            if any(nav in line.lower() for nav in ['apply', 'share', 'contact', 'help center', 'english', 'location:', 'employment type:', 'job code:']):
                if not found_content:  # Only skip these if we haven't found real content yet
                    continue
            
            # Look for content indicators
            if any(content_indicator in line.lower() for content_indicator in ['responsibilities', 'what you will do', 'team introduction', 'we\'re looking', 'role', 'position', 'candidate', 'experience', 'skills', 'requirements']):
                found_content = True
            
            if found_content or len(line) > 50:  # Include longer lines even if content not officially found
                content_lines.append(line)
        
        if content_lines:
            description = '\n'.join(content_lines)
        
        # If description is too short, return a message
        if len(description) < 50:
            return "Job description content not fully available"
        
        return description

    def _clean_description(self, description: str) -> str:
        """Clean up the job description for TikTok"""
        if not description:
            return "No description available"
        
        # Remove excessive whitespace first
        description = re.sub(r'\s+', ' ', description)
        
        # Remove common TikTok header/footer content and navigation
        unwanted_patterns = [
            r'Life at TikTok.*Apply.*',
            r'Apply to this job.*Share this listing.*',
            r'©.*TikTok.*',
            r'Cookie Policy.*Privacy Policy.*',
            r'Contact Us.*Help Center.*',
            r'Share this listing:.*',
            r'Apply to this job\s*$',
            r'Share this listing:\s*$',
            # Remove header metadata
            r'^.*?Location:\s*[^.]+\s*Employment Type:\s*[^.]+\s*Job Code:\s*[^\s]+\s*',
            # Remove navigation elements
            r'^.*?(Home|Jobs|Search|Filter)\s*',
            r'Back to top\s*',
            r'Save this job\s*',
            r'Apply now\s*'
        ]
        
        for pattern in unwanted_patterns:
            description = re.sub(pattern, '', description, flags=re.IGNORECASE | re.DOTALL)
        
        # Remove department/category prefixes that aren't part of the description
        description = re.sub(r'^(Security|Technology|Client|Global Operations|Algorithm|Machine learning|Multimedia|Backend)\s+', '', description)
        
        # Try to find and extract the main job content
        # Look for key sections that indicate job description
        job_content_patterns = [
            r'(Responsibilities.*?)(?:Apply|Share|Contact|Back to top|$)',
            r'(What you will do.*?)(?:Apply|Share|Contact|Back to top|$)',
            r'(Job Description.*?)(?:Apply|Share|Contact|Back to top|$)',
            r'(About the role.*?)(?:Apply|Share|Contact|Back to top|$)',
            r'(Role Description.*?)(?:Apply|Share|Contact|Back to top|$)'
        ]
        
        for pattern in job_content_patterns:
            match = re.search(pattern, description, re.DOTALL | re.IGNORECASE)
            if match:
                content = match.group(1).strip()
                if len(content) > 100:
                    description = content
                    break
        
        # Final cleanup
        description = description.strip()
        description = re.sub(r'\n\s*\n', '\n\n', description)  # Normalize paragraph breaks
        
        # If description is too short, return a message
        if len(description) < 50:
            return "Job description content not fully available"
        
        return description

    async def _extract_metadata(self, page: Page) -> Dict[str, str]:
        """Extract metadata fields from TikTok job detail page"""
        metadata = {}
        
        try:
            # Wait for content to load
            await page.wait_for_timeout(1000)
            
            # TikTok-specific metadata fields (excluding job_code)
            metadata_fields = [
                ("Employment Type", "employment_type"),
                ("Department", "department"),
                ("Team", "team"),
                ("Location", "location_detail"),
                ("Job Type", "job_type"),
                ("Experience Level", "experience_level")
            ]

            # Method 1: Extract from the structured information section
            try:
                page_text = await page.inner_text("body")
                
                # Extract Employment Type
                emp_type_match = re.search(r'Employment Type:\s*([^\n]+)', page_text, re.IGNORECASE)
                if emp_type_match:
                    metadata["employment_type"] = emp_type_match.group(1).strip()
                
                # Extract Location (different from main location extraction)
                location_match = re.search(r'Location:\s*([^\n]+)', page_text, re.IGNORECASE)
                if location_match:
                    metadata["location_detail"] = location_match.group(1).strip()
                    
            except Exception:
                pass

            # Method 2: Look for structured data using HTML elements
            try:
                # Look for paragraphs containing metadata
                paragraphs = await page.query_selector_all("p")
                current_field = None
                
                for p in paragraphs:
                    text = await p.inner_text()
                    text = text.strip()
                    
                    # Check if this paragraph contains metadata
                    if "Employment Type:" in text:
                        value = text.replace("Employment Type:", "").strip()
                        if value:
                            metadata["employment_type"] = value
                    elif "Job Code:" in text:
                        value = text.replace("Job Code:", "").strip()
                        if value:
                            metadata["job_code"] = value
                    elif "Location:" in text:
                        value = text.replace("Location:", "").strip()
                        if value:
                            metadata["location_detail"] = value
                            
            except Exception:
                pass

            # Method 3: Look for department/team information from job title or content
            try:
                # Extract department from job title (e.g., "Global Security Organization")
                title_el = await page.query_selector("h1")
                if title_el:
                    title_text = await title_el.inner_text()
                    if " - " in title_text:
                        parts = title_text.split(" - ")
                        if len(parts) > 1:
                            potential_dept = parts[-1].strip()
                            if "organization" in potential_dept.lower() or "team" in potential_dept.lower():
                                metadata["department"] = potential_dept
                
                # Look for team information in the description or tags
                page_content = await page.inner_text("body")
                
                # Look for common team/department patterns
                team_patterns = [
                    r'(Technology - Security)',
                    r'(Global Operations - Commerce ops)',
                    r'(Corporate Functions)',
                    r'(Technology - Machine learning)',
                    r'(Technology - Algorithm)',
                    r'(Technology - Backend)',
                    r'(Marketing & Communications)',
                    r'Team:\s*([^\n,]+)'
                ]
                
                for pattern in team_patterns:
                    match = re.search(pattern, page_content, re.IGNORECASE)
                    if match and "team" not in metadata:
                        metadata["team"] = match.group(1).strip()
                        break
                        
            except Exception:
                pass

            # Method 4: Extract additional metadata from tags or badges
            try:
                # Look for tag-like elements that might contain metadata
                tags = await page.query_selector_all("span, div[class*='tag'], div[class*='badge']")
                for tag in tags:
                    tag_text = await tag.inner_text()
                    tag_text = tag_text.strip()
                    
                    # Common employment types
                    if tag_text.lower() in ['regular', 'full-time', 'part-time', 'contract', 'intern']:
                        if "employment_type" not in metadata:
                            metadata["employment_type"] = tag_text
                    
                    # Experience levels
                    if tag_text.lower() in ['entry', 'junior', 'senior', 'lead', 'principal', 'staff']:
                        if "experience_level" not in metadata:
                            metadata["experience_level"] = tag_text
                            
            except Exception:
                pass

        except Exception as e:
            self.logger.debug(f"Error extracting metadata: {e}")

        return metadata

    async def _extract_job_code_from_page(self, page: Page) -> Optional[str]:
        """Extract job code from TikTok job detail page"""
        try:
            # Wait for content to load
            await page.wait_for_timeout(1000)
            
            # Method 1: Look for "Job Code:" text pattern
            page_text = await page.inner_text("body")
            
            # Search for job code patterns in the page text
            job_code_patterns = [
                r'Job Code:\s*([A-Z]\d+[A-Z]*)',  # Job Code: A172540 or A166197A
                r'Job Code:\s*([A-Za-z]\d+[A-Za-z]*)',  # Job Code: a123456b (case insensitive)
                r'Job Code:\s*([A-Z][0-9]+[A-Z]*)',  # Job Code: A123456B
                r'Job Code:\s*([A-Za-z][0-9]+[A-Za-z]*)',  # More flexible pattern
                r'Job ID:\s*([A-Z]\d+[A-Z]*)',  # Alternative: Job ID: A172540A
                r'Job ID:\s*([A-Za-z]\d+[A-Za-z]*)',  # Alternative: Job ID: a172540b
                r'Job Code:\s*([A-Z]+\d+[A-Z]*)',  # Multiple letters at start: ABC123D
                r'Job ID:\s*([A-Z]+\d+[A-Z]*)'  # Multiple letters at start: ABC123D
            ]
            
            for pattern in job_code_patterns:
                match = re.search(pattern, page_text, re.IGNORECASE)
                if match:
                    job_code = match.group(1)
                    self.logger.debug(f"Found job code from page text: {job_code}")
                    return job_code
            
            # Method 2: Look for job code in specific HTML elements
            try:
                # Look for elements that might contain job code
                job_code_selectors = [
                    "*:has-text('Job Code:')",
                    "*:has-text('Job ID:')",
                    "p:has-text('Job Code')",
                    "div:has-text('Job Code')",
                    "span:has-text('Job Code')"
                ]
                
                for selector in job_code_selectors:
                    try:
                        elements = await page.query_selector_all(selector)
                        for element in elements:
                            text = await element.inner_text()
                            # Extract job code from element text
                            for pattern in job_code_patterns:
                                match = re.search(pattern, text, re.IGNORECASE)
                                if match:
                                    job_code = match.group(1)
                                    self.logger.debug(f"Found job code from element {selector}: {job_code}")
                                    return job_code
                    except Exception as e:
                        self.logger.debug(f"Error with selector {selector}: {e}")
                        continue
                        
            except Exception as e:
                self.logger.debug(f"Error in job code element extraction: {e}")
            
            # Method 3: Look in meta tags or structured data
            try:
                # Check for job code in meta tags or data attributes
                meta_selectors = [
                    "meta[name*='job']",
                    "meta[property*='job']", 
                    "[data-job-id]",
                    "[data-job-code]"
                ]
                
                for selector in meta_selectors:
                    try:
                        element = await page.query_selector(selector)
                        if element:
                            # Check various attributes
                            for attr in ['content', 'data-job-id', 'data-job-code', 'value']:
                                value = await element.get_attribute(attr)
                                if value:
                                    # Check if this looks like a job code (letter(s) + digits + optional letter(s))
                                    if re.match(r'^[A-Za-z]+\d+[A-Za-z]*$', value):
                                        self.logger.debug(f"Found job code from meta {selector}: {value}")
                                        return value
                    except Exception as e:
                        self.logger.debug(f"Error with meta selector {selector}: {e}")
                        continue
                        
            except Exception as e:
                self.logger.debug(f"Error in meta job code extraction: {e}")
                
            self.logger.debug("No job code found on page")
            return None

        except Exception as e:
            self.logger.debug(f"Error extracting job code from page: {e}")
            return None

    def _extract_job_id_from_url(self, url: str) -> str:
        """Extract job ID from TikTok URL"""
        try:
            # TikTok URLs have format: https://lifeattiktok.com/search/7550763298204731666
            patterns = [
                r'/search/(\d+)',  # TikTok specific pattern
                r'/job/([^/?]+)',
                r'/position/([^/?]+)',
                r'job_id=([^&]+)',
                r'id=([^&]+)'
            ]
            
            for pattern in patterns:
                match = re.search(pattern, url)
                if match:
                    return match.group(1)
        except Exception:
            pass
        return "UNKNOWN"

    async def _go_to_next_page(self, page: Page) -> bool:
        """Navigate to next page using TikTok's pagination - try buttons first, then URL"""
        try:
            current_url = page.url
            self.logger.info(f"Current URL: {current_url}")
            
            # Store current job URLs for comparison
            current_job_cards = await page.query_selector_all("a[href*='/search/'][href*='7']")
            current_job_urls = []
            for card in current_job_cards[:3]:  # Get first 3 for comparison
                href = await card.get_attribute('href')
                if href:
                    current_job_urls.append(href)
            
            # Method 1: Try clicking pagination buttons first (more reliable)
            pagination_selectors = [
                "button[aria-label*='Next']",
                "button:has-text('Next')",
                "a:has-text('Next')",
                "button:has-text('2')",
                "button:has-text('3')",
                "a:has-text('2')",
                "a:has-text('3')",
                "[class*='pagination'] button[class*='next']",
                "[class*='pagination'] a[class*='next']",
                "button[class*='next']:not([disabled])",
                ".pagination-next",
                ".next-page"
            ]
            
            for selector in pagination_selectors:
                try:
                    next_btn = await page.query_selector(selector)
                    if next_btn:
                        # Check if button is enabled
                        is_disabled = await next_btn.get_attribute("disabled")
                        is_hidden = await next_btn.is_hidden()
                        
                        if not is_disabled and not is_hidden:
                            self.logger.info(f"Attempting to click pagination button: {selector}")
                            
                            # Click the button
                            await next_btn.click()
                            await page.wait_for_load_state("networkidle")
                            await page.wait_for_timeout(3000)
                            
                            # Check if URL or content changed
                            new_url = page.url
                            new_job_cards = await page.query_selector_all("a[href*='/search/'][href*='7']")
                            
                            if len(new_job_cards) > 0:
                                new_job_urls = []
                                for card in new_job_cards[:3]:
                                    href = await card.get_attribute('href')
                                    if href:
                                        new_job_urls.append(href)
                                
                                # Check if we got different jobs
                                if new_job_urls != current_job_urls:
                                    self.logger.info(f"✓ Successfully navigated via button click, found {len(new_job_cards)} new job cards")
                                    return True
                                else:
                                    self.logger.debug(f"Button clicked but same jobs found, trying next selector")
                            else:
                                self.logger.debug(f"Button clicked but no jobs found, trying next selector")
                            
                except Exception as e:
                    self.logger.debug(f"Error with pagination button {selector}: {e}")
                    continue
            
            # Method 2: Try URL-based pagination if buttons don't work
            if "offset=" in current_url:
                # Extract current offset value
                import re
                offset_match = re.search(r'offset=(\d+)', current_url)
                if offset_match:
                    current_offset = int(offset_match.group(1))
                    next_offset = current_offset + 12  # TikTok uses 12 jobs per page
                    
                    # Replace the offset value in the URL
                    new_url = re.sub(r'offset=\d+', f'offset={next_offset}', current_url)
                    
                    self.logger.info(f"Trying URL-based pagination with offset {next_offset}: {new_url}")
                    await page.goto(new_url)
                    await page.wait_for_load_state("networkidle")
                    await page.wait_for_timeout(3000)
                    
                    # Check if we have different job results
                    new_job_cards = await page.query_selector_all("a[href*='/search/'][href*='7']")
                    if len(new_job_cards) > 0:
                        new_job_urls = []
                        for card in new_job_cards[:3]:
                            href = await card.get_attribute('href')
                            if href:
                                new_job_urls.append(href)
                        
                        # Check if we have different job URLs
                        if new_job_urls != current_job_urls:
                            self.logger.info(f"✓ Successfully navigated via URL, found {len(new_job_cards)} new job cards")
                            return True
                        else:
                            self.logger.info("Same job URLs found via URL pagination, may have reached the end")
                            return False
                    else:
                        self.logger.info("No job cards found on next page via URL")
                        return False
                else:
                    self.logger.error("Could not extract current offset from URL")
                    return False
            else:
                # If no offset in URL, try to add it for pagination
                separator = "&" if "?" in current_url else "?"
                new_url = f"{current_url}{separator}offset=12"
                
                self.logger.info(f"Adding offset parameter for pagination: {new_url}")
                await page.goto(new_url)
                await page.wait_for_load_state("networkidle")
                await page.wait_for_timeout(3000)
                
                # Check if we have jobs
                new_job_cards = await page.query_selector_all("a[href*='/search/'][href*='7']")
                if len(new_job_cards) > 0:
                    self.logger.info(f"Successfully navigated to next page, found {len(new_job_cards)} job cards")
                    return True
                else:
                    self.logger.info("No job cards found on next page")
                    return False

        except Exception as e:
            self.logger.error(f"Error navigating to next page: {e}")
            return False