param(
  [ValidateSet('chatgpt', 'gemini')]
  [string]$Provider = 'chatgpt',
  [int]$Port = 0,
  [string]$ListenHost = '127.0.0.1',
  [string]$TaskName = '',
  [string]$AllowedHosts = '',
  [string]$TailscaleIps = '',
  [string]$PublicBaseUrl = ''
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'import-shared-browser-env.ps1')
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\python.exe'
$prefix = $Provider.ToUpperInvariant()
if(-not $Port) { $Port = if($Provider -eq 'gemini'){8788}else{8787} }
if(-not $TaskName) { $TaskName = if($Provider -eq 'gemini'){'Gemini Oracle MCP'}else{'ChatGPT Oracle MCP'} }
$runnerName = if($Provider -eq 'gemini'){'start-gemini-mcp.ps1'}else{'start-mcp.ps1'}
$runner = Join-Path $PSScriptRoot $runnerName
if(-not (Test-Path -LiteralPath $python)) { throw "Virtual environment missing: $python" }

$tokenName = "${prefix}_ORACLE_BEARER_TOKEN"
$token = [Environment]::GetEnvironmentVariable($tokenName, 'User')
if(-not $token) {
  $bytes = New-Object byte[] 32
  $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
  $rng.GetBytes($bytes)
  $rng.Dispose()
  $token = (($bytes | ForEach-Object { $_.ToString('x2') }) -join '')
  [Environment]::SetEnvironmentVariable($tokenName, $token, 'User')
  Write-Output 'Generated a user-level bearer token for the HTTP MCP.'
}

[Environment]::SetEnvironmentVariable("${prefix}_ORACLE_ROOT", $root, 'User')
[Environment]::SetEnvironmentVariable("${prefix}_ORACLE_TRANSPORT", 'http', 'User')
[Environment]::SetEnvironmentVariable("${prefix}_ORACLE_HOST", $ListenHost, 'User')
[Environment]::SetEnvironmentVariable("${prefix}_ORACLE_PORT", [string]$Port, 'User')
if($AllowedHosts) {
  [Environment]::SetEnvironmentVariable("${prefix}_ORACLE_ALLOWED_HOSTS", $AllowedHosts, 'User')
}
if($TailscaleIps) {
  [Environment]::SetEnvironmentVariable("${prefix}_ORACLE_TAILSCALE_IPS", $TailscaleIps, 'User')
}
if($PublicBaseUrl) {
  [Environment]::SetEnvironmentVariable("${prefix}_ORACLE_PUBLIC_BASE_URL", $PublicBaseUrl, 'User')
}
if($env:ORACLE_SHARED_BROWSER -ne '0') {
  $sharedCdp = if($env:ORACLE_BROWSER_CDP_URL){$env:ORACLE_BROWSER_CDP_URL}else{'http://127.0.0.1:9333'}
  $sharedProfile = if($env:ORACLE_BROWSER_PROFILE_DIR){$env:ORACLE_BROWSER_PROFILE_DIR}else{Join-Path $root 'runtime\oracle-browser-profile'}
  [Environment]::SetEnvironmentVariable("${prefix}_BROWSER_CDP_URL", $sharedCdp, 'User')
  [Environment]::SetEnvironmentVariable("${prefix}_PROFILE_DIR", $sharedProfile, 'User')
} elseif($Provider -eq 'chatgpt') {
  [Environment]::SetEnvironmentVariable('CHATGPT_BROWSER_CDP_URL', 'http://127.0.0.1:9222', 'User')
  [Environment]::SetEnvironmentVariable('CHATGPT_CHROME_EXECUTABLE', 'D:\Program Files\Google\Chrome\Application\chrome.exe', 'User')
}

$user = "$env:USERDOMAIN\$env:USERNAME"
$taskRun = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$runner`" -Transport http -Port $Port -ListenHost `"$ListenHost`""
if($AllowedHosts) { $taskRun += " -AllowedHosts `"$AllowedHosts`"" }
if($TailscaleIps) { $taskRun += " -TailscaleIps `"$TailscaleIps`"" }
if($PublicBaseUrl) { $taskRun += " -PublicBaseUrl `"$PublicBaseUrl`"" }
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $taskRun -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$taskSettings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -RestartCount 10 -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $taskSettings -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName
Write-Output "Registered and started scheduled task: $TaskName"
Write-Output "MCP listener: ${ListenHost}:$Port"
Write-Output "Listen address: ${ListenHost}:$Port"
if($AllowedHosts) { Write-Output "Allowed MCP Host(s): $AllowedHosts" }
Write-Output "Bearer token is stored in the current user's environment; it is not printed."
if($TailscaleIps) { Write-Output "Direct Tailscale access requires no Bearer; local address(es): $TailscaleIps" }
