---
name: file_batch_rename
description: "Batch rename files: pattern matching, sequence padding, case conversion, regex replace. Supports preview."
category: files
version: "1.0.0"
author: flowcraft
script_path: scripts/rename.py
script_language: python
tags: [python, filesystem, batch, rename]
timeout_seconds: 30
source: workspace
enabled: true
---

# File Batch Rename Skill

Rename multiple files at once using patterns.

## Usage
The script reads a JSON task from stdin.

## Input JSON format
{"directory": "target/dir", "pattern": "*.txt", "operation": "prefix|suffix|replace|sequential", "value": "prefix_text_or_regex_pattern", "preview": true}
