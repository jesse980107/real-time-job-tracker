import asyncio
import json
import re
import logging
from datetime import datetime
from typing import Dict, List, Any, Optional, Set
from urllib.parse import urljoin, quote, urlparse, parse_qs, urlencode, urlunparse
from playwright.async_api import async_playwright, Page, TimeoutError as PlaywrightTimeoutError


def now_iso() -> str:
    return datetime.now().isoformat()


class AmazonScraper:
    def __init__(self, website_config: Dict[str, Any], global_config: Dict[str, Any]):
        """Initialize the Amazon job scraper"""
        self.cfg = website_config
        self.g = global_config
        self.logger = logging.getLogger("job_tracker")
        
        # Configuration
        self.config = website_config
        
        # Extract configuration values
        sc = self.cfg.get("scraping_config", {})
        self.max_jobs = sc.get("max_jobs", 100)
        self.sleep_after_nav_ms = sc.get("sleep_after_nav_ms", 1000)
        self.sleep_after_open_ms = sc.get("sleep_after_open_ms", 600)
        self.target_locations = sc.get("locations", ["USA", "CAN"])
        self.result_limit = sc.get("result_limit", 10)
        
        # Website config
        self.base_url = self.cfg['website_info']['base_url']
        
        # Playwright settings
        pg = self.g["playwright_global"]
        self.timeout = pg["timeout"]
        self.user_agent = pg["user_agent"]
        self.headless = self.g["global_settings"]["headless"]
        
        # State tracking
        self.scraped: List[Dict[str, Any]] = []
        self.seen_urls: Set[str] = set()

    async def scrape_jobs(self) -> List[Dict[str, Any]]:
        """Main entry point for scraping Amazon jobs"""
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

                self.logger.info(f"Amazon max_jobs = {self.max_jobs}")
                
                # Build URL with location filters
                url_with_filters = self._build_url_with_locations(self.base_url, self.target_locations)
                await self._open_list_page(page, url_with_filters)

                await self._harvest_and_parse(page)

                await context.close()
                await browser.close()

            end_time = datetime.now()
            duration = (end_time - start_time).total_seconds()
            self.scraping_duration = duration
            self.logger.info(f"Scraping duration: {duration:.2f} seconds")
            self.logger.info(f"Amazon scraping finished. Total jobs collected: {len(self.scraped)}")
            return self.scraped

        except Exception as e:
            self.logger.error(f"Amazon scraping error: {e}")
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
        
        # Amazon uses country[]= parameters for location filtering
        params = []
        for loc in locations:
            params.append(f"country%5B%5D={quote(loc)}")
        
        # Add default parameters
        params.extend([
            "offset=0",
            f"result_limit={self.result_limit}",
            "sort=relevant",
            "distanceType=Mi",
            "radius=24km"
        ])
        
        query_string = "&".join(params)
        separator = "&" if "?" in base else "?"
        return f"{base}{separator}{query_string}"

    def _build_url_with_page(self, base: str, page_num: int, locations: List[str]) -> str:
        """Build URL with location filters and specific page offset"""
        if not locations:
            locations = []
        
        # Calculate offset based on page number and result limit
        offset = (page_num - 1) * self.result_limit
        
        # Amazon uses country[]= parameters for location filtering
        params = []
        for loc in locations:
            params.append(f"country%5B%5D={quote(loc)}")
        
        # Add parameters with specific offset
        params.extend([
            f"offset={offset}",
            f"result_limit={self.result_limit}",
            "sort=relevant",
            "distanceType=Mi",
            "radius=24km"
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
            
            # Wait for page to load
            await page.wait_for_timeout(3000)
            
            # Debug: Check what's actually on the page
            page_title = await page.title()
            self.logger.info(f"Page title: {page_title}")
            
            # Check for common elements and results
            body_text = await page.inner_text("body")
            if "Search results" in body_text or "results" in body_text:
                # Look for results count indication
                if " of " in body_text:
                    results_match = re.search(r'(\d+)\s*of\s*(\d+)', body_text)
                    if results_match:
                        self.logger.info(f"Found results indicator: {results_match.group(0)}")
            
            # Wait for job listings to load with multiple selectors
            job_cards_found = False
            card_selectors = [
                "div.row",
                "div[class*='job']",
                "div[class*='result']",
                "[data-job-id]",
                "article"
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
                
                # Debug: check if there are any job-related links
                job_links = await page.locator("a[href*='/en/jobs/']").count()
                self.logger.info(f"Found {job_links} job links on page")
                
                # Check for error messages or no results
                if "No results found" in body_text or "0 results" in body_text:
                    self.logger.info("No jobs available for the selected criteria")
                elif "Access Denied" in body_text or "403" in body_text:
                    self.logger.error("Access denied - may need different user agent or headers")
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

                # Process the job
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
            # Give page time to load
            await page.wait_for_timeout(2000)
            
            # Method 1: Look for direct job links
            job_link_selectors = [
                "a[href*='/en/jobs/']",
                "a.job-link",
                "h3.job-title a",
                "div[class*='job'] a[href*='/jobs/']"
            ]
            
            for selector in job_link_selectors:
                try:
                    links = await page.query_selector_all(selector)
                    if links:
                        self.logger.info(f"Found {len(links)} job links with selector: {selector}")
                        for link in links:
                            href = await link.get_attribute("href")
                            if href and "/en/jobs/" in href:
                                full_url = urljoin(page.url, href)
                                job_links.append(full_url)
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

            self.logger.info(f"Found {len(unique_links)} job links on page")
            return unique_links

        except Exception as e:
            self.logger.error(f"Error collecting job links: {e}")
            return []

    async def _parse_job_detail(self, listing_page: Page, job_url: str) -> Optional[Dict[str, Any]]:
        """Parse job details by navigating to job detail page"""
        try:
            # Navigate to job detail page
            current_url = listing_page.url
            await listing_page.goto(job_url)
            await listing_page.wait_for_load_state("networkidle")
            
            # Extract job details
            title = await self._extract_title(listing_page)
            description = await self._extract_description(listing_page)
            location = await self._extract_location(listing_page)
            job_id = self._extract_job_id_from_url(job_url)
            metadata = await self._extract_metadata(listing_page)
            
            # Navigate back to listing page
            await listing_page.goto(current_url)
            await listing_page.wait_for_load_state("networkidle")
            
            job_data = {
                "jobId": f"Amazon_{job_id}",
                "title": title,
                "company": "Amazon",
                "location": location,
                "url": job_url,
                "description": description,
                "source": "Amazon",
                "status": "active",
                "scraped_date": now_iso(),
            }
            
            # Add metadata if available
            if metadata:
                job_data.update(metadata)
            
            return job_data
            
        except Exception as e:
            self.logger.error(f"Error parsing job detail {job_url}: {e}")
            return None

    async def _extract_title(self, page: Page) -> str:
        """Extract job title from detail page"""
        title_selectors = [
            "h1.title",
            "h1",
            ".job-title h1",
            "[data-test='job-title']"
        ]
        
        for selector in title_selectors:
            try:
                element = await page.query_selector(selector)
                if element:
                    title = await element.inner_text()
                    if title and title.strip():
                        return title.strip()
            except Exception:
                continue
        
        return "Unknown Title"

    async def _extract_description(self, page: Page) -> str:
        """Extract job description from detail page"""
        description_selectors = [
            "div[data-testid='job-description']",
            "div.job-description",
            "div.section:not(:has(header)):not(:has(nav))",
            "div.content:not(:has(header)):not(:has(nav))",
            "div#job-detail-body",
            "main div:not(:has(nav)):not(:has(header))",
            "section[class*='description']"
        ]
        
        # First try to wait for the main content to load
        try:
            await page.wait_for_selector("main", timeout=5000)
        except:
            pass
        
        for selector in description_selectors:
            try:
                elements = await page.query_selector_all(selector)
                for element in elements:
                    desc = await element.inner_text()
                    if desc and len(desc.strip()) > 100:  # Increased minimum length
                        # Check if this looks like actual job content
                        desc_lower = desc.lower()
                        if any(keyword in desc_lower for keyword in ['responsibility', 'requirement', 'experience', 'skill', 'qualification', 'about', 'role', 'position', 'candidate']):
                            return self._clean_description(desc.strip())
            except Exception:
                continue
        
        # Fallback: try to get main content area and filter out navigation
        try:
            main_content = await page.query_selector("main")
            if main_content:
                # Get all text but try to filter out navigation
                full_text = await main_content.inner_text()
                # Look for sections that contain job description keywords
                paragraphs = full_text.split('\n')
                description_parts = []
                
                in_description = False
                for para in paragraphs:
                    para = para.strip()
                    if not para:
                        continue
                    
                    # Skip navigation elements
                    if any(nav_item in para.lower() for nav_item in ['home', 'teams', 'locations', 'my career', 'sign out', 'account security', 'settings']):
                        continue
                    
                    # Look for description start indicators
                    if any(keyword in para.lower() for keyword in ['job summary', 'description', 'about the role', 'what you\'ll do', 'responsibilities', 'key job responsibilities']):
                        in_description = True
                    
                    if in_description and len(para) > 20:
                        description_parts.append(para)
                        
                    # Stop if we hit a footer or application section
                    if any(end_indicator in para.lower() for end_indicator in ['apply now', 'basic qualifications', 'preferred qualifications', 'amazon is an equal opportunity']):
                        break
                
                if description_parts:
                    combined_desc = '\n'.join(description_parts)
                    if len(combined_desc) > 100:
                        return self._clean_description(combined_desc)
        except Exception:
            pass
        
        return "Description not available"

    def _clean_description(self, description: str) -> str:
        """Clean up the job description"""
        # Remove excessive whitespace
        description = re.sub(r'\s+', ' ', description)
        
        # Remove common Amazon header/footer content
        unwanted_patterns = [
            r'HomeTeamsLocationsJob categoriesMy careerMy applicationsMy profileAccount securitySettingsSign outResourcesDisability accommodationsBenefitsInclusive experiencesInterview tipsLeadership principles',
            r'Amazon is an equal opportunity employer.*',
            r'Join us on.*',
            r'Find Careers.*',
            r'Working At Amazon.*',
            r'Help.*FAQ.*',
            r'My career.*My applications.*My profile.*',
            r'Account security.*Settings.*Sign out.*',
            r'Resources.*Disability accommodations.*Benefits.*'
        ]
        
        for pattern in unwanted_patterns:
            description = re.sub(pattern, '', description, flags=re.IGNORECASE | re.DOTALL)
        
        # Remove leading/trailing whitespace
        description = description.strip()
        
        # If description is too short after cleaning, return a placeholder
        if len(description) < 50:
            return "Job description not fully available"
        
        return description

    async def _extract_location(self, page: Page) -> str:
        """Extract location from detail page - focuses on actual job location"""
        
        # Method 1: Target the specific Job details section
        try:
            # Look for location in the Job details section specifically
            job_details_selectors = [
                "div:has-text('Job details') + * li:has([aria-label*='location'])",
                "div:has-text('Job details') ~ * li:has([aria-label*='location'])",
                "section:has-text('Job details') li:has([aria-label*='location'])",
                "li[class*='association']:has([aria-label*='location'])",
                "li.association-wrapper:has([aria-label*='location'])"
            ]
            
            for selector in job_details_selectors:
                try:
                    element = await page.query_selector(selector)
                    if element:
                        location_text = await element.inner_text()
                        if location_text and "," in location_text:
                            # Clean up the location text
                            location = location_text.strip()
                            # Remove any extra text like "Location:" prefix
                            location = re.sub(r'^Location:\s*', '', location, flags=re.IGNORECASE)
                            # Replace newlines with semicolons for multiple locations
                            location = re.sub(r'\n+', '; ', location)
                            # Clean up any extra whitespace
                            location = re.sub(r'\s+', ' ', location)
                            if location and len(location) > 5:
                                return location
                except Exception:
                    continue
            
            # Method 2: Look for the location icon and its associated content
            location_icon_selectors = [
                "[aria-label*='location'] + *",
                "[aria-label*='location'] ~ *", 
                "span[aria-label*='location'] + ul",
                "span[aria-label*='location'] ~ ul"
            ]
            
            for selector in location_icon_selectors:
                try:
                    element = await page.query_selector(selector)
                    if element:
                        location_text = await element.inner_text()
                        if location_text and "," in location_text:
                            location = location_text.strip()
                            # Replace newlines with semicolons for multiple locations
                            location = re.sub(r'\n+', '; ', location)
                            # Clean up any extra whitespace
                            location = re.sub(r'\s+', ' ', location)
                            if location and len(location) > 5:
                                return location
                except Exception:
                    continue
            
            # Method 3: Target specific job detail structure
            try:
                # Look for the pattern in the HTML structure we saw in the screenshot
                page_content = await page.content()
                
                # Extract location from the job details section
                location_patterns = [
                    r'<li[^>]*association[^>]*>.*?<span[^>]*aria-label[^>]*location[^>]*>.*?</span>.*?<ul[^>]*>.*?<li[^>]*>([^<]+)</li>',
                    r'aria-label[^>]*location[^>]*>.*?</span>.*?<ul[^>]*association-content[^>]*>.*?<li[^>]*>([^<]+)</li>',
                    r'<span[^>]*location[^>]*</span>.*?<ul[^>]*>.*?<li[^>]*>([A-Z]{2,3},\s*[A-Z]{2},\s*[^<]+)</li>'
                ]
                
                for pattern in location_patterns:
                    matches = re.findall(pattern, page_content, re.DOTALL | re.IGNORECASE)
                    for match in matches:
                        location = match.strip()
                        # Validate it looks like a real location
                        if re.match(r'^[A-Z]{2,3},\s*[A-Z]{2},\s*[^,]+$', location):
                            return location
                        
            except Exception:
                pass
            
            # Method 4: Simple text pattern matching in page text (as fallback)
            try:
                page_text = await page.inner_text("body")
                
                # Look for primary location patterns (not in recommendations)
                location_patterns = [
                    r'(USA,\s*[A-Z]{2},\s*[^,\n;]+)(?=\s*(?:\n|$|Apply now|Job details|Description))',
                    r'(CAN,\s*[A-Z]{2},\s*[^,\n;]+)(?=\s*(?:\n|$|Apply now|Job details|Description))',
                    r'([^,\n;]+,\s*[A-Z]{2},\s*USA)(?=\s*(?:\n|$|Apply now|Job details|Description))',
                    r'([^,\n;]+,\s*[A-Z]{2},\s*Canada)(?=\s*(?:\n|$|Apply now|Job details|Description))'
                ]
                
                for pattern in location_patterns:
                    match = re.search(pattern, page_text)
                    if match:
                        location = match.group(1).strip()
                        # Avoid locations from recommended jobs section
                        if "Trento" not in location and "Updated about" not in location:
                            return location
                        
            except Exception:
                pass
                
        except Exception:
            pass
        
        return "Location not specified"

    async def _extract_metadata(self, page: Page) -> Dict[str, Any]:
        """Extract additional metadata from job detail page"""
        metadata = {}
        
        try:
            # Method 1: Extract department from Job details section specifically
            dept_selectors = [
                "div:has-text('Job details') + * li:has([aria-label*='category'])",
                "div:has-text('Job details') ~ * li:has([aria-label*='category'])",
                "section:has-text('Job details') li:has([aria-label*='category'])",
                "li[class*='association']:has([aria-label*='category'])",
                "li.association-wrapper:has([aria-label*='category'])"
            ]
            
            for selector in dept_selectors:
                try:
                    element = await page.query_selector(selector)
                    if element:
                        dept_text = await element.inner_text()
                        if dept_text and len(dept_text.strip()) > 2:
                            # Clean up department text
                            dept = dept_text.strip()
                            # Remove any prefix like "Job category:" 
                            dept = re.sub(r'^(Job category|Category|Department):\s*', '', dept, flags=re.IGNORECASE)
                            if dept and len(dept) > 2:
                                metadata['department'] = dept
                                break
                except Exception:
                    continue
            
            # Method 2: Look for department via category icon and content
            if 'department' not in metadata:
                category_selectors = [
                    "[aria-label*='category'] + *",
                    "[aria-label*='category'] ~ *",
                    "span[aria-label*='category'] + ul",
                    "span[aria-label*='category'] ~ ul"
                ]
                
                for selector in category_selectors:
                    try:
                        element = await page.query_selector(selector)
                        if element:
                            dept_text = await element.inner_text()
                            if dept_text and len(dept_text.strip()) > 2:
                                dept = dept_text.strip().split('\n')[0].strip()
                                if dept and len(dept) > 2:
                                    metadata['department'] = dept
                                    break
                    except Exception:
                        continue
            
            # Method 3: Extract from HTML structure directly
            if 'department' not in metadata:
                try:
                    page_content = await page.content()
                    
                    # Look for the department/category pattern in HTML
                    dept_patterns = [
                        r'<li[^>]*association[^>]*>.*?<span[^>]*aria-label[^>]*category[^>]*>.*?</span>.*?<ul[^>]*>.*?<li[^>]*>([^<]+)</li>',
                        r'aria-label[^>]*category[^>]*>.*?</span>.*?<ul[^>]*association-content[^>]*>.*?<li[^>]*>([^<]+)</li>',
                        r'<span[^>]*category[^>]*</span>.*?<ul[^>]*>.*?<li[^>]*>([^<]+)</li>'
                    ]
                    
                    for pattern in dept_patterns:
                        matches = re.findall(pattern, page_content, re.DOTALL | re.IGNORECASE)
                        for match in matches:
                            dept = match.strip()
                            # Validate it looks like a department name
                            if len(dept) > 2 and not re.match(r'^[A-Z]{2,3},', dept):
                                metadata['department'] = dept
                                break
                        if 'department' in metadata:
                            break
                            
                except Exception:
                    pass
            
            # Method 4: Enhanced department pattern matching from page text
            if 'department' not in metadata:
                page_text = await page.inner_text("body")
                
                # Look for department patterns near "Job details" section
                dept_patterns = [
                    # Direct department mentions
                    r'Job\s+details[^]*?([A-Za-z][^,\n]*(?:Engineering|Operations|Development|Management|Support|IT|Technology|Science|Marketing|Sales|HR|Human Resources|Finance|Legal|Security|Design|Research|Analytics|Quality)[^,\n]*)',
                    r'(Operations,\s*IT,?\s*&?\s*Support\s*Engineering)',
                    r'(Software\s*Development)',
                    r'(Data\s*Science)',
                    r'(Machine\s*Learning)',
                    r'(Human\s*Resources)',
                    r'(Sales\s*&?\s*Marketing)',
                    r'(Facilities,?\s*Maintenance,?\s*&?\s*Real\s*Estate)',
                    r'(Product\s*Management)',
                    r'(Quality\s*Assurance)',
                    r'(Customer\s*Service)',
                    # Job category patterns
                    r'Job\s+Category:\s*([^,\n]+)',
                    r'Department:\s*([^,\n]+)',
                    r'Team:\s*([^,\n]+)',
                    r'Organization:\s*([^,\n]+)',
                    r'Division:\s*([^,\n]+)',
                ]
                
                for pattern in dept_patterns:
                    match = re.search(pattern, page_text, re.IGNORECASE | re.DOTALL)
                    if match:
                        dept = match.group(1).strip()
                        # Filter out location-like strings and ensure it's reasonable
                        if (len(dept) > 2 and 
                            not re.match(r'^[A-Z]{2,3},', dept) and 
                            not re.match(r'^[A-Z]{2}$', dept) and
                            "Trento" not in dept and
                            "Springs" not in dept):
                            metadata['department'] = dept
                            break
            
            # Method 5: Infer department from job title if still not found
            if 'department' not in metadata:
                try:
                    title_element = await page.query_selector("h1")
                    title_text = ""
                    if title_element:
                        title_text = await title_element.inner_text()
                    
                    title_lower = title_text.lower()
                    
                    # Department inference based on job title keywords
                    dept_keywords = {
                        'Engineering': ['engineer', 'engineering', 'technical', 'software', 'developer', 'architect', 'infrastructure', 'systems'],
                        'Operations': ['operations', 'ops', 'manager', 'coordinator', 'facility', 'logistics', 'supply chain', 'delivery'],
                        'Data Science': ['data scientist', 'machine learning', 'ml', 'analytics', 'data analysis'],
                        'Product Management': ['product manager', 'product owner', 'product strategy', 'product'],
                        'Sales & Marketing': ['sales', 'marketing', 'business development', 'account manager'],
                        'Human Resources': ['hr', 'human resources', 'recruiter', 'people'],
                        'Finance': ['financial', 'accountant', 'finance', 'controller'],
                        'Security': ['security', 'cybersecurity', 'compliance'],
                        'Design': ['designer', 'ux', 'ui', 'design'],
                        'Research': ['research', 'scientist'],
                        'Quality Assurance': ['qa', 'quality', 'testing']
                    }
                    
                    best_dept = None
                    max_matches = 0
                    
                    for dept, keywords in dept_keywords.items():
                        matches = sum(1 for keyword in keywords if keyword in title_lower)
                        if matches > max_matches:
                            max_matches = matches
                            best_dept = dept
                    
                    if best_dept and max_matches > 0:
                        metadata['department'] = best_dept
                        
                except Exception:
                    pass
            
            # Extract posted date if available
            posted_patterns = [
                r'Posted\s+([^,\n]+)',
                r'Date Posted:\s*([^,\n]+)',
                r'Published:\s*([^,\n]+)'
            ]
            
            page_text = await page.inner_text("body")
            for pattern in posted_patterns:
                posted_match = re.search(pattern, page_text)
                if posted_match:
                    metadata['date_posted'] = posted_match.group(1).strip()
                    break
            
        except Exception as e:
            self.logger.debug(f"Error extracting metadata: {e}")
        
        return metadata

    def _extract_job_id_from_url(self, url: str) -> str:
        """Extract job ID from Amazon job URL"""
        try:
            # Amazon job URLs format: /en/jobs/{job_id}/{job-slug}
            match = re.search(r'/en/jobs/(\d+)/', url)
            if match:
                return match.group(1)
        except Exception:
            pass
        return "UNKNOWN"

    async def _go_to_next_page(self, page: Page) -> bool:
        """Navigate to the next page of results"""
        try:
            # Look for pagination buttons with enhanced error handling
            next_selectors = [
                "button.btn_circle_right",
                "button[aria-label*='Next page']",
                "a[aria-label*='Next']",
                "button[aria-label*='next' i]",
                "a[href*='offset=']"
            ]
            
            for selector in next_selectors:
                try:
                    next_element = await page.query_selector(selector)
                    if next_element:
                        # Check if button is disabled
                        is_disabled = await next_element.get_attribute("disabled")
                        aria_disabled = await next_element.get_attribute("aria-disabled")
                        
                        if is_disabled == "true" or aria_disabled == "true":
                            self.logger.debug(f"Next button found but disabled: {selector}")
                            continue
                        
                        # Check if element is visible and clickable
                        if await next_element.is_visible():
                            self.logger.debug(f"Successfully navigated using selector: {selector}")
                            await next_element.click()
                            
                            # Wait for navigation with enhanced timeout
                            await page.wait_for_load_state("networkidle", timeout=15000)
                            return True
                        else:
                            self.logger.debug(f"Next button not visible: {selector}")
                            
                except Exception as e:
                    self.logger.debug(f"Error with next selector {selector}: {e}")
                    continue
            
            self.logger.debug("No next page navigation options found")
            return False
            
        except Exception as e:
            self.logger.debug(f"Error in pagination: {e}")
            return False