[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$PackageRoot,
    [string]$OutputDirectory,
    [switch]$RequireSignatures
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Release.ps1')
$root = (Resolve-Path -LiteralPath $PackageRoot).Path
foreach ($obsolete in @('app', 'data', 'Visual Enhancer.app.exe', 'VE_CLI.app.exe', 'qt.conf')) {
    if (Test-Path -LiteralPath (Join-Path $root $obsolete)) { throw "Obsolete package layout: $obsolete" }
}
$qtSuffix = if (Test-Path -LiteralPath (Join-Path $root 'Qt6Cored.dll')) { 'd' } else { '' }
foreach ($required in @('resources/desktop/qml/Main.qml', 'resources/desktop/app.ico',
    'resources/shaders', "platforms/qwindows$qtSuffix.dll", "platforms/qoffscreen$qtSuffix.dll",
    "Qt6Core$qtSuffix.dll", "Qt6Gui$qtSuffix.dll", "Qt6Quick$qtSuffix.dll", 'qml/QtQuick/Controls/Basic',
    'bin/ffmpeg/bin/ffmpeg.exe', 'bin/ffmpeg/bin/ffprobe.exe', 'bin/avif/avifenc.exe',
    'bin/avif/avifdec.exe', 'bin/exiv2/exiv2.exe', 'LICENSE.txt', 'THIRD-PARTY-NOTICES.txt',
    'bin/runtime/reshade/runtime-manifest.json',
    'bin/runtime/reshade/models/moge-2-vits/model.onnx',
    'bin/runtime/reshade/presets/FDVanilla/FDVanilla_reshade.ini')) {
    if (-not (Test-Path -LiteralPath (Join-Path $root $required))) { throw "Missing package dependency: $required" }
}
$owned = @(Get-VeOwnedBinaries $root)
$ownedPaths = @($owned.FullName)
$releaseVersion = $owned[0].VersionInfo.ProductVersion
$contracts = @{
    neural_rendering = 'dlssnr/neuroframe_engine_neural_rendering.dll'
    super_resolution = 'dlsssr/neuroframe_engine_super_resolution.dll'
    ray_reconstruction = 'dlssrr/neuroframe_engine_ray_reconstruction.dll'
    frame_interpolation = 'dlssg/neuroframe_engine_frame_interpolation.dll'
    fsr_frame_generation = 'fsrfg/neuroframe_engine_fsr_frame_generation.dll'
    rtx_video = 'rtx_video/neuroframe_engine_upscaling.dll'
    video_effects = 'vfx/neuroframe_engine_video_effects.dll'
    reshade = 'reshade/neuroframe_engine_reshade.dll'
}
foreach ($module in $contracts.Keys) {
    $contractPath = Join-Path (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)) "native (dev)/modules/$module/contract.json"
    $contract = Get-Content -LiteralPath $contractPath -Raw | ConvertFrom-Json
    if ($module -eq 'fsr_frame_generation') {
        if ($contract.abi -ne 1 -or $contract.initialization_abi -ne 1 -or $contract.provider -ne '3.1.6') { throw 'Invalid FSR 3 engine contract.' }
    } elseif ($module -ne 'video_effects' -and $contract.initialization_abi -ne 2) { throw "Outdated initialization contract: $module" }
    $exports = @(Get-VePeExports (Join-Path $root ('bin/runtime/' + $contracts[$module])))
    foreach ($export in $contract.exports) {
        if ($export -notin $exports) { throw "Missing $module export: $export" }
    }
}
# Require the pinned FSR 3 payload and one unambiguous FFmpeg dependency set.
$fsrRuntime = Join-Path $root 'bin/runtime/fsrfg'
$fsrManifest = Get-Content -LiteralPath (Join-Path $fsrRuntime 'runtime-manifest.json') -Raw | ConvertFrom-Json
if ($fsrManifest.selected_provider -ne '3.1.6') { throw 'The package must select AMD FSR 3.1.6.' }
foreach ($record in $fsrManifest.binaries) {
    if ($record.File -ne [IO.Path]::GetFileName($record.File)) { throw 'Invalid FSR manifest filename.' }
    $path = Join-Path $fsrRuntime $record.File
    if ((Get-FileHash -LiteralPath $path).Hash -ne $record.SHA256) { throw "FSR runtime checksum mismatch: $($record.File)" }
}
foreach ($required in @('amd_fidelityfx_framegeneration_dx12.dll','WinPixEventRuntime.dll','LICENSE-AMD-FSR.md','LICENSE-AMD-OPTICAL-FLOW.txt','LICENSE-PIX.txt','contract.json')) {
    if (-not (Test-Path -LiteralPath (Join-Path $fsrRuntime $required) -PathType Leaf)) { throw "Missing FSR runtime dependency: $required" }
}
foreach ($file in Get-ChildItem -LiteralPath $fsrRuntime -Recurse -File) {
    if ($file.Name -match '(?i)nvofapi|nvcuda|cudart|cublas|python|torch') { throw "Forbidden FSR runtime dependency: $($file.Name)" }
}
if ((Get-FileHash -LiteralPath (Join-Path $fsrRuntime 'amd_fidelityfx_framegeneration_dx12.dll')).Hash -ne '02297BEEDD285E822D3A64F314CF00FAF378DCEC0EDC47FF0C4DD71B3A8C2F18') { throw 'Unrecognized AMD FSR vendor DLL.' }
$mediaRuntime = Join-Path $root 'bin/runtime/av'
$mediaManifest = Get-Content -LiteralPath (Join-Path $mediaRuntime 'source-manifest.json') -Raw | ConvertFrom-Json
if ($mediaManifest.version -ne '8.1.2') { throw 'The DirectX video path requires the pinned FFmpeg runtime.' }
$mediaNames = @('avutil-60.dll','avcodec-62.dll','avformat-62.dll','swscale-9.dll','swresample-6.dll')
if (@(Get-ChildItem -LiteralPath $mediaRuntime -File -Filter '*.dll').Count -ne $mediaNames.Count) { throw 'Ambiguous AV runtime dependency set.' }
foreach ($name in $mediaNames) {
    $record = @($mediaManifest.libraries | Where-Object name -EQ $name)
    if ($record.Count -ne 1 -or (Get-FileHash -LiteralPath (Join-Path $mediaRuntime $name)).Hash -ne $record[0].sha256) { throw "AV runtime checksum mismatch: $name" }
}
# ReShade motion is AMD FidelityFX Optical Flow only; its private runtime must stay free of NVIDIA Optical Flow,
# RTGI shaders, Python and PyTorch.
$reshadeRuntime = Join-Path $root 'bin/runtime/reshade'
foreach ($file in (Get-ChildItem -LiteralPath $reshadeRuntime -Recurse -File)) {
    if ($file.Name -match '(?i)nvofapi|nvofa|^rtgi|pytorch|torch|python') { throw "Forbidden ReShade runtime payload: $($file.FullName)" }
    if ($file.Extension -in @('.fx', '.fxh') -and $file.Name -match '(?i)rtgi|mxao_gi|global.?illumination') { throw "RTGI shader is not allowed: $($file.FullName)" }
}
foreach ($binary in @('ve_reshade_runtime.dll', 'neuroframe_engine_reshade.dll', 'onnxruntime.dll')) {
    $path = Join-Path $reshadeRuntime $binary
    if (-not (Test-Path -LiteralPath $path)) { throw "Missing ReShade runtime binary: $binary" }
    $bytes = [IO.File]::ReadAllBytes($path)
    $text = [Text.Encoding]::ASCII.GetString($bytes) + [Text.Encoding]::Unicode.GetString($bytes)
    if ($text -match '(?i)nvofapi') { throw "ReShade runtime binary references NVIDIA Optical Flow: $binary" }
}

