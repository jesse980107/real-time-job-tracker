"""
Logging utility module
Provides unified logging configuration and management functionality
"""

import logging
import os
from datetime import datetime
from pathlib import Path


def setup_logger(log_level: str = "INFO", log_file: str = "logs/scraper.log") -> logging.Logger:
    """
    Setup logger
    
    Args:
        log_level: Log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        log_file: Log file path
        
    Returns:
        Configured logger instance
    """
    # Create log directory
    log_dir = Path(log_file).parent
    log_dir.mkdir(exist_ok=True)
    
    # Create logger
    logger = logging.getLogger("job_tracker")
    logger.setLevel(getattr(logging, log_level.upper()))
    
    # Avoid adding duplicate handlers
    if logger.handlers:
        return logger
    
    # Create formatter
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    # File handler
    file_handler = logging.FileHandler(log_file, encoding='utf-8')
    file_handler.setLevel(getattr(logging, log_level.upper()))
    file_handler.setFormatter(formatter)
    
    # Console handler
    console_handler = logging.StreamHandler()
    console_handler.setLevel(getattr(logging, log_level.upper()))
    console_handler.setFormatter(formatter)
    
    # Add handlers
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    
    return logger


def log_scraper_result(logger: logging.Logger, website: str, job_count: int, 
                      new_jobs: int, errors: int = 0) -> None:
    """
    Log scraping results
    
    Args:
        logger: Logger instance
        website: Website name
        job_count: Total number of jobs scraped
        new_jobs: Number of new jobs
        errors: Number of errors
    """
    logger.info(f"Scraping results - Website: {website}, Total jobs: {job_count}, New jobs: {new_jobs}, Errors: {errors}")


def log_error_with_context(logger: logging.Logger, error: Exception, 
                          context: str = "", website: str = "") -> None:
    """
    Log error with context information
    
    Args:
        logger: Logger instance
        error: Exception object
        context: Error context
        website: Related website
    """
    error_msg = f"Error: {str(error)}"
    if context:
        error_msg += f" - Context: {context}"
    if website:
        error_msg += f" - Website: {website}"
    
    logger.error(error_msg, exc_info=True)