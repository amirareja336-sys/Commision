# remove-legacy.ps1 — archive or delete unused Tk/PHP/config leftovers.
#   .\remove-legacy.ps1              # dry-run
#   .\remove-legacy.ps1 -Apply       # move into _legacy_archive\
#   .\remove-legacy.ps1 -Apply -Delete  # permanent delete (no recycle)

param(
    [switch]$Apply,
    [switch]$Delete
)

$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
if (-not $Root) { $Root = Get-Location }

$Targets = @(
    "server",
    "scripts\export_physician_performance.py",
    "scripts\reconciliation_app_v5.py",
    "scripts\service_analyzer.py",
    "scripts\secret_helper.py",
    "configs"
)

$Archive = Join-Path $Root "_legacy_archive"
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"

Write-Host "Root: $Root"
Write-Host $(if (-not $Apply) { "MODE: dry-run (pass -Apply to change files)" }
             elseif ($Delete) { "MODE: permanent delete" }
             else { "MODE: move to $Archive\$stamp" })
Write-Host ""

foreach ($rel in $Targets) {
    $path = Join-Path $Root $rel
    if (-not (Test-Path -LiteralPath $path)) {
        Write-Host "MISSING  $rel"
        continue
    }
    $item = Get-Item -LiteralPath $path
    $kind = if ($item.PSIsContainer) { "DIR " } else { "FILE" }
    Write-Host "FOUND    $kind  $rel"

    if (-not $Apply) { continue }

    if ($Delete) {
        Remove-Item -LiteralPath $path -Recurse -Force
        Write-Host "DELETED  $rel"
    } else {
        $destDir = Join-Path $Archive $stamp
        New-Item -ItemType Directory -Force -Path $destDir | Out-Null
        Move-Item -LiteralPath $path -Destination (Join-Path $destDir $rel)
        Write-Host "MOVED    $rel  ->  _legacy_archive\$stamp\$rel"
    }
}

if (-not $Apply) {
    Write-Host ""
    Write-Host "No files changed. Re-run with -Apply to move them aside."
}