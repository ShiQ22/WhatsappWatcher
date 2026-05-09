# build.ps1 — WhatsAppWatcher full release build
# Run from the project root: .\build.ps1
# Prerequisites:
#   - .NET 8 SDK installed (dotnet command available)
#   - Python 3.11 with PyInstaller (py -3.11 -m PyInstaller)
#   - bin\ffmpeg.exe present in project root (not committed)
#
# Output: deploy\WhatsAppWatcher\
#   WhatsAppWatcher.exe
#   config.json              (copy manually before deploying via GPO)
#   bin\ffmpeg.exe
#   RecorderHelper\          (self-contained win-x64, no .NET runtime required)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ProjectRoot = $PSScriptRoot
$DeployDir   = Join-Path $ProjectRoot "deploy\WhatsAppWatcher"

Write-Host ""
Write-Host "=== RH-6 Build ==="
Write-Host "Project root : $ProjectRoot"
Write-Host "Deploy target: $DeployDir"
Write-Host ""

# ── Step 1: Publish RecorderHelper (self-contained win-x64) ──────────────────
Write-Host "--- Step 1: dotnet publish RecorderHelper (self-contained win-x64) ---"
Push-Location $ProjectRoot
dotnet publish RecorderHelper/RecorderHelper.csproj `
    -c Release `
    -r win-x64 `
    --self-contained true `
    -o dist\RecorderHelper
if ($LASTEXITCODE -ne 0) { throw "dotnet publish failed (exit $LASTEXITCODE)" }
Pop-Location
Write-Host ""

# ── Step 2: Build WhatsAppWatcher with PyInstaller ───────────────────────────
Write-Host "--- Step 2: PyInstaller ---"
Push-Location $ProjectRoot
py -3.11 -m PyInstaller --noconfirm --clean whatsapp_watcher.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed (exit $LASTEXITCODE)" }
Pop-Location
Write-Host ""

# ── Step 3: Assemble deployment folder ───────────────────────────────────────
Write-Host "--- Step 3: Assembling deployment folder ---"

# Clean and recreate deploy dir
if (Test-Path $DeployDir) {
    Remove-Item $DeployDir -Recurse -Force
}
New-Item -ItemType Directory -Path $DeployDir | Out-Null

# WhatsAppWatcher.exe
$pyExe = Join-Path $ProjectRoot "dist\WhatsAppWatcher.exe"
if (-not (Test-Path $pyExe)) { throw "WhatsAppWatcher.exe not found at $pyExe" }
Copy-Item $pyExe "$DeployDir\"

# bin\ffmpeg.exe
$ffmpeg = Join-Path $ProjectRoot "bin\ffmpeg.exe"
if (-not (Test-Path $ffmpeg)) { throw "bin\ffmpeg.exe not found — place a Windows x64 static build there before running this script" }
New-Item -ItemType Directory -Path "$DeployDir\bin" | Out-Null
Copy-Item $ffmpeg "$DeployDir\bin\"

# RecorderHelper\ (entire publish output)
$rhSrc = Join-Path $ProjectRoot "dist\RecorderHelper"
if (-not (Test-Path $rhSrc)) { throw "dist\RecorderHelper not found" }
Copy-Item $rhSrc "$DeployDir\RecorderHelper" -Recurse

# Remove PDB files from deployment (debug symbols, not needed on agent PCs)
Get-ChildItem "$DeployDir\RecorderHelper" -Filter "*.pdb" | Remove-Item -Force

Write-Host ""
Write-Host "=== Build complete ==="
Write-Host ""
Write-Host "Deployment folder contents:"
Get-ChildItem $DeployDir -Recurse | Select-Object FullName, Length |
    ForEach-Object { Write-Host ("  " + $_.FullName.Replace($DeployDir, "")) + ("  " + $_.Length + " bytes") }

Write-Host ""
Write-Host "IMPORTANT: Copy your config.json into $DeployDir before deploying via GPO."
Write-Host ""

# ── Step 4: Verification ─────────────────────────────────────────────────────
Write-Host "--- Step 4: Verification ---"
$required = @(
    "WhatsAppWatcher.exe",
    "bin\ffmpeg.exe",
    "RecorderHelper\RecorderHelper.exe",
    "RecorderHelper\appsettings.json"
)
$ok = $true
foreach ($rel in $required) {
    $full = Join-Path $DeployDir $rel
    if (Test-Path $full) {
        $sz = (Get-Item $full).Length
        Write-Host "  [OK] $rel  ($sz bytes)"
    } else {
        Write-Host "  [MISSING] $rel"
        $ok = $false
    }
}
if (-not $ok) { throw "Verification failed — one or more required files are missing." }

Write-Host ""
Write-Host "All checks passed. Ready for GPO deployment."
