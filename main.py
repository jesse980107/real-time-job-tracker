#!/usr/bin/env python3
"""
Main program file - Automated job scraping system
Manages scraping tasks for all websites, coordinates configuration and data processing
"""

import asyncio
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any

# Add project root directory to Python path
sys.path.append(str(Path(__file__).parent))

from utils.logger import setup_logger
from utils.file_utils import load_json, save_json
from utils.deduplication import deduplicate_jobs


class JobTracker:
    """Main job tracker class"""
    
    def __init__(self, config_path: str = "config.json"):
        """Initialize JobTracker"""
        self.config_path = config_path
        self.config = self._load_global_config()
        self.logger = setup_logger(self.config["global_settings"]["log_level"])
        self.scraped_jobs = []
        
    def _load_global_config(self) -> Dict[str, Any]:
        """Load global configuration file"""
        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except FileNotFoundError:
            raise FileNotFoundError(f"Global configuration file {self.config_path} does not exist")
        except json.JSONDecodeError as e:
            raise ValueError(f"Configuration file format error: {e}")
    
    def _load_website_config(self, website_name: str) -> Dict[str, Any]:
        """Load configuration file for specific website"""
        config_path = f"scrapers/{website_name}/config.json"
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except FileNotFoundError:
            self.logger.error(f"Website configuration file {config_path} does not exist")
            return {}
        except json.JSONDecodeError as e:
            self.logger.error(f"Website configuration file format error: {e}")
            return {}
    
    async def _run_website_scraper(self, website_name: str) -> List[Dict[str, Any]]:
        """Run scraper for specific website"""
        self.logger.info(f"Starting to scrape website: {website_name}")
        
        # Load website-specific configuration
        website_config = self._load_website_config(website_name)
        if not website_config:
            return []
        
        # Check if website is enabled
        if not website_config.get("website_info", {}).get("enabled", True):
            self.logger.info(f"Website {website_name} is disabled, skipping")
            return []
        
        try:
            # Dynamically import corresponding scraper module
            module_path = f"scrapers.{website_name}.scraper"
            module = __import__(module_path, fromlist=[''])
            
            # Create scraper instance and run
            scraper_class = getattr(module, f"{website_name.title().replace('_', '')}Scraper")
            scraper = scraper_class(website_config, self.config)
            
            jobs = await scraper.scrape_jobs()
            self.logger.info(f"Scraped {len(jobs)} jobs from {website_name}")
            
            return jobs
            
        except ImportError as e:
            self.logger.error(f"Cannot import scraper module for {website_name}: {e}")
            return []
        except Exception as e:
            self.logger.error(f"Error occurred while scraping {website_name}: {e}")
            return []
    
    async def run_all_scrapers(self) -> None:
        """Run all enabled website scrapers sequentially"""
        enabled_websites = self.config.get("enabled_websites", [])
        
        if not enabled_websites:
            self.logger.warning("No websites enabled, program terminating")
            return
        
        self.logger.info(f"Starting to scrape {len(enabled_websites)} websites")
        
        all_jobs = []
        
        for website in enabled_websites:
            try:
                jobs = await self._run_website_scraper(website)
                all_jobs.extend(jobs)
                
                # Global delay
                global_delay = self.config["global_settings"].get("global_delay", 5)
                if global_delay > 0:
                    self.logger.info(f"Waiting {global_delay} seconds before continuing...")
                    await asyncio.sleep(global_delay)
                    
            except Exception as e:
                self.logger.error(f"Error occurred while processing website {website}: {e}")
                continue
        
        # Process all scraped jobs
        if all_jobs:
            await self._process_scraped_jobs(all_jobs)
        else:
            self.logger.info("No jobs scraped")
    
    async def _process_scraped_jobs(self, jobs: List[Dict[str, Any]]) -> None:
        """Process scraped job data"""
        self.logger.info(f"Starting to process {len(jobs)} jobs")
        
        # Load existing data
        output_file = self.config["global_settings"]["output_file"]
        existing_data = load_json(output_file, default={"jobs": [], "metadata": {}})
        existing_jobs = existing_data.get("jobs", [])
        
        # Deduplication processing
        deduplicated_jobs = deduplicate_jobs(existing_jobs, jobs)
        new_jobs_count = len(deduplicated_jobs) - len(existing_jobs)
        
        # Update data
        updated_data = {
            "jobs": deduplicated_jobs,
            "metadata": {
                "total_jobs": len(deduplicated_jobs),
                "new_jobs_this_run": new_jobs_count,
                "last_updated": datetime.now().isoformat()
            }
        }
        
        # Save data
        save_json(output_file, updated_data)
        
        # Update run status
        self._update_run_status(new_jobs_count)
        
        self.logger.info(f"Processing completed, added {new_jobs_count} new jobs")
    
    def _update_run_status(self, new_jobs_count: int) -> None:
        """Update run status"""
        status_file = "data/last_run.json"
        status_data = {
            "last_run_time": datetime.now().isoformat(),
            "new_jobs_found": new_jobs_count,
            "enabled_websites": self.config.get("enabled_websites", [])
        }
        save_json(status_file, status_data)


async def main():
    """Main function"""
    print("=== Automated Job Scraping System ===")
    print("Starting...")
    
    try:
        # Create necessary directories
        Path("data").mkdir(exist_ok=True)
        Path("logs").mkdir(exist_ok=True)
        
        # Initialize and run JobTracker
        tracker = JobTracker()
        await tracker.run_all_scrapers()
        
        print("Scraping completed!")
        
    except KeyboardInterrupt:
        print("\nProgram interrupted by user")
    except Exception as e:
        print(f"Program execution error: {e}")
        logging.error(f"Program execution error: {e}")
    finally:
        print("Program ended")


if __name__ == "__main__":
    asyncio.run(main())