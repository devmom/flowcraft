---
name: data_analysis
description: "CSV/JSON/Excel data analysis: summaries, trends, correlation, grouping, charts. Requires pandas/numpy/matplotlib."
category: data
version: "1.0.0"
author: flowcraft
script_path: scripts/analysis.py
script_language: python
tags: [python, pandas, statistics, csv, chart]
timeout_seconds: 60
source: workspace
enabled: true
---

# Data Analysis Skill

Analyze structured data files. This is a deterministic script that runs pandas-based analysis.

## Usage
The script reads a JSON task description from stdin and outputs results as JSON.

## Input JSON format
{"file_path": "path/to/data.csv", "operation": "summarize|trend|correlation|group", "columns": ["col1", "col2"], "group_by": "col_name"}
