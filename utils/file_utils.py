"""
File operation utility module
Provides JSON file reading/writing and data processing functionality
"""

import json
import os
from pathlib import Path
from typing import Dict, Any, Optional
import logging
from datetime import datetime


def load_json(file_path: str, default: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Load JSON file
    
    Args:
        file_path: JSON file path
        default: Default value to return if file does not exist
        
    Returns:
        Parsed JSON data
    """
    try:
        if not os.path.exists(file_path):
            if default is not None:
                return default
            raise FileNotFoundError(f"File does not exist: {file_path}")
        
        with open(file_path, 'r', encoding='utf-8') as f:
            return json.load(f)
            
    except json.JSONDecodeError as e:
        logging.error(f"JSON parsing error {file_path}: {e}")
        if default is not None:
            return default
        raise
    except Exception as e:
        logging.error(f"File reading error {file_path}: {e}")
        if default is not None:
            return default
        raise


def save_json(file_path: str, data: Dict[str, Any], indent: int = 2) -> bool:
    """
    Save data to JSON file
    
    Args:
        file_path: JSON file path
        data: Data to save
        indent: JSON formatting indentation
        
    Returns:
        Whether save was successful
    """
    try:
        # Ensure directory exists
        Path(file_path).parent.mkdir(parents=True, exist_ok=True)
        
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=indent)
        
        return True
        
    except Exception as e:
        logging.error(f"Error saving JSON file {file_path}: {e}")
        return False


def backup_file(file_path: str, backup_dir: str = "backups") -> bool:
    """
    Backup file
    
    Args:
        file_path: File path to backup
        backup_dir: Backup directory
        
    Returns:
        Whether backup was successful
    """
    try:
        if not os.path.exists(file_path):
            return False
        
        # Create backup directory
        Path(backup_dir).mkdir(exist_ok=True)
        
        # Generate backup filename
        file_name = Path(file_path).name
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_name = f"{timestamp}_{file_name}"
        backup_path = os.path.join(backup_dir, backup_name)
        
        # Copy file
        import shutil
        shutil.copy2(file_path, backup_path)
        
        logging.info(f"File backup successful: {file_path} -> {backup_path}")
        return True
        
    except Exception as e:
        logging.error(f"File backup error {file_path}: {e}")
        return False


def ensure_directory(dir_path: str) -> bool:
    """
    Ensure directory exists
    
    Args:
        dir_path: Directory path
        
    Returns:
        Whether directory was successfully created or already exists
    """
    try:
        Path(dir_path).mkdir(parents=True, exist_ok=True)
        return True
    except Exception as e:
        logging.error(f"Directory creation error {dir_path}: {e}")
        return False


def get_file_size(file_path: str) -> int:
    """
    Get file size
    
    Args:
        file_path: File path
        
    Returns:
        File size in bytes
    """
    try:
        return os.path.getsize(file_path)
    except Exception:
        return 0


def is_file_empty(file_path: str) -> bool:
    """
    Check if file is empty
    
    Args:
        file_path: File path
        
    Returns:
        Whether file is empty
    """
    return get_file_size(file_path) == 0