"""
文件操作工具模块
提供JSON文件的读写和数据处理功能
"""

import json
import os
from pathlib import Path
from typing import Dict, Any, Optional
import logging
from datetime import datetime


def load_json(file_path: str, default: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    加载JSON文件
    
    Args:
        file_path: JSON文件路径
        default: 如果文件不存在时返回的默认值
        
    Returns:
        解析后的JSON数据
    """
    try:
        if not os.path.exists(file_path):
            if default is not None:
                return default
            raise FileNotFoundError(f"文件不存在: {file_path}")
        
        with open(file_path, 'r', encoding='utf-8') as f:
            return json.load(f)
            
    except json.JSONDecodeError as e:
        logging.error(f"JSON解析错误 {file_path}: {e}")
        if default is not None:
            return default
        raise
    except Exception as e:
        logging.error(f"读取文件错误 {file_path}: {e}")
        if default is not None:
            return default
        raise


def save_json(file_path: str, data: Dict[str, Any], indent: int = 2) -> bool:
    """
    保存数据到JSON文件
    
    Args:
        file_path: JSON文件路径
        data: 要保存的数据
        indent: JSON格式化缩进
        
    Returns:
        是否保存成功
    """
    try:
        # 确保目录存在
        Path(file_path).parent.mkdir(parents=True, exist_ok=True)
        
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=indent)
        
        return True
        
    except Exception as e:
        logging.error(f"保存JSON文件错误 {file_path}: {e}")
        return False


def backup_file(file_path: str, backup_dir: str = "backups") -> bool:
    """
    备份文件
    
    Args:
        file_path: 要备份的文件路径
        backup_dir: 备份目录
        
    Returns:
        是否备份成功
    """
    try:
        if not os.path.exists(file_path):
            return False
        
        # 创建备份目录
        Path(backup_dir).mkdir(exist_ok=True)
        
        # 生成备份文件名
        file_name = Path(file_path).name
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_name = f"{timestamp}_{file_name}"
        backup_path = os.path.join(backup_dir, backup_name)
        
        # 复制文件
        import shutil
        shutil.copy2(file_path, backup_path)
        
        logging.info(f"文件备份成功: {file_path} -> {backup_path}")
        return True
        
    except Exception as e:
        logging.error(f"备份文件错误 {file_path}: {e}")
        return False


def ensure_directory(dir_path: str) -> bool:
    """
    确保目录存在
    
    Args:
        dir_path: 目录路径
        
    Returns:
        是否成功创建或目录已存在
    """
    try:
        Path(dir_path).mkdir(parents=True, exist_ok=True)
        return True
    except Exception as e:
        logging.error(f"创建目录错误 {dir_path}: {e}")
        return False


def get_file_size(file_path: str) -> int:
    """
    获取文件大小
    
    Args:
        file_path: 文件路径
        
    Returns:
        文件大小（字节）
    """
    try:
        return os.path.getsize(file_path)
    except Exception:
        return 0


def is_file_empty(file_path: str) -> bool:
    """
    检查文件是否为空
    
    Args:
        file_path: 文件路径
        
    Returns:
        文件是否为空
    """
    return get_file_size(file_path) == 0