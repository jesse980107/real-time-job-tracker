# Automated Job Scraping System

## Project Overview
An automated system that detects and scrapes new job postings from recruitment websites like Google Career, with local storage management.

## Tech Stack
- **Web Scraping**: Playwright (Python)
- **Main Program**: Python 3.8+
- **Data Storage**: Local JSON files
- **Execution Method**: Sequential scraping of each website

## Project Architecture

### Directory Structure
```
Real-Time Job Tracker/
├── README.md              # Project documentation
├── main.py                # Main management file - program entry point
├── config.json            # Global configuration - high-level management
├── requirements.txt       # Python dependencies
├── scrapers/              # Website scraper directory
│   ├── __init__.py
│   ├── google_career/
│   │   ├── scraper.py
│   │   └── config.json    # Google Career specific configuration
│   ├── linkedin/
│   │   ├── scraper.py
│   │   └── config.json    # LinkedIn specific configuration
│   └── indeed/
│       ├── scraper.py
│       └── config.json    # Indeed specific configuration
├── utils/                 # Utility modules
│   ├── __init__.py
│   ├── file_utils.py      # JSON file operations
│   ├── logger.py          # Logging utilities
│   └── deduplication.py   # Deduplication utilities
├── data/                  # Data storage directory
│   ├── jobs.json          # Stores all job data
│   └── last_run.json      # Records last run status
└── logs/                  # Log directory
    └── scraper.log
```

### Layered Configuration Design

#### Global Configuration (config.json)
```json
{
  "enabled_websites": ["google_career", "linkedin", "indeed"],
  "global_settings": {
    "output_file": "data/jobs.json",
    "log_level": "INFO",
    "max_retries": 3,
    "global_delay": 5,
    "headless": true
  },
  "playwright_global": {
    "timeout": 30000,
    "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
  }
}
```

#### Website-Specific Configuration (scrapers/google_career/config.json)
```json
{
  "website_info": {
    "name": "Google Career",
    "base_url": "https://careers.google.com/jobs/results/",
    "enabled": true
  },
  "scraping_config": {
    "interval": 3600,
    "max_pages": 10,
    "selectors": {
      "job_card": ".job-card",
      "title": ".job-title",
      "location": ".job-location",
      "link": ".job-link"
    }
  },
  "search_params": {
    "keywords": ["software engineer", "data scientist"],
    "locations": ["New York", "San Francisco"],
    "experience_levels": ["entry", "mid", "senior"]
  },
  "playwright_options": {
    "wait_for_selector": ".job-card",
    "scroll_behavior": true,
    "wait_timeout": 10000
  }
}
```

## Workflow

1. Run `python main.py` to start the system
2. Read global `config.json` to get list of enabled websites
3. Sequential loop through each enabled website
4. Load corresponding website-specific configuration file for each website
5. Call corresponding scraper.py and pass in configuration
6. Each scraper uses Playwright and specific configuration to scrape jobs
7. Compare with existing `jobs.json` data for deduplication
8. Update JSON files and run status records
9. Log records and handle errors

## Core Features

- **Layered Configuration**: Global configuration manages high-level settings, website configuration manages specific scraping parameters
- **Incremental Updates**: Only scrape newly published jobs, avoid duplication
- **Deduplication Mechanism**: Use job ID or URL as unique identifier
- **Error Handling**: Single website failure doesn't affect other website scraping
- **Logging**: Record scraping process and results
- **Flexible Configuration**: Can individually adjust scraping strategy for each website

## Data Structure

### Job Data Format (jobs.json)
```json
{
  "jobs": [
    {
      "id": "unique_job_id",
      "title": "Software Engineer",
      "company": "Google",
      "location": "New York, NY",
      "url": "https://careers.google.com/jobs/results/123456",
      "description": "Job description...",
      "posted_date": "2024-01-15",
      "scraped_date": "2024-01-15T10:30:00Z",
      "source": "google_career",
      "status": "active"
    }
  ],
  "metadata": {
    "total_jobs": 1,
    "last_updated": "2024-01-15T10:30:00Z"
  }
}
```

## Installation and Running

### 1. Install Dependencies
```bash
pip install -r requirements.txt
```

### 2. Install Playwright Browsers
```bash
playwright install
```

### 3. Run Program
```bash
python main.py
```

## Development Plan

### Phase 1: Basic Architecture
- [x] Create project structure
- [ ] Implement main program framework
- [ ] Create configuration management system
- [ ] Implement logging and utility modules

### Phase 2: Core Features
- [ ] Implement Google Career scraper
- [ ] Implement data deduplication and storage
- [ ] Add error handling and retry mechanisms

### Phase 3: Extended Features
- [ ] Add LinkedIn scraper
- [ ] Add Indeed scraper
- [ ] Optimize performance and stability

## Important Notes

1. **Anti-scraping Mechanisms**: Set reasonable request intervals, use random User-Agent
2. **Error Handling**: Handle network exceptions and page changes
3. **Data Consistency**: Ensure unified data format and integrity
4. **Performance Optimization**: Avoid duplicate scraping and memory leaks

## Contributing Guidelines

1. Fork the project
2. Create a feature branch
3. Commit changes
4. Push to branch
5. Create Pull Request

## License

MIT License