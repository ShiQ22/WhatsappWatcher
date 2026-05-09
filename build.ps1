# build.ps1 - WhatsAppWatcher full release build
# Run from the project root: .\build.ps1
# Output: deploy\WhatsAppWatcher\

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ProjectRoot = $PSScriptRoot
$DeployDir = Join-Path $ProjectRoot "deploy\WhatsAppWatcher"

Write-Host ""
Write-Host "=== RH-6 Build ==="
Write-Host "Project root : $ProjectRoot"
Write-Host "Deploy target: $DeployDir"
Write-Host ""

Write-Host "--- Step 1: dotnet publish RecorderHelper self-contained win-x64 ---"
Push-Location $ProjectRoot
dotnet publish RecorderHelper/RecorderHelper.csproj `
    -c Release `
    -r win-x64 `
    --self-contained true `
    -o dist\RecorderHelper
if ($LASTEXITCODE -ne 0) { throw "dotnet publish failed. Exit code: $LASTEXITCODE" }
Pop-Location
Write-Host ""

Write-Host "--- Step 2: PyInstaller ---"
Push-Location $ProjectRoot
python -m PyInstaller --noconfirm --clean whatsapp_watcher.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed. Exit code: $LASTEXITCODE" }
Pop-Location
Write-Host ""

Write-Host "--- Step 3: Assembling deployment folder ---"

if (Test-Path $DeployDir) {
    Remove-Item $DeployDir -Recurse -Force
}
New-Item -ItemType Directory -Path $DeployDir | Out-Null

$pyExe = Join-Path $ProjectRoot "dist\WhatsAppWatcher.exe"
if (-not (Test-Path $pyExe)) { throw "WhatsAppWatcher.exe not found at $pyExe" }
Copy-Item $pyExe "$DeployDir\"

$ffmpeg = Join-Path $ProjectRoot "bin\ffmpeg.exe"
if (-not (Test-Path $ffmpeg)) { throw "bin\ffmpeg.exe not found. Place a Windows x64 static build there before running this script." }
New-Item -ItemType Directory -Path "$DeployDir\bin" | Out-Null
Copy-Item $ffmpeg "$DeployDir\bin\"

$rhSrc = Join-Path $ProjectRoot "dist\RecorderHelper"
if (-not (Test-Path $rhSrc)) { throw "dist\RecorderHelper not found" }
Copy-Item $rhSrc "$DeployDir\RecorderHelper" -Recurse

Get-ChildItem "$DeployDir\RecorderHelper" -Filter "*.pdb" -ErrorAction SilentlyContinue | Remove-Item -Force

Write-Host ""
Write-Host "=== Build complete ==="
Write-Host ""
Write-Host "Deployment folder contents:"
Get-ChildItem $DeployDir -Recurse | Select-Object FullName, Length |
    ForEach-Object { Write-Host ("  " + $_.FullName.Replace($DeployDir, "")) ("  " + $_.Length + " bytes") }

Write-Host ""
Write-Host "IMPORTANT: Copy your config.json into $DeployDir before deploying via GPO."
Write-Host ""

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
        Write-Host "  [OK] $rel ($sz bytes)"
    } else {
        Write-Host "  [MISSING] $rel"
        $ok = $false
    }
}

if (-not $ok) { throw "Verification failed. One or more required files are missing." }

Write-Host ""
Write-Host "All checks passed. Ready for GPO deployment."
