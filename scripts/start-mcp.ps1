param(
  [ValidateSet('stdio', 'http')]
  [string]$Transport = 'stdio',
  [int]$Port = 8787,
  [string]$BearerToken = '',
  [string]$AllowedHosts = ''
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\python.exe'
if(-not (Test-Path -LiteralPath $python)) { throw "Virtual environment missing: $python" }
$env:CHATGPT_ORACLE_ROOT = $root
$env:PYTHONPATH = $root
$env:CHATGPT_BROWSER_CDP_URL = if($env:CHATGPT_BROWSER_CDP_URL){$env:CHATGPT_BROWSER_CDP_URL}else{'http://127.0.0.1:9222'}
$env:CHATGPT_ORACLE_TRANSPORT = $Transport
$env:CHATGPT_ORACLE_PORT = [string]$Port
if($BearerToken) { $env:CHATGPT_ORACLE_BEARER_TOKEN = $BearerToken }
if($AllowedHosts) { $env:CHATGPT_ORACLE_ALLOWED_HOSTS = $AllowedHosts }
Set-Location -LiteralPath $root
& $python -m chatgpt_oracle.server
