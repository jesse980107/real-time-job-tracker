"""
Google Career 职位抓取器
使用Playwright自动化抓取Google Career网站的职位信息
按照详细需求实现：递归点击job cards，提取详细职位信息
"""

import asyncio
import logging
import json
import re
from typing import List, Dict, Any, Optional
from datetime import datetime
from playwright.async_api import async_playwright, Page, Browser
from urllib.parse import urljoin, urlparse


class GoogleCareerScraper:
    """Google Career职位抓取器"""
    
    def __init__(self, website_config: Dict[str, Any], global_config: Dict[str, Any]):
        """
        初始化抓取器
        
        Args:
            website_config: 网站专用配置
            global_config: 全局配置
        """
        self.website_config = website_config
        self.global_config = global_config
        self.logger = logging.getLogger("job_tracker")
        
        # 目标网址
        self.target_url = "https://www.google.com/about/careers/applications/jobs/results/?location=United%20States&location=Canada&sort_by=date"
        
        # 全局配置
        self.headless = global_config["global_settings"]["headless"]
        self.timeout = global_config["playwright_global"]["timeout"]
        self.user_agent = global_config["playwright_global"]["user_agent"]
        
        self.scraped_jobs = []
        self.current_index = 0
    
    async def scrape_jobs(self) -> List[Dict[str, Any]]:
        """
        主要的抓取方法
        
        Returns:
            抓取到的职位列表
        """
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=self.headless,
                    args=['--no-sandbox', '--disable-dev-shm-usage']
                )
                
                page = await browser.new_page(user_agent=self.user_agent)
                
                # 设置页面超时
                page.set_default_timeout(self.timeout)
                
                # 执行搜索和抓取
                await self._perform_search_and_scrape(page)
                
                await browser.close()
                
                self.logger.info(f"Google Career抓取完成，共获得 {len(self.scraped_jobs)} 个职位")
                return self.scraped_jobs
                
        except Exception as e:
            self.logger.error(f"Google Career抓取过程中发生错误: {e}")
            return []
    
    async def _perform_search_and_scrape(self, page: Page) -> None:
        """
        执行搜索和抓取的完整流程
        
        Args:
            page: Playwright页面对象
        """
        try:
            # 步骤1: 访问目标网址
            self.logger.info(f"访问页面: {self.target_url}")
            await page.goto(self.target_url)
            
            # 步骤2: 等待页面加载并点击第一个job card的"Learn more"
            self.logger.info("等待Learn more按钮加载...")
            # await page.wait_for_selector('li.lLd3Je a[aria-label^="Learn more about"]', timeout=10000)
            self.logger.info("Learn more按钮已加载，准备点击")
            
            # 点击第一个job card的Learn more按钮
            first_learn_more = await page.query_selector('li.lLd3Je a[aria-label^="Learn more about"]')
            if first_learn_more:
                await first_learn_more.click()
                await page.wait_for_load_state('networkidle')
                self.logger.info("成功点击第一个Learn more按钮")
            else:
                self.logger.error("未找到Learn more按钮")
            
            # 步骤3: 开始多页面抓取
            await self._start_multi_page_scraping(page)
            
        except Exception as e:
            self.logger.error(f"搜索和抓取过程中发生错误: {e}")
    
    async def _start_multi_page_scraping(self, page: Page) -> None:
        """
        开始多页面抓取
        
        Args:
            page: Playwright页面对象
        """
        try:
            page_number = 1
            max_pages = 5  # 最多抓取5页，可以根据需要调整
            
            while page_number <= max_pages:
                self.logger.info(f"🔍 开始抓取第 {page_number} 页")
                
                # 抓取当前页面的所有job cards
                await self._start_recursive_scraping(page, page_number)
                
                # 尝试跳转到下一页
                has_next_page = await self._goto_next_page(page)
                
                if not has_next_page:
                    self.logger.info("✅ 已到达最后一页或无法找到下一页按钮")
                    break
                
                page_number += 1
                
                # 等待页面加载
                await page.wait_for_load_state('networkidle')
                await asyncio.sleep(2)  # 额外等待确保页面完全加载
                
        except Exception as e:
            self.logger.error(f"多页面抓取过程中发生错误: {e}")
    
    async def _start_recursive_scraping(self, page: Page, page_number: int = 1) -> None:
        """
        开始递归抓取当前页面的所有job cards
        
        Args:
            page: Playwright页面对象
            page_number: 当前页面编号
        """
        try:
            # 获取所有job cards
            job_cards = await page.query_selector_all('li.zE6MFb')
            total_jobs = len(job_cards)
            self.logger.info(f"第 {page_number} 页发现 {total_jobs} 个职位")
            
            if total_jobs == 0:
                self.logger.warning(f"第 {page_number} 页没有找到任何职位")
                return
            
            # 开始递归点击和提取
            await self._click_and_scrape_job_card(page, 0)
            
        except Exception as e:
            self.logger.error(f"第 {page_number} 页递归抓取时发生错误: {e}")
    
    async def _goto_next_page(self, page: Page) -> bool:
        """
        跳转到下一页
        
        Args:
            page: Playwright页面对象
            
        Returns:
            bool: 是否成功跳转到下一页
        """
        try:
            self.logger.info("🔄 查找下一页按钮...")
            
            # 使用JavaScript查找并点击下一页按钮
            next_page_clicked = await page.evaluate('''
                () => {
                    const nextPageLink = document.querySelector('div[jsname="ViaHrd"] a');
                    if (nextPageLink) {
                        console.log('找到下一页按钮，准备点击');
                        nextPageLink.click();
                        return true;
                    }
                    return false;
                }
            ''')
            
            if next_page_clicked:
                self.logger.info("✅ 成功点击下一页按钮")
                return True
            else:
                self.logger.info("❌ 未找到下一页按钮")
                return False
                
        except Exception as e:
            self.logger.error(f"跳转下一页时发生错误: {e}")
            return False
    
    async def _click_and_scrape_job_card(self, page: Page, index: int) -> None:
        """
        递归点击并抓取job card数据
        
        Args:
            page: Playwright页面对象
            index: 当前job card索引
        """
        try:
            # 重新获取job cards (因为DOM可能已更新)
            job_cards = await page.query_selector_all('li.zE6MFb')
            
            if index >= len(job_cards):
                self.logger.info('✅ 所有job card已处理完毕')
                return
            
            # 点击指定索引的job card
            link = await job_cards[index].query_selector('a')
            if link:
                self.logger.info(f'🔘 点击第 {index + 1} 个job card')
                await link.click()
                
                # 等待页面加载
                await page.wait_for_timeout(800)  # 等待800ms
                await page.wait_for_load_state('networkidle')
                
                # 提取数据
                job_data = await self._extract_job_data(page)
                if job_data:
                    self.scraped_jobs.append(job_data)
                    self.logger.info(f'✅ 成功提取第 {index + 1} 个职位数据: {job_data.get("title", "Unknown")}')
                    self._log_progress(index + 1, len(job_cards))
                
                # 递归调用下一个
                await self._click_and_scrape_job_card(page, index + 1)
            else:
                self.logger.warning(f'⚠️ 第 {index + 1} 个job card没有链接，跳过')
                await self._click_and_scrape_job_card(page, index + 1)
                
        except Exception as e:
            self.logger.error(f'❌ 处理第 {index + 1} 个job card时出错: {e}')
            # 继续处理下一个
            await self._click_and_scrape_job_card(page, index + 1)
    
    async def _extract_job_data(self, page: Page) -> Optional[Dict[str, Any]]:
        """
        从详情页面提取职位数据
        
        Args:
            page: Playwright页面对象
            
        Returns:
            职位信息字典或None
        """
        try:
            # 等待job card容器加载
            await page.wait_for_selector('div.DkhPwc', timeout=5000)
            
            # 提取jobId (从URL中提取连续5位以上数字)
            current_url = page.url
            job_id_match = re.search(r'\d{5,}', current_url)
            job_id = job_id_match.group(0) if job_id_match else None
            
            # 提取标题
            title_element = await page.query_selector('h2.p1N2lc')
            title = await title_element.inner_text() if title_element else ""
            
            # 提取地点
            location_elements = await page.query_selector_all('.r0wTof')
            locations = []
            for loc_elem in location_elements:
                loc_text = await loc_elem.inner_text()
                locations.append(loc_text.strip())
            location = '; '.join(locations)
            
            # 提取职位级别
            level_element = await page.query_selector('.wVSTAb')
            level = await level_element.inner_text() if level_element else ""
            
            # 提取职位描述
            job_description = await self._extract_job_description(page)
            
            # 构建职位数据
            job_data = {
                'jobId': job_id,
                'title': title.strip(),
                'company': 'Google',
                'location': location,
                'level': level.strip(),
                'jobDescription': job_description,
                'url': current_url,
                'scraped_date': datetime.now().isoformat(),
                'source': 'google_career',
                'status': 'active'
            }
            
            return job_data
            
        except Exception as e:
            self.logger.error(f'❌ 提取职位数据时出错: {e}')
            return None
    
    async def _extract_job_description(self, page: Page) -> str:
        """
        提取职位描述
        
        Args:
            page: Playwright页面对象
            
        Returns:
            格式化的职位描述
        """
        try:
            description_parts = []
            
            # 提取Minimum qualifications
            min_qual = await self._extract_section_content(page, 'Minimum qualifications')
            if min_qual:
                description_parts.extend(['Minimum qualifications:', min_qual, ''])
            
            # 提取Preferred qualifications
            pref_qual = await self._extract_section_content(page, 'Preferred qualifications')
            if pref_qual:
                description_parts.extend(['Preferred qualifications:', pref_qual, ''])
            
            # 提取About the job
            about_job = await self._extract_paragraph_content(page, 'About the job')
            if about_job:
                description_parts.extend(['About the job:', about_job, ''])
            
            # 提取Responsibilities
            responsibilities = await self._extract_section_content(page, 'Responsibilities')
            if responsibilities:
                description_parts.extend(['Responsibilities:', responsibilities])
            
            return '\n'.join(description_parts)
            
        except Exception as e:
            self.logger.error(f'❌ 提取职位描述时出错: {e}')
            return ""
    
    async def _extract_section_content(self, page: Page, heading_text: str) -> str:
        """
        提取ul列表内容
        
        Args:
            page: Playwright页面对象
            heading_text: 章节标题文本
            
        Returns:
            章节内容
        """
        try:
            # 查找包含指定文本的h3标签
            h3_elements = await page.query_selector_all('h3')
            target_h3 = None
            
            for h3 in h3_elements:
                h3_text = await h3.inner_text()
                if heading_text.lower() in h3_text.lower():
                    target_h3 = h3
                    break
            
            if not target_h3:
                return ""
            
            # 获取下一个兄弟元素
            next_element = await page.evaluate(
                '(h3) => h3.nextElementSibling',
                target_h3
            )
            
            if next_element:
                tag_name = await page.evaluate('(el) => el.tagName', next_element)
                if tag_name == 'UL':
                    # 提取ul中的所有li内容
                    li_elements = await page.query_selector_all('li')
                    # 过滤出属于当前ul的li元素
                    li_texts = []
                    ul_lis = await page.evaluate(
                        '(ul) => Array.from(ul.querySelectorAll("li")).map(li => li.textContent.trim())',
                        next_element
                    )
                    return '\n'.join(ul_lis)
            
            return ""
            
        except Exception as e:
            self.logger.error(f'❌ 提取section内容时出错: {e}')
            return ""
    
    async def _extract_paragraph_content(self, page: Page, heading_text: str) -> str:
        """
        提取段落内容
        
        Args:
            page: Playwright页面对象
            heading_text: 章节标题文本
            
        Returns:
            段落内容
        """
        try:
            # 查找包含指定文本的h3标签
            h3_elements = await page.query_selector_all('h3')
            target_h3 = None
            
            for h3 in h3_elements:
                h3_text = await h3.inner_text()
                if heading_text.lower() in h3_text.lower():
                    target_h3 = h3
                    break
            
            if not target_h3:
                return ""
            
            # 获取后续的段落内容
            paragraphs = await page.evaluate('''
                (h3) => {
                    const paragraphs = [];
                    let current = h3.nextElementSibling;
                    while (current && current.tagName !== 'H3') {
                        if (current.tagName === 'P') {
                            paragraphs.push(current.textContent.trim());
                        }
                        current = current.nextElementSibling;
                    }
                    return paragraphs;
                }
            ''', target_h3)
            
            return '\n\n'.join(paragraphs)
            
        except Exception as e:
            self.logger.error(f'❌ 提取paragraph内容时出错: {e}')
            return ""
    
    def _log_progress(self, current: int, total: int) -> None:
        """
        记录进度
        
        Args:
            current: 当前进度
            total: 总数
        """
        percentage = (current / total) * 100
        self.logger.info(f'📈 进度: {current}/{total} ({percentage:.1f}%)')
    
    async def _retry_operation(self, operation, max_retries: int = 3):
        """
        重试机制
        
        Args:
            operation: 要执行的操作
            max_retries: 最大重试次数
        """
        for attempt in range(max_retries):
            try:
                return await operation()
            except Exception as e:
                if attempt == max_retries - 1:
                    raise e
                self.logger.warning(f'⚠️ 操作失败，重试中... ({attempt + 1}/{max_retries})')
                await asyncio.sleep(1)
    
    async def save_scraped_data(self, filename: str = "google_jobs.json") -> None:
        """
        保存爬取的数据
        
        Args:
            filename: 保存的文件名
        """
        try:
            with open(filename, 'w', encoding='utf-8') as f:
                json.dump(self.scraped_jobs, f, ensure_ascii=False, indent=2)
            self.logger.info(f'✅ 数据已保存到 {filename}')
            self.logger.info(f'📊 共爬取 {len(self.scraped_jobs)} 个职位')
        except Exception as e:
            self.logger.error(f'❌ 保存数据时出错: {e}')


# 测试和运行函数
async def main():
    """测试函数"""
    try:
        # 模拟配置
        website_config = {
            "website_info": {
                "base_url": "https://www.google.com/about/careers/"
            },
            "scraping_config": {
                "max_pages": 5
            },
            "playwright_options": {
                "wait_for_selector": "li.zE6MFb",
                "wait_timeout": 10000
            }
        }
        
        global_config = {
            "global_settings": {
                "headless": False
            },
            "playwright_global": {
                "timeout": 30000,
                "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            }
        }
        
        # 创建抓取器实例
        scraper = GoogleCareerScraper(website_config, global_config)
        
        # 执行抓取
        jobs = await scraper.scrape_jobs()
        
        # 保存数据
        await scraper.save_scraped_data()
        
        print(f"抓取完成！共获得 {len(jobs)} 个职位")
        
    except Exception as e:
        print(f"执行过程中发生错误: {e}")


if __name__ == "__main__":
    asyncio.run(main())