param(
    [string]$CdpUrl = '',
    [ValidateRange(0.01, 3600)]
    [double]$TimeoutSeconds = 120,
    [ValidateRange(0.01, 60)]
    [double]$RetrySeconds = 2,
    [string]$LogPath = ''
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'import-shared-browser-env.ps1')
$endpoint = if ($CdpUrl) { $CdpUrl } elseif ($env:ORACLE_BROWSER_CDP_URL) { $env:ORACLE_BROWSER_CDP_URL } else { 'http://127.0.0.1:9333' }
$uri = [uri]$endpoint
$versionUrl = '{0}://{1}:{2}/json/version' -f $uri.Scheme, $uri.Host, $uri.Port

function Write-CdpStatus([string]$Message) {
    $line = '{0} {1}' -f [DateTimeOffset]::Now.ToString('o'), $Message
    Write-Host $line
    if ($LogPath) {
        $null = New-Item -ItemType Directory -Path (Split-Path -Parent $LogPath) -Force
        Add-Content -LiteralPath $LogPath -Value $line -Encoding UTF8
    }
}

# A shared profile can hold its ProcessSingleton lock for a Chrome started as
# a normal desktop window without --remote-debugging-port. That instance
# cannot be attached to, and a second launch on the same profile exits early,
# so the broker waits for a debuggable listener instead of failing arity.
$timer = [System.Diagnostics.Stopwatch]::StartNew()
$waiting = $false
while ($true) {
    $listener = Get-NetTCPConnection -State Listen -LocalPort $uri.Port -ErrorAction SilentlyContinue |
        Where-Object { $_.LocalAddress -in @('127.0.0.1', '0.0.0.0', '::', '::1') }
    if ($listener) {
        try {
            $version = Invoke-RestMethod -Uri $versionUrl -TimeoutSec 3
            Write-CdpStatus "Browser CDP $endpoint is ready ($($version.Browser))."
            return
        } catch {
            if (-not $waiting) {
                Write-CdpStatus "Port $($uri.Port) is listening but $versionUrl is not answering yet; waiting."
            }
        }
    }
    if (-not $waiting) {
        Write-CdpStatus "Waiting up to ${TimeoutSeconds}s for browser CDP $endpoint before broker initialization."
        $waiting = $true
    }
    if ($timer.Elapsed.TotalSeconds -ge $TimeoutSeconds) {
        $message = "Browser CDP $endpoint was not ready within ${TimeoutSeconds}s; the broker was not initialized. " +
            "Start the shared Chrome via scripts/setup-browser.ps1, or close the desktop Chrome that holds the profile lock."
        Write-CdpStatus $message
        throw $message
    }
    $remaining = $TimeoutSeconds - $timer.Elapsed.TotalSeconds
    Start-Sleep -Milliseconds ([int][Math]::Max(1, [Math]::Min($RetrySeconds, $remaining) * 1000))
}
