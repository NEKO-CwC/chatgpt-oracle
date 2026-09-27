$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\python.exe'
if(-not (Test-Path -LiteralPath $python)) { throw "Virtual environment missing: $python" }
$env:CHATGPT_ORACLE_ROOT = $root
$env:CHATGPT_BROWSER_HEADLESS = '0'
& $python -m chatgpt_oracle.cli browser-setup

