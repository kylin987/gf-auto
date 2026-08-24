param(
    [Parameter(Mandatory = $true)][string]$ClientDir,
    [Parameter(Mandatory = $true)][string]$ReleaseVersion
)

$ErrorActionPreference = "Stop"

function Resolve-Makensis {
    $candidates = @(
        "$env:ProgramFiles\NSIS\makensis.exe",
        "${env:ProgramFiles(x86)}\NSIS\makensis.exe",
        "C:\ProgramData\chocolatey\bin\makensis.exe"
    )
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path $candidate)) {
            return $candidate
        }
    }
    $command = Get-Command makensis.exe -ErrorAction SilentlyContinue
    if ($null -ne $command) {
        return $command.Source
    }
    throw "makensis.exe not found"
}

function Escape-NsisString {
    param([string]$Value)
    return $Value.Replace('$', '$$').Replace('"', '$\"')
}

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$version = $ReleaseVersion.Trim().TrimStart("v")
$tag = "v$version"
$releaseDir = Join-Path $repoRoot "release"
$installerPath = Join-Path $releaseDir "yhs-fish-plugin-setup-$tag.exe"
$tempDir = Join-Path $repoRoot "temp\fish-installer"
$nsiPath = Join-Path $tempDir "installer.nsi"
$makensis = Resolve-Makensis

if (Test-Path $tempDir) {
    Remove-Item -LiteralPath $tempDir -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $tempDir | Out-Null

$escapedClientDir = Escape-NsisString -Value ((Resolve-Path $ClientDir).Path)
$escapedInstaller = Escape-NsisString -Value $installerPath
$escapedAppIcon = Escape-NsisString -Value ((Resolve-Path (Join-Path $repoRoot "assets\brand\fish-app-icon.ico")).Path)

$nsi = @"
Unicode true
Name "yhs-fish-plugin"
OutFile "$escapedInstaller"
InstallDir "`$PROGRAMFILES32\yhs-fish-plugin"
RequestExecutionLevel admin
SetCompressor /SOLID lzma
ShowInstDetails show
BrandingText "yhs-fish-plugin $tag"
!define APP_EXE "XianYuApis.exe"
!define CLIENT_DIR "$escapedClientDir"
!define APP_ICON "$escapedAppIcon"
Icon "`${APP_ICON}"

Page directory
Page instfiles

Section "Install"
  SetOverwrite on
  nsExec::ExecToLog 'taskkill /IM `${APP_EXE} /F'
  SetOutPath "`$INSTDIR"
  File /r "`${CLIENT_DIR}\*.*"
  CreateDirectory "`$SMPROGRAMS\yhs-fish-plugin"
  CreateShortcut "`$DESKTOP\yhs-fish-plugin.lnk" "`$INSTDIR\`${APP_EXE}" "" "`$INSTDIR\_internal\assets\brand\fish-app-icon.ico" 0
  CreateShortcut "`$SMPROGRAMS\yhs-fish-plugin\yhs-fish-plugin.lnk" "`$INSTDIR\`${APP_EXE}" "" "`$INSTDIR\_internal\assets\brand\fish-app-icon.ico" 0
  Exec '"`$INSTDIR\`${APP_EXE}"'
SectionEnd
"@

$encoding = New-Object System.Text.UTF8Encoding($true)
[System.IO.File]::WriteAllText($nsiPath, $nsi, $encoding)
& $makensis $nsiPath | Out-Host
if ($LASTEXITCODE -ne 0 -or -not (Test-Path $installerPath)) {
    throw "NSIS installer build failed"
}
