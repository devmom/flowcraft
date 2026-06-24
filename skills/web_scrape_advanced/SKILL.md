---
name: web_scrape_advanced
description: "高级网页抓取：支持B站/YouTube/SPA页面，自动解析JSON-LD/OG/API数据，提取标题/描述/播放量/标签"
category: network
version: "1.0.0"
author: flowcraft
script_path: scripts/scrape.py
script_language: python
tags: [python, web, scrape, bilibili, youtube, spa]
timeout_seconds: 30
source: workspace
enabled: true
---

# 高级网页抓取技能

支持现代 SPA 页面（B站、YouTube等），从HTML中提取结构化数据。

## 策略
1. HTML meta 标签：og:title, og:description, keywords
2. JSON-LD 结构化数据 (schema.org)
3. B站专用：__INITIAL_STATE__ / __NEPTUNE_IS_MY_WAIFU__
4. 通用：从 `<script>` 标签中提取 JSON 数据

## 输入格式 (JSON stdin)
{"url": "https://...", "extract": ["title", "description", "keywords", "author"]}
