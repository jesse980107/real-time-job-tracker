"""
Google Career job scraper
Use Playwright to automatically scrape job information from Google Career website
Implemented according to detailed requirements: recursively click job cards, extract detailed job information
"""

import asyncio
import logging
import json
import re
from typing import List, Dict, Any, Optional
from datetime import datetime
from playwright.async_api import async_playwright, Page, Browser
from urllib.parse import urljoin, urlparse


class GoogleCareerScraper:
    """Google Career job scraper"""
    
    def __init__(self, website_config: Dict[str, Any], global_config: Dict[str, Any]):
        """
        Initialize scraper
        
        Args:
            website_config: Website-specific configuration
            global_config: Global configuration
        """
        self.website_config = website_config
        self.global_config = global_config
        self.logger = logging.getLogger("job_tracker")
        
        # Target URL
        self.target_url = "https://www.google.com/about/careers/applications/jobs/results/?location=United%20States&location=Canada&sort_by=date"
        
        # Global configuration
        self.headless = global_config["global_settings"]["headless"]
        self.timeout = global_config["playwright_global"]["timeout"]
        self.user_agent = global_config["playwright_global"]["user_agent"]
        
        self.scraped_jobs = []
        self.current_index = 0
        self.total_jobs_scraped = 0  # Total scraped job counter
    
    async def scrape_jobs(self) -> List[Dict[str, Any]]:
        """
        Main scraping method
        
        Returns:
            List of scraped job positions
        """
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=self.headless,
                    args=['--no-sandbox', '--disable-dev-shm-usage']
                )
                
                page = await browser.new_page(user_agent=self.user_agent)
                
                # Set page timeout
                page.set_default_timeout(self.timeout)
                
                # Execute search and scraping
                await self._perform_search_and_scrape(page)
                
                await browser.close()
                
                self.logger.info(f"Google Career scraping completed, obtained {len(self.scraped_jobs)} job positions")
                return self.scraped_jobs
                
        except Exception as e:
            self.logger.error(f"Error occurred during Google Career scraping: {e}")
            return []
    
    async def _perform_search_and_scrape(self, page: Page) -> None:
        """
        Execute complete search and scraping workflow
        
        Args:
            page: Playwright page object
        """
        try:
            # Step 1: Visit target URL
            self.logger.info(f"Visiting page: {self.target_url}")
            await page.goto(self.target_url)
            
            # Step 2: Wait for page load and click the first job card's "Learn more"
            self.logger.info("Waiting for Learn more button to load...")
            # await page.wait_for_selector('li.lLd3Je a[aria-label^="Learn more about"]', timeout=10000)
            self.logger.info("Learn more button loaded, preparing to click")
            
            # Click the first job card's Learn more button (using JavaScript execution)
            learn_more_clicked = await page.evaluate('''
                () => {
                    const learnMoreBtn = document.querySelector('li.lLd3Je a[aria-label^="Learn more about"]');
                    if (learnMoreBtn) {
                        console.log('Found Learn more button, preparing to click');
                        learnMoreBtn.click();
                        return true;
                    }
                    return false;
                }
            ''')
            
            if learn_more_clicked:
                await page.wait_for_load_state('networkidle')
                self.logger.info("Successfully clicked first Learn more button")
            else:
                self.logger.error("Learn more button not found")
            
            # Step 3: Start multi-page scraping
            await self._start_multi_page_scraping(page)
            
        except Exception as e:
            self.logger.error(f"Error occurred during search and scraping process: {e}")
    
    async def _start_multi_page_scraping(self, page: Page) -> None:
        """
        Start multi-page scraping
        
        Args:
            page: Playwright page object
        """
        try:
            page_number = 1
            max_pages = 5  # Maximum 5 pages to scrape, can be adjusted as needed
            
            while page_number <= max_pages:
                self.logger.info(f"🔍 Starting to scrape page {page_number}")
                
                # Scrape all job cards on current page
                await self._start_recursive_scraping(page, page_number)
                
                # Try to navigate to next page
                has_next_page = await self._goto_next_page(page)
                
                if not has_next_page:
                    self.logger.info("✅ Reached last page or cannot find next page button")
                    break
                
                page_number += 1
                
                # Wait for page load
                await page.wait_for_load_state('networkidle')
                await asyncio.sleep(2)  # Additional wait to ensure page fully loads
                
        except Exception as e:
            self.logger.error(f"Error occurred during multi-page scraping: {e}")
    
    async def _start_recursive_scraping(self, page: Page, page_number: int = 1) -> None:
        """
        Start recursive scraping of all job cards on current page
        
        Args:
            page: Playwright page object
            page_number: Current page number
        """
        try:
            # Get all job cards
            job_cards = await page.query_selector_all('li.zE6MFb')
            total_jobs = len(job_cards)
            self.logger.info(f"Found {total_jobs} job positions on page {page_number}")
            
            if total_jobs == 0:
                self.logger.warning(f"No job positions found on page {page_number}, starting page status debugging")
                
                # Debug page status
                page_info = await page.evaluate('''
                    () => {
                        return {
                            url: window.location.href,
                            title: document.title,
                            jobCards: document.querySelectorAll('li.zE6MFb').length,
                            allJobElements: document.querySelectorAll('*[class*="job"], *[class*="Job"]').length,
                            learnMoreButtons: document.querySelectorAll('*[aria-label*="Learn"], *[aria-label*="learn"]').length,
                            hasMainContent: !!document.querySelector('main, #main, [role="main"]'),
                            bodyText: document.body.innerText.substring(0, 200)
                        };
                    }
                ''')
                
                print(f"🔍 Page {page_number} debug information:")
                print(f"  URL: {page_info['url']}")
                print(f"  Title: {page_info['title']}")
                print(f"  Job cards: {page_info['jobCards']}")
                print(f"  All job elements: {page_info['allJobElements']}")
                print(f"  Learn more buttons: {page_info['learnMoreButtons']}")
                print(f"  Page content preview: {page_info['bodyText']}")
                
                self.logger.info(f"Page {page_number} debug information:")
                self.logger.info(f"  URL: {page_info['url']}")
                self.logger.info(f"  Title: {page_info['title']}")
                self.logger.info(f"  Job cards: {page_info['jobCards']}")
                self.logger.info(f"  All job elements: {page_info['allJobElements']}")
                self.logger.info(f"  Learn more buttons: {page_info['learnMoreButtons']}")
                self.logger.info(f"  Page content preview: {page_info['bodyText']}")
                
                # Try to click first job card's Learn more button (maximum 3 retries)
                max_retries = 3
                success = False
                
                for attempt in range(max_retries):
                    print(f"🔄 Page {page_number} attempting to click Learn more (attempt {attempt + 1}/{max_retries})")
                    success = await self._click_first_learn_more(page)
                    
                    if success:
                        # Wait longer to ensure page fully loads
                        await asyncio.sleep(3)
                        
                        # Re-get job cards
                        job_cards = await page.query_selector_all('li.zE6MFb')
                        total_jobs = len(job_cards)
                        print(f"✅ Page {page_number} found {total_jobs} job positions after clicking Learn more")
                        
                        if total_jobs > 0:
                            self.logger.info(f"After clicking Learn more, found {total_jobs} job positions on page {page_number}")
                            break
                        else:
                            print(f"⚠️ Page {page_number} Learn more clicked successfully but no positions found, attempting retry...")
                            await asyncio.sleep(2)  # Wait 2 seconds before retry
                    else:
                        print(f"❌ Page {page_number} Learn more click failed, waiting before retry...")
                        await asyncio.sleep(2)
                
                if total_jobs == 0:
                    print(f"❌ Page {page_number} still found no positions after {max_retries} attempts, skipping this page")
                    self.logger.warning(f"Page {page_number} still found no positions after {max_retries} attempts")
                    return
            
            # Start recursive clicking and extraction
            await self._click_and_scrape_job_card(page, 0)
            
        except Exception as e:
            self.logger.error(f"Error occurred during recursive scraping on page {page_number}: {e}")
    
    async def _click_first_learn_more(self, page: Page) -> bool:
        """
        Click the first job card's Learn more button
        
        Args:
            page: Playwright page object
            
        Returns:
            bool: Whether click was successful
        """
        try:
            self.logger.info("Looking for first Learn more button...")
            
            # Execute using JavaScript and add debug information
            debug_info = await page.evaluate('''
                () => {
                    // Find Learn more button using multiple possible selectors
                    const selectors = [
                        'li.lLd3Je a[aria-label^="Learn more about"]',
                        'li.lLd3Je a',
                        'a[aria-label^="Learn more about"]',
                        'a[aria-label*="Learn more"]'
                    ];
                    
                    let foundElement = null;
                    let usedSelector = '';
                    
                    for (const selector of selectors) {
                        foundElement = document.querySelector(selector);
                        if (foundElement) {
                            usedSelector = selector;
                            break;
                        }
                    }
                    
                    if (foundElement) {
                        console.log('Found Learn more button, using selector:', usedSelector);
                        console.log('Button HTML:', foundElement.outerHTML);
                        foundElement.click();
                        return { success: true, selector: usedSelector, html: foundElement.outerHTML };
                    } else {
                        console.log('Learn more button not found');
                        // Check all possible Learn more related elements on page
                        const allElements = document.querySelectorAll('*[aria-label*="Learn"], *[aria-label*="learn"], li.lLd3Je, li.lLd3Je *');
                        console.log('Number of related elements on page:', allElements.length);
                        
                        const elementInfo = [];
                        for (let i = 0; i < Math.min(5, allElements.length); i++) {
                            elementInfo.push({
                                tagName: allElements[i].tagName,
                                className: allElements[i].className,
                                ariaLabel: allElements[i].getAttribute('aria-label'),
                                textContent: allElements[i].textContent?.substring(0, 50)
                            });
                        }
                        
                        return { success: false, elements: elementInfo };
                    }
                }
            ''')
            
            success = debug_info.get('success', False)
            
            if success:
                self.logger.info(f"✅ Successfully clicked Learn more button, using selector: {debug_info.get('selector', 'unknown')}")
            else:
                self.logger.warning("❌ Learn more button not found")
                self.logger.info(f"Related elements found on page: {debug_info.get('elements', [])}")
            
            if success:
                await page.wait_for_load_state('networkidle')
                self.logger.info("✅ Successfully clicked first Learn more button")
                return True
            else:
                self.logger.warning("❌ Learn more button not found")
                return False
                
        except Exception as e:
            self.logger.error(f"Error occurred while clicking Learn more button: {e}")
            return False
    
    async def _goto_next_page(self, page: Page) -> bool:
        """
        Navigate to next page
        
        Args:
            page: Playwright page object
            
        Returns:
            bool: Whether navigation to next page was successful
        """
        try:
            self.logger.info("🔄 Looking for next page button...")
            
            # Use JavaScript to find and click next page button
            next_page_clicked = await page.evaluate('''
                () => {
                    const nextPageLink = document.querySelector('div[jsname="ViaHrd"] a');
                    if (nextPageLink) {
                        console.log('Found next page button, preparing to click');
                        nextPageLink.click();
                        return true;
                    }
                    return false;
                }
            ''')
            
            if next_page_clicked:
                self.logger.info("✅ Successfully clicked next page button")
                return True
            else:
                self.logger.info("❌ Next page button not found")
                return False
                
        except Exception as e:
            self.logger.error(f"Error occurred while navigating to next page: {e}")
            return False
    
    async def _click_and_scrape_job_card(self, page: Page, index: int) -> None:
        """
        Recursively click and scrape job card data
        
        Args:
            page: Playwright page object
            index: Current job card index
        """
        try:
            # Re-get job cards (since DOM may have been updated)
            job_cards = await page.query_selector_all('li.zE6MFb')
            
            if index >= len(job_cards):
                self.logger.info('✅ All job cards have been processed')
                return
            
            # Use JavaScript to click job card at specified index
            click_success = await page.evaluate(f'''
                (index) => {{
                    const jobCards = document.querySelectorAll('li.zE6MFb');
                    if (index >= jobCards.length) return false;
                    
                    const jobCard = jobCards[index];
                    const link = jobCard.querySelector('a');
                    if (link) {{
                        console.log('Clicking job card ' + (index + 1));
                        link.click();
                        return true;
                    }}
                    return false;
                }}
            ''', index)
            
            if click_success:
                self.logger.info(f'🔘 Clicking job card {index + 1}')
                
                # Wait for page load
                await page.wait_for_timeout(500)  # Wait 500ms
                await page.wait_for_load_state('networkidle')
                
                # Extract data
                job_data = await self._extract_job_data(page)
                if job_data:
                    self.scraped_jobs.append(job_data)
                    self.total_jobs_scraped += 1
                    self.logger.info(f'✅ Scraped job {self.total_jobs_scraped}: {job_data.get("title", "Unknown")}')
                    print(f'🎯 Scraped job {self.total_jobs_scraped}: {job_data.get("title", "Unknown")}')
                    self._log_progress(index + 1, len(job_cards))
                
                # Recursively call next one
                await self._click_and_scrape_job_card(page, index + 1)
            else:
                self.logger.warning(f'⚠️ Job card {index + 1} has no link, skipping')
                await self._click_and_scrape_job_card(page, index + 1)
                
        except Exception as e:
            self.logger.error(f'❌ Error processing job card {index + 1}: {e}')
            # Continue processing next one
            await self._click_and_scrape_job_card(page, index + 1)
    
    async def _extract_job_data(self, page: Page) -> Optional[Dict[str, Any]]:
        """
        Extract job data from detail page
        
        Args:
            page: Playwright page object
            
        Returns:
            Job information dictionary or None
        """
        try:
            # Wait for job card container to load
            # await page.wait_for_selector('div.DkhPwc', timeout=5000)
            
            # Extract jobId (extract 5 or more consecutive digits from URL)
            current_url = page.url
            job_id_match = re.search(r'\d{5,}', current_url)
            job_id = job_id_match.group(0) if job_id_match else None
            
            # Extract title
            title_element = await page.query_selector('h2.p1N2lc')
            title = await title_element.inner_text() if title_element else ""
            
            # Extract location
            location_elements = await page.query_selector_all('.r0wTof')
            locations = []
            for loc_elem in location_elements:
                loc_text = await loc_elem.inner_text()
                locations.append(loc_text.strip())
            location = '; '.join(locations)
            
            # Extract job level
            level_element = await page.query_selector('.wVSTAb')
            level = await level_element.inner_text() if level_element else ""
            
            # Extract job description
            job_description = await self._extract_job_description(page)
            
            # Build job data
            job_data = {
                'jobId': job_id,
                'title': title.strip(),
                'company': 'Google',
                'location': location,
                'level': level.strip(),
                'jobDescription': job_description,
                'url': current_url,
                'scraped_date': datetime.now().isoformat(),
                'source': 'google_career',
                'status': 'active'
            }
            
            return job_data
            
        except Exception as e:
            self.logger.error(f'❌ Error extracting job data: {e}')
            return None
    
    async def _extract_job_description(self, page: Page) -> str:
        """
        Extract job description
        
        Args:
            page: Playwright page object
            
        Returns:
            Formatted job description
        """
        try:
            description_parts = []
            
            # Extract Minimum qualifications
            min_qual = await self._extract_section_content(page, 'Minimum qualifications')
            if min_qual:
                description_parts.extend(['Minimum qualifications:', min_qual, ''])
            
            # Extract Preferred qualifications
            pref_qual = await self._extract_section_content(page, 'Preferred qualifications')
            if pref_qual:
                description_parts.extend(['Preferred qualifications:', pref_qual, ''])
            
            # Extract About the job
            about_job = await self._extract_paragraph_content(page, 'About the job')
            if about_job:
                description_parts.extend(['About the job:', about_job, ''])
            
            # Extract Responsibilities
            responsibilities = await self._extract_section_content(page, 'Responsibilities')
            if responsibilities:
                description_parts.extend(['Responsibilities:', responsibilities])
            
            return '\n'.join(description_parts)
            
        except Exception as e:
            self.logger.error(f'❌ Error extracting job description: {e}')
            return ""
    
    async def _extract_section_content(self, page: Page, heading_text: str) -> str:
        """
        Extract ul list content
        
        Args:
            page: Playwright page object
            heading_text: Section heading text
            
        Returns:
            Section content
        """
        try:
            # Find h3 tag containing specified text
            h3_elements = await page.query_selector_all('h3')
            target_h3 = None
            
            for h3 in h3_elements:
                h3_text = await h3.inner_text()
                if heading_text.lower() in h3_text.lower():
                    target_h3 = h3
                    break
            
            if not target_h3:
                return ""
            
            # Get next sibling element
            next_element = await page.evaluate(
                '(h3) => h3.nextElementSibling',
                target_h3
            )
            
            if next_element:
                tag_name = await page.evaluate('(el) => el.tagName', next_element)
                if tag_name == 'UL':
                    # Extract all li content from ul
                    li_elements = await page.query_selector_all('li')
                    # Filter li elements belonging to current ul
                    li_texts = []
                    ul_lis = await page.evaluate(
                        '(ul) => Array.from(ul.querySelectorAll("li")).map(li => li.textContent.trim())',
                        next_element
                    )
                    return '\n'.join(ul_lis)
            
            return ""
            
        except Exception as e:
            self.logger.error(f'❌ Error extracting section content: {e}')
            return ""
    
    async def _extract_paragraph_content(self, page: Page, heading_text: str) -> str:
        """
        Extract paragraph content
        
        Args:
            page: Playwright page object
            heading_text: Section heading text
            
        Returns:
            Paragraph content
        """
        try:
            # Find h3 tag containing specified text
            h3_elements = await page.query_selector_all('h3')
            target_h3 = None
            
            for h3 in h3_elements:
                h3_text = await h3.inner_text()
                if heading_text.lower() in h3_text.lower():
                    target_h3 = h3
                    break
            
            if not target_h3:
                return ""
            
            # Get subsequent paragraph content
            paragraphs = await page.evaluate('''
                (h3) => {
                    const paragraphs = [];
                    let current = h3.nextElementSibling;
                    while (current && current.tagName !== 'H3') {
                        if (current.tagName === 'P') {
                            paragraphs.push(current.textContent.trim());
                        }
                        current = current.nextElementSibling;
                    }
                    return paragraphs;
                }
            ''', target_h3)
            
            return '\n\n'.join(paragraphs)
            
        except Exception as e:
            self.logger.error(f'❌ Error extracting paragraph content: {e}')
            return ""
    
    def _log_progress(self, current: int, total: int) -> None:
        """
        Log progress
        
        Args:
            current: Current progress
            total: Total count
        """
        percentage = (current / total) * 100
        self.logger.info(f'📈 Progress: {current}/{total} ({percentage:.1f}%)')
    
    async def _retry_operation(self, operation, max_retries: int = 3):
        """
        Retry mechanism
        
        Args:
            operation: Operation to execute
            max_retries: Maximum retry attempts
        """
        for attempt in range(max_retries):
            try:
                return await operation()
            except Exception as e:
                if attempt == max_retries - 1:
                    raise e
                self.logger.warning(f'⚠️ Operation failed, retrying... ({attempt + 1}/{max_retries})')
                await asyncio.sleep(1)
    
    async def save_scraped_data(self, filename: str = "google_jobs.json") -> None:
        """
        Save scraped data
        
        Args:
            filename: Filename to save to
        """
        try:
            with open(filename, 'w', encoding='utf-8') as f:
                json.dump(self.scraped_jobs, f, ensure_ascii=False, indent=2)
            self.logger.info(f'✅ Data saved to {filename}')
            self.logger.info(f'📊 Total scraped {len(self.scraped_jobs)} job positions')
        except Exception as e:
            self.logger.error(f'❌ Error saving data: {e}')


# Test and run functions
async def main():
    """Test function"""
    try:
        # Simulate configuration
        website_config = {
            "website_info": {
                "base_url": "https://www.google.com/about/careers/"
            },
            "scraping_config": {
                "max_pages": 5
            },
            "playwright_options": {
                "wait_for_selector": "li.zE6MFb",
                "wait_timeout": 10000
            }
        }
        
        global_config = {
            "global_settings": {
                "headless": False
            },
            "playwright_global": {
                "timeout": 30000,
                "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            }
        }
        
        # Create scraper instance
        scraper = GoogleCareerScraper(website_config, global_config)
        
        # Execute scraping
        jobs = await scraper.scrape_jobs()
        
        # Save data
        await scraper.save_scraped_data()
        
        print(f"Scraping completed! Obtained {len(jobs)} job positions")
        
    except Exception as e:
        print(f"Error occurred during execution: {e}")


if __name__ == "__main__":
    asyncio.run(main())