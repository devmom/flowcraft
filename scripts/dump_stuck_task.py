#!/usr/bin/env python3
"""卡死任务诊断报告生成器。

用法:
    # 自动找出今天所有卡死的任务并生成报告
    python scripts/dump_stuck_task.py

    # 指定任务ID
    python scripts/dump_stuck_task.py --task task_a1b2c3d4

    # 指定日期 + 输出到文件
    python scripts/dump_stuck_task.py --date 2026-06-01 -o report.txt

把生成的报告内容发给我，我就能分析卡死原因。
"""

import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

LOG_DIR = Path(__file__).resolve().parent.parent / "log"
DB_PATH = Path(__file__).resolve().parent.parent / "core" / "data" / "flowcraft.db"


def load_jsonl(date_str: str) -> list[dict]:
    path = LOG_DIR / f"flowcraft-{date_str}.jsonl"
    if not path.exists():
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


def load_text_log(date_str: str) -> str:
    path = LOG_DIR / f"flowcraft-{date_str}.log"
    if not path.exists():
        return ""
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def identify_stuck_tasks(entries: list[dict]) -> list[str]:
    """Find tasks that started but never completed."""
    tasks: dict[str, dict] = {}
    for e in entries:
        tid = e.get("task_id", "")
        if not tid or tid == "--------":
            continue
        if tid not in tasks:
            tasks[tid] = {"started": False, "ended": False, "last_event": None}
        event = e.get("event", "")
        if "begin" in event or "started" in event:
            tasks[tid]["started"] = True
        if "end" in event or "completed" in event or "failed" in event or "cancelled" in event:
            tasks[tid]["ended"] = True
            tasks[tid]["last_event"] = event
        # Track the last event
        tasks[tid]["last_ts"] = e.get("timestamp", "")
        tasks[tid]["last_msg"] = e.get("message", "")

    stuck = []
    for tid, info in tasks.items():
        if info["started"] and not info["ended"]:
            stuck.append(tid)
    return stuck


