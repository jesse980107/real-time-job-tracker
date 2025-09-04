#!/usr/bin/env python3
"""
Time utility module - Unified time management
Provides standardized time formatting and timezone processing functionality
"""

from datetime import datetime
import pytz


class TimeUtil:
    """Unified time utility class"""
    
    @staticmethod
    def get_current_timestamp_in_la() -> str:
        """
        Get current timestamp in Los Angeles timezone, formatted as ISO 8601
        
        Returns:
            str: ISO 8601 formatted Los Angeles timezone timestamp (e.g.: 2024-01-15T10:30:45.123-08:00)
        """
        now = datetime.now()
        la_timezone = pytz.timezone('America/Los_Angeles')
        la_time = la_timezone.localize(now) if now.tzinfo is None else now.astimezone(la_timezone)
        # Format as YYYY-MM-DDTHH:mm:ss.sss-XX:XX
        timestamp = la_time.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]  # Keep 3 digits of milliseconds
        offset = la_time.strftime("%z")
        formatted_offset = f"{offset[:3]}:{offset[3:]}"  # Add colon separator
        return f"{timestamp}{formatted_offset}"
    
    @staticmethod
    def get_current_timestamp_in_timezone(timezone_name: str) -> str:
        """
        Get current timestamp in specified timezone
        
        Args:
            timezone_name (str): Timezone name (e.g.: 'America/Los_Angeles', 'UTC', 'Asia/Shanghai')
            
        Returns:
            str: ISO 8601 formatted timestamp in specified timezone
        """
        now = datetime.now()
        target_timezone = pytz.timezone(timezone_name)
        target_time = target_timezone.localize(now) if now.tzinfo is None else now.astimezone(target_timezone)
        # Format as YYYY-MM-DDTHH:mm:ss.sss-XX:XX
        timestamp = target_time.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]  # Keep 3 digits of milliseconds
        offset = target_time.strftime("%z")
        formatted_offset = f"{offset[:3]}:{offset[3:]}" if offset else "Z"  # Add Z for UTC
        return f"{timestamp}{formatted_offset}"


# Convenience functions for direct import and use
def get_current_timestamp_in_la() -> str:
    """Convenience function to get current Los Angeles timestamp"""
    return TimeUtil.get_current_timestamp_in_la()

def get_current_timestamp_in_timezone(timezone_name: str) -> str:
    """Convenience function to get current timestamp in specified timezone"""
    return TimeUtil.get_current_timestamp_in_timezone(timezone_name)
