$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$core = Join-Path $root "core"
Set-Location $core
python -m uvicorn flowcraft_core.api.server:app --host 127.0.0.1 --port 8765 --reload

