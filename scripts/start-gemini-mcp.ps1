param(
    [ValidateSet('stdio', 'http')]
    [string]$Transport = 'stdio',
    [int]$Port = 8788,
    [string]$ListenHost = '127.0.0.1',
    [string]$AllowedHosts = '',
    [string]$TailscaleIps = '',
    [string]$PublicBaseUrl = '',
    [ValidateRange(0.01, 3600)]
    [double]$StartupWaitSeconds = 300
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'import-shared-browser-env.ps1')
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) { throw "Virtual environment missing: $pythonPath" }
$env:GEMINI_ORACLE_ROOT = $projectRoot
$env:PYTHONPATH = $projectRoot
$env:GEMINI_ORACLE_TRANSPORT = $Transport
$env:GEMINI_ORACLE_PORT = [string]$Port
$env:GEMINI_ORACLE_HOST = $ListenHost
if ($AllowedHosts) { $env:GEMINI_ORACLE_ALLOWED_HOSTS = $AllowedHosts }
if ($TailscaleIps) { $env:GEMINI_ORACLE_TAILSCALE_IPS = $TailscaleIps }
elseif (-not $env:GEMINI_ORACLE_TAILSCALE_IPS) {
    $env:GEMINI_ORACLE_TAILSCALE_IPS = [Environment]::GetEnvironmentVariable('GEMINI_ORACLE_TAILSCALE_IPS', 'User')
}
if ($PublicBaseUrl) { $env:GEMINI_ORACLE_PUBLIC_BASE_URL = $PublicBaseUrl }
if (-not $env:GEMINI_ORACLE_BEARER_TOKEN) {
    $env:GEMINI_ORACLE_BEARER_TOKEN = [Environment]::GetEnvironmentVariable('GEMINI_ORACLE_BEARER_TOKEN', 'User')
}
if ($Transport -eq 'http') {
    & (Join-Path $PSScriptRoot 'wait-mcp-listen-address.ps1') -ListenHost $ListenHost -TimeoutSeconds $StartupWaitSeconds -LogPath (Join-Path $projectRoot 'runtime\logs\gemini-startup.log')
    & (Join-Path $PSScriptRoot 'assert-mcp-port-free.ps1') -Port $Port -ListenHost $ListenHost
}
$sharedBrowser = if ($env:ORACLE_SHARED_BROWSER) { $env:ORACLE_SHARED_BROWSER } else { '1' }
if ($sharedBrowser -and $sharedBrowser -ne '0' -and $env:GEMINI_ORACLE_SKIP_CDP_WAIT -ne '1') {
    # Same shared profile as the ChatGPT broker; both tasks start at logon.
    & (Join-Path $PSScriptRoot 'wait-browser-cdp.ps1') -CdpUrl $(if ($env:ORACLE_BROWSER_CDP_URL) { $env:ORACLE_BROWSER_CDP_URL } else { 'http://127.0.0.1:9333' }) -TimeoutSeconds $StartupWaitSeconds -LogPath (Join-Path $projectRoot 'runtime\logs\gemini-startup.log')
}
Set-Location -LiteralPath $projectRoot
& $pythonPath -m chatgpt_oracle.gemini_server
exit $LASTEXITCODE
