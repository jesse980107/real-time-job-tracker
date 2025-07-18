#!/usr/bin/env python3
"""
时间工具模块 - 统一时间管理
提供标准化的时间格式化和时区处理功能
"""

from datetime import datetime
import pytz


class TimeUtil:
    """统一的时间工具类"""
    
    @staticmethod
    def get_current_timestamp_in_la() -> str:
        """
        获取洛杉矶时区的当前时间戳，格式为ISO 8601
        
        Returns:
            str: ISO 8601格式的洛杉矶时区时间戳 (例如: 2024-01-15T10:30:45.123-08:00)
        """
        now = datetime.now()
        la_timezone = pytz.timezone('America/Los_Angeles')
        la_time = la_timezone.localize(now) if now.tzinfo is None else now.astimezone(la_timezone)
        # 格式化为 YYYY-MM-DDTHH:mm:ss.sss-XX:XX
        timestamp = la_time.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]  # 保留3位毫秒
        offset = la_time.strftime("%z")
        formatted_offset = f"{offset[:3]}:{offset[3:]}"  # 添加冒号分隔符
        return f"{timestamp}{formatted_offset}"
    
    @staticmethod
    def get_current_timestamp_in_timezone(timezone_name: str) -> str:
        """
        获取指定时区的当前时间戳
        
        Args:
            timezone_name (str): 时区名称 (例如: 'America/Los_Angeles', 'UTC', 'Asia/Shanghai')
            
        Returns:
            str: ISO 8601格式的指定时区时间戳
        """
        now = datetime.now()
        target_timezone = pytz.timezone(timezone_name)
        target_time = target_timezone.localize(now) if now.tzinfo is None else now.astimezone(target_timezone)
        # 格式化为 YYYY-MM-DDTHH:mm:ss.sss-XX:XX
        timestamp = target_time.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]  # 保留3位毫秒
        offset = target_time.strftime("%z")
        formatted_offset = f"{offset[:3]}:{offset[3:]}" if offset else "Z"  # UTC时添加Z
        return f"{timestamp}{formatted_offset}"


# 便捷函数，直接导入使用
def get_current_timestamp_in_la() -> str:
    """获取洛杉矶当前时间戳的便捷函数"""
    return TimeUtil.get_current_timestamp_in_la()

def get_current_timestamp_in_timezone(timezone_name: str) -> str:
    """获取指定时区当前时间戳的便捷函数"""
    return TimeUtil.get_current_timestamp_in_timezone(timezone_name)
