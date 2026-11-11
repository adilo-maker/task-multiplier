# Builds a release archive for the Task Multiplier plugin.
#
# Usage (from the repository root):
#   powershell -ExecutionPolicy Bypass -File .\scripts\package.ps1
#
# The archive is written to dist\task_multiplier-<version>.zip and contains
# the plugin, installer, README, license and changelog at the archive root.

param(
    [string]$OutputDir = (Join-Path (Split-Path $PSScriptRoot -Parent) 'dist')
)

$ErrorActionPreference = 'Stop'

$root = Split-Path $PSScriptRoot -Parent
$plugin = Join-Path $root 'task_multiplier.py'
$installer = Join-Path $root 'install.ps1'

if (-not (Test-Path -LiteralPath $plugin)) {
    throw "Plugin file not found: $plugin"
}
if (-not (Test-Path -LiteralPath $installer)) {
    throw "Installer not found: $installer"
}

$versionLine = Select-String -LiteralPath $plugin -Pattern '^# version = "([^"]+)"' |
    Select-Object -First 1
if (-not $versionLine) {
    throw 'Could not find "# version = ..." in task_multiplier.py'
}
$version = $versionLine.Matches[0].Groups[1].Value

$installerText = Get-Content -LiteralPath $installer -Raw
if ($installerText -notmatch "installed_version\s*=\s*'$([regex]::Escape($version))'") {
    throw "install.ps1 does not declare installed_version = '$version'"
}

# Windows PowerShell 5.1 needs a BOM to read the Russian installer text.
$bytes = [System.IO.File]::ReadAllBytes($installer)
$hasBom = $bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF
if (-not $hasBom) {
    throw 'install.ps1 must be saved as UTF-8 with BOM'
}

$stage = Join-Path $OutputDir "task_multiplier-$version"
$zip = Join-Path $OutputDir "task_multiplier-$version.zip"

if (Test-Path -LiteralPath $stage) {
    Remove-Item -LiteralPath $stage -Recurse -Force
}
if (Test-Path -LiteralPath $zip) {
    Remove-Item -LiteralPath $zip -Force
}
New-Item -ItemType Directory -Force -Path $stage | Out-Null

$files = @(
    'task_multiplier.py',
    'install.ps1',
    'README.md',
    'LICENSE',
    'CHANGELOG.md'
)
foreach ($name in $files) {
    $source = Join-Path $root $name
    if (-not (Test-Path -LiteralPath $source)) {
        throw "Release file not found: $source"
    }
    Copy-Item -LiteralPath $source -Destination (Join-Path $stage $name)
}

Compress-Archive -Path (Join-Path $stage '*') -DestinationPath $zip -CompressionLevel Optimal

$hash = (Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash
Write-Host ''
Write-Host "Release archive: $zip" -ForegroundColor Green
Write-Host "Version:         $version"
Write-Host "SHA256:          $hash"
Write-Host ''
Write-Host 'Contents:'
Get-ChildItem -LiteralPath $stage | Select-Object Name, Length | Format-Table -AutoSize
