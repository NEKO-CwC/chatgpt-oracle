param(
    [int]$Port = 0,
    [string]$ChromePath = $env:GEMINI_CHROME_EXECUTABLE,
    [string]$ProfilePath = $env:GEMINI_PROFILE_DIR
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'import-shared-browser-env.ps1')
if($env:ORACLE_SHARED_BROWSER -ne '0'){
    if(-not $PSBoundParameters.ContainsKey('ProfilePath')){$ProfilePath=''}
    & (Join-Path $PSScriptRoot 'setup-browser.ps1') -Port $Port -ChromePath $ChromePath -ProfilePath $ProfilePath -Interactive
    return
}
if(-not $Port){$Port=9333}
if (-not $ProfilePath) { $ProfilePath = Join-Path $env:USERPROFILE '.agent-browser\gemini-native-profile' }
if (-not $ChromePath) {
    $ChromePath = @(
        'D:\Program Files\Google\Chrome\Application\chrome.exe',
        'C:\Program Files\Google\Chrome\Application\chrome.exe',
        'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe'
    ) | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
}
if (-not $ChromePath -or -not (Test-Path -LiteralPath $ChromePath)) { throw 'Set -ChromePath to the native Chrome executable.' }
$listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($listener) {
    $ownerIds = @($listener | Select-Object -ExpandProperty OwningProcess -Unique)
    if ($ownerIds.Count -ne 1) { throw 'CDP port has multiple owners.' }
    $owner = Get-CimInstance Win32_Process -Filter "ProcessId = $($ownerIds[0])"
    if ($owner.Name -ne 'chrome.exe' -or $owner.CommandLine -notlike "*$ProfilePath*") {
        throw 'CDP port is occupied by a different browser/profile.'
    }
    Write-Host "Native Gemini browser is already running on loopback CDP $Port."
    return
}
$browserArguments = @(
    "--remote-debugging-port=$Port", '--remote-debugging-address=127.0.0.1',
    ('--user-data-dir="' + $ProfilePath + '"'), '--no-first-run',
    '--no-default-browser-check', '--start-maximized', 'https://gemini.google.com'
)
Start-Process -FilePath $ChromePath -ArgumentList $browserArguments -WindowStyle Normal | Out-Null
Write-Host 'Complete Google sign-in manually in the dedicated Chrome window.'
