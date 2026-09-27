param(
  [int]$Port = 8787,
  [string]$TaskName = 'ChatGPT Oracle MCP',
  [string]$AllowedHosts = ''
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\python.exe'
$runner = Join-Path $PSScriptRoot 'start-mcp.ps1'
if(-not (Test-Path -LiteralPath $python)) { throw "Virtual environment missing: $python" }

$token = [Environment]::GetEnvironmentVariable('CHATGPT_ORACLE_BEARER_TOKEN', 'User')
if(-not $token) {
  $bytes = New-Object byte[] 32
  $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
  $rng.GetBytes($bytes)
  $rng.Dispose()
  $token = (($bytes | ForEach-Object { $_.ToString('x2') }) -join '')
  [Environment]::SetEnvironmentVariable('CHATGPT_ORACLE_BEARER_TOKEN', $token, 'User')
  Write-Output 'Generated a user-level bearer token for the HTTP MCP.'
}

[Environment]::SetEnvironmentVariable('CHATGPT_ORACLE_ROOT', $root, 'User')
[Environment]::SetEnvironmentVariable('CHATGPT_ORACLE_TRANSPORT', 'http', 'User')
[Environment]::SetEnvironmentVariable('CHATGPT_ORACLE_HOST', '127.0.0.1', 'User')
[Environment]::SetEnvironmentVariable('CHATGPT_ORACLE_PORT', [string]$Port, 'User')
if($AllowedHosts) {
  [Environment]::SetEnvironmentVariable('CHATGPT_ORACLE_ALLOWED_HOSTS', $AllowedHosts, 'User')
}
[Environment]::SetEnvironmentVariable('CHATGPT_BROWSER_CDP_URL', 'http://127.0.0.1:9222', 'User')
[Environment]::SetEnvironmentVariable('CHATGPT_CHROME_EXECUTABLE', 'D:\Program Files\Google\Chrome\Application\chrome.exe', 'User')

$user = "$env:USERDOMAIN\$env:USERNAME"
$taskRun = "powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$runner`" -Transport http -Port $Port"
if($AllowedHosts) { $taskRun += " -AllowedHosts `"$AllowedHosts`"" }
& schtasks.exe /Create /TN $TaskName /TR $taskRun /SC ONLOGON /RL LIMITED /F | Out-Null
if($LASTEXITCODE -ne 0) { throw "schtasks could not register the HTTP MCP task" }
& schtasks.exe /Run /TN $TaskName | Out-Null
Write-Output "Registered and started scheduled task: $TaskName"
Write-Output "Local MCP: http://127.0.0.1:$Port/mcp"
if($AllowedHosts) { Write-Output "Allowed MCP Host(s): $AllowedHosts" }
Write-Output "Bearer token is stored in the current user's environment; it is not printed."
