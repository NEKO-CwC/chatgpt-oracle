$ErrorActionPreference = 'Stop'

function Merge-OracleUserModelGuide {
    param([string]$SourceText, [string]$ExistingText)
    $begin = '<!-- USER_MODEL_GUIDE_BEGIN -->'
    $end = '<!-- USER_MODEL_GUIDE_END -->'
    $pattern = '(?s)' + [regex]::Escape($begin) + '.*?' + [regex]::Escape($end)
    $sourceSections = [regex]::Matches($SourceText, $pattern)
    if ($sourceSections.Count -ne 1) {
        throw 'Source SKILL.md must contain exactly one user model guide section.'
    }
    if (-not $ExistingText.Contains($begin) -and -not $ExistingText.Contains($end)) {
        return $SourceText
    }
    $existingSections = [regex]::Matches($ExistingText, $pattern)
    if ($existingSections.Count -ne 1 -or
        [regex]::Matches($ExistingText, [regex]::Escape($begin)).Count -ne 1 -or
        [regex]::Matches($ExistingText, [regex]::Escape($end)).Count -ne 1) {
        throw 'Installed SKILL.md has an incomplete or duplicated user model guide; preserve it and repair the markers before updating.'
    }
    # String replacement preserves literal $1, backticks and other user text.
    return $SourceText.Replace($sourceSections[0].Value, $existingSections[0].Value)
}

$root = Split-Path -Parent $PSScriptRoot
$source = Join-Path $root 'skills\chatgpt-oracle'
$targetRoot = if($env:CODEX_HOME){$env:CODEX_HOME}else{Join-Path $env:USERPROFILE '.codex'}
$target = Join-Path $targetRoot 'skills\chatgpt-oracle'
$entryText = [IO.File]::ReadAllText((Join-Path $source 'SKILL.md'))
$existingEntry = Join-Path $target 'SKILL.md'
$existingText = if (Test-Path -LiteralPath $existingEntry) { [IO.File]::ReadAllText($existingEntry) } else { '' }
$entryText = Merge-OracleUserModelGuide -SourceText $entryText -ExistingText $existingText
New-Item -ItemType Directory -Force -Path $target | Out-Null
foreach($file in Get-ChildItem -LiteralPath $source -File -Recurse){
    $relativePath = $file.FullName.Substring($source.Length + 1)
    $destination = Join-Path $target $relativePath
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $destination) | Out-Null
    if ($relativePath -eq 'SKILL.md') {
        [IO.File]::WriteAllText($destination, $entryText, (New-Object Text.UTF8Encoding($false)))
    } else {
        Copy-Item -LiteralPath $file.FullName -Destination $destination -Force
    }
}
Write-Output "Installed Skill package at $target"
