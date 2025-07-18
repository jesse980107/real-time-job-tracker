"""
日志工具模块
提供统一的日志配置和管理功能
"""

import logging
import os
from datetime import datetime
from pathlib import Path


def setup_logger(log_level: str = "INFO", log_file: str = "logs/scraper.log") -> logging.Logger:
    """
    设置日志记录器
    
    Args:
        log_level: 日志级别 (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        log_file: 日志文件路径
        
    Returns:
        配置好的logger实例
    """
    # 创建日志目录
    log_dir = Path(log_file).parent
    log_dir.mkdir(exist_ok=True)
    
    # 创建logger
    logger = logging.getLogger("job_tracker")
    logger.setLevel(getattr(logging, log_level.upper()))
    
    # 避免重复添加handler
    if logger.handlers:
        return logger
    
    # 创建格式化器
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    # 文件处理器
    file_handler = logging.FileHandler(log_file, encoding='utf-8')
    file_handler.setLevel(getattr(logging, log_level.upper()))
    file_handler.setFormatter(formatter)
    
    # 控制台处理器
    console_handler = logging.StreamHandler()
    console_handler.setLevel(getattr(logging, log_level.upper()))
    console_handler.setFormatter(formatter)
    
    # 添加处理器
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    
    return logger


def log_scraper_result(logger: logging.Logger, website: str, job_count: int, 
                      new_jobs: int, errors: int = 0) -> None:
    """
    记录抓取结果
    
    Args:
        logger: 日志记录器
        website: 网站名称
        job_count: 抓取的职位总数
        new_jobs: 新增职位数
        errors: 错误数量
    """
    logger.info(f"抓取结果 - 网站: {website}, 总职位: {job_count}, 新增: {new_jobs}, 错误: {errors}")


def log_error_with_context(logger: logging.Logger, error: Exception, 
                          context: str = "", website: str = "") -> None:
    """
    记录带上下文的错误信息
    
    Args:
        logger: 日志记录器
        error: 异常对象
        context: 错误上下文
        website: 相关网站
    """
    error_msg = f"错误: {str(error)}"
    if context:
        error_msg += f" - 上下文: {context}"
    if website:
        error_msg += f" - 网站: {website}"
    
    logger.error(error_msg, exc_info=True)