def get_task_info(task_id: str) -> dict | None:
    """Try to read task info from the database."""
    try:
        import sqlite3
        db = sqlite3.connect(str(DB_PATH))
        db.row_factory = sqlite3.Row
        row = db.execute(
            "SELECT id, title, objective, status, task_type, risk_level, "
            "failed_reason, created_at, updated_at, completed_at "
            "FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        db.close()
        if row:
            return dict(row)
    except Exception:
        pass
    return None


def dump_task_report(task_id: str, entries: list[dict], text_log: str = "") -> str:
    """Generate a comprehensive diagnostic report for a single task."""
    task_entries = [e for e in entries if e.get("task_id") == task_id]
    if not task_entries:
        return f"[WARN] No log entries found for task {task_id}"

    db_info = get_task_info(task_id)

    lines = []
    lines.append("=" * 70)
    lines.append(f"卡死任务诊断报告: {task_id}")
    lines.append("=" * 70)

    # ── 1. 任务基本信息 ──
    lines.append("\n## 1. 任务基本信息")
    if db_info:
        lines.append(f"  标题:     {db_info.get('title', '?')}")
        lines.append(f"  目标:     {db_info.get('objective', '?')[:200]}")
        lines.append(f"  状态:     {db_info.get('status', '?')}")
        lines.append(f"  类型:     {db_info.get('task_type', '?')}")
        lines.append(f"  风险等级: {db_info.get('risk_level', '?')}")
        lines.append(f"  失败原因: {db_info.get('failed_reason') or '(无)'}")
        lines.append(f"  创建时间: {db_info.get('created_at', '?')}")
        lines.append(f"  更新时间: {db_info.get('updated_at', '?')}")
        lines.append(f"  完成时间: {db_info.get('completed_at') or '(未完成)'}")
    else:
        lines.append("  (数据库信息不可用)")

    # ── 2. 完整事件时间线 ──
    lines.append(f"\n## 2. 完整事件时间线 ({len(task_entries)} 条)")

    task_entries.sort(key=lambda e: e.get("timestamp", ""))
    last_ts = None
    for e in task_entries:
        ts = e.get("timestamp", "")[-12:]
        level = e.get("level", "INFO")
        phase = e.get("phase", "")
        event = e.get("event", "")
        msg = e.get("message", "")

        # Calculate gap from previous event
        gap_str = ""
        if last_ts:
            try:
                t1 = datetime.fromisoformat(e.get("timestamp", ""))
                t2 = datetime.fromisoformat(last_ts)
                gap = (t1 - t2).total_seconds()
                if gap > 1.0:
                    gap_str = f"  ⬆ +{gap:.1f}s gap"
            except Exception:
                pass
        last_ts = e.get("timestamp", "")

        icon = {"INFO": " ", "WARN": "⚠", "ERROR": "✗", "DEBUG": "·"}.get(level, "?")
        elapsed = f" ({e['elapsed_s']:.2f}s)" if e.get("elapsed_s") else ""
        step = f" step={e['step_index']}" if e.get("step_index") else ""
        round_ = f" round={e['round_index']}" if e.get("round_index") else ""
        tool = f" tool={e['tool_name']}" if e.get("tool_name") else ""

        lines.append(f"  {icon} {ts} [{phase}] {event}{elapsed}{step}{round_}{tool}{gap_str}")
        lines.append(f"     {msg}")

    # ── 3. 阶段耗时分析 ──
    lines.append("\n## 3. 阶段耗时分析")

    # Pipeline stages
    stages = defaultdict(list)
    for e in task_entries:
        stage = e.get("event", "")
        if ".begin" in stage:
            stage_name = stage.replace(".begin", "")
            stages[stage_name].append({"start": e, "end": None})
        elif ".end" in stage:
            stage_name = stage.replace(".end", "")
            for s in stages.get(stage_name, []):
                if s["end"] is None:
                    s["end"] = e
                    break

    for stage_name, instances in sorted(stages.items()):
        for i, inst in enumerate(instances):
            if inst["end"] and inst["start"]:
                try:
                    t1 = datetime.fromisoformat(inst["start"]["timestamp"])
                    t2 = datetime.fromisoformat(inst["end"]["timestamp"])
                    duration = (t2 - t1).total_seconds()
                    lines.append(f"  {stage_name}: {duration:.2f}s")
                except Exception:
                    lines.append(f"  {stage_name}: ? (time parse error)")
            elif inst["start"] and not inst["end"]:
                lines.append(f"  {stage_name}: ⚠ 未完成! (卡在这里)")
                lines.append(f"    最后事件: {inst['start'].get('message', '')[:120]}")

    # LLM calls
    llm_times = []
    for e in task_entries:
        if e.get("phase") == "llm" and e.get("elapsed_s"):
            llm_times.append(e)
    if llm_times:
        total = sum(e["elapsed_s"] for e in llm_times)
        lines.append(f"\n  LLM调用次数: {len(llm_times)}")
        lines.append(f"  LLM总耗时:   {total:.2f}s")
        for e in llm_times:
            lines.append(f"    {e.get('event', '')}: {e['elapsed_s']:.2f}s — {e.get('message', '')[:100]}")

    # Tool calls
    tool_times = [e for e in task_entries if e.get("phase") == "tool" and e.get("elapsed_s")]
    if tool_times:
        total_tool = sum(e["elapsed_s"] for e in tool_times)
        lines.append(f"\n  工具调用次数: {len(tool_times)}")
        lines.append(f"  工具总耗时:   {total_tool:.2f}s")

    # ── 4. 异常和错误 ──
    lines.append("\n## 4. 异常与错误")
    errors = [e for e in task_entries if e.get("level") in ("ERROR", "WARN")]
    if errors:
        for e in errors:
            ts = e.get("timestamp", "")[-12:]
            lines.append(f"  [{e.get('level')}] {ts} {e.get('event')}")
            lines.append(f"    {e.get('message', '')}")
    else:
        lines.append("  (无)")

    # ── 5. 关键诊断信号 ──
    lines.append("\n## 5. 关键诊断信号")
    signals = []

    # Timeout
    timeouts = [e for e in task_entries if "timeout" in e.get("event", "")]
    if timeouts:
        signals.append(f"⚠ 超时: {len(timeouts)} 次")
        for e in timeouts:
            signals.append(f"   {e.get('timestamp', '')[-12:]} {e.get('message', '')}")

    # Dead loop
    loops = [e for e in task_entries if "loop" in e.get("event", "")]
    if loops:
        signals.append(f"⚠ 死循环: {len(loops)} 次检测")
        for e in loops:
            signals.append(f"   {e.get('message', '')}")

    # Approval wait
    approvals = [e for e in task_entries if "approval" in e.get("event", "")]
    if approvals:
        signals.append(f"⏸ 等待审批: {len(approvals)} 次")
        for e in approvals:
            signals.append(f"   {e.get('message', '')}")

    # Retries
    retries = [e for e in task_entries if "retry" in e.get("event", "")]
    if retries:
        signals.append(f"↻ 重试: {len(retries)} 次")
        for e in retries:
            signals.append(f"   {e.get('message', '')}")

    # Step limit
    limits = [e for e in task_entries if "limit" in e.get("event", "")]
    if limits:
        signals.append(f"✗ 达到限制: {len(limits)} 次")

    if signals:
        lines.extend(signals)
    else:
        lines.append("  (无明显异常信号 — 可能是静默卡死)")

    # ── 6. 原始日志片段 ──
    if text_log:
        lines.append("\n## 6. 原始日志片段 (最后20条相关行)")
        text_lines = text_log.split("\n")
        relevant = [l for l in text_lines if task_id[:8] in l]
        for l in relevant[-20:]:
            lines.append(f"  {l[:200]}")

    lines.append("\n" + "=" * 70)
    return "\n".join(lines)


def main():
    args = sys.argv[1:]
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    task_id = None
    output_file = None
    auto_mode = True  # auto-detect stuck tasks

    i = 0
    while i < len(args):
        if args[i] == "--task" and i + 1 < len(args):
            task_id = args[i + 1]
            auto_mode = False
            i += 2
        elif args[i] == "--date" and i + 1 < len(args):
            date_str = args[i + 1]
            i += 2
        elif args[i] in ("-o", "--output") and i + 1 < len(args):
            output_file = args[i + 1]
            i += 2
        else:
            i += 1

    entries = load_jsonl(date_str)
    text_log = load_text_log(date_str)

    if not entries:
        print(f"今天({date_str})还没有日志。请先运行 FlowCraft 触发任务。")
        sys.exit(1)

    print(f"日志日期: {date_str}")
    print(f"日志条目: {len(entries)} 条")

    if auto_mode:
        stuck = identify_stuck_tasks(entries)
        if not stuck:
            print("\n没有检测到卡死的任务。✅")
            # Fallback: show recently failed tasks
            failed = set()
            for e in entries:
                if "failed" in e.get("event", "") or "cancelled" in e.get("event", ""):
                    failed.add(e.get("task_id", ""))
            if failed:
                print(f"\n但有 {len(failed)} 个任务以失败/取消结束:")
                for tid in list(failed)[:5]:
                    info = get_task_info(tid)
                    if info:
                        print(f"  {tid[:16]}... [{info.get('status')}] {info.get('title', '')[:60]}")
            sys.exit(0)

        print(f"\n检测到 {len(stuck)} 个可能卡死的任务:")
        for tid in stuck:
            info = get_task_info(tid)
            if info:
                print(f"  {tid[:16]}... [{info.get('status')}] {info.get('title', '')[:80]}")

        # Generate reports for all stuck tasks
        all_reports = []
        for tid in stuck:
            report = dump_task_report(tid, entries, text_log)
            all_reports.append(report)

        full_report = "\n\n".join(all_reports)
    else:
        if not task_id:
            print("请用 --task <task_id> 指定任务ID")
            sys.exit(1)
        full_report = dump_task_report(task_id, entries, text_log)

    if output_file:
        with open(output_file, "w", encoding="utf-8") as f:
            f.write(full_report)
        print(f"\n报告已写入: {output_file}")
    else:
        print(full_report)
        print("\n💡 提示: 把上面的报告内容发给我，我可以分析卡死原因。")
        print(f"   也可以用 -o report.txt 保存到文件。")


if __name__ == "__main__":
    main()
