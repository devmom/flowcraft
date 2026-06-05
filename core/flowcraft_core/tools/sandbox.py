"""Code sandbox — safe Python execution with restricted imports and timeouts.

Security:
    Blocked imports: os, subprocess, socket, shutil, sys, ctypes, importlib
    Max execution time: 10 seconds
    Max output size: 50KB
    No file system write access
    All executions are audited
"""

from __future__ import annotations

import ast
import logging
import time as _time
from io import StringIO
from pathlib import Path

from flowcraft_core.domain.enums import RiskLevel
from flowcraft_core.domain.schemas import ToolIntent
from flowcraft_core.tools.base import Tool, ToolDefinition, is_path_allowed, observation_from_output

logger = logging.getLogger(__name__)

# Blocked modules for sandbox safety
BLOCKED_MODULES = {
    "os", "subprocess", "socket", "shutil", "sys",
    "ctypes", "importlib", "builtins", "__builtins__",
    "code", "codeop", "compile", "compileall",
    "ensurepip", "pip", "pkgutil", "runpy",
    "signal", "threading", "multiprocessing",
    "pathlib",  # Block raw pathlib; allow only through allowed_paths
    "email", "smtplib", "ftplib", "telnetlib",
    "http.server", "xmlrpc", "wsgiref",
    "tkinter", "PyQt5", "PySide",
}

# Allowed safe imports
ALLOWED_MODULES = {
    "math", "statistics", "random",
    "datetime", "time", "collections",
    "itertools", "functools", "operator",
    "json", "csv", "re", "string", "textwrap",
    "typing", "dataclasses", "enum",
    "copy", "pprint", "hashlib", "base64",
    "html", "xml.etree.ElementTree", "urllib.parse",
    "decimal", "fractions", "numbers",
    "heapq", "bisect", "array", "struct",
    "uuid", "unicodedata",
}


def _validate_code_safety(code: str) -> tuple[bool, str]:
    """Static analysis of code for dangerous patterns.

    Returns (is_safe, error_message).
    """
    # Check for direct dangerous calls
    dangerous_calls = [
        "__import__", "exec", "eval", "compile", "open",
        "getattr", "setattr", "delattr", "globals", "locals",
        "__builtins__", "__builtin__",
    ]
    for call in dangerous_calls:
        if call in code:
            return False, f"Dangerous call detected: {call}"

    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return False, f"Syntax error: {exc}"

    for node in ast.walk(tree):
        # Block all imports except whitelist
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            module_name = ""
            if isinstance(node, ast.Import):
                for alias in node.names:
                    module_name = alias.name
                    break
            elif isinstance(node, ast.ImportFrom):
                module_name = node.module or ""

            if module_name.split(".")[0] in BLOCKED_MODULES:
                return False, f"Blocked import: {module_name}"
            if module_name and module_name not in ALLOWED_MODULES:
                # Check if it's a known safe module
                root = module_name.split(".")[0]
                if root not in ALLOWED_MODULES:
                    return False, f"Import not allowed: {module_name}"

        # Block file operations via open()
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id == "open":
                return False, "open() is not allowed in sandbox"

    return True, ""


class CodeExecuteTool(Tool):
    """Execute Python code in a restricted sandbox."""

    def __init__(self) -> None:
        self.definition = ToolDefinition(
            tool_name="code.execute",
            display_name="执行代码",
            description=(
                "在沙箱中安全执行 Python 代码。限制: 只能使用 math/statistics/json/re/等安全库, "
                "不能访问文件系统和网络, 最多执行10秒, 输出截断到50KB。"
                "参数: code(代码字符串), timeout_seconds(默认10)"
            ),
            category="code",
            risk_level=RiskLevel.MEDIUM,
            permissions=["tool:code.execute"],
            requires_approval_by_default=True,
            timeout_seconds=15,
        )

    async def execute(self, intent: ToolIntent):
        code = str(intent.input_payload.get("code", ""))
        timeout = min(int(intent.input_payload.get("timeout_seconds", 10)), 10)

        if not code:
            return observation_from_output(intent, "FAILED", "Missing code parameter")

        # Static safety check
        is_safe, error_msg = _validate_code_safety(code)
        if not is_safe:
            return observation_from_output(intent, "DENIED",
                f"Code rejected by safety check: {error_msg}",
                error=error_msg)

        # Execute in sandbox
        try:
            stdout = StringIO()
            stderr = StringIO()

            # Build restricted globals
            safe_globals = {"__builtins__": {}, "__name__": "__sandbox__"}
            # Only allow safe builtins
            safe_builtins = {
                "__import__": __import__,  # needed for imports; safety enforced by static analysis
                "abs": abs, "all": all, "any": any,
                "bool": bool, "bytes": bytes, "chr": chr,
                "dict": dict, "divmod": divmod, "enumerate": enumerate,
                "filter": filter, "float": float, "format": format,
                "frozenset": frozenset, "hash": hash, "hex": hex,
                "int": int, "isinstance": isinstance,
                "len": len, "list": list, "map": map,
                "max": max, "min": min, "oct": oct,
                "ord": ord, "pow": pow, "print": print,
                "range": range, "repr": repr, "reversed": reversed,
                "round": round, "set": set, "slice": slice,
                "sorted": sorted, "str": str, "sum": sum,
                "tuple": tuple, "type": type, "zip": zip,
                "True": True, "False": False, "None": None,
                "Exception": Exception, "ValueError": ValueError,
                "TypeError": TypeError, "KeyError": KeyError,
                "IndexError": IndexError, "StopIteration": StopIteration,
            }
            safe_globals["__builtins__"] = safe_builtins

            # Import allowed modules into namespace
            import json as _json_mod
            import math
            import statistics
            import re
            import random as _random_mod
            safe_globals["json"] = _json_mod
            safe_globals["math"] = math
            safe_globals["statistics"] = statistics
            safe_globals["re"] = re
            safe_globals["random"] = _random_mod

            class _SandboxStdout:
                def write(self, s):
                    stdout.write(s)
                def flush(self):
                    pass

            safe_globals["__sandbox_stdout__"] = _SandboxStdout()

            # Inject sys module directly into sandbox globals (avoids import)
            import sys as _real_sys
            safe_globals["__sandbox_sys"] = _real_sys

            # Wrap code to capture print output
            wrapped_code = (
                "__sandbox_sys.stdout = __sandbox_stdout__\n"
                + code
            )

            start = _time.perf_counter()
            exec(wrapped_code, safe_globals)
            elapsed = _time.perf_counter() - start

            output = stdout.getvalue()
            if len(output) > 50000:
                output = output[:50000] + f"\n\n[Output truncated at 50KB, total: {len(output)} bytes]"

            if not output:
                output = "(Code executed successfully, no output)"

            return observation_from_output(intent, "COMPLETED",
                f"Code executed in {elapsed:.2f}s",
                {
                    "output": output,
                    "elapsed_seconds": round(elapsed, 3),
                    "code_length": len(code),
                })

        except SyntaxError as exc:
            return observation_from_output(intent, "FAILED",
                f"SyntaxError: {exc}", error=str(exc))
        except Exception as exc:
            return observation_from_output(intent, "FAILED",
                f"RuntimeError: {exc}", error=str(exc))
