$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$core = Join-Path $root "core"
Set-Location $core
$env:PYTHONPATH = $core
python -m flowcraft_core.simple_server

