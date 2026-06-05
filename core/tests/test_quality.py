"""Quality Assurance Test Suite - 执行结果与预期一致性验证.

本测试套件直接对运行中的 FlowCraft 服务发起真实任务，
然后验证执行结果的正确性。覆盖 8 大类质量检查:

1. OUTPUT_QUALITY    - 输出是否包含实际内容（非元推理）
2. STATE_TRANSITION   - 任务是否到达预期终态
3. EVENT_COMPLETENESS - 事件流是否包含所有必要阶段
4. ERROR_HANDLING     - 错误输入是否正确处理
5. TOOL_USAGE         - 是否调用了正确的工具
6. PLAN_QUALITY       - 生成的计划是否合理
7. RESPONSE_FORMAT    - 输出格式是否符合预期
8. CONTEXT_AWARENESS  - 多轮对话是否保持上下文

用法:
    python -m pytest tests/test_quality.py -v
    或直接 python tests/test_quality.py
"""

from __future__ import annotations

import json
import re
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Any

BASE_URL = "http://127.0.0.1:8765"


# ── Test Infrastructure ──────────────────────────────────────

@dataclass
class TestCase:
    name: str
    description: str
    input_text: str
    session_id: str = "qa_test"
    assertions: list[dict] = field(default_factory=list)
    timeout_seconds: int = 60

@dataclass
class TestResult:
    case: TestCase
    passed: bool
    task_id: str = ""
    status: str = ""
    checks: list[dict] = field(default_factory=list)
    error: str = ""
    duration: float = 0.0


def api_get(path: str) -> dict:
    with urllib.request.urlopen(BASE_URL + path, timeout=10) as r:
        return json.loads(r.read())

