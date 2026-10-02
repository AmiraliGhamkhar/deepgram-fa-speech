<#
.SYNOPSIS
    Build the Windows desktop application with Nuitka (standalone folder).

.DESCRIPTION
    Produces dist\MedicalSTT\ -- a complete folder containing
    MedicalSTT.exe and every dependency, runnable on a clean Windows
    10/11 machine with no Python installed.

    Deliberate choices:
      * Nuitka standalone, NOT PyInstaller: the binary is compiled to
        C and is markedly harder to unpack and inspect.
      * No UPX: never pass --upx (and assert it was not passed).
      * No console window: --windows-console-mode=disable.
      * config\ and data\ are shipped inside the folder. No secrets are
        ever baked in: user configuration is created in %APPDATA% on
        first run and the shared secret is DPAPI-protected.
      * A folder build, not --onefile: no temp extraction, faster
        startup, and no antivirus false positives from self-extracting
        archives.

    Code signing is intentionally NOT done here. Sign the resulting
    dist\MedicalSTT\MedicalSTT.exe afterwards with your own certificate;
    see README > Code signing.

.PARAMETER Python
    Python interpreter used to run Nuitka (3.10-3.12).

.PARAMETER SkipVerify
    Skip the post-build "does it actually run?" check.
#>
[CmdletBinding()]
param(
    [string]$Python = "python",
    [switch]$SkipVerify
)

$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

if ($args -contains "--upx") {
    throw "UPX packing is not allowed for this project."
}

$DistDir = Join-Path $PWD "dist\MedicalSTT"
$BuildDir = Join-Path $PWD "build"

Write-Host "==> Cleaning previous build output" -ForegroundColor Cyan
foreach ($dir in @($DistDir, $BuildDir)) {
    if (Test-Path $dir) { Remove-Item -Recurse -Force $dir }
}

Write-Host "==> Installing build dependencies" -ForegroundColor Cyan
& $Python -m pip install --upgrade pip
& $Python -m pip install "nuitka>=2.5,<3.0" "ordered-set>=4.1" "zstandard>=0.22"

Write-Host "==> Verifying the source tree is clean of secrets" -ForegroundColor Cyan
# Fails the build if a provider credential is ever committed here.
$forbidden = Select-String -Path (Get-ChildItem -Recurse -Include *.py,*.yaml,*.yml -File |
                                  Where-Object { $_.FullName -notmatch '\\dist\\|\\build\\|\\\.venv\\' }) `
                             -Pattern 'dg_[0-9a-f]{20,}' -ErrorAction SilentlyContinue
if ($forbidden) {
    $forbidden | ForEach-Object { Write-Host $_.Path -ForegroundColor Red }
    throw "A Deepgram API key pattern was found in the source tree. The client must never contain one."
}

Write-Host "==> Running the test suite" -ForegroundColor Cyan
& $Python -m pytest tests -q
if ($LASTEXITCODE -ne 0) { throw "Tests failed; refusing to build." }

$version = (& $Python -c "import medical_stt; print(medical_stt.__version__)").Trim()

Write-Host "==> Compiling with Nuitka (standalone, no console, no UPX)" -ForegroundColor Cyan
& $Python -m nuitka `
    --standalone `
    --assume-yes-for-downloads `
    --enable-plugin=tk-inter `
    --windows-console-mode=disable `
    --windows-icon-from-ico="" `
    --output-dir=build `
    --output-filename=MedicalSTT.exe `
    --include-package=medical_stt `
    --include-data-dir=config=config `
    --include-data-dir=data=data `
    --nofollow-import-to=tkinter.test `
    --nofollow-import-to=pytest `
    --nofollow-import-to=unittest `
    --company-name="Medical STT" `
    --product-name="Medical STT" `
    --file-description="Medical STT dictation client (no credentials embedded)" `
    --file-version="$version.0" `
    --product-version="$version.0" `
    --copyright="Medical STT" `
    run.py

if ($LASTEXITCODE -ne 0) { throw "Nuitka failed with exit code $LASTEXITCODE" }

# Nuitka writes the executable into the output dir; flatten it into the
# single distributable folder.
$built = Get-ChildItem -Path $PSScriptRoot -Recurse -Filter "MedicalSTT.exe" -File |
         Where-Object { $_.FullName -like "*build*" } | Select-Object -First 1
if (-not $built) { throw "MedicalSTT.exe was not produced." }
$builtFolder = $built.Directory.FullName
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $DistDir) | Out-Null
if (Test-Path $DistDir) { Remove-Item -Recurse -Force $DistDir }
Move-Item $builtFolder $DistDir

Write-Host "==> Build complete: $DistDir" -ForegroundColor Green

if (-not $SkipVerify) {
    Write-Host "==> Verifying the bundle on this machine" -ForegroundColor Cyan
    $exe = Join-Path $DistDir "MedicalSTT.exe"
    if (-not (Test-Path $exe)) { throw "Executable missing from the bundle." }
    if (-not (Test-Path (Join-Path $DistDir "config\settings.yaml"))) {
        throw "config\ was not bundled."
    }
    if (-not (Test-Path (Join-Path $DistDir "data\corrections.yaml"))) {
        throw "data\ was not bundled."
    }
    # --version writes to %APPDATA%\MedicalSTT\logs\app.log and exits 0,
    # which proves the bundle starts with no console and no Python.
    & $exe --version
    if ($LASTEXITCODE -ne 0) {
        throw "The built executable did not start correctly (exit $LASTEXITCODE)."
    }
    Write-Host "==> Bundle verified" -ForegroundColor Green
}

Write-Host ""
Write-Host "Next steps:" -ForegroundColor Cyan
Write-Host "  1. Copy the whole folder to the target machine."
Write-Host "  2. Run MedicalSTT.exe once; enter the host URL and shared secret."
Write-Host "  3. (Recommended) code-sign the exe -- see README > Code signing."
