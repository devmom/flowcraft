#!/usr/bin/env python3
"""Quick log analysis tool for FlowCraft diagnostic logs.

Usage:
    python scripts/analyze_logs.py                     # Analyze today's log
    python scripts/analyze_logs.py 2026-06-01          # Analyze specific date
    python scripts/analyze_logs.py --task task_abc123  # Filter by task_id
    python scripts/analyze_logs.py --stuck              # Find stuck tasks
    python scripts/analyze_logs.py --timeline           # Show timeline view
"""

import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

LOG_DIR = Path(__file__).resolve().parent.parent / "log"


def load_logs(date_str: str | None = None) -> list[dict]:
    """Load JSONL log entries for a given date."""
    if date_str is None:
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = LOG_DIR / f"flowcraft-{date_str}.jsonl"
    if not path.exists():
        print(f"No log file found for {date_str}: {path}")
        return []
    entries = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return entries


def find_stuck_tasks(entries: list[dict]):
    """Identify tasks that appear stuck."""
    tasks = defaultdict(list)
    for e in entries:
        tid = e.get("task_id", "")
        if tid:
            tasks[tid].append(e)

    print("\n=== Potentially Stuck Tasks ===\n")
    stuck_count = 0
    for tid, events in sorted(tasks.items()):
        # Check for: started but no end event
        has_start = any("begin" in e.get("event", "") for e in events)
        has_end = any("end" in e.get("event", "") for e in events)
        has_error = any(e.get("level") == "ERROR" for e in events)
        has_timeout = any("timeout" in e.get("event", "") for e in events)
        has_loop = any("loop" in e.get("event", "") for e in events)

        if has_start and not has_end:
            stuck_count += 1
            print(f"[STUCK] {tid[:16]}... ({len(events)} events)")
            # Show timeline
            for e in events:
                ts = e.get("timestamp", "")[-12:]
                level = e.get("level", "INFO")
                event = e.get("event", "")
                msg = e.get("message", "")[:100]
                elapsed = f" ({e['elapsed_s']:.1f}s)" if "elapsed_s" in e else ""
                print(f"  {ts} [{level}] {event}{elapsed} | {msg}")
            print()

        if has_timeout or has_loop:
            if tid not in [t for t, _ in [(tid, events)]]:
                continue
            print(f"[ISSUE] {tid[:16]}... timeout={'YES' if has_timeout else 'no'} loop={'YES' if has_loop else 'no'}")
            for e in events:
                if "timeout" in e.get("event", "") or "loop" in e.get("event", ""):
                    print(f"  {e.get('timestamp', '')[-12:]} [{e.get('level')}] {e.get('event')} | {e.get('message', '')}")
            print()

    if stuck_count == 0:
        print("No obviously stuck tasks found.")
    else:
        print(f"Total: {stuck_count} potentially stuck task(s)")


def show_timeline(entries: list[dict], task_filter: str | None = None):
    """Show a clean timeline of events."""
    if task_filter:
        entries = [e for e in entries if task_filter in e.get("task_id", "")]
    if not entries:
        print("No events to show.")
        return

    print("\n=== Event Timeline ===\n")
    for e in entries:
        ts = e.get("timestamp", "")[-12:]
        tid = e.get("task_id", "")[:8]
        level = e.get("level", "INFO")
        phase = e.get("phase", "")
        event = e.get("event", "")
        msg = e.get("message", "")[:120]

        # Color indicators
        icon = {"INFO": " ", "WARN": "⚠", "ERROR": "✗", "DEBUG": "·"}.get(level, " ")
        phase_icon = {"pipeline": "📋", "llm": "🤖", "execute": "⚡", "tool": "🔧"}.get(phase, "  ")
        elapsed = f" ({e['elapsed_s']:.2f}s)" if "elapsed_s" in e else ""

        print(f"{icon}{phase_icon} {ts} [{tid}] {event}{elapsed}")
        print(f"     {msg}")
        print()


def show_llm_stats(entries: list[dict]):
    """Show LLM call statistics."""
    llm_calls = [e for e in entries if e.get("phase") == "llm" and e.get("event") == "llm.result"]
    llm_timeouts = [e for e in entries if e.get("phase") == "llm" and e.get("event") == "llm.timeout"]

    print("\n=== LLM Statistics ===\n")
    print(f"Total LLM calls: {len(llm_calls) + len(llm_timeouts)}")
    print(f"Successful: {len(llm_calls)}")
    print(f"Timeouts: {len(llm_timeouts)}")

    if llm_calls:
        times = [e.get("elapsed_s", 0) for e in llm_calls if e.get("elapsed_s")]
        if times:
            print(f"Avg response time: {sum(times)/len(times):.2f}s")
            print(f"Min: {min(times):.2f}s  Max: {max(times):.2f}s")

    # Show timeout details
    if llm_timeouts:
        print(f"\nTimeout details:")
        for e in llm_timeouts:
            ts = e.get("timestamp", "")[-12:]
            tid = e.get("task_id", "")[:8]
            print(f"  {ts} [{tid}] {e.get('message', '')}")


def show_tool_stats(entries: list[dict]):
    """Show tool call statistics."""
    tool_calls = [e for e in entries if e.get("phase") == "tool" and e.get("event") == "tool.result"]
    if not tool_calls:
        return

    print("\n=== Tool Statistics ===\n")
    by_tool = defaultdict(list)
    for e in tool_calls:
        by_tool[e.get("tool_name", "unknown")].append(e)

    for tool_name, calls in sorted(by_tool.items()):
        success = sum(1 for c in calls if "SUCCESS" in c.get("message", ""))
        failed = sum(1 for c in calls if "FAILED" in c.get("message", ""))
        denied = sum(1 for c in calls if "DENIED" in c.get("message", ""))
        times = [c.get("elapsed_s", 0) for c in calls if c.get("elapsed_s")]
        avg_time = sum(times) / len(times) if times else 0
        print(f"  {tool_name}: {len(calls)} calls (ok={success} fail={failed} denied={denied}) avg={avg_time:.2f}s")


def main():
    args = sys.argv[1:]
    date_str = None
    task_filter = None
    mode = "full"  # full, stuck, timeline, stats

    i = 0
    while i < len(args):
        if args[i] == "--task":
            task_filter = args[i + 1]
            i += 2
        elif args[i] == "--stuck":
            mode = "stuck"
            i += 1
        elif args[i] == "--timeline":
            mode = "timeline"
            i += 1
        elif args[i] == "--stats":
            mode = "stats"
            i += 1
        elif args[i].startswith("202"):
            date_str = args[i]
            i += 1
        else:
            i += 1

    entries = load_logs(date_str)
    if not entries:
        print("No log data available. Run FlowCraft first to generate logs.")
        return

    print(f"Loaded {len(entries)} log entries from {date_str or 'today'}")

    if mode == "stuck":
        find_stuck_tasks(entries)
    elif mode == "timeline":
        show_timeline(entries, task_filter)
    elif mode == "stats":
        show_llm_stats(entries)
        show_tool_stats(entries)
    else:
        show_llm_stats(entries)
        show_tool_stats(entries)
        find_stuck_tasks(entries)


if __name__ == "__main__":
    main()
