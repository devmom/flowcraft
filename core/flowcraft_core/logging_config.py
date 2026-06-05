"""FlowCraft 详细诊断日志系统.

日志格式:
    [时间戳] [级别] [模块:函数] [task_id前缀] [耗时] 消息

日志文件:
    log/flowcraft-YYYY-MM-DD.jsonl    — JSON Lines, 结构化, 便于分析
    log/flowcraft-YYYY-MM-DD.log       — 纯文本, 人类可读

使用方式:
    from flowcraft_core.logging_config import get_trace_logger
    trace = get_trace_logger(__name__)
    trace.step_begin(task_id, "plan.create", "开始生成计划", ...)
    trace.step_end(task_id, "plan.create", elapsed=2.3, result="4步 LINEAR")
    trace.llm_call(task_id, "planner._generate_plan_llm", ...)
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time as _time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


# ── Log directory ────────────────────────────────────────────
LOG_DIR = Path(os.environ.get("FLOWCRAFT_LOG_DIR", Path(__file__).resolve().parent.parent.parent / "log"))
LOG_DIR.mkdir(parents=True, exist_ok=True)


# ── Helpers ──────────────────────────────────────────────────
def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ts_ms() -> str:
    """时间戳精确到毫秒, 方便排序."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + \
           f"{datetime.now(timezone.utc).microsecond // 1000:03d}"


def _short_id(task_id: str | None) -> str:
    if not task_id:
        return "--------"
    return task_id[:8]


# ── Structured Log Record ────────────────────────────────────
@dataclass
class TraceRecord:
    timestamp: str = ""
    level: str = "INFO"
    module: str = ""
    function: str = ""
    task_id: str = ""
    event: str = ""
    phase: str = ""          # e.g. "intent", "plan", "execute", "tool", "llm", "memory"
    message: str = ""
    elapsed_s: float | None = None
    attempt: int | None = None
    total_attempts: int | None = None
    step_index: int | None = None
    round_index: int | None = None
    tool_name: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        d = asdict(self)
        # Remove empty values for cleaner output
        d = {k: v for k, v in d.items() if v not in (None, "", 0, [], {}) or k in ("timestamp", "level", "message", "event")}
        return json.dumps(d, ensure_ascii=False, default=str)


# ── Trace Logger ─────────────────────────────────────────────
class TraceLogger:
    """结构化 trace logger, 同时输出 JSONL (结构化) 和 纯文本.

    关键设计:
    - 每个日志条目都有 task_id, 方便 grep/过滤
    - 自动记录时间戳精确到 ms
    - LLM 调用记录耗时和 token 数
    - 支持上下文管理器 (with trace.span(...)) 自动记录耗时
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self._jsonl_path: Path | None = None
        self._text_path: Path | None = None
        self._jsonl_file: Any = None
        self._text_file: Any = None
        self._last_date: str = ""
        self._lock = threading.Lock()
        self._auto_flush = True
        # 自动打开文件（使用今天的日期）
        self._rotate_if_needed()

    def _rotate_if_needed(self) -> None:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today == self._last_date and self._jsonl_file:
            return
        with self._lock:
            if today == self._last_date:
                return
            # close old
            if self._jsonl_file:
                try:
                    self._jsonl_file.close()
                except Exception:
                    pass
            if self._text_file:
                try:
                    self._text_file.close()
                except Exception:
                    pass
            # open new
            self._jsonl_path = LOG_DIR / f"flowcraft-{today}.jsonl"
            self._text_path = LOG_DIR / f"flowcraft-{today}.log"
            self._jsonl_file = open(str(self._jsonl_path), "a", encoding="utf-8")
            self._text_file = open(str(self._text_path), "a", encoding="utf-8")
            self._last_date = today

    def _emit(self, record: TraceRecord) -> None:
        self._rotate_if_needed()
        # 纯文本
        text_line = self._format_text(record)
        # JSONL
        json_line = record.to_json()
        with self._lock:
            if self._text_file:
                self._text_file.write(text_line + "\n")
                if self._auto_flush:
                    self._text_file.flush()
            if self._jsonl_file:
                self._jsonl_file.write(json_line + "\n")
                if self._auto_flush:
                    self._jsonl_file.flush()

    def _format_text(self, r: TraceRecord) -> str:
        """纯文本格式: [时间] [级别] [模块:函数] [task_id] [阶段] 消息  (耗时/尝试等)"""
        parts = [
            f"[{r.timestamp}]",
            f"[{r.level}]",
            f"[{r.module}:{r.function}]",
        ]
        if r.task_id:
            parts.append(f"[task:{_short_id(r.task_id)}]")
        if r.phase:
            parts.append(f"[phase:{r.phase}]")
        parts.append(r.message)

        extras = []
        if r.elapsed_s is not None:
            extras.append(f"elapsed={r.elapsed_s:.3f}s")
        if r.attempt is not None:
            extras.append(f"attempt={r.attempt}{'/' + str(r.total_attempts) if r.total_attempts else ''}")
        if r.step_index is not None:
            extras.append(f"step={r.step_index}")
        if r.round_index is not None:
            extras.append(f"round={r.round_index}")
        if r.tool_name:
            extras.append(f"tool={r.tool_name}")
        if r.extra:
            for k, v in r.extra.items():
                if isinstance(v, (int, float, str, bool)):
                    extras.append(f"{k}={v}")

        result = " ".join(parts)
        if extras:
            result += "  (" + ", ".join(extras) + ")"
        return result

    def _log(self, level: str, module: str, function: str, task_id: str | None,
             event: str, phase: str, message: str, **kwargs: Any) -> None:
        self._emit(TraceRecord(
            timestamp=_ts_ms(),
            level=level,
            module=module,
            function=function,
            task_id=task_id or "",
            event=event,
            phase=phase,
            message=message,
            **kwargs,
        ))

    # ── Convenience methods ──────────────────────────────

    def pipeline_begin(self, task_id: str, stage: str, message: str, **kwargs) -> None:
        """Pipeline 阶段开始.""" 
        self._log("INFO", self.name, stage, task_id, f"{stage}.begin", "pipeline",
                  f"▶ {message}", **kwargs)

    def pipeline_end(self, task_id: str, stage: str, message: str, elapsed_s: float, **kwargs) -> None:
        """Pipeline 阶段结束."""
        self._log("INFO", self.name, stage, task_id, f"{stage}.end", "pipeline",
                  f"✓ {message}", elapsed_s=elapsed_s, **kwargs)

    def pipeline_error(self, task_id: str, stage: str, message: str, exc: Exception | None = None, **kwargs) -> None:
        """Pipeline 阶段出错."""
        self._log("ERROR", self.name, stage, task_id, f"{stage}.error", "pipeline",
                  f"✗ {message}", **kwargs)

    def llm_call(self, task_id: str, function: str, message: str, **kwargs) -> None:
        """LLM 调用."""
        self._log("INFO", self.name, function, task_id, "llm.call", "llm",
                  f"🤖 LLM: {message}", **kwargs)

    def llm_result(self, task_id: str, function: str, message: str, elapsed_s: float, **kwargs) -> None:
        """LLM 返回."""
        self._log("INFO", self.name, function, task_id, "llm.result", "llm",
                  f"🤖 LLM完成: {message}", elapsed_s=elapsed_s, **kwargs)

    def llm_timeout(self, task_id: str, function: str, message: str, elapsed_s: float, **kwargs) -> None:
        """LLM 超时."""
        self._log("WARN", self.name, function, task_id, "llm.timeout", "llm",
                  f"⏰ LLM超时: {message}", elapsed_s=elapsed_s, **kwargs)

    def step_begin(self, task_id: str, step_idx: int, title: str, objective: str, **kwargs) -> None:
        """步骤开始."""
        self._log("INFO", self.name, "execute_step", task_id, "step.begin", "execute",
                  f"▶ 步骤{step_idx}: {title}", step_index=step_idx,
                  extra={"objective": objective[:200], **kwargs})

    def step_end(self, task_id: str, step_idx: int, title: str, elapsed_s: float, result: str = "", **kwargs) -> None:
        """步骤结束."""
        self._log("INFO", self.name, "execute_step", task_id, "step.end", "execute",
                  f"✓ 步骤{step_idx}完成: {title}", step_index=step_idx, elapsed_s=elapsed_s,
                  extra={"result_preview": result[:120], **kwargs})

    def step_failed(self, task_id: str, step_idx: int, title: str, reason: str, **kwargs) -> None:
        """步骤失败."""
        self._log("ERROR", self.name, "execute_step", task_id, "step.failed", "execute",
                  f"✗ 步骤{step_idx}失败: {title} — {reason}", step_index=step_idx, **kwargs)

    def tool_call(self, task_id: str, step_idx: int, round_idx: int, tool_name: str, purpose: str, **kwargs) -> None:
        """工具调用."""
        self._log("INFO", self.name, "tool_invoke", task_id, "tool.call", "tool",
                  f"🔧 调用: {tool_name} — {purpose[:120]}",
                  step_index=step_idx, round_index=round_idx, tool_name=tool_name, **kwargs)

    def tool_result(self, task_id: str, step_idx: int, round_idx: int, tool_name: str,
                    status: str, summary: str, elapsed_s: float, **kwargs) -> None:
        """工具返回."""
        self._log("INFO", self.name, "tool_invoke", task_id, "tool.result", "tool",
                  f"🔧 完成 [{status}]: {tool_name} — {summary[:120]}",
                  step_index=step_idx, round_index=round_idx, tool_name=tool_name,
                  elapsed_s=elapsed_s, **kwargs)

    def loop_detect(self, task_id: str, step_idx: int, round_idx: int, tool_name: str,
                    consecutive_failures: int, **kwargs) -> None:
        """死循环检测."""
        self._log("WARN", self.name, "loop_detect", task_id, "loop.detect", "execute",
                  f"⚠ 死循环检测: {tool_name} 连续失败{consecutive_failures}次!",
                  step_index=step_idx, round_index=round_idx, tool_name=tool_name, **kwargs)

    def approval_wait(self, task_id: str, step_idx: int, tool_name: str, reason: str, **kwargs) -> None:
        """等待审批."""
        self._log("WARN", self.name, "approval", task_id, "approval.wait", "execute",
                  f"⏸ 等待审批: {tool_name} — {reason[:120]}",
                  step_index=step_idx, tool_name=tool_name, **kwargs)

    def retry(self, task_id: str, step_idx: int, attempt: int, max_attempts: int, reason: str, **kwargs) -> None:
        """重试."""
        self._log("WARN", self.name, "retry", task_id, "step.retry", "execute",
                  f"↻ 重试 step={step_idx}, attempt={attempt}/{max_attempts}: {reason}",
                  step_index=step_idx, attempt=attempt, total_attempts=max_attempts, **kwargs)

    def info(self, task_id: str | None, event: str, message: str, **kwargs) -> None:
        """通用 info."""
        self._log("INFO", self.name, event, task_id, event, "", message, **kwargs)

    def warn(self, task_id: str | None, event: str, message: str, **kwargs) -> None:
        """通用 warn."""
        self._log("WARN", self.name, event, task_id, event, "", message, **kwargs)

    def error(self, task_id: str | None, event: str, message: str, **kwargs) -> None:
        """通用 error."""
        self._log("ERROR", self.name, event, task_id, event, "", message, **kwargs)

    def debug(self, task_id: str | None, event: str, message: str, **kwargs) -> None:
        """通用 debug."""
        self._log("DEBUG", self.name, event, task_id, event, "", message, **kwargs)


# ── Global logger registry ───────────────────────────────────
_trace_loggers: dict[str, TraceLogger] = {}
_trace_lock = threading.Lock()


def get_trace_logger(name: str) -> TraceLogger:
    """获取或创建 TraceLogger."""
    with _trace_lock:
        if name not in _trace_loggers:
            _trace_loggers[name] = TraceLogger(name)
        return _trace_loggers[name]


# ── Context manager for duration tracking ────────────────────
class TraceSpan:
    """用上下文管理器自动记录耗时.

    用法:
        trace = get_trace_logger(__name__)
        with TraceSpan(trace, task_id, "plan.generate", "LLM生成计划") as span:
            result = await llm_call()
        # 退出时自动调用 trace.pipeline_end(...)
    """

    def __init__(self, trace: TraceLogger, task_id: str, event: str, message: str,
                 phase: str = "", **extra) -> None:
        self.trace = trace
        self.task_id = task_id
        self.event = event
        self.message = message
        self.phase = phase
        self.extra = extra
        self.start = 0.0

    def __enter__(self) -> TraceSpan:
        self.start = _time.monotonic()
        self.trace.pipeline_begin(self.task_id, self.event, self.message, **self.extra)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        elapsed = _time.monotonic() - self.start
        if exc_type:
            self.trace.pipeline_error(self.task_id, self.event,
                                      f"{self.message} — 失败: {exc_val}",
                                      elapsed_s=elapsed)
        else:
            self.trace.pipeline_end(self.task_id, self.event, f"{self.message}", elapsed_s=elapsed)
        return False  # Don't suppress exceptions

    async def __aenter__(self) -> TraceSpan:
        self.start = _time.monotonic()
        self.trace.pipeline_begin(self.task_id, self.event, self.message, **self.extra)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> bool:
        elapsed = _time.monotonic() - self.start
        if exc_type:
            self.trace.pipeline_error(self.task_id, self.event,
                                      f"{self.message} — 失败: {exc_val}",
                                      elapsed_s=elapsed)
        else:
            self.trace.pipeline_end(self.task_id, self.event, f"{self.message}", elapsed_s=elapsed)
        return False


# ── Auto-install on first import ─────────────────────────────
def _install_trace_handler() -> None:
    """Inject a handler that redirects standard logger messages to trace log files.

    This ensures ALL existing logger.info/warning/error calls also appear in
    the trace log files, not just explicit trace logger calls.
    """
    root = logging.getLogger()
    if any(isinstance(h, _TraceLogHandler) for h in root.handlers):
        return  # already installed
    handler = _TraceLogHandler()
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter(
        fmt="%(asctime)s.%(msecs)03d [%(levelname)-5s] %(name)s:%(funcName)s [%(threadName)s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    ))
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)


class _TraceLogHandler(logging.Handler):
    """将标准 logging 消息也写入 trace log 文件."""
    def emit(self, record: logging.LogRecord) -> None:
        try:
            # Determine which log files to write to
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            text_path = LOG_DIR / f"flowcraft-{today}.log"
            jsonl_path = LOG_DIR / f"flowcraft-{today}.jsonl"

            text_line = self.format(record)

            with open(str(text_path), "a", encoding="utf-8") as f:
                f.write(text_line + "\n")

            # Also as JSONL (as a secondary, simpler format)
            json_record = {
                "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
                "level": record.levelname,
                "module": record.name,
                "function": record.funcName,
                "message": record.getMessage(),
                "logger": "stdlib",
            }
            with open(str(jsonl_path), "a", encoding="utf-8") as f:
                f.write(json.dumps(json_record, ensure_ascii=False, default=str) + "\n")

        except Exception:
            self.handleError(record)


# Auto-install
_install_trace_handler()
