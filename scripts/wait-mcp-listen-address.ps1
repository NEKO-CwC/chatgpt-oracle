param(
    [Parameter(Mandatory = $true)]
    [string]$ListenHost,
    [ValidateRange(0.01, 3600)]
    [double]$TimeoutSeconds = 300,
    [ValidateRange(0.01, 60)]
    [double]$RetrySeconds = 2,
    [string]$LogPath = ''
)

$ErrorActionPreference = 'Stop'
$listenAddress = $null
if (-not [System.Net.IPAddress]::TryParse($ListenHost, [ref]$listenAddress)) { return }
if ([System.Net.IPAddress]::IsLoopback($listenAddress) -or
    $listenAddress.Equals([System.Net.IPAddress]::Any) -or
    $listenAddress.Equals([System.Net.IPAddress]::IPv6Any)) { return }

function Write-AddressStatus([string]$Message) {
    $line = '{0} {1}' -f [DateTimeOffset]::Now.ToString('o'), $Message
    Write-Host $line
    if ($LogPath) {
        $null = New-Item -ItemType Directory -Path (Split-Path -Parent $LogPath) -Force
        Add-Content -LiteralPath $LogPath -Value $line -Encoding UTF8
    }
}

$timer = [System.Diagnostics.Stopwatch]::StartNew()
$waiting = $false
while ($true) {
    $assigned = @(Get-NetIPAddress -IPAddress $listenAddress.ToString() -ErrorAction SilentlyContinue |
        Where-Object { $_.AddressState -in @('Preferred', 'Deprecated') })
    if ($assigned.Count) {
        Write-AddressStatus "MCP listen address $ListenHost is ready."
        return
    }
    if (-not $waiting) {
        Write-AddressStatus "Waiting up to ${TimeoutSeconds}s for MCP listen address $ListenHost before broker initialization."
        $waiting = $true
    }
    if ($timer.Elapsed.TotalSeconds -ge $TimeoutSeconds) {
        $message = "MCP listen address $ListenHost was not ready within ${TimeoutSeconds}s; broker was not initialized."
        Write-AddressStatus $message
        throw $message
    }
    $remaining = $TimeoutSeconds - $timer.Elapsed.TotalSeconds
    Start-Sleep -Milliseconds ([int][Math]::Max(1, [Math]::Min($RetrySeconds, $remaining) * 1000))
}
