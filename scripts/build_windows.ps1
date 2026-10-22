# Build the LimitPattern Windows green build (onedir zip) and, if it succeeds, a onefile exe.
$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $true
# PyInstaller 的 spec 会打印中文。不设这个，Windows 控制台 cp1252 会把打包打崩。
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

function Get-PeSubsystemSafe([string]$Path) {
    $stream = [System.IO.File]::OpenRead($Path)
    try {
        $reader = New-Object System.IO.BinaryReader($stream)
        $stream.Seek(0x3C, [System.IO.SeekOrigin]::Begin) | Out-Null
        $peOffset = $reader.ReadInt32()
        $stream.Seek($peOffset + 24 + 68, [System.IO.SeekOrigin]::Begin) | Out-Null
        return $reader.ReadUInt16()
    }
    finally {
        $stream.Close()
    }
}

$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

Write-Host "== frontend =="
$frontendIndex = Join-Path $Root "frontend\dist\index.html"
if (Test-Path (Join-Path $Root "frontend\package.json")) {
    Push-Location (Join-Path $Root "frontend")
    if (Test-Path "package-lock.json") {
        npm ci
    }
    else {
        npm install
    }
    npm run build
    Pop-Location
}
else {
    Write-Host "frontend/ is absent; the package will use the stub page"
}

Write-Host "== python dependencies =="
python -m pip install --upgrade pip
function Install-Requirements([string]$RelativePath) {
    $full = Join-Path $Root $RelativePath
    if (Test-Path $full) {
        Write-Host "pip install -r $RelativePath"
        python -m pip install -r $full
    }
}
Install-Requirements "desktop/requirements-dev.txt"
Install-Requirements "requirements.txt"
Install-Requirements "backend/requirements.txt"
# 聚类才需要 sklearn。打进 EXE 会把包变得很大，这一版不装。
Install-Requirements "desktop/requirements-build.txt"

Write-Host "== icon =="
python scripts/make_icon.py

Write-Host "== launcher tests =="
python -m pytest desktop/tests -q

Write-Host "== pyinstaller onedir =="
foreach ($dir in @("desktop\build", "desktop\dist")) {
    $full = Join-Path $Root $dir
    if (Test-Path $full) {
        Remove-Item -Recurse -Force $full
    }
}
python -m PyInstaller --noconfirm --clean `
    --distpath desktop/dist `
    --workpath desktop/build `
    desktop/LimitPattern.spec

$onedir = Join-Path $Root "desktop\dist\LimitPattern"
$verify = Join-Path $PSScriptRoot "verify_bundle.ps1"
if (Test-Path $frontendIndex) {
    & $verify -Root $onedir -RequireFrontend
}
else {
    & $verify -Root $onedir
}

Write-Host "== smoke =="
$smokeData = Join-Path $env:RUNNER_TEMP "lp-smoke"
if (-not $env:RUNNER_TEMP) {
    $smokeData = Join-Path $Root "desktop\dist\smoke-data"
}
New-Item -ItemType Directory -Force -Path $smokeData | Out-Null
$env:LP_DATA_DIR = $smokeData
$env:LP_AUTO_SYNC = "0"
$env:LP_SCHEDULER = "0"
Remove-Item Env:LP_USE_STUB -ErrorAction SilentlyContinue
$exe = Join-Path $onedir "LimitPattern.exe"
$process = Start-Process -FilePath $exe -ArgumentList "--smoke","--check-gui" -WorkingDirectory $onedir -PassThru -WindowStyle Hidden
if (-not $process.WaitForExit(240000)) {
    Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
    if (Test-Path (Join-Path $smokeData "logs\launcher.log")) {
        Get-Content (Join-Path $smokeData "logs\launcher.log") -Tail 200
    }
    throw "smoke timed out"
}
$process.Refresh()
Write-Host "smoke exit code $($process.ExitCode)"
$logPath = Join-Path $smokeData "logs\launcher.log"
if (Test-Path $logPath) {
    Get-Content $logPath -Tail 200
}
if ($process.ExitCode -ne 0) {
    throw "smoke failed with exit code $($process.ExitCode)"
}
if (-not (Test-Path $logPath)) {
    throw "smoke log was not written"
}
$smokeLog = Get-Content $logPath -Raw
if ($smokeLog -notmatch "应用来源=backend") {
    throw "smoke did not start the real backend"
}
if ($smokeLog -notmatch "首页检查通过") {
    throw "smoke did not open the homepage"
}
if ($smokeLog -notmatch "静态目录（frontend）") {
    throw "smoke did not serve the built frontend"
}

Write-Host "== pyinstaller onefile (best effort) =="
$onefileDir = Join-Path $Root "desktop\dist-onefile"
$onefileExe = Join-Path $onefileDir "LimitPattern-onefile.exe"
$onefileOk = $false
try {
    if (Test-Path (Join-Path $Root "desktop\build-onefile")) {
        Remove-Item -Recurse -Force (Join-Path $Root "desktop\build-onefile")
    }
    python -m PyInstaller --noconfirm `
        --distpath desktop/dist-onefile `
        --workpath desktop/build-onefile `
        desktop/LimitPattern-onefile.spec
    if ((Test-Path $onefileExe) -and ((Get-PeSubsystemSafe $onefileExe) -eq 2)) {
        $onefileOk = $true
    }
    else {
        Write-Warning "onefile exe missing or not a GUI subsystem"
    }
}
catch {
    Write-Warning "onefile build failed: $_"
}

Write-Host "== zip =="
$stage = Join-Path $Root "desktop\dist\LimitPattern-win64"
if (Test-Path $stage) {
    Remove-Item -Recurse -Force $stage
}
New-Item -ItemType Directory -Force -Path $stage | Out-Null
Copy-Item -Recurse $onedir (Join-Path $stage "LimitPattern")
Copy-Item (Join-Path $Root "desktop\使用说明.txt") (Join-Path $stage "使用说明.txt")
if ($onefileOk) {
    Copy-Item $onefileExe (Join-Path $stage "LimitPattern-onefile.exe")
}
$zip = Join-Path $Root "desktop\dist\LimitPattern-win64.zip"
if (Test-Path $zip) {
    Remove-Item -Force $zip
}
# Windows 自带的 tar.exe 在这台 runner 上会随机 0xC0000005，改用标准库打包。
$env:LP_STAGE = $stage
$env:LP_ZIP = $zip
python -c @"
import os
import zipfile
from pathlib import Path

stage = Path(os.environ['LP_STAGE'])
dest = Path(os.environ['LP_ZIP'])
with zipfile.ZipFile(dest, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
    for path in stage.rglob('*'):
        if path.is_file():
            archive.write(path, path.relative_to(stage).as_posix())
with zipfile.ZipFile(dest) as archive:
    names = archive.namelist()
print('\n'.join(names[:30]))
if not any(name.endswith('LimitPattern.exe') for name in names):
    raise SystemExit('zip does not contain LimitPattern.exe')
print('entries', len(names), 'bytes', dest.stat().st_size)
"@
if (-not (Test-Path $zip)) {
    throw "zip was not created"
}
$zipSize = (Get-Item $zip).Length
if ($zipSize -lt 1MB) {
    throw "zip is unexpectedly small: $zipSize"
}
Write-Host "created $zip ($zipSize bytes)"
Set-Content -Encoding utf8 (Join-Path $Root "desktop\dist\onefile_status.txt") ($(if ($onefileOk) { "ok" } else { "failed" }))
