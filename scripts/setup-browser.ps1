param(
    [int]$Port = 0,
    [string]$ChromePath = '',
    [string]$ProfilePath = '',
    [switch]$Interactive
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'import-shared-browser-env.ps1')
$projectRoot = Split-Path -Parent $PSScriptRoot
if($env:ORACLE_SHARED_BROWSER -eq '0'){
    $python=Join-Path $projectRoot '.venv\Scripts\python.exe'
    $env:CHATGPT_ORACLE_ROOT=$projectRoot
    $env:CHATGPT_BROWSER_HEADLESS='0'
    & $python -m chatgpt_oracle.cli browser-setup
    exit $LASTEXITCODE
}
$cdpUrl=if($env:ORACLE_BROWSER_CDP_URL){[uri]$env:ORACLE_BROWSER_CDP_URL}else{[uri]'http://127.0.0.1:9333'}
if($cdpUrl.Scheme -ne 'http' -or $cdpUrl.Host -notin @('127.0.0.1','localhost','::1','[::1]')){throw 'Shared Chrome CDP must use loopback HTTP.'}
if(-not $Port){$Port=$cdpUrl.Port}
if($Port -ne $cdpUrl.Port){throw 'Set ORACLE_BROWSER_CDP_URL before changing the shared CDP port.'}
$configuredProfile=if($env:ORACLE_BROWSER_PROFILE_DIR){$env:ORACLE_BROWSER_PROFILE_DIR}else{Join-Path $projectRoot 'runtime\oracle-browser-profile'}
if($ProfilePath -and [IO.Path]::GetFullPath($ProfilePath) -ne [IO.Path]::GetFullPath($configuredProfile)){throw 'Set ORACLE_BROWSER_PROFILE_DIR before changing the shared profile.'}
$ProfilePath=[IO.Path]::GetFullPath($configuredProfile)
if(-not $ChromePath){
    $ChromePath=@($env:CHATGPT_CHROME_EXECUTABLE,$env:GEMINI_CHROME_EXECUTABLE,
        'D:\Program Files\Google\Chrome\Application\chrome.exe',
        'C:\Program Files\Google\Chrome\Application\chrome.exe',
        'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe') |
        Where-Object {$_ -and (Test-Path -LiteralPath $_)} | Select-Object -First 1
}
if(-not $ChromePath -or -not (Test-Path -LiteralPath $ChromePath)){throw 'Set -ChromePath to native Chrome.'}
$listener=Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue
if($listener){
    $owners=@($listener | Select-Object -ExpandProperty OwningProcess -Unique)
    if($owners.Count -ne 1){throw 'Shared CDP port has multiple owners.'}
    $browser=Get-CimInstance Win32_Process -Filter "ProcessId = $($owners[0])"
    if($browser.Name -ne 'chrome.exe' -or -not $browser.CommandLine -or $browser.CommandLine.IndexOf($ProfilePath,[StringComparison]::OrdinalIgnoreCase) -lt 0){throw 'CDP belongs to another profile; do not launch a second browser.'}
    Write-Output "Shared native Chrome already running: loopback CDP $Port"
    return
}
$arguments=@("--remote-debugging-port=$Port",'--remote-debugging-address=127.0.0.1',
    ('--user-data-dir="'+$ProfilePath+'"'),'--no-first-run','--no-default-browser-check')
if($Interactive){
    $arguments+=@('--start-maximized','https://chatgpt.com/','https://gemini.google.com/app')
    Start-Process -FilePath $ChromePath -ArgumentList $arguments -WindowStyle Normal | Out-Null
}else{
    $arguments+=@('--start-minimized','about:blank')
    Start-Process -FilePath $ChromePath -ArgumentList $arguments -WindowStyle Hidden | Out-Null
}
Write-Output "Shared native Chrome started: loopback CDP $Port. Complete any account sign-in manually."

