# 自动职位抓取系统

## 项目简介
自动化系统，检测和抓取Google Career等招聘网站的新职位发布，并进行本地存储管理。

## 技术栈
- **网页抓取**: Playwright (Python)
- **主程序**: Python 3.8+
- **数据存储**: 本地JSON文件
- **执行方式**: 串行抓取各个网站

## 项目架构

### 目录结构
```
Real-Time Job Tracker/
├── README.md              # 项目说明文档
├── main.py                # 主管理文件 - 程序入口
├── config.json            # 全局配置 - 高层级管理
├── requirements.txt       # Python依赖
├── scrapers/              # 各网站抓取器目录
│   ├── __init__.py
│   ├── google_career/
│   │   ├── scraper.py
│   │   └── config.json    # Google Career 专用配置
│   ├── linkedin/
│   │   ├── scraper.py
│   │   └── config.json    # LinkedIn 专用配置
│   └── indeed/
│       ├── scraper.py
│       └── config.json    # Indeed 专用配置
├── utils/                 # 工具模块
│   ├── __init__.py
│   ├── file_utils.py      # JSON文件操作
│   ├── logger.py          # 日志工具
│   └── deduplication.py   # 去重工具
├── data/                  # 数据存储目录
│   ├── jobs.json          # 存储所有job数据
│   └── last_run.json      # 记录上次运行状态
└── logs/                  # 日志目录
    └── scraper.log
```

### 配置文件分层设计

#### 全局配置 (config.json)
```json
{
  "enabled_websites": ["google_career", "linkedin", "indeed"],
  "global_settings": {
    "output_file": "data/jobs.json",
    "log_level": "INFO",
    "max_retries": 3,
    "global_delay": 5,
    "headless": true
  },
  "playwright_global": {
    "timeout": 30000,
    "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
  }
}
```

#### 网站专用配置 (scrapers/google_career/config.json)
```json
{
  "website_info": {
    "name": "Google Career",
    "base_url": "https://careers.google.com/jobs/results/",
    "enabled": true
  },
  "scraping_config": {
    "interval": 3600,
    "max_pages": 10,
    "selectors": {
      "job_card": ".job-card",
      "title": ".job-title",
      "location": ".job-location",
      "link": ".job-link"
    }
  },
  "search_params": {
    "keywords": ["software engineer", "data scientist"],
    "locations": ["New York", "San Francisco"],
    "experience_levels": ["entry", "mid", "senior"]
  },
  "playwright_options": {
    "wait_for_selector": ".job-card",
    "scroll_behavior": true,
    "wait_timeout": 10000
  }
}
```

## 工作流程

1. 运行 `python main.py` 启动系统
2. 读取全局 `config.json` 获取启用的网站列表
3. 串行循环遍历每个启用的网站
4. 为每个网站加载对应的专用配置文件
5. 调用对应的scraper.py并传入配置
6. 每个scraper使用Playwright和专用配置抓取职位
7. 与现有 `jobs.json` 数据比较进行去重
8. 更新JSON文件和运行状态记录
9. 记录日志并处理错误

## 核心功能

- **分层配置**: 全局配置管理高层级设置，网站配置管理具体抓取参数
- **增量更新**: 只抓取新发布的职位，避免重复
- **去重机制**: 使用job ID或URL作为唯一标识符
- **错误处理**: 单个网站失败不影响其他网站抓取
- **日志记录**: 记录抓取过程和结果
- **灵活配置**: 可单独调整每个网站的抓取策略

## 数据结构

### 职位数据格式 (jobs.json)
```json
{
  "jobs": [
    {
      "id": "unique_job_id",
      "title": "Software Engineer",
      "company": "Google",
      "location": "New York, NY",
      "url": "https://careers.google.com/jobs/results/123456",
      "description": "Job description...",
      "posted_date": "2024-01-15",
      "scraped_date": "2024-01-15T10:30:00Z",
      "source": "google_career",
      "status": "active"
    }
  ],
  "metadata": {
    "total_jobs": 1,
    "last_updated": "2024-01-15T10:30:00Z"
  }
}
```

## 安装和运行

### 1. 安装依赖
```bash
pip install -r requirements.txt
```

### 2. 安装Playwright浏览器
```bash
playwright install
```

### 3. 运行程序
```bash
python main.py
```

## 开发计划

### Phase 1: 基础架构
- [x] 创建项目结构
- [ ] 实现主程序框架
- [ ] 创建配置管理系统
- [ ] 实现日志和工具模块

### Phase 2: 核心功能
- [ ] 实现Google Career抓取器
- [ ] 实现数据去重和存储
- [ ] 添加错误处理和重试机制

### Phase 3: 扩展功能
- [ ] 添加LinkedIn抓取器
- [ ] 添加Indeed抓取器
- [ ] 优化性能和稳定性

## 注意事项

1. **反爬虫机制**: 设置合理的请求间隔，使用随机User-Agent
2. **错误处理**: 网络异常和页面变化的处理
3. **数据一致性**: 确保数据格式统一和完整性
4. **性能优化**: 避免重复抓取和内存泄漏

## 贡献指南

1. Fork项目
2. 创建功能分支
3. 提交更改
4. 推送到分支
5. 创建Pull Request

## 许可证

MIT License