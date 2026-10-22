# Verify the onedir layout: GUI subsystem, stub page, pythonnet and WebView2 files.
param(
    [Parameter(Mandatory = $true)][string]$Root,
    [switch]$RequireFrontend
)

$ErrorActionPreference = "Stop"

function Get-PeSubsystem([string]$Path) {
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

function Assert-HasFile([string]$Pattern, [string]$Label) {
    $match = Get-ChildItem -Path $Root -Recurse -File -ErrorAction SilentlyContinue |
        Where-Object { ($_.FullName -replace '\\', '/') -like $Pattern } |
        Select-Object -First 1
    if (-not $match) {
        throw "bundle is missing $Label ($Pattern)"
    }
    Write-Host "ok $Label -> $($match.FullName)"
}

if (-not (Test-Path $Root)) {
    throw "onedir folder not found: $Root"
}

$exe = Join-Path $Root "LimitPattern.exe"
if (-not (Test-Path $exe)) {
    throw "LimitPattern.exe not found"
}
$subsystem = Get-PeSubsystem $exe
if ($subsystem -ne 2) {
    throw "PE subsystem is $subsystem, expected 2 (WINDOWS_GUI)"
}
Write-Host "ok PE subsystem = 2"

Assert-HasFile "*/desktop/stub_static/index.html" "stub index"
Assert-HasFile "*/Python.Runtime.dll" "Python.Runtime.dll"
Assert-HasFile "*/ClrLoader.dll" "ClrLoader.dll"
Assert-HasFile "*/Microsoft.Web.WebView2.Core.dll" "WebView2 Core"
Assert-HasFile "*/Microsoft.Web.WebView2.WinForms.dll" "WebView2 WinForms"
Assert-HasFile "*/WebView2Loader.dll" "WebView2Loader.dll"
Assert-HasFile "*cffi_backend*" "cffi backend"

if ($RequireFrontend) {
    Assert-HasFile "*/frontend/dist/index.html" "frontend dist"
}
