import asyncio
import json
import logging
import re
from datetime import datetime
from typing import Dict, List, Optional, Any
from urllib.parse import urljoin, urlparse

from playwright.async_api import Page, Browser, async_playwright

logger = logging.getLogger("job_tracker")

def now_iso() -> str:
    return datetime.now().isoformat()

class WarnerBrosScraper:
    def __init__(self, website_config: Dict[str, Any], global_config: Dict[str, Any]):
        self.config = website_config
        self.global_config = global_config
        self.base_url = website_config['base_url']
        self.max_jobs = website_config.get('max_jobs', 5)
        self.scraped_jobs = []

    async def scrape_jobs(self) -> List[Dict[str, Any]]:
        """Main method to scrape jobs from Warner Bros careers page"""
        try:
            logger.info(f"Starting Warner Bros job scraping from {self.base_url}")
            
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=self.global_config.get("global_settings", {}).get("headless", True),
                    args=["--no-sandbox", "--disable-dev-shm-usage"]
                )
                context = await browser.new_context(
                    user_agent=self.global_config.get("playwright_global", {}).get("user_agent", 
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
                )
                page = await context.new_page()
                await self._setup_page(page)
                
                # Navigate to the base search page first
                logger.info(f"Navigating to base search page: {self.base_url}")
                await page.goto(self.base_url, wait_until='networkidle')
                await page.wait_for_timeout(3000)
                
                # Handle cookie popup first if it exists
                await self._handle_cookie_popup(page)
                
                # Apply location filters by clicking checkboxes
                await self._apply_location_filters(page)
                
                # Wait for filtered results to load
                await page.wait_for_timeout(3000)
                
                # Wait for job listings to load
                await page.wait_for_selector(self.config['selectors']['job_cards'], timeout=30000)
                
                # Handle multi-location jobs by expanding dropdowns first
                await self._expand_multi_location_jobs(page)
                
                # Get job cards and extract URLs after expansion
                job_cards = await page.query_selector_all(self.config['selectors']['job_cards'])
                logger.info(f"Found {len(job_cards)} job cards (after expanding multi-location jobs)")
                
                # Collect all job URLs first
                job_urls = []
                for i, card in enumerate(job_cards[:self.max_jobs]):
                    try:
                        job_url = await card.get_attribute('href')
                        if job_url and job_url.startswith('/'):
                            job_url = urljoin(self.base_url, job_url)
                        if job_url:
                            job_urls.append(job_url)
                    except Exception as e:
                        logger.error(f"Error extracting URL from card {i + 1}: {str(e)}")
                        continue
                
                logger.info(f"Collected {len(job_urls)} job URLs")
                
                # Process each job URL
                for i, job_url in enumerate(job_urls):
                    try:
                        job_data = await self._extract_job_from_url(page, job_url, i + 1)
                        if job_data and self._should_include_job(job_data):
                            self.scraped_jobs.append(job_data)
                            logger.info(f"Successfully scraped job {i + 1}: {job_data.get('title', 'Unknown')}")
                        elif job_data:
                            logger.info(f"Filtered out job {i + 1} (location not in allowed regions): {job_data.get('location', 'Unknown location')}")
                        
                        # Add delay between jobs
                        await page.wait_for_timeout(2000)
                        
                    except Exception as e:
                        logger.error(f"Error scraping job {i + 1}: {str(e)}")
                        continue
                
                await browser.close()
                logger.info(f"Completed Warner Bros scraping. Total jobs: {len(self.scraped_jobs)}")
                return self.scraped_jobs
            
        except Exception as e:
            logger.error(f"Error in Warner Bros scraper: {str(e)}")
            return []

    async def _setup_page(self, page: Page):
        """Setup page with user agent and other configurations"""
        await page.set_extra_http_headers({
            'Accept-Language': 'en-US,en;q=0.9',
        })

    async def _handle_cookie_popup(self, page: Page) -> None:
        """Handle GDPR cookie popup that might be blocking interactions"""
        try:
            logger.info("Checking for cookie popup...")
            
            # Wait a bit for any popups to appear
            await page.wait_for_timeout(2000)
            
            # Try pressing Escape first to dismiss any modal
            await page.keyboard.press('Escape')
            await page.wait_for_timeout(1000)
            
            # Common cookie popup selectors - be more specific
            cookie_selectors = [
                "button:has-text('Accept All')",
                "button:has-text('Accept')", 
                "button:has-text('I Accept')",
                "button:has-text('OK')",
                "button:has-text('Agree')",
                "button:has-text('Allow')",
                "[data-testid*='accept']",
                "[id*='accept']",
                ".accept-button"
            ]
            
            for selector in cookie_selectors:
                try:
                    cookie_button = await page.wait_for_selector(selector, timeout=2000)
                    if cookie_button and await cookie_button.is_visible():
                        await cookie_button.click()
                        logger.info(f"✓ Clicked cookie popup button: {selector}")
                        await page.wait_for_timeout(1000)
                        return
                except:
                    continue
                    
            logger.info("No cookie popup found or already dismissed")
            
        except Exception as e:
            logger.debug(f"Error handling cookie popup: {str(e)}")

    async def _apply_location_filters(self, page: Page) -> None:
        """Apply location filters by clicking checkboxes for US and Canada"""
        try:
            logger.info("Applying location filters for US and Canada...")
            
            # Wait for page to stabilize
            await page.wait_for_timeout(2000)
            
            # Try to find and click the country checkboxes directly using more specific selectors
            # Based on the screenshot, look for checkboxes with specific text patterns
            
            filters_applied = 0
            
            # Method 1: Try clicking the visible labels/spans that control the hidden checkboxes
            try:
                # Look for Canada checkbox first
                canada_selectors = [
                    "span:has-text('Canada')",
                    "label:has-text('Canada')",
                    "[data-ph-at-text='Canada']",
                    "//span[contains(text(), 'Canada') and contains(text(), '24')]",
                    "//label[contains(text(), 'Canada')]"
                ]
                
                for selector in canada_selectors:
                    try:
                        if selector.startswith('//'):
                            element = await page.wait_for_selector(f"xpath={selector}", timeout=3000)
                        else:
                            element = await page.wait_for_selector(selector, timeout=3000)
                        
                        if element and await element.is_visible():
                            await element.click()
                            logger.info(f"✓ Clicked Canada element: {selector}")
                            filters_applied += 1
                            await page.wait_for_timeout(1000)
                            break
                    except:
                        continue
                        
            except Exception as e:
                logger.debug(f"Method 1 Canada failed: {str(e)}")
            
            # Method 1: Try clicking the visible US label/span
            try:
                # Look for US checkbox
                us_selectors = [
                    "span:has-text('United States')",
                    "span:has-text('America')",
                    "label:has-text('United States')",
                    "label:has-text('America')",
                    "[data-ph-at-text='United States of America']",
                    "//span[contains(text(), 'United States') and contains(text(), '266')]",
                    "//span[contains(text(), 'America') and contains(text(), 'jobs')]",
                    "//label[contains(text(), 'United States')]",
                    "//label[contains(text(), 'America')]"
                ]
                
                for selector in us_selectors:
                    try:
                        if selector.startswith('//'):
                            element = await page.wait_for_selector(f"xpath={selector}", timeout=3000)
                        else:
                            element = await page.wait_for_selector(selector, timeout=3000)
                        
                        if element and await element.is_visible():
                            await element.click()
                            logger.info(f"✓ Clicked US element: {selector}")
                            filters_applied += 1
                            await page.wait_for_timeout(1000)
                            break
                    except:
                        continue
                        
            except Exception as e:
                logger.debug(f"Method 1 US failed: {str(e)}")
            
            # Method 2: Force click the hidden checkboxes using JavaScript
            if filters_applied < 2:
                logger.info("Trying to force click hidden checkboxes with JavaScript...")
                
                try:
                    # Check current checkbox states and click if needed
                    result = await page.evaluate("""
                        () => {
                            let clicked = 0;
                            
                            // Try to click Canada checkbox
                            const canadaCheckbox = document.querySelector('input[data-ph-at-text="Canada"]');
                            if (canadaCheckbox && !canadaCheckbox.checked) {
                                canadaCheckbox.click();
                                clicked++;
                                console.log('Clicked Canada checkbox via JS');
                            } else if (canadaCheckbox && canadaCheckbox.checked) {
                                console.log('Canada checkbox already checked');
                                clicked++;
                            }
                            
                            // Try to click US checkbox  
                            const usCheckbox = document.querySelector('input[data-ph-at-text="United States of America"]');
                            if (usCheckbox && !usCheckbox.checked) {
                                usCheckbox.click();
                                clicked++;
                                console.log('Clicked US checkbox via JS');
                            } else if (usCheckbox && usCheckbox.checked) {
                                console.log('US checkbox already checked');
                                clicked++;
                            }
                            
                            return {
                                clicked: clicked,
                                canadaFound: !!canadaCheckbox,
                                usFound: !!usCheckbox,
                                canadaChecked: canadaCheckbox ? canadaCheckbox.checked : false,
                                usChecked: usCheckbox ? usCheckbox.checked : false
                            };
                        }
                    """)
                    
                    logger.info(f"JavaScript result: {result}")
                    filters_applied += result.get('clicked', 0)
                        
                except Exception as e:
                    logger.debug(f"JavaScript method failed: {str(e)}")
            
            # Method 3: Try clicking parent elements of the checkboxes
            if filters_applied < 2:
                logger.info("Trying method 3: clicking parent elements...")
                
                try:
                    # Find checkboxes by their data attributes and click their parents
                    canada_parents = await page.query_selector_all('[data-ph-at-text="Canada"]')
                    for checkbox in canada_parents:
                        try:
                            parent = await checkbox.query_selector('xpath=..')
                            if parent and await parent.is_visible():
                                await parent.click()
                                logger.info("✓ Clicked Canada parent element")
                                filters_applied += 1
                                await page.wait_for_timeout(1000)
                                break
                        except:
                            continue
                    
                    us_parents = await page.query_selector_all('[data-ph-at-text="United States of America"]')
                    for checkbox in us_parents:
                        try:
                            parent = await checkbox.query_selector('xpath=..')
                            if parent and await parent.is_visible():
                                await parent.click()
                                logger.info("✓ Clicked US parent element")
                                filters_applied += 1
                                await page.wait_for_timeout(1000)
                                break
                        except:
                            continue
                            
                except Exception as e:
                    logger.debug(f"Method 3 failed: {str(e)}")
            
            # Method 4: If still no luck, try a more general approach
            if filters_applied < 2:
                logger.info("Trying aggressive checkbox approach...")
                
                # Get all checkboxes and manually check their associated text
                checkboxes = await page.query_selector_all("input[type='checkbox']")
                logger.info(f"Found {len(checkboxes[:30])} checkboxes to examine")  # Limit to first 30
                
                for i, checkbox in enumerate(checkboxes[:30]):  # Only check first 30 to avoid timeout
                    try:
                        # Get attributes to identify the checkbox
                        aria_label = await checkbox.get_attribute('aria-label') or ""
                        data_text = await checkbox.get_attribute('data-ph-at-text') or ""
                        
                        # Get surrounding text context
                        parent = await checkbox.query_selector('xpath=..')
                        if parent:
                            parent_text = await parent.text_content() or ""
                        else:
                            parent_text = ""
                        
                        combined_text = f"{aria_label} {data_text} {parent_text}".lower()
                        
                        # Check if this is Canada or US checkbox
                        is_canada = 'canada' in combined_text and ('24' in combined_text or 'jobs' in combined_text)
                        is_us = ('united states' in combined_text or 'america' in combined_text) and 'jobs' in combined_text
                        
                        if is_canada or is_us:
                            logger.info(f"Found {'Canada' if is_canada else 'US'} checkbox: {combined_text[:100]}...")
                            
                            # Check if already selected
                            is_checked = await checkbox.is_checked()
                            if not is_checked:
                                # Try to click via JavaScript to bypass visibility issues
                                await page.evaluate('(element) => element.click()', checkbox)
                                logger.info(f"✓ Force-clicked {'Canada' if is_canada else 'US'} checkbox via JS")
                                filters_applied += 1
                                await page.wait_for_timeout(1000)
                            else:
                                logger.info(f"✓ {'Canada' if is_canada else 'US'} checkbox already selected")
                                filters_applied += 1
                                
                        if filters_applied >= 2:
                            break
                            
                    except Exception as e:
                        logger.debug(f"Error with checkbox {i}: {str(e)}")
                        continue
                        
                logger.info(f"Aggressive approach completed. Total filters applied: {filters_applied}")
            
            # Final fallback: Try to force click using specific element IDs if we found them earlier
            if filters_applied < 2:
                logger.info("Final fallback: trying to click any country filter we can find...")
                
                try:
                    # Try to find any elements that might trigger country selection
                    country_elements = await page.query_selector_all('[data-ph-at-facetkey="facet-country"]')
                    logger.info(f"Found {len(country_elements)} country facet elements")
                    
                    for element in country_elements[:5]:  # Try first 5
                        try:
                            # Get the text to see what country this is
                            parent = await element.query_selector('xpath=..')
                            if parent:
                                text = await parent.text_content() or ""
                                if 'canada' in text.lower() or 'united states' in text.lower() or 'america' in text.lower():
                                    await page.evaluate('(element) => element.click()', element)
                                    logger.info(f"✓ Clicked country element: {text[:50]}...")
                                    filters_applied += 1
                                    await page.wait_for_timeout(1000)
                        except:
                            continue
                            
                except Exception as e:
                    logger.debug(f"Final fallback failed: {str(e)}")
            
            # Method 2: If Method 1 failed, try a simpler approach
            if filters_applied == 0:
                logger.info("Trying simplified checkbox detection...")
                
                # Look for all checkboxes and check their context
                checkboxes = await page.query_selector_all("input[type='checkbox']")
                logger.info(f"Found {len(checkboxes)} total checkboxes")
                
                for i, checkbox in enumerate(checkboxes[:20]):  # Limit to first 20 to avoid timeout
                    try:
                        # Get surrounding text to determine what this checkbox is for
                        parent = await checkbox.query_selector('xpath=..')
                        if parent:
                            text = await parent.text_content()
                            if text and ('canada' in text.lower() or 'united states' in text.lower() or 'america' in text.lower()):
                                if not await checkbox.is_checked():
                                    await checkbox.click()
                                    logger.info(f"✓ Clicked checkbox for: {text.strip()[:50]}...")
                                    filters_applied += 1
                                    await page.wait_for_timeout(1000)
                                else:
                                    logger.info(f"✓ Checkbox already selected: {text.strip()[:50]}...")
                                    filters_applied += 1
                                
                                if filters_applied >= 2:
                                    break
                    except Exception as e:
                        logger.debug(f"Error with checkbox {i}: {str(e)}")
                        continue
            
            if filters_applied > 0:
                logger.info(f"Successfully applied {filters_applied} location filters")
                # Wait for results to update
                await page.wait_for_timeout(3000)
            else:
                logger.warning("Could not find or apply location filters")
                
        except Exception as e:
            logger.error(f"Error applying location filters: {str(e)}")
            # Continue with scraping even if filters failed

    async def _expand_multi_location_jobs(self, page: Page):
        """Expand multi-location job dropdowns to show all job variations"""
        try:
            logger.info("Looking for multi-location job dropdowns...")
            
            # Look for job cards that have multi-location dropdowns
            # Based on the screenshot, look for text patterns like "Job available in X locations"
            multi_location_patterns = [
                "//button[contains(text(), 'Job available in')]",
                "//span[contains(text(), 'Job available in')]/following-sibling::button",
                "//div[contains(text(), 'Job available in')]//button",
                "[aria-label*='locations']",
                "button[data-ph-at-id*='location']",
                ".job-multi-locations button"
            ]
            
            expanded_count = 0
            
            # Also look for text containing "Job available in X locations"
            try:
                # Search for elements containing the multi-location text
                multi_location_elements = await page.query_selector_all("text=Job available in")
                if multi_location_elements:
                    logger.info(f"Found {len(multi_location_elements)} 'Job available in' text elements")
                    
                # Look for elements with specific text patterns
                location_texts = await page.query_selector_all("//*[contains(text(), 'Job available in')]")
                logger.info(f"Found {len(location_texts)} elements with 'Job available in' text")
                
                for element in location_texts:
                    try:
                        # Get the parent container that might have the clickable button
                        parent = await element.evaluate("el => el.closest('div, li, article')")
                        if parent:
                            # Look for buttons or clickable elements in the parent
                            buttons = await page.query_selector_all("button", parent)
                            for button in buttons:
                                try:
                                    # Check if button is visible and has dropdown functionality
                                    is_visible = await button.is_visible()
                                    if is_visible:
                                        # Try to click the button
                                        await button.click()
                                        expanded_count += 1
                                        logger.info(f"Expanded multi-location dropdown (button)")
                                        await page.wait_for_timeout(1500)
                                        break
                                except Exception as e:
                                    continue
                    except Exception as e:
                        continue
                        
            except Exception as e:
                logger.debug(f"Error searching for multi-location text elements: {str(e)}")
            
            # Try XPath patterns for multi-location dropdowns
            for pattern in multi_location_patterns:
                try:
                    if pattern.startswith('//'):
                        # XPath selector
                        buttons = await page.locator(pattern).all()
                    else:
                        # CSS selector
                        buttons = await page.query_selector_all(pattern)
                    
                    for button in buttons:
                        try:
                            # Check if button is visible and clickable
                            is_visible = await button.is_visible()
                            if is_visible:
                                # Get button text to confirm it's a multi-location button
                                button_text = await button.inner_text()
                                if any(phrase in button_text.lower() for phrase in ['available in', 'locations', 'location']):
                                    # Click to expand the dropdown
                                    await button.click()
                                    expanded_count += 1
                                    logger.info(f"Expanded multi-location dropdown: {button_text}")
                                    
                                    # Wait for the expanded content to load
                                    await page.wait_for_timeout(1500)
                                    
                        except Exception as e:
                            # Continue with next button if this one fails
                            continue
                            
                except Exception as e:
                    # Continue with next selector if this one fails
                    continue
            
            # Try a more general approach - look for any clickable elements near "Job available" text
            try:
                page_content = await page.content()
                if "Job available in" in page_content:
                    logger.info("Found 'Job available in' text in page content")
                    
                    # Look for any buttons or clickable elements that might be dropdowns
                    all_buttons = await page.query_selector_all("button, [role='button'], .dropdown-toggle")
                    
                    for button in all_buttons:
                        try:
                            button_text = await button.inner_text()
                            # Check if the button text suggests it's a location dropdown
                            if any(keyword in button_text.lower() for keyword in ['available', 'location', 'dropdown']):
                                is_visible = await button.is_visible()
                                if is_visible:
                                    await button.click()
                                    expanded_count += 1
                                    logger.info(f"Expanded potential location dropdown: {button_text}")
                                    await page.wait_for_timeout(1000)
                        except Exception as e:
                            continue
                            
            except Exception as e:
                logger.debug(f"Error in general dropdown search: {str(e)}")
            
            if expanded_count > 0:
                logger.info(f"Successfully expanded {expanded_count} multi-location job dropdowns")
                # Wait a bit more for all expanded content to fully load
                await page.wait_for_timeout(3000)
            else:
                logger.info("No multi-location job dropdowns found to expand")
                
        except Exception as e:
            logger.error(f"Error expanding multi-location jobs: {str(e)}")

    async def _extract_job_from_url(self, page: Page, job_url: str, job_index: int) -> Optional[Dict[str, Any]]:
        """Extract job data from a job URL"""
        try:
            logger.info(f"Processing job {job_index}: {job_url}")
            
            # Navigate to job detail page
            await page.goto(job_url, wait_until='networkidle')
            await page.wait_for_timeout(2000)
            
            # Extract job data from detail page
            job_data = await self._extract_job_details(page, job_url)
            
            return job_data
            
        except Exception as e:
            logger.error(f"Error extracting job from URL {job_index}: {str(e)}")
            return None

    async def _extract_job_from_card(self, page: Page, card, job_index: int) -> Optional[Dict[str, Any]]:
        """Extract job data from a job card and detailed page"""
        try:
            # Get job URL
            job_url = await card.get_attribute('href')
            if not job_url:
                logger.warning(f"No URL found for job {job_index}")
                return None
            
            # Make URL absolute
            if job_url.startswith('/'):
                job_url = urljoin(self.base_url, job_url)
            
            logger.info(f"Processing job {job_index}: {job_url}")
            
            # Navigate to job detail page
            await page.goto(job_url, wait_until='networkidle')
            await page.wait_for_timeout(2000)
            
            # Extract job data from detail page
            job_data = await self._extract_job_details(page, job_url)
            
            return job_data
            
        except Exception as e:
            logger.error(f"Error extracting job from card {job_index}: {str(e)}")
            return None

    async def _extract_job_details(self, page: Page, job_url: str) -> Dict[str, Any]:
        """Extract detailed job information from job detail page"""
        try:
            # Extract title
            title = await self._extract_title(page)
            
            # Extract location
            location = await self._extract_location(page)
            
            # Extract job ID
            job_id = await self._extract_job_id(page, job_url)
            
            # Extract description
            description = await self._extract_description(page)
            
            # Extract additional metadata
            metadata = await self._extract_metadata(page)
            
            # Build job data
            job_data = {
                'jobId': f"{self.config['data_mapping']['jobId_prefix']}{job_id}",
                'title': title,
                'company': self.config['data_mapping']['company'],
                'location': location,
                'url': job_url,
                'description': description,
                'source': self.config['data_mapping']['source'],
                'status': self.config['data_mapping']['status'],
                'scraped_date': now_iso()
            }
            
            # Add metadata
            job_data.update(metadata)
            
            return job_data
            
        except Exception as e:
            logger.error(f"Error extracting job details from {job_url}: {str(e)}")
            return {}

    async def _extract_title(self, page: Page) -> str:
        """Extract job title"""
        try:
            title_element = await page.query_selector(self.config['job_detail_selectors']['title'])
            if title_element:
                title = await title_element.inner_text()
                return title.strip()
        except Exception as e:
            logger.error(f"Error extracting title: {str(e)}")
        
        return "Unknown Title"

    async def _extract_location(self, page: Page) -> str:
        """Extract job location"""
        try:
            # Method 1: Try to get location from specific HTML elements first
            location_selectors = [
                # Based on the developer tools screenshot, target the span with job-location class
                "span.job-location",
                ".job-location",
                "[class*='job-location']",
                # Alternative selectors for location spans
                "span[data-ph-id*='job-fields']:has-text('Canada')",
                "span[data-ph-id*='job-fields']:has-text('United States')",
                # More generic location selectors
                ".au-target.job-location",
                "[au-target-id] span:has-text('Canada')",
                "[au-target-id] span:has-text('United States')",
                # Look for spans containing location patterns
                "span:has-text(', Canada')",
                "span:has-text(', United States')",
            ]
            
            for selector in location_selectors:
                try:
                    element = await page.wait_for_selector(selector, timeout=2000)
                    if element:
                        location = await element.text_content()
                        if location and location.strip():
                            location = location.strip()
                            
                            # Clean the location text extracted from HTML element
                            # Remove "Location" label and whitespace
                            location = re.sub(r'^Location\s*', '', location, flags=re.IGNORECASE)
                            location = re.sub(r'\s+', ' ', location)  # Normalize all whitespace to single spaces
                            location = location.strip()
                            
                            # Basic validation that this looks like a location
                            if any(country in location for country in ['Canada', 'United States']) and ',' in location:
                                logger.info(f"Location extracted from element: {location}")
                                return location
                except:
                    continue
            
            # Method 2: Try to extract from job details area
            try:
                # Look for location in the job header area
                job_header = await page.query_selector(".job-details, .job-header, .job-info")
                if job_header:
                    header_text = await job_header.text_content()
                    if header_text:
                        # Look for location patterns in the header
                        location_match = re.search(r'([A-Za-z\s]+,\s*[A-Za-z\s]+,\s*(?:Canada|United States))', header_text)
                        if location_match:
                            location = location_match.group(1).strip()
                            logger.info(f"Location extracted from job header: {location}")
                            return location
            except:
                pass
            
            # Method 3: Fallback to page content regex (original method)
            page_content = await page.content()
            
            # Look for standard location patterns in the visible text
            location_patterns = [
                r'Location\s*([^<\n]+?)(?:\s*Job Type|\s*WB Games|\s*Job Id|\n)',
                r'Location\s*([^<\n]+?)(?:\s*Job Type|\s*Full time|\s*Part time|\n)',
                r'addressLocality":"([^"]+)","addressRegion":"([^"]+)","addressCountry":"([^"]+)"',
                r'Location([^<\n]+?)(?:WB Games|Job Type|\n)',
            ]
            
            for pattern in location_patterns:
                match = re.search(pattern, page_content, re.IGNORECASE | re.DOTALL)
                if match:
                    if len(match.groups()) == 3:  # addressLocality, addressRegion, addressCountry
                        city, region, country = match.groups()
                        location = f"{city}, {region}, {country}"
                        return location.strip()
                    elif len(match.groups()) == 1:
                        location = match.group(1).strip()
                        original_location = location  # Keep original for debugging
                        
                        # Clean up common issues
                        location = re.sub(r'^["\s]+|["\s]+$', '', location)  # Remove quotes and spaces
                        location = re.sub(r'\s+', ' ', location)  # Normalize spaces
                        
                        # Apply all cleaning patterns in sequence - remove all elif to apply all patterns
                        
                        # Step 1: Handle "Apply for ... job with ... in Location" pattern
                        location = re.sub(r'^Apply\s+for\s+.+?\s+job\s+with\s+.+?\s+in\s+', '', location, flags=re.IGNORECASE)
                        
                        # Step 2: Handle other "Apply for ... in Location" patterns
                        location = re.sub(r'^Apply\s+for\s+.+?\s+in\s+', '', location, flags=re.IGNORECASE)
                        
                        # Step 3: Handle truncated patterns like "al in Montreal"
                        location = re.sub(r'^.*al\s+in\s+', '', location, flags=re.IGNORECASE)
                        location = re.sub(r'^al\s+in\s+', '', location, flags=re.IGNORECASE)
                        
                        # Step 4: Handle business unit prefixes
                        location = re.sub(r'^Games\s+in\s+', '', location, flags=re.IGNORECASE)
                        location = re.sub(r'^Discovery\s+in\s+', '', location, flags=re.IGNORECASE)
                        
                        # Step 5: Handle job title prefixes
                        location = re.sub(r'^Culture\s+Coordinator\s+in\s+', '', location, flags=re.IGNORECASE)
                        location = re.sub(r'^People\s+&\s+Culture\s+Coordinator\s+in\s+', '', location, flags=re.IGNORECASE)
                        location = re.sub(r'^C\s+Technology\s+in\s+', '', location, flags=re.IGNORECASE)
                        location = re.sub(r'^P&C\s+Technology\s+in\s+', '', location, flags=re.IGNORECASE)
                        location = re.sub(r'^Program\s+Manager\s+in\s+', '', location, flags=re.IGNORECASE)
                        
                        # Step 6: Handle "in Remote" or just "in " patterns
                        location = re.sub(r'^\w+\s+in\s+Remote,\s*', 'Remote, ', location, flags=re.IGNORECASE)
                        location = re.sub(r'^in\s+', '', location, flags=re.IGNORECASE)
                        
                        # Step 6: Final cleanup - remove any remaining unwanted patterns
                        location = re.sub(r'^.+?\s+job\s+with\s+.+?\s*', '', location, flags=re.IGNORECASE)
                        location = re.sub(r'^Apply\s+for\s+.+?\s*', '', location, flags=re.IGNORECASE)
                        
                        # Step 7: Final trim and validation
                        location = location.strip()
                        location = re.sub(r'^\s*,\s*', '', location)  # Remove leading comma
                        location = re.sub(r'\s*,\s*$', '', location)  # Remove trailing comma
                        location = re.sub(r'\s+', ' ', location)  # Normalize spaces
                        
                        # Log the transformation for debugging
                        if original_location != location:
                            logger.info(f"Location cleaned: '{original_location}' -> '{location}'")
                        
                        if location and len(location) > 3 and not any(x in location.lower() for x in ['map', 'nonce', 'script', 'json']):
                            return location
            
            # Fallback: look for city, state/province patterns
            city_state_patterns = [
                r'([A-Za-z\s]+),\s*([A-Za-z\s]+),\s*(United States|Canada|India|Mexico|Japan|Italy)',
                r'([A-Za-z\s]+),\s*([A-Za-z\s]+),\s*([A-Za-z\s]+)\s*(?:Job Type|Full time)'
            ]
            
            for pattern in city_state_patterns:
                match = re.search(pattern, page_content, re.IGNORECASE)
                if match:
                    location = ', '.join(match.groups()).strip()
                    return location
                    
        except Exception as e:
            logger.error(f"Error extracting location: {str(e)}")
        
        return "Location not specified"

    async def _extract_job_id(self, page: Page, job_url: str) -> str:
        """Extract job ID"""
        try:
            # Try to extract from page content
            job_id_selectors = [
                "[data-ph-at-text='jobId-text']",
                ".job-id",
                ".job-details .job-id"
            ]
            
            for selector in job_id_selectors:
                job_id_element = await page.query_selector(selector)
                if job_id_element:
                    job_id = await job_id_element.inner_text()
                    job_id = job_id.replace('Job Id', '').replace('Job ID', '').strip()
                    if job_id:
                        return job_id
            
            # Extract from URL
            url_match = re.search(r'/job/([^/]+)/', job_url)
            if url_match:
                return url_match.group(1)
            
            # Extract job ID from page content using regex
            page_content = await page.content()
            job_id_matches = [
                re.search(r'Job Id[:\s]*([A-Z0-9]+)', page_content, re.IGNORECASE),
                re.search(r'Job ID[:\s]*([A-Z0-9]+)', page_content, re.IGNORECASE),
                re.search(r'R\d{9}', page_content),  # Warner Bros specific pattern
            ]
            
            for match in job_id_matches:
                if match:
                    return match.group(1) if len(match.groups()) > 0 else match.group(0)
                    
        except Exception as e:
            logger.error(f"Error extracting job ID: {str(e)}")
        
        return "unknown_id"

    async def _extract_description(self, page: Page) -> str:
        """Extract job description"""
        try:
            # Try to find description container
            description_selectors = [
                "[data-ph-at-id='job-description']",
                ".job-description",
                ".job-content",
                ".au-target.job-description"
            ]
            
            for selector in description_selectors:
                description_element = await page.query_selector(selector)
                if description_element:
                    description = await description_element.inner_text()
                    if description and len(description.strip()) > 50:
                        # Clean up the description
                        description = self._clean_description(description)
                        return description
            
            # Fallback: extract from main content area
            main_content = await page.query_selector('main')
            if main_content:
                content = await main_content.inner_text()
                # Extract content after title and before footer
                lines = content.split('\n')
                description_lines = []
                start_collecting = False
                
                for line in lines:
                    line = line.strip()
                    if not line:
                        continue
                    
                    # Start collecting after finding job title or summary
                    if any(keyword in line.lower() for keyword in ['summary of position', 'who we are', 'summary']):
                        start_collecting = True
                        description_lines.append(line)
                        continue
                    
                    if start_collecting:
                        # Stop before certain sections
                        if any(keyword in line.lower() for keyword in ['share this opportunity', 'similar jobs', 'apply now']):
                            break
                        description_lines.append(line)
                
                if description_lines:
                    description = '\n'.join(description_lines)
                    return self._clean_description(description)
                    
        except Exception as e:
            logger.error(f"Error extracting description: {str(e)}")
        
        return "Description not available"

    def _clean_description(self, description: str) -> str:
        """Clean and format job description"""
        if not description:
            return "Description not available"
        
        # Remove excessive whitespace
        description = re.sub(r'\n\s*\n', '\n\n', description)
        description = re.sub(r' {2,}', ' ', description)
        
        # Remove common unwanted phrases
        unwanted_phrases = [
            r'Back to search results',
            r'Apply Now.*?Save',
            r'Save.*?Apply Now',
            r'Share this opportunity.*',
            r'Get notified for similar jobs.*',
            r'Similar Jobs.*',
            r'Skip to main content.*'
        ]
        
        for phrase in unwanted_phrases:
            description = re.sub(phrase, '', description, flags=re.IGNORECASE | re.DOTALL)
        
        return description.strip()

    async def _extract_metadata(self, page: Page) -> Dict[str, Any]:
        """Extract additional metadata"""
        metadata = {}
        
        try:
            # Extract job type
            job_type_selectors = [
                "[data-ph-at-text='job-type-text']",
                ".job-type",
                ".job-details .job-type"
            ]
            
            for selector in job_type_selectors:
                job_type_element = await page.query_selector(selector)
                if job_type_element:
                    job_type = await job_type_element.inner_text()
                    job_type = job_type.replace('Job Type', '').strip()
                    if job_type:
                        metadata['job_type'] = job_type
                        break
            
            # Extract business/division
            business_selectors = [
                "[data-ph-at-text='business-text']",
                ".business",
                ".division"
            ]
            
            for selector in business_selectors:
                business_element = await page.query_selector(selector)
                if business_element:
                    business = await business_element.inner_text()
                    if business and business != "Business":
                        metadata['business'] = business.strip()
                        break
            
        except Exception as e:
            logger.error(f"Error extracting metadata: {str(e)}")
        
        return metadata

    def _should_include_job(self, job_data: Dict[str, Any]) -> bool:
        """Check if job should be included based on location filters"""
        location_params = self.config.get('location_params', {})
        allowed_countries = location_params.get('countries', [])
        
        if not allowed_countries:
            return True  # No filters, include all jobs
        
        job_location = job_data.get('location', '').lower()
        
        # Check if job location contains any of the allowed countries
        for country in allowed_countries:
            country_lower = country.lower()
            # Check for different variations
            if ('united states' in country_lower and ('united states' in job_location or 'usa' in job_location or 'california' in job_location or 'america' in job_location)) or \
               ('canada' in country_lower and ('canada' in job_location or 'quebec' in job_location or 'ontario' in job_location)):
                return True
        
        return False

    def get_scraped_count(self) -> int:
        """Return the number of jobs scraped"""
        return len(self.scraped_jobs)