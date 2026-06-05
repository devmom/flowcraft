"""P1: Code Sandbox Tests — safety validation + execution."""

from __future__ import annotations

import asyncio

import pytest

from flowcraft_core.tools.sandbox import (
    CodeExecuteTool, _validate_code_safety, BLOCKED_MODULES, ALLOWED_MODULES,
)
from flowcraft_core.domain.schemas import ToolIntent


def make_intent(code: str) -> ToolIntent:
    return ToolIntent(
        task_id="t_sand", step_id="s1", tool_name="code.execute",
        purpose="test", input_summary="run code",
        input_payload={"code": code},
        expected_result="output",
    )


class TestCodeSafetyValidator:
    """Static analysis safety checks."""

    @pytest.mark.unit
    def test_safe_math_code_passes(self) -> None:
        ok, err = _validate_code_safety("result = 2 + 2\nprint(result)")
        assert ok is True
        assert err == ""

    @pytest.mark.unit
    def test_blocks_eval(self) -> None:
        ok, err = _validate_code_safety("eval('2+2')")
        assert ok is False
        assert "eval" in err.lower()

    @pytest.mark.unit
    def test_blocks_exec(self) -> None:
        ok, err = _validate_code_safety("exec('print(1)')")
        assert ok is False
        assert "exec" in err.lower()

    @pytest.mark.unit
    def test_blocks_os_import(self) -> None:
        ok, err = _validate_code_safety("import os\nos.system('dir')")
        assert ok is False
        assert "os" in err.lower()

    @pytest.mark.unit
    def test_blocks_subprocess_import(self) -> None:
        ok, err = _validate_code_safety("from subprocess import run")
        assert ok is False

    @pytest.mark.unit
    def test_blocks_open_call(self) -> None:
        ok, err = _validate_code_safety("open('/etc/passwd')")
        assert ok is False
        assert "open" in err.lower()

    @pytest.mark.unit
    def test_blocks_getattr_dangerous(self) -> None:
        ok, err = _validate_code_safety("getattr(os, 'system')")
        assert ok is False

    @pytest.mark.unit
    def test_allows_safe_import(self) -> None:
        ok, err = _validate_code_safety("import math\nprint(math.pi)")
        assert ok is True

    @pytest.mark.unit
    def test_syntax_error_detected(self) -> None:
        ok, err = _validate_code_safety("print( ")
        assert ok is False
        assert "Syntax" in err

    @pytest.mark.unit
    def test_blocks_unknown_import(self) -> None:
        ok, err = _validate_code_safety("import requests")
        assert ok is False


class TestCodeExecution:
    """Actual sandbox execution."""

    @pytest.mark.component
    def test_execute_simple_code(self) -> None:
        tool = CodeExecuteTool()
        intent = make_intent("print(2 + 2)")
        obs = asyncio.run(tool.execute(intent))
        assert obs.status == "COMPLETED"
        assert "4" in obs.output_payload.get("output", "")

    @pytest.mark.component
    def test_execute_with_json_import(self) -> None:
        tool = CodeExecuteTool()
        code = "import json\ndata = {'key': 'value'}\nprint(json.dumps(data))"
        intent = make_intent(code)
        obs = asyncio.run(tool.execute(intent))
        assert obs.status == "COMPLETED"

    @pytest.mark.component
    def test_execute_blocked_code_denied(self) -> None:
        tool = CodeExecuteTool()
        intent = make_intent("import os")
        obs = asyncio.run(tool.execute(intent))
        assert obs.status == "DENIED"

    @pytest.mark.component
    def test_execute_empty_code_fails(self) -> None:
        tool = CodeExecuteTool()
        intent = make_intent("")
        obs = asyncio.run(tool.execute(intent))
        assert obs.status == "FAILED"

    @pytest.mark.component
    def test_execute_syntax_error(self) -> None:
        tool = CodeExecuteTool()
        intent = make_intent("print(")
        obs = asyncio.run(tool.execute(intent))
        assert obs.status in ("FAILED", "DENIED")

    @pytest.mark.unit
    def test_sandbox_definition_requires_approval(self) -> None:
        tool = CodeExecuteTool()
        assert tool.definition.requires_approval_by_default is True
        assert tool.definition.tool_name == "code.execute"
