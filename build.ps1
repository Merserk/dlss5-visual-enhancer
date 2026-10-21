[CmdletBinding()]
param(
    [ValidateSet('Debug', 'Release', 'RelWithDebInfo')][string]$Configuration = 'Release',
    [string]$QtRoot,
    [string]$BuildDir = 'out',
    [switch]$Test,
    [switch]$Deploy,
    [string]$Destination,
    [string]$NativeBuildRoot,
    [string]$RuntimeSource,
    [string]$MediaRuntimeSource,
    [switch]$RebuildNative,
    [string]$CertificateThumbprint,
    [string]$TimestampServer = 'http://timestamp.digicert.com',
    [switch]$MachineCertificateStore,
    [switch]$RequireSignatures,
    [switch]$ScanDefender,
    [string]$OutputDirectory,
    [switch]$StageOnly
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'tools/Toolchain.ps1')
$resolvedQt = Resolve-VeQtRoot $QtRoot
$resolvedBuild = Resolve-VePath $BuildDir $PSScriptRoot
$generator = Get-VeVisualStudioGenerator
Invoke-VeCommand 'python' @((Join-Path $PSScriptRoot 'tools/prepare-nvenc-headers.py'))
if ($RebuildNative) {
    $nativeRoot = Join-Path (Split-Path -Parent $PSScriptRoot) 'native (dev)'
    $nativeShell = (Get-Process -Id $PID).Path
    foreach ($feature in @('neural_rendering', 'super_resolution', 'ray_reconstruction', 'frame_interpolation', 'fsr_frame_generation', 'rtx_video', 'video_effects', 'reshade')) {
        Invoke-VeCommand $nativeShell @('-NoProfile', '-File', (Join-Path $nativeRoot "modules/$feature/build_bridge.ps1"), '-NoDeploy')
    }
    $NativeBuildRoot = Join-Path $nativeRoot 'modules'
}
$testOption = if ($Test) { 'ON' } else { 'OFF' }
Write-Host "Building C++23 with $generator and Qt at $resolvedQt"
Invoke-VeCommand 'cmake' @('-S', $PSScriptRoot, '-B', $resolvedBuild, '-G', $generator, '-A', 'x64',
    "-DCMAKE_PREFIX_PATH=$resolvedQt", "-DBUILD_TESTING=$testOption")
Invoke-VeCommand 'cmake' @('--build', $resolvedBuild, '--config', $Configuration, '--parallel', '8')
if ($Test) {
    $originalPath = $env:PATH
    try {
        $env:PATH = (Join-Path $resolvedQt 'bin') + ';' + $originalPath
        Invoke-VeCommand 'ctest' @('--test-dir', $resolvedBuild, '-C', $Configuration, '--output-on-failure')
    } finally { $env:PATH = $originalPath }
}
if ($Deploy) {
    & (Join-Path $PSScriptRoot 'tools/deploy.ps1') -BuildDir $resolvedBuild -QtRoot $resolvedQt `
        -Configuration $Configuration -Destination $Destination -NativeBuildRoot $NativeBuildRoot `
        -RuntimeSource $RuntimeSource -MediaRuntimeSource $MediaRuntimeSource `
        -CertificateThumbprint $CertificateThumbprint `
        -TimestampServer $TimestampServer -MachineCertificateStore:$MachineCertificateStore `
        -RequireSignatures:$RequireSignatures -ScanDefender:$ScanDefender -OutputDirectory $OutputDirectory -StageOnly:$StageOnly
}
