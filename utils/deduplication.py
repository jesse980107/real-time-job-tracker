"""
去重工具模块
提供职位数据去重和比较功能
"""

import hashlib
from typing import List, Dict, Any, Set
from datetime import datetime
import logging


def generate_job_hash(job: Dict[str, Any]) -> str:
    """
    生成职位的唯一哈希值
    
    Args:
        job: 职位数据字典
        
    Returns:
        职位的哈希值
    """
    # 使用关键字段生成哈希
    key_fields = [
        job.get("title", ""),
        job.get("company", ""),
        job.get("location", ""),
        job.get("url", "")
    ]
    
    # 创建用于哈希的字符串
    hash_string = "|".join(str(field).strip().lower() for field in key_fields)
    
    # 生成MD5哈希
    return hashlib.md5(hash_string.encode('utf-8')).hexdigest()


def deduplicate_jobs(existing_jobs: List[Dict[str, Any]], 
                    new_jobs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    去重处理职位数据
    
    Args:
        existing_jobs: 现有职位列表
        new_jobs: 新抓取的职位列表
        
    Returns:
        去重后的职位列表
    """
    try:
        # 创建现有职位的哈希集合
        existing_hashes = set()
        for job in existing_jobs:
            if not job.get("id"):
                job["id"] = generate_job_hash(job)
            existing_hashes.add(job["id"])
        
        # 处理新职位
        deduplicated_jobs = existing_jobs.copy()
        new_job_count = 0
        
        for job in new_jobs:
            # 生成新职位的哈希ID
            job_hash = generate_job_hash(job)
            job["id"] = job_hash
            
            # 检查是否重复
            if job_hash not in existing_hashes:
                # 添加时间戳
                job["scraped_date"] = datetime.now().isoformat()
                job["status"] = "active"
                
                deduplicated_jobs.append(job)
                existing_hashes.add(job_hash)
                new_job_count += 1
            else:
                # 更新现有职位的最后抓取时间
                _update_existing_job(deduplicated_jobs, job_hash, job)
        
        logging.info(f"去重完成: 现有职位 {len(existing_jobs)}, 新增职位 {new_job_count}")
        return deduplicated_jobs
        
    except Exception as e:
        logging.error(f"去重处理错误: {e}")
        return existing_jobs


def _update_existing_job(jobs: List[Dict[str, Any]], job_id: str, new_job: Dict[str, Any]) -> None:
    """
    更新现有职位的信息
    
    Args:
        jobs: 职位列表
        job_id: 职位ID
        new_job: 新的职位信息
    """
    for job in jobs:
        if job.get("id") == job_id:
            # 更新最后抓取时间
            job["last_seen"] = datetime.now().isoformat()
            
            # 更新可能变化的字段
            updatable_fields = ["description", "posted_date", "salary", "employment_type"]
            for field in updatable_fields:
                if field in new_job and new_job[field]:
                    job[field] = new_job[field]
            
            break


def find_duplicate_jobs(jobs: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    """
    查找重复的职位
    
    Args:
        jobs: 职位列表
        
    Returns:
        重复职位的分组列表
    """
    hash_groups = {}
    
    for job in jobs:
        job_hash = job.get("id") or generate_job_hash(job)
        
        if job_hash not in hash_groups:
            hash_groups[job_hash] = []
        hash_groups[job_hash].append(job)
    
    # 返回包含重复项的组
    return [group for group in hash_groups.values() if len(group) > 1]


def remove_inactive_jobs(jobs: List[Dict[str, Any]], days_threshold: int = 30) -> List[Dict[str, Any]]:
    """
    移除长时间未见的职位
    
    Args:
        jobs: 职位列表
        days_threshold: 天数阈值
        
    Returns:
        过滤后的职位列表
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
                    # 如果日期格式有问题，保留职位
                    active_jobs.append(job)
            else:
                # 如果没有时间信息，保留职位
                active_jobs.append(job)
        
        logging.info(f"移除过期职位: {removed_count} 个")
        return active_jobs
        
    except Exception as e:
        logging.error(f"移除过期职位错误: {e}")
        return jobs


def get_job_statistics(jobs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    获取职位统计信息
    
    Args:
        jobs: 职位列表
        
    Returns:
        统计信息字典
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
            # 按公司统计
            company = job.get("company", "未知")
            stats["by_company"][company] = stats["by_company"].get(company, 0) + 1
            
            # 按地点统计
            location = job.get("location", "未知")
            stats["by_location"][location] = stats["by_location"].get(location, 0) + 1
            
            # 按来源统计
            source = job.get("source", "未知")
            stats["by_source"][source] = stats["by_source"].get(source, 0) + 1
            
            # 按状态统计
            status = job.get("status", "未知")
            stats["by_status"][status] = stats["by_status"].get(status, 0) + 1
        
        return stats
        
    except Exception as e:
        logging.error(f"统计信息生成错误: {e}")
        return {"total": len(jobs)}