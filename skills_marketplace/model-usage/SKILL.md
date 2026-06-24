---
name: model-usage
description: "Summarize CodexBar local cost logs by model for Codex or Claude, including current or full breakdowns."
category: data
version: "1.0.0"
author: "openclaw-ported"
script_path: "scripts/main.py"
script_language: python
tags: ["python", "bash", "git", "cli", "documentation"]
timeout_seconds: 120
source: marketplace
enabled: true
---

# Model usage

## Overview

Get per-model usage cost from CodexBar's local cost logs. Supports "current model" (most recent daily entry) or "all models" summaries for Codex or Claude.

Live CodexBar CLI invocation is currently documented for macOS only. The bundled Python summarizer is portable: if you already have exported CodexBar JSON, `--input` mode works anywhere Python is available.

## Quick start

1. Fetch cost JSON via CodexBar CLI or pass a JSON file.
2. Use the bundled script to summarize by model.

```bash
python {baseDir}/scripts/model_usage.py --provider codex --mode current
python {baseDir}/scripts/model_usage.py --provider codex --mode all
python {baseDir}/scripts/model_usage.py --provider claude --mode all --format json --pretty
```

## Current model logic

- Uses the most recent daily row with `modelBreakdowns`.
- Picks the model with the highest cost in that row.
- Falls back to the last entry in `modelsUsed` when breakdowns are missing.
- Override with `--model <name>` when you need a specific model.

## Inputs

- Default: runs `codexbar cost --format json --provider <codex|claude>`.
- macOS: use the bundled CodexBar CLI install path above for live local usage reads.
- Linux/other platforms: use `--input` with exported CodexBar JSON until this skill documents a supported local CodexBar install path for that platform.
- File or stdin:

```bash
codexbar cost --provider codex --format json > /tmp/cost.json
python {baseDir}/scripts/model_usage.py --input /tmp/cost.json --mode all
cat /tmp/cost.json | python {baseDir}/scripts/model_usage.py --input - --mode current
```

## Output

- Text (default) or JSON (`--format json --pretty`).
- Values are cost-only per model; tokens are not split by model in CodexBar output.

## References

- Read `references/codexbar-cli.md` for CLI flags and cost JSON fields.


## References


### codexbar-cli

# CodexBar CLI quick ref (usage + cost)

## Install

- App: Preferences -> Advanced -> Install CLI
- Repo: ./bin/install-codexbar-cli.sh

## Commands

- Usage snapshot (web/cli sources):
  - codexbar usage --format json --pretty
  - codexbar --provider all --format json
- Local cost usage (Codex + Claude only):
  - codexbar cost --format json --pretty
  - codexbar cost --provider codex|claude --format json

## Cost JSON fields

The payload is an array (one per provider).

- provider, source, updatedAt
- sessionTokens, sessionCostUSD
- last30DaysTokens, last30DaysCostUSD
- daily[]: date, inputTokens, outputTokens, cacheReadTokens, cacheCreationTokens, totalTokens, totalCost, modelsUsed, modelBreakdowns[]
- modelBreakdowns[]: modelName, cost
- totals: totalInputTokens, totalOutputTokens, cacheReadTokens, cacheCreationTokens, totalTokens, totalCost

## Notes

- Cost usage is local-only. It reads JSONL logs under:
  - Codex: ~/.codex/sessions/\*_/_.jsonl
  - Claude: ~/.config/claude/projects/**/\*.jsonl or ~/.claude/projects/**/\*.jsonl
- If web usage is required (non-local), use codexbar usage (not cost).