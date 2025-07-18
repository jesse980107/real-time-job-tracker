#!/usr/bin/env python3
"""
主程序文件 - 自动职位抓取系统
管理所有网站的抓取任务，协调配置和数据处理
"""

import asyncio
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any

# 添加项目根目录到Python路径
sys.path.append(str(Path(__file__).parent))

from utils.logger import setup_logger
from utils.file_utils import load_json, save_json
from utils.deduplication import deduplicate_jobs


class JobTracker:
    """主要的职位跟踪器类"""
    
    def __init__(self, config_path: str = "config.json"):
        """初始化JobTracker"""
        self.config_path = config_path
        self.config = self._load_global_config()
        self.logger = setup_logger(self.config["global_settings"]["log_level"])
        self.scraped_jobs = []
        
    def _load_global_config(self) -> Dict[str, Any]:
        """加载全局配置文件"""
        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except FileNotFoundError:
            raise FileNotFoundError(f"全局配置文件 {self.config_path} 不存在")
        except json.JSONDecodeError as e:
            raise ValueError(f"配置文件格式错误: {e}")
    
    def _load_website_config(self, website_name: str) -> Dict[str, Any]:
        """加载特定网站的配置文件"""
        config_path = f"scrapers/{website_name}/config.json"
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except FileNotFoundError:
            self.logger.error(f"网站配置文件 {config_path} 不存在")
            return {}
        except json.JSONDecodeError as e:
            self.logger.error(f"网站配置文件格式错误: {e}")
            return {}
    
    async def _run_website_scraper(self, website_name: str) -> List[Dict[str, Any]]:
        """运行特定网站的抓取器"""
        self.logger.info(f"开始抓取网站: {website_name}")
        
        # 加载网站专用配置
        website_config = self._load_website_config(website_name)
        if not website_config:
            return []
        
        # 检查网站是否启用
        if not website_config.get("website_info", {}).get("enabled", True):
            self.logger.info(f"网站 {website_name} 已被禁用，跳过")
            return []
        
        try:
            # 动态导入对应的scraper模块
            module_path = f"scrapers.{website_name}.scraper"
            module = __import__(module_path, fromlist=[''])
            
            # 创建scraper实例并运行
            scraper_class = getattr(module, f"{website_name.title().replace('_', '')}Scraper")
            scraper = scraper_class(website_config, self.config)
            
            jobs = await scraper.scrape_jobs()
            self.logger.info(f"从 {website_name} 抓取到 {len(jobs)} 个职位")
            
            return jobs
            
        except ImportError as e:
            self.logger.error(f"无法导入 {website_name} 的scraper模块: {e}")
            return []
        except Exception as e:
            self.logger.error(f"抓取 {website_name} 时发生错误: {e}")
            return []
    
    async def run_all_scrapers(self) -> None:
        """串行运行所有启用的网站抓取器"""
        enabled_websites = self.config.get("enabled_websites", [])
        
        if not enabled_websites:
            self.logger.warning("没有启用的网站，程序结束")
            return
        
        self.logger.info(f"开始抓取 {len(enabled_websites)} 个网站")
        
        all_jobs = []
        
        for website in enabled_websites:
            try:
                jobs = await self._run_website_scraper(website)
                all_jobs.extend(jobs)
                
                # 全局延迟
                global_delay = self.config["global_settings"].get("global_delay", 5)
                if global_delay > 0:
                    self.logger.info(f"等待 {global_delay} 秒后继续...")
                    await asyncio.sleep(global_delay)
                    
            except Exception as e:
                self.logger.error(f"处理网站 {website} 时发生错误: {e}")
                continue
        
        # 处理抓取到的所有职位
        if all_jobs:
            await self._process_scraped_jobs(all_jobs)
        else:
            self.logger.info("没有抓取到任何职位")
    
    async def _process_scraped_jobs(self, jobs: List[Dict[str, Any]]) -> None:
        """处理抓取到的职位数据"""
        self.logger.info(f"开始处理 {len(jobs)} 个职位")
        
        # 加载现有数据
        output_file = self.config["global_settings"]["output_file"]
        existing_data = load_json(output_file, default={"jobs": [], "metadata": {}})
        existing_jobs = existing_data.get("jobs", [])
        
        # 去重处理
        deduplicated_jobs = deduplicate_jobs(existing_jobs, jobs)
        new_jobs_count = len(deduplicated_jobs) - len(existing_jobs)
        
        # 更新数据
        updated_data = {
            "jobs": deduplicated_jobs,
            "metadata": {
                "total_jobs": len(deduplicated_jobs),
                "new_jobs_this_run": new_jobs_count,
                "last_updated": datetime.now().isoformat()
            }
        }
        
        # 保存数据
        save_json(output_file, updated_data)
        
        # 更新运行状态
        self._update_run_status(new_jobs_count)
        
        self.logger.info(f"处理完成，新增 {new_jobs_count} 个职位")
    
    def _update_run_status(self, new_jobs_count: int) -> None:
        """更新运行状态"""
        status_file = "data/last_run.json"
        status_data = {
            "last_run_time": datetime.now().isoformat(),
            "new_jobs_found": new_jobs_count,
            "enabled_websites": self.config.get("enabled_websites", [])
        }
        save_json(status_file, status_data)


async def main():
    """主函数"""
    print("=== 自动职位抓取系统 ===")
    print("正在启动...")
    
    try:
        # 创建必要的目录
        Path("data").mkdir(exist_ok=True)
        Path("logs").mkdir(exist_ok=True)
        
        # 初始化并运行JobTracker
        tracker = JobTracker()
        await tracker.run_all_scrapers()
        
        print("抓取完成！")
        
    except KeyboardInterrupt:
        print("\n程序被用户中断")
    except Exception as e:
        print(f"程序运行出错: {e}")
        logging.error(f"程序运行出错: {e}")
    finally:
        print("程序结束")


if __name__ == "__main__":
    asyncio.run(main())