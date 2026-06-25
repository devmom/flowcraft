#!/usr/bin/env python3
"""Batch converter: OpenClaw skills → FlowCraft.

Converts priority OpenClaw skills and places them in D:/work/FlowCraft/skills/

Priority list:
  P0: skill-creator, coding-agent
  P1: diagram-maker, github, summarize, autoreview
  P2: technical-documentation, meme-maker, model-usage, session-logs
"""

import os
import sys
from pathlib import Path

# Add converter to path
sys.path.insert(0, str(Path(__file__).parent / "core" / "flowcraft_core" / "skills"))
from convert_skill import SkillConverter

OPENCLAW_PUBLIC = Path("D:/work/OpenClawCode/skills")
OPENCLAW_AGENTS = Path("D:/work/OpenClawCode/.agents/skills")
OUTPUT = Path("D:/work/FlowCraft/skills")
OUTPUT_MARKETPLACE = Path("D:/work/FlowCraft/skills_marketplace")

# ── Priority Skills ────────────────────────────────────────

# P0: Core meta-skills (go to main skills/)
P0_SKILLS = [
    (OPENCLAW_PUBLIC / "skill-creator", OUTPUT, "code"),
    (OPENCLAW_PUBLIC / "coding-agent", OUTPUT, "code"),
]

# P1: High-value general skills
P1_SKILLS = [
    (OPENCLAW_PUBLIC / "diagram-maker", OUTPUT, "media"),
    (OPENCLAW_PUBLIC / "github", OUTPUT_MARKETPLACE, "code"),
    (OPENCLAW_PUBLIC / "gh-issues", OUTPUT_MARKETPLACE, "code"),
    (OPENCLAW_PUBLIC / "summarize", OUTPUT, "text"),
    (OPENCLAW_AGENTS / "autoreview", OUTPUT, "code"),
    (OPENCLAW_AGENTS / "technical-documentation", OUTPUT, "text"),
]

# P2: Secondary useful skills
P2_SKILLS = [
    (OPENCLAW_PUBLIC / "meme-maker", OUTPUT_MARKETPLACE, "media"),
    (OPENCLAW_PUBLIC / "model-usage", OUTPUT_MARKETPLACE, "data"),
    (OPENCLAW_PUBLIC / "session-logs", OUTPUT_MARKETPLACE, "system"),
    (OPENCLAW_PUBLIC / "taskflow", OUTPUT_MARKETPLACE, "code"),
    (OPENCLAW_PUBLIC / "gitcrawl", OUTPUT_MARKETPLACE, "code"),
]

# B-class: CLI wrappers (markdown-only, marketplace)
B_CLASS_SKILLS = [
    (OPENCLAW_PUBLIC / "notion", OUTPUT_MARKETPLACE, "general"),
    (OPENCLAW_PUBLIC / "obsidian", OUTPUT_MARKETPLACE, "general"),
    (OPENCLAW_PUBLIC / "trello", OUTPUT_MARKETPLACE, "general"),
    (OPENCLAW_PUBLIC / "1password", OUTPUT_MARKETPLACE, "system"),
    (OPENCLAW_PUBLIC / "weather", OUTPUT_MARKETPLACE, "general"),
    (OPENCLAW_PUBLIC / "spotify-player", OUTPUT_MARKETPLACE, "media"),
    (OPENCLAW_PUBLIC / "video-frames", OUTPUT_MARKETPLACE, "media"),
    (OPENCLAW_PUBLIC / "tmux", OUTPUT_MARKETPLACE, "system"),
    (OPENCLAW_PUBLIC / "healthcheck", OUTPUT_MARKETPLACE, "system"),
    (OPENCLAW_PUBLIC / "blogwatcher", OUTPUT_MARKETPLACE, "general"),
    (OPENCLAW_PUBLIC / "himalaya", OUTPUT_MARKETPLACE, "general"),
    (OPENCLAW_PUBLIC / "gog", OUTPUT_MARKETPLACE, "general"),
    (OPENCLAW_PUBLIC / "openai-whisper-api", OUTPUT_MARKETPLACE, "media"),
    (OPENCLAW_PUBLIC / "sherpa-onnx-tts", OUTPUT_MARKETPLACE, "media"),
    (OPENCLAW_PUBLIC / "node-connect", OUTPUT_MARKETPLACE, "code"),
    (OPENCLAW_PUBLIC / "camsnap", OUTPUT_MARKETPLACE, "media"),
    (OPENCLAW_PUBLIC / "openhue", OUTPUT_MARKETPLACE, "system"),
    (OPENCLAW_PUBLIC / "blucli", OUTPUT_MARKETPLACE, "system"),
]


def convert_batch(skills, label=""):
    """Convert a batch of skills."""
    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"{'='*60}")

    converted = skipped = errors = 0

    for source_dir, output_dir, category in skills:
        if not source_dir.exists():
            print(f"  MISS  {source_dir.name}: directory not found")
            skipped += 1
            continue

        converter = SkillConverter(
            source_dir=source_dir,
            output_dir=output_dir,
            category=category,
            mode="auto",
            dry_run=False,
            verbose=True,
        )
        result = converter.convert()

        if result["status"] == "success":
            converted += 1
            print(f"  OK   {result['skill_name']} [{result['effective_mode']}] -> {output_dir.name}/")
        elif result["status"] == "skipped":
            skipped += 1
            print(f"  SKIP {result['skill_name']}: {result.get('reason', '')}")
        else:
            errors += 1
            print(f"  FAIL {result['skill_name']}: {result.get('error', '')}")

    print(f"  --- {label}: {converted} ok, {skipped} skipped, {errors} errors ---")
    return converted, skipped, errors


def main():
    total_ok = total_skip = total_err = 0

    ok, sk, er = convert_batch(P0_SKILLS, "P0: Core Meta-Skills")
    total_ok += ok; total_skip += sk; total_err += er

    ok, sk, er = convert_batch(P1_SKILLS, "P1: High-Value General Skills")
    total_ok += ok; total_skip += sk; total_err += er

    ok, sk, er = convert_batch(P2_SKILLS, "P2: Secondary Useful Skills")
    total_ok += ok; total_skip += sk; total_err += er

    ok, sk, er = convert_batch(B_CLASS_SKILLS, "B-Class: CLI Wrapper Skills")
    total_ok += ok; total_skip += sk; total_err += er

    print(f"\n{'='*60}")
    print(f"  TOTAL: {total_ok} converted, {total_skip} skipped, {total_err} errors")
    print(f"{'='*60}")

    # List all available skills
    print(f"\nAll FlowCraft skills now:")
    for root, dirs, files in os.walk(OUTPUT):
        for f in files:
            if f == "SKILL.md":
                rel = os.path.relpath(root, OUTPUT)
                print(f"  skills/{rel}/")
                break


if __name__ == "__main__":
    main()
