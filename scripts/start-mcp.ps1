param(
  [ValidateSet('stdio', 'http')]
  [string]$Transport = 'stdio',
  [int]$Port = 8787,
  [string]$ListenHost = '',
  [string]$BearerToken = '',
  [string]$AllowedHosts = '',
  [string]$TailscaleIps = '',
  [string]$PublicBaseUrl = '',
  [ValidateRange(0.01, 3600)]
  [double]$StartupWaitSeconds = 300
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'import-shared-browser-env.ps1')
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\python.exe'
if(-not (Test-Path -LiteralPath $python)) { throw "Virtual environment missing: $python" }
$env:CHATGPT_ORACLE_ROOT = $root
$env:PYTHONPATH = $root
$env:CHATGPT_BROWSER_CDP_URL = if($env:CHATGPT_BROWSER_CDP_URL){$env:CHATGPT_BROWSER_CDP_URL}else{'http://127.0.0.1:9222'}
$env:CHATGPT_ORACLE_TRANSPORT = $Transport
$env:CHATGPT_ORACLE_PORT = [string]$Port
if($ListenHost) { $env:CHATGPT_ORACLE_HOST = $ListenHost }
if($BearerToken) { $env:CHATGPT_ORACLE_BEARER_TOKEN = $BearerToken }
if(-not $env:CHATGPT_ORACLE_BEARER_TOKEN) {
  $env:CHATGPT_ORACLE_BEARER_TOKEN = [Environment]::GetEnvironmentVariable('CHATGPT_ORACLE_BEARER_TOKEN', 'User')
}
if($AllowedHosts) { $env:CHATGPT_ORACLE_ALLOWED_HOSTS = $AllowedHosts }
if($TailscaleIps) { $env:CHATGPT_ORACLE_TAILSCALE_IPS = $TailscaleIps }
elseif(-not $env:CHATGPT_ORACLE_TAILSCALE_IPS) {
  $env:CHATGPT_ORACLE_TAILSCALE_IPS = [Environment]::GetEnvironmentVariable('CHATGPT_ORACLE_TAILSCALE_IPS', 'User')
}
if($PublicBaseUrl) { $env:CHATGPT_ORACLE_PUBLIC_BASE_URL = $PublicBaseUrl }
$sharedBrowser = if($env:ORACLE_SHARED_BROWSER){$env:ORACLE_SHARED_BROWSER}else{'1'}
if($sharedBrowser -and $sharedBrowser -ne '0' -and $env:CHATGPT_ORACLE_SKIP_CDP_WAIT -ne '1') {
  # Profiles with four resident Sessions cannot sit behind a 60 s client
  # timeout while the first browser call times out. Wait for a debuggable CDP
  # endpoint up front; the WMI/network readiness still gates the listener.
  & (Join-Path $PSScriptRoot 'wait-browser-cdp.ps1') -CdpUrl $(if($env:ORACLE_BROWSER_CDP_URL){$env:ORACLE_BROWSER_CDP_URL}else{'http://127.0.0.1:9333'}) -TimeoutSeconds $StartupWaitSeconds -LogPath (Join-Path $root 'runtime\logs\chatgpt-startup.log')
}
if($Transport -eq 'http') {
  $effectiveListenHost=if($env:CHATGPT_ORACLE_HOST){$env:CHATGPT_ORACLE_HOST}else{'127.0.0.1'}
  & (Join-Path $PSScriptRoot 'wait-mcp-listen-address.ps1') -ListenHost $effectiveListenHost -TimeoutSeconds $StartupWaitSeconds -LogPath (Join-Path $root 'runtime\logs\chatgpt-startup.log')
  & (Join-Path $PSScriptRoot 'assert-mcp-port-free.ps1') -Port $Port -ListenHost $effectiveListenHost
}
Set-Location -LiteralPath $root
& $python -m chatgpt_oracle.server
exit $LASTEXITCODE
