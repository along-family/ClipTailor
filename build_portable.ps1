param(
    [string]$Version = "v2",
    [switch]$OneFile
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$python = Get-Command py -ErrorAction SilentlyContinue
if (-not $python) {
    throw "Python launcher 'py' was not found. Install Python 3.11+ on the build machine."
}

& py -3 -m PyInstaller --version *> $null
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller is not installed. Install it on the build machine with: py -3 -m pip install pyinstaller"
}

function Find-Tool {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Name
    )

    $localCandidate = Join-Path $root "tools\ffmpeg\bin\$Name"
    if (Test-Path -LiteralPath $localCandidate) {
        return (Resolve-Path -LiteralPath $localCandidate).Path
    }

    $pathCandidate = Get-Command $Name -ErrorAction SilentlyContinue
    if ($pathCandidate) {
        return $pathCandidate.Source
    }

    $wingetRoot = Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Packages"
    if (Test-Path -LiteralPath $wingetRoot) {
        $match = Get-ChildItem -LiteralPath $wingetRoot -Recurse -Filter $Name -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTime -Descending |
            Select-Object -First 1
        if ($match) {
            return $match.FullName
        }
    }

    return $null
}

$ffmpeg = Find-Tool "ffmpeg.exe"
$ffprobe = Find-Tool "ffprobe.exe"
if (-not $ffmpeg -or -not $ffprobe) {
    throw "ffmpeg.exe and ffprobe.exe were not found. Put them under tools\ffmpeg\bin, or install FFmpeg on this build machine."
}

$distRoot = Join-Path $root "release"
$packageName = "ClipTailor-$Version-portable"
$packageDir = Join-Path $distRoot $packageName
$workDir = Join-Path $root "build\pyinstaller"
$pyiDist = Join-Path $root "dist"

if (Test-Path -LiteralPath $packageDir) {
    Remove-Item -LiteralPath $packageDir -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $packageDir | Out-Null

$pyinstallerArgs = @(
    "--name", "ClipTailor",
    "--noconsole",
    "--noconfirm",
    "--clean",
    "--workpath", $workDir,
    "--distpath", $pyiDist,
    "video_ad_trimmer.py"
)

& py -3 -c "import tkinterdnd2" *> $null
if ($LASTEXITCODE -eq 0) {
    $pyinstallerArgs = @("--collect-all", "tkinterdnd2") + $pyinstallerArgs
}

if ($OneFile) {
    $ffmpegAddBinary = "$ffmpeg;tools\ffmpeg\bin"
    $ffprobeAddBinary = "$ffprobe;tools\ffmpeg\bin"
    $pyinstallerArgs = @(
        "--onefile",
        "--add-binary", $ffmpegAddBinary,
        "--add-binary", $ffprobeAddBinary
    ) + $pyinstallerArgs
}

& py -3 -m PyInstaller @pyinstallerArgs
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed."
}

if ($OneFile) {
    Copy-Item -LiteralPath (Join-Path $pyiDist "ClipTailor.exe") -Destination (Join-Path $packageDir "ClipTailor.exe") -Force
} else {
    Copy-Item -Path (Join-Path $pyiDist "ClipTailor\*") -Destination $packageDir -Recurse -Force
}

if (-not $OneFile) {
    $toolDir = Join-Path $packageDir "tools\ffmpeg\bin"
    New-Item -ItemType Directory -Force -Path $toolDir | Out-Null
    Copy-Item -LiteralPath $ffmpeg -Destination (Join-Path $toolDir "ffmpeg.exe") -Force
    Copy-Item -LiteralPath $ffprobe -Destination (Join-Path $toolDir "ffprobe.exe") -Force
}

$launcherName = -join [char[]](
    0x542f, 0x52a8, 0x89c6, 0x9891, 0x88c1, 0x526a, 0x5de5, 0x5177,
    0x002e, 0x0062, 0x0061, 0x0074
)

@'
@echo off
cd /d "%~dp0"
start "" "%~dp0ClipTailor.exe"
'@ | Set-Content -LiteralPath (Join-Path $packageDir $launcherName) -Encoding ASCII -NoNewline

Copy-Item -LiteralPath (Join-Path $root "PORTABLE_README.md") -Destination (Join-Path $packageDir "README.md") -Force

Write-Host "Portable package created:"
Write-Host $packageDir