$inventory = foreach ($file in (Get-ChildItem -LiteralPath $root -Recurse -File |
    Where-Object { $_.Extension -in @('.exe', '.dll') -and -not $_.FullName.StartsWith((Join-Path $root 'outputs') + '\', [StringComparison]::OrdinalIgnoreCase) -and -not $_.FullName.StartsWith((Join-Path $root 'logs') + '\', [StringComparison]::OrdinalIgnoreCase) } | Sort-Object FullName)) {
    $isOwned = $file.FullName -in $ownedPaths
    $signature = Get-AuthenticodeSignature -LiteralPath $file.FullName
    $security = Get-VePeSecurity $file.FullName
    if ($isOwned) {
        if ($file.VersionInfo.CompanyName -ne 'Merserk' -or
            $file.VersionInfo.OriginalFilename -ne $file.Name) { throw "Incorrect application identity: $($file.FullName)" }
        if (-not $security.x64 -or -not $security.aslr -or -not $security.dep -or
            -not $security.high_entropy_aslr -or -not $security.control_flow_guard) {
            throw "Missing Windows security protection: $($file.FullName)"
        }
        if ($file.Extension -eq '.exe' -and $file.VersionInfo.ProductVersion -ne $releaseVersion) {
            throw "Inconsistent application release version: $($file.Name)"
        }
        if (($file.Name -eq 'Visual Enhancer.exe' -and $security.subsystem -ne 2) -or
            ($file.Name -eq 'VE_CLI.exe' -and $security.subsystem -ne 3)) { throw "Incorrect application subsystem: $($file.Name)" }
        if ($RequireSignatures -and ($signature.Status -ne 'Valid' -or -not $signature.TimeStamperCertificate)) {
            throw "A trusted, timestamped signature is required: $($file.Name)"
        }
        if ($signature.Status -notin @('Valid', 'NotSigned')) { throw "Invalid application signature: $($file.Name)" }
    }
    [pscustomobject]@{
        path = [IO.Path]::GetRelativePath($root, $file.FullName).Replace('\', '/')
        size_bytes = $file.Length
        sha256 = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
        application_owned = $isOwned
        company = $file.VersionInfo.CompanyName
        product = $file.VersionInfo.ProductName
        file_version = $file.VersionInfo.FileVersion
        product_version = $file.VersionInfo.ProductVersion
        signature_status = $signature.Status.ToString()
        signer = if ($signature.SignerCertificate) { $signature.SignerCertificate.Subject } else { $null }
        timestamped = [bool]$signature.TimeStamperCertificate
        windows_security = $security
    }
}
$report = [ordered]@{
    schema_version = 2
    product = 'Visual Enhancer'
    release_version = $releaseVersion
    generated_utc = [DateTime]::UtcNow.ToString('o')
    signatures_required = [bool]$RequireSignatures
    layout = 'Direct GUI and CLI executables; root outputs/logs; per-user settings and caches.'
    neural_rendering = 'Private NVIDIA interface; existing temporary nvngx.dll caller alias retained for compatibility.'
    binaries = @($inventory)
}
if ($OutputDirectory) {
    New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
    $report | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $OutputDirectory 'binary-inventory.json') -Encoding utf8
    $payload = @(Get-ChildItem -LiteralPath $root -Recurse -File | Where-Object {
        -not $_.FullName.StartsWith((Join-Path $root 'outputs') + '\', [StringComparison]::OrdinalIgnoreCase) -and
        -not $_.FullName.StartsWith((Join-Path $root 'logs') + '\', [StringComparison]::OrdinalIgnoreCase)
    } | Sort-Object FullName | ForEach-Object {
        [pscustomobject]@{ path = [IO.Path]::GetRelativePath($root, $_.FullName).Replace('\','/'); size_bytes = $_.Length; sha256 = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant() }
    })
    $payload | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $OutputDirectory 'package-files.json') -Encoding utf8
    @($payload | ForEach-Object { "$($_.sha256)  $($_.path)" }) |
        Set-Content -LiteralPath (Join-Path $OutputDirectory 'SHA256SUMS.txt') -Encoding utf8
    Write-Host "Inventory and SHA-256 hashes: $OutputDirectory"
}
Write-Host "Release audit passed for $($owned.Count) application binaries; $($inventory.Count) binaries inventoried."
