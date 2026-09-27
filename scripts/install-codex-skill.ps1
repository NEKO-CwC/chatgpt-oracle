$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$targetRoot = if($env:CODEX_HOME){$env:CODEX_HOME}else{Join-Path $env:USERPROFILE '.codex'}
$target = Join-Path $targetRoot 'skills\chatgpt-oracle'
New-Item -ItemType Directory -Force -Path $target | Out-Null
Copy-Item -LiteralPath (Join-Path $root 'skills\chatgpt-oracle\SKILL.md') -Destination (Join-Path $target 'SKILL.md') -Force
Write-Output "Installed Skill at $target\SKILL.md"

