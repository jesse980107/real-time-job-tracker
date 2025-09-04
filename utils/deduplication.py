"""
Deduplication utility module
Provides job data deduplication and comparison functionality
"""

import hashlib
from typing import List, Dict, Any, Set
from datetime import datetime
import logging


def generate_job_hash(job: Dict[str, Any]) -> str:
    """
    Generate unique hash for job
    
    Args:
        job: Job data dictionary
        
    Returns:
        Job hash value
    """
    # Use key fields to generate hash
    key_fields = [
        job.get("title", ""),
        job.get("company", ""),
        job.get("location", ""),
        job.get("url", "")
    ]
    
    # Create string for hashing
    hash_string = "|".join(str(field).strip().lower() for field in key_fields)
    
    # Generate MD5 hash
    return hashlib.md5(hash_string.encode('utf-8')).hexdigest()


def deduplicate_jobs(existing_jobs: List[Dict[str, Any]], 
                    new_jobs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Deduplicate job data
    
    Args:
        existing_jobs: List of existing jobs
        new_jobs: List of newly scraped jobs
        
    Returns:
        Deduplicated job list
    """
    try:
        # Create hash set for existing jobs
        existing_hashes = set()
        for job in existing_jobs:
            if not job.get("id"):
                job["id"] = generate_job_hash(job)
            existing_hashes.add(job["id"])
        
        # Process new jobs
        deduplicated_jobs = existing_jobs.copy()
        new_job_count = 0
        
        for job in new_jobs:
            # Generate hash ID for new job
            job_hash = generate_job_hash(job)
            job["id"] = job_hash
            
            # Check for duplicates
            if job_hash not in existing_hashes:
                # Add timestamp
                job["scraped_date"] = datetime.now().isoformat()
                job["status"] = "active"
                
                deduplicated_jobs.append(job)
                existing_hashes.add(job_hash)
                new_job_count += 1
            else:
                # Update last scrape time for existing job
                _update_existing_job(deduplicated_jobs, job_hash, job)
        
        logging.info(f"Deduplication completed: existing jobs {len(existing_jobs)}, new jobs {new_job_count}")
        return deduplicated_jobs
        
    except Exception as e:
        logging.error(f"Deduplication processing error: {e}")
        return existing_jobs


def _update_existing_job(jobs: List[Dict[str, Any]], job_id: str, new_job: Dict[str, Any]) -> None:
    """
    Update information of existing job
    
    Args:
        jobs: Job list
        job_id: Job ID
        new_job: New job information
    """
    for job in jobs:
        if job.get("id") == job_id:
            # Update last seen time
            job["last_seen"] = datetime.now().isoformat()
            
            # Update fields that may change
            updatable_fields = ["description", "posted_date", "salary", "employment_type"]
            for field in updatable_fields:
                if field in new_job and new_job[field]:
                    job[field] = new_job[field]
            
            break


def find_duplicate_jobs(jobs: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    """
    Find duplicate jobs
    
    Args:
        jobs: Job list
        
    Returns:
        List of duplicate job groups
    """
    hash_groups = {}
    
    for job in jobs:
        job_hash = job.get("id") or generate_job_hash(job)
        
        if job_hash not in hash_groups:
            hash_groups[job_hash] = []
        hash_groups[job_hash].append(job)
    
    # Return groups containing duplicates
    return [group for group in hash_groups.values() if len(group) > 1]


def remove_inactive_jobs(jobs: List[Dict[str, Any]], days_threshold: int = 30) -> List[Dict[str, Any]]:
    """
    Remove jobs not seen for a long time
    
    Args:
        jobs: Job list
        days_threshold: Days threshold
        
    Returns:
        Filtered job list
    """
    try:
        from datetime import datetime, timedelta
        
        current_time = datetime.now()
        threshold_date = current_time - timedelta(days=days_threshold)
        
        active_jobs = []
        removed_count = 0
        
        for job in jobs:
            last_seen = job.get("last_seen") or job.get("scraped_date")
            
            if last_seen:
                try:
                    last_seen_date = datetime.fromisoformat(last_seen.replace('Z', '+00:00'))
                    if last_seen_date >= threshold_date:
                        active_jobs.append(job)
                    else:
                        removed_count += 1
                except ValueError:
                    # If date format has problems, keep the job
                    active_jobs.append(job)
            else:
                # If no time information, keep the job
                active_jobs.append(job)
        
        logging.info(f"Removed expired jobs: {removed_count}")
        return active_jobs
        
    except Exception as e:
        logging.error(f"Error removing expired jobs: {e}")
        return jobs


def get_job_statistics(jobs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Get job statistics
    
    Args:
        jobs: Job list
        
    Returns:
        Statistics dictionary
    """
    if not jobs:
        return {"total": 0}
    
    try:
        stats = {
            "total": len(jobs),
            "by_company": {},
            "by_location": {},
            "by_source": {},
            "by_status": {}
        }
        
        for job in jobs:
            # Statistics by company
            company = job.get("company", "Unknown")
            stats["by_company"][company] = stats["by_company"].get(company, 0) + 1
            
            # Statistics by location
            location = job.get("location", "Unknown")
            stats["by_location"][location] = stats["by_location"].get(location, 0) + 1
            
            # Statistics by source
            source = job.get("source", "Unknown")
            stats["by_source"][source] = stats["by_source"].get(source, 0) + 1
            
            # Statistics by status
            status = job.get("status", "Unknown")
            stats["by_status"][status] = stats["by_status"].get(status, 0) + 1
        
        return stats
        
    except Exception as e:
        logging.error(f"Error generating statistics: {e}")
        return {"total": len(jobs)}