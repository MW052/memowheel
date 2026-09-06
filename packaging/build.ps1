# Build Trip Video into a one-folder, no-Python-needed app.
# Run from anywhere:  powershell -ExecutionPolicy Bypass -File packaging\build.ps1
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

Write-Host "Cleaning previous build so no stale code is bundled..."
Remove-Item -Recurse -Force build, dist -ErrorAction SilentlyContinue

Write-Host "Installing build + app dependencies (one time)..."
py -m pip install --upgrade pip
py -m pip install -r requirements.txt
py -m pip install pyinstaller

Write-Host "Building (this can take several minutes and produce a large folder)..."
py -m PyInstaller --noconfirm packaging\trip_video.spec

$appDir = "dist\TripVideo"
if (Test-Path $appDir) {
    # Ship the user guides next to the exe (visible, double-clickable), so the
    # zip a recipient unpacks already contains the manual - self-contained HTML,
    # no server needed. Renamed to friendly, obvious filenames.
    Write-Host "Bundling the user guide next to the app..."
    Copy-Item "docs\manual.html"    (Join-Path $appDir "User Guide - English.html") -Force
    Copy-Item "docs\manual-he.html" (Join-Path $appDir "User Guide - Hebrew.html")  -Force
}

$exe = "dist\TripVideo\TripVideo.exe"
if (Test-Path $exe) {
    Write-Host ""
    Write-Host "Done. The app is: $exe"
    Write-Host "The user guide ships alongside it (User Guide - English.html / - Hebrew.html)."
    Write-Host "Zip the whole 'dist\TripVideo' folder and share it. The recipient"
    Write-Host "double-clicks TripVideo.exe - no Python or install needed."
} else {
    Write-Host "Build finished but $exe was not found - check the PyInstaller output above."
}
