param([int]$Port, [string]$ListenHost = '127.0.0.1')
$ErrorActionPreference = 'Stop'
$wildcard = $ListenHost -in @('0.0.0.0', '::')
$addresses = @($ListenHost, '0.0.0.0', '::')
if($ListenHost -eq 'localhost'){$addresses+=@('127.0.0.1','::1')}
$occupied = @(Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue |
    Where-Object {$wildcard -or $_.LocalAddress -in $addresses})
if($occupied.Count){
    $owners = ($occupied | Select-Object -ExpandProperty OwningProcess -Unique) -join ','
    throw "MCP listen address ${ListenHost}:$Port is already occupied by PID $owners. Stop the existing broker before startup/migration."
}
