# FlowCraft Development

## Current MVP Scaffold

Implemented:

- FastAPI local API skeleton
- SQLite schema initialization
- Structured domain models
- Trace event recording
- Deterministic development Model Gateway
- Intent Engine
- Direct and Linear Planner
- Plan Validator
- Policy Engine
- Approval Manager
- Tool Registry and Tool Harness
- Built-in file read, file write, and command tools
- Runtime path from task creation to intent, plan, policy, and completion or approval

## Setup

```powershell
cd D:\work\FlowCraft\core
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .[dev]
```

If PyPI SSL access fails, retry after fixing local network or certificate interception.

## Run API

Zero-dependency stdlib API, recommended until external dependencies are available:

```powershell
cd D:\work\FlowCraft
.\scripts\dev-api-stdlib.ps1
```

FastAPI API, after dependencies are installed:

```powershell
cd D:\work\FlowCraft
.\scripts\dev-api.ps1
```

Health check:

```text
GET http://127.0.0.1:8765/health
```

Create task:

```text
POST http://127.0.0.1:8765/api/tasks
{
  "session_id": "default",
  "input": "解释 FlowCraft 是什么"
}
```

PowerShell 5 may send Chinese JSON bodies with a legacy encoding. For Chinese input, send UTF-8 bytes:

```powershell
$json = @{ session_id = "default"; input = "解释 FlowCraft 是什么" } | ConvertTo-Json
$bytes = [System.Text.Encoding]::UTF8.GetBytes($json)
Invoke-RestMethod -Uri http://127.0.0.1:8765/api/tasks -Method Post -ContentType "application/json; charset=utf-8" -Body $bytes
```

## Verify

```powershell
cd D:\work\FlowCraft\core
python -m compileall flowcraft_core
.\.venv\Scripts\python.exe -m pytest
```
