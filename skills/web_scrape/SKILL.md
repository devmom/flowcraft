---
name: web_scrape
description: "Extract structured data from web pages: titles, text, tables, links. Output as JSON/CSV."
category: network
version: "1.0.0"
author: flowcraft
script_path: scripts/scrape.py
script_language: python
tags: [python, web, scrape, extract, html]
timeout_seconds: 60
source: workspace
enabled: true
---

# Web Scrape Skill

Extract data from web pages.

## Usage
The script reads a JSON task from stdin.

## Input JSON format
{"url": "https://example.com", "extract": ["title", "text", "links", "tables"], "selector": "css_selector", "output_format": "json"}
