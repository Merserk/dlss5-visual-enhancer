[CmdletBinding()]
param(
    [ValidateSet('Debug', 'Release', 'RelWithDebInfo')][string]$Configuration = 'Release',
    [string]$QtRoot,
    [string]$BuildDir = 'out',
    [string]$Destination,
    [string]$NativeBuildRoot,
    [string]$RuntimeSource,
    [string]$MediaRuntimeSource,
    [string]$CertificateThumbprint,
    [string]$TimestampServer = 'http://timestamp.digicert.com',
    [switch]$MachineCertificateStore,
    [switch]$RequireSignatures,
    [switch]$ScanDefender,
    [string]$OutputDirectory,
    [switch]$StageOnly
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Toolchain.ps1')
$workspace = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
if (-not $Destination) { $Destination = Join-Path $workspace 'Visual Enhancer' }
$destinationRoot = Resolve-VePath $Destination (Split-Path -Parent $PSScriptRoot)
if ($destinationRoot -eq $workspace -or $destinationRoot -eq (Join-Path $workspace 'Build')) {
    throw 'Deploy into a dedicated application directory.'
}
$operation = Join-Path (Split-Path -Parent $destinationRoot) ('.ve-deployments/' + [Guid]::NewGuid().ToString('N'))
$stage = Join-Path $operation 'package'
if (-not $OutputDirectory) { $OutputDirectory = Join-Path $operation 'reports' }
New-Item -ItemType Directory -Path $operation -Force | Out-Null
# An old installation can supply dependencies, but never executable launchers or user state.
if (-not $RuntimeSource) {
    $prepared = Join-Path $workspace 'native (dev)/external/VisualEnhancerRuntime'
    if (Test-Path -LiteralPath (Join-Path $prepared 'bin/runtime')) { $RuntimeSource = $prepared }
    elseif (Test-Path -LiteralPath (Join-Path $destinationRoot 'bin/runtime')) { $RuntimeSource = $destinationRoot }
    else { $RuntimeSource = Join-Path $destinationRoot 'app' }
}
& (Join-Path $PSScriptRoot 'package.ps1') -Configuration $Configuration -QtRoot $QtRoot -BuildDir $BuildDir `
    -Destination $stage -NativeBuildRoot $NativeBuildRoot -RuntimeSource $RuntimeSource -MediaRuntimeSource $MediaRuntimeSource `
    -CertificateThumbprint $CertificateThumbprint -TimestampServer $TimestampServer `
    -MachineCertificateStore:$MachineCertificateStore -RequireSignatures:$RequireSignatures -OutputDirectory $OutputDirectory
$entrypointTests = Join-Path $workspace 'Tests/entrypoint_tests.ps1'
if (Test-Path -LiteralPath $entrypointTests -PathType Leaf) {
    & $entrypointTests -PackageRoot $stage -OutputDirectory $OutputDirectory
}
[ordered]@{ stage = $stage; destination = $destinationRoot; operation = $operation } |
    ConvertTo-Json | Set-Content -LiteralPath (Join-Path $OutputDirectory 'stage.json') -Encoding utf8
if ($ScanDefender) {
    & (Join-Path $PSScriptRoot 'scan-release.ps1') -PackageRoot $stage -OutputDirectory $OutputDirectory
}
if ($StageOnly) {
    Write-Host "Validated staged package: $stage"
    return
}
& (Join-Path $PSScriptRoot 'promote-package.ps1') -Stage $stage -Destination $destinationRoot `
    -BackupDirectory (Join-Path $operation 'previous') -OutputDirectory $OutputDirectory
Write-Host "Ready-to-run application: $destinationRoot"