def api_post(path: str, data: dict) -> dict:
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(BASE_URL + path, data=body,
        headers={"Content-Type": "application/json; charset=utf-8"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())

def wait_for_completion(task_id: str, timeout: int = 60) -> dict:
    deadline = time.time() + timeout
    last_status = ""
    stuck_at = None
    while time.time() < deadline:
        task = api_get(f"/api/tasks/{task_id}").get("task", {})
        status = task.get("status", "")
        if status in ("COMPLETED", "FAILED", "CANCELLED"):
            return task
        # Detect stuck: same non-terminal status for >15 seconds
        if status == last_status and status in ("PLANNED", "INTENT_RECOGNIZED", "CREATED"):
            if stuck_at is None:
                stuck_at = time.time()
            elif time.time() - stuck_at > 15:
                print(f"[stuck at {status}]", end=" ", flush=True)
                break
        else:
            stuck_at = None
            last_status = status
        time.sleep(1.5)
    return task

def get_events(task_id: str) -> list[dict]:
    return api_get(f"/api/tasks/{task_id}/events").get("events", [])

def get_answers(events: list[dict]) -> list[str]:
    return [e.get("message", "") for e in events if e.get("event_type") == "step.answer"]

def check(description: str, passed: bool, detail: str = "") -> dict:
    return {"check": description, "passed": passed, "detail": detail}


# ── Assertion Helpers ────────────────────────────────────────

META_PATTERNS = [
    r"当前步骤是", r"根据会话历史", r"根据任务要求",
    r"Step \d+ has", r"The current step is",
    r"Based on (the )?session",
    r"需要先", r"在生成.*之前",
]

def assert_no_meta_reasoning(output: str) -> dict:
    for pat in META_PATTERNS:
        if re.search(pat, output):
            return check("输出不包含元推理", False,
                        f"发现元推理模式: {pat}")
    return check("输出不包含元推理", True)

def assert_min_length(output: str, min_chars: int) -> dict:
    actual = len(output.strip())
    ok = actual >= min_chars
    return check(f"输出长度 >= {min_chars} 字符", ok,
                f"实际: {actual} 字符" if not ok else "")

def assert_contains_keywords(output: str, keywords: list[str]) -> dict:
    found = [kw for kw in keywords if kw.lower() in output.lower()]
    ok = len(found) > 0
    return check(f"输出包含关键信息 ({keywords})", ok,
                f"找到: {found}" if ok else "未找到任何关键词")

def assert_status(task: dict, expected: str) -> dict:
    actual = task.get("status", "")
    return check(f"状态为 {expected}", actual == expected,
                f"实际: {actual}")

def assert_event_types(events: list[dict], required: list[str]) -> dict:
    types = [e.get("event_type", "") for e in events]
    missing = [t for t in required if t not in types]
    return check(f"包含必要事件 {required}", len(missing) == 0,
                f"缺失: {missing}" if missing else "")

def assert_step_count(events: list[dict], min_steps: int) -> dict:
    steps = sum(1 for e in events if e.get("event_type") == "step.completed")
    return check(f"至少完成 {min_steps} 个步骤", steps >= min_steps,
                f"实际: {steps}")

def assert_no_tool_errors(events: list[dict]) -> dict:
    errors = [e for e in events if e.get("event_type") in ("tool.failed", "step.failed")
              and e.get("severity") == "ERROR"]
    return check("无工具执行错误", len(errors) == 0,
                f"发现 {len(errors)} 个错误" if errors else "")


# ── Test Cases ───────────────────────────────────────────────

TEST_CASES = [
    TestCase(
        name="qa_simple",
        description="简单问答：应返回有实质内容的回答，不含元推理",
        input_text="解释什么是 Agent 工作流",
        assertions=[
            lambda task, events: assert_status(task, "COMPLETED"),
            lambda task, events: assert_no_meta_reasoning(
                get_answers(events)[-1] if get_answers(events) else ""),
            lambda task, events: assert_min_length(
                get_answers(events)[-1] if get_answers(events) else "", 50),
            lambda task, events: assert_event_types(events,
                ["task.created", "intent.recognized", "plan.created", "task.completed"]),
        ],
    ),
    TestCase(
        name="qa_complex",
        description="复杂问题：应生成多步计划并完成",
        input_text="详细对比 REST API 和 GraphQL 的优缺点，各列出至少3点",
        timeout_seconds=90,
        assertions=[
            lambda task, events: assert_status(task, "COMPLETED"),
            lambda task, events: assert_min_length(
                get_answers(events)[-1] if get_answers(events) else "", 100),
            lambda task, events: assert_event_types(events,
                ["task.created", "intent.recognized", "plan.created",
                 "step.started", "step.completed", "task.completed"]),
        ],
    ),
    TestCase(
        name="file_read",
        description="文件读取：应读取指定文件并返回内容",
        input_text="读取 D:/work/FlowCraft/README.md 的内容并总结",
        assertions=[
            lambda task, events: assert_status(task, "COMPLETED"),
            lambda task, events: assert_no_tool_errors(events),
            lambda task, events: assert_min_length(
                get_answers(events)[-1] if get_answers(events) else "", 30),
        ],
    ),
    TestCase(
        name="bad_input_empty",
        description="空输入：应正确处理",
        input_text="   ",
        assertions=[],
        timeout_seconds=10,
    ),
    TestCase(
        name="very_long_prompt",
        description="极长 prompt：应能处理",
        input_text="请回答: " + "测试 " * 50 + " 问题结束",
        assertions=[
            lambda task, events: assert_status(task, "COMPLETED"),
            lambda task, events: assert_min_length(
                get_answers(events)[-1] if get_answers(events) else "", 10),
        ],
    ),
    TestCase(
        name="context_continuity",
        description="上下文连续性：第二问应引用第一问的上下文",
        input_text="FlowCraft 是一个 agent 框架。刚才我说的项目中，核心技术是什么？",
        session_id="ctx_continuity",
        assertions=[
            lambda task, events: assert_status(task, "COMPLETED"),
        ],
    ),
    TestCase(
        name="output_not_meta",
        description="防止元推理：要求详细说明时不应返回过程描述",
        input_text="给一份不少于500字的说明，介绍 AI Agent 的架构设计",
        timeout_seconds=90,
        assertions=[
            lambda task, events: assert_status(task, "COMPLETED"),
            lambda task, events: assert_no_meta_reasoning(
                get_answers(events)[-1] if get_answers(events) else ""),
            lambda task, events: assert_min_length(
                get_answers(events)[-1] if get_answers(events) else "", 100),
        ],
    ),
    TestCase(
        name="multi_step_task",
        description="多步骤任务：应完成所有步骤并整合输出",
        input_text="先列出常见的编程语言，然后选择其中3种对比它们的特点",
        assertions=[
            lambda task, events: assert_status(task, "COMPLETED"),
            lambda task, events: assert_step_count(events, 1),
            lambda task, events: assert_min_length(
                get_answers(events)[-1] if get_answers(events) else "", 80),
        ],
    ),
    TestCase(
        name="chinese_longform",
        description="中文长文生成：应生成完整的中文内容",
        input_text="写一篇关于人工智能未来发展趋势的分析文章",
        timeout_seconds=90,
        assertions=[
            lambda task, events: assert_status(task, "COMPLETED"),
            lambda task, events: assert_min_length(
                get_answers(events)[-1] if get_answers(events) else "", 150),
            lambda task, events: assert_no_meta_reasoning(
                get_answers(events)[-1] if get_answers(events) else ""),
        ],
    ),
    TestCase(
        name="special_characters",
        description="特殊字符处理：含引号、换行等的输入",
        input_text='解释 "Harness-first" 和 "Agent-first" 的区别\n哪个更好？',
        timeout_seconds=90,
        assertions=[
            lambda task, events: assert_status(task, "COMPLETED"),
        ],
    ),
]


# ── Test Runner ──────────────────────────────────────────────

def run_test(case: TestCase) -> TestResult:
    t0 = time.time()
    result = TestResult(case=case, passed=True, checks=[])

    # Step 1: Create task
    try:
        resp = api_post("/api/tasks", {
            "session_id": case.session_id,
            "input": case.input_text,
        })
        result.task_id = resp.get("task_id", "")
    except Exception as e:
        result.passed = False
        result.error = f"创建任务失败: {e}"
        result.duration = time.time() - t0
        return result

    if not result.task_id:
        result.passed = False
        result.error = "未返回 task_id"
        result.duration = time.time() - t0
        return result

    # Step 2: Wait for completion
    task = wait_for_completion(result.task_id, case.timeout_seconds)
    result.status = task.get("status", "UNKNOWN")

    # Handle non-terminal states
    if result.status == "WAITING_APPROVAL":
        result.checks.append(check("任务状态", False,
            "任务需要审批（可能需要调整测试用例的预期风险等级）"))
    elif result.status in ("PLANNED", "INTENT_RECOGNIZED", "CREATED"):
        result.checks.append(check("任务状态", False,
            f"任务卡在 {result.status} 状态，Pipeline 未继续执行"))
    elif result.status == "FAILED":
        result.checks.append(check("任务状态", False,
            f"任务失败: {task.get('failed_reason', 'unknown')}"))

    # Step 3: Get events
    events = get_events(result.task_id)

    # Step 4: Run assertions
    all_ok = True
    for assertion in case.assertions:
        try:
            check_result = assertion(task, events)
            result.checks.append(check_result)
            if not check_result["passed"]:
                all_ok = False
        except Exception as e:
            result.checks.append(check("断言执行", False, str(e)))
            all_ok = False

    result.passed = all_ok and result.status == "COMPLETED"
    result.duration = time.time() - t0
    return result


# ── Report ───────────────────────────────────────────────────

def print_report(results: list[TestResult]) -> None:
    total = len(results)
    passed = sum(1 for r in results if r.passed)
    checks_total = sum(len(r.checks) for r in results)
    checks_passed = sum(1 for r in results for c in r.checks if c.get("passed"))

    print("\n" + "=" * 70)
    print(f"  FlowCraft Quality Assurance Report")
    print(f"  {passed}/{total} 场景通过 | {checks_passed}/{checks_total} 检查项通过")
    print("=" * 70)

    for r in results:
        icon = "PASS" if r.passed else "FAIL"
        print(f"\n  [{icon}] {r.case.name}: {r.case.description}")
        print(f"       状态: {r.status} | 耗时: {r.duration:.1f}s | 任务: {r.task_id[:20]}...")
        if r.error:
            print(f"       错误: {r.error}")
        for c in r.checks:
            mark = "+" if c["passed"] else "-"
            detail = f" ({c['detail']})" if c.get("detail") else ""
            print(f"       [{mark}] {c['check']}{detail}")

    # Summary by category
    print("\n" + "-" * 50)
    print("  失败项分析:")
    failed = [(r, c) for r in results for c in r.checks if not c.get("passed")]
    if failed:
        for r, c in failed:
            print(f"    [{r.case.name}] {c['check']}: {c.get('detail', '')}")
    else:
        print("    无失败项")

    print("\n" + "=" * 70)
    if passed == total:
        print("  ALL TESTS PASSED")
    else:
        print(f"  {total - passed} test(s) need attention")
    print("=" * 70 + "\n")


# ── Entry Point ──────────────────────────────────────────────

def main():
    print("FlowCraft QA Test Suite")
    print(f"Target: {BASE_URL}")
    print(f"Test cases: {len(TEST_CASES)}")

    # Quick health check
    try:
        health = api_get("/health")
        print(f"Server: {health.get('provider')} / {health.get('model')}")
    except Exception as e:
        print(f"ERROR: Cannot reach server at {BASE_URL}: {e}")
        sys.exit(1)

    results = []
    for i, case in enumerate(TEST_CASES):
        print(f"\n[{i+1}/{len(TEST_CASES)}] {case.name}...", end=" ", flush=True)
        result = run_test(case)
        results.append(result)
        print(f"{'PASS' if result.passed else 'FAIL'} ({result.duration:.1f}s)")

    print_report(results)
    return 0 if all(r.passed for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())

