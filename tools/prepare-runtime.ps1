[CmdletBinding()]
param(
    [string]$QtRoot,
    [string]$DownloadRoot = (Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'VisualEnhancerBuildCache'),
    [string]$Destination
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Toolchain.ps1')
$projectRoot = Split-Path -Parent $PSScriptRoot
$workspaceRoot = Split-Path -Parent $projectRoot
$nativeRoot = Join-Path $workspaceRoot 'native (dev)'
$externalRoot = Join-Path $nativeRoot 'external'
$resolvedQt = Resolve-VeQtRoot $QtRoot
if (-not $Destination) { $Destination = Join-Path $externalRoot 'VisualEnhancerRuntime' }
$destinationRoot = Resolve-VePath $Destination $projectRoot
$downloadDirectory = Resolve-VePath $DownloadRoot $projectRoot
$sevenZip = Get-Command 7z.exe -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -First 1
if (-not $sevenZip) { $sevenZip = Join-Path ([Environment]::GetFolderPath('ProgramFiles')) '7-Zip/7z.exe' }
if (-not (Test-Path -LiteralPath $sevenZip -PathType Leaf)) { throw 'Install 7-Zip to unpack the upstream dependencies.' }
New-Item -ItemType Directory -Force -Path $downloadDirectory, $destinationRoot | Out-Null

# Versions and SHA-256 values are pinned. Only vendor dependencies are extracted
# from archives; no application executable or engine DLL comes from a release.
# Shared libav is prepared separately with the pinned DirectX-capable build.
$downloads = @(
    @{Name='ffmpeg-9.0.2-full_build.7z';Url='https://github.com/GyanD/codexffmpeg/releases/download/9.0.2/ffmpeg-9.0.2-full_build.7z';Hash='f0e46253c70dfe902bac915dfb4224f0cf1b7c6eeab9da2ccc9a5581f9a71b13'},
    @{Name='windows-artifacts.zip';Url='https://github.com/AOMediaCodec/libavif/releases/download/v1.4.2/windows-artifacts.zip';Hash='cb2d9fea43dcbab1d0707e3b37eb7b08070ad2fb60a2c188c39ec12382c0484a'},
    @{Name='exiv2-0.28.9-2022msvc-AMD64.zip';Url='https://github.com/Exiv2/exiv2/releases/download/v0.28.9/exiv2-0.28.9-2022msvc-AMD64.zip';Hash='47b455d7102d5d3b92227e580c582d4c36f0291902dc70af95062c32852d3f1c'},
    @{Name='pillow_heif-1.8.1-cp312-cp312-win_amd64.whl';Url='https://files.pythonhosted.org/packages/db/fd/2ff579be4694ac68cc73bfaafe1abc255bd658b678bfb3b33922784ddaf0/pillow_heif-1.8.1-cp312-cp312-win_amd64.whl';Hash='ce0ff957ad901a5a6bf8cd22ea26c4304bab7cf2f93d0a2f03046487e5711910'},
    # The private NR model has no public SDK/source package. Extract its original
    # NVIDIA DLL alone from the project's published archive, then verify it.
    @{Name='Visual.Enhancer.v15.0.zip';Url='https://github.com/Merserk/dlss5-visual-enhancer/releases/download/v15.0/Visual.Enhancer.v15.0.zip';Hash='0b29deb922c5e8601b1493feb935755e07b89164a88d3922d76e33b6e5ecb93e'}
)
$extracted = @{}
foreach ($download in $downloads) {
    $archive = Join-Path $downloadDirectory $download.Name
    if (-not (Test-Path -LiteralPath $archive -PathType Leaf) -or (Get-FileHash -LiteralPath $archive).Hash -ne $download.Hash) {
        Invoke-VeCommand 'curl.exe' @('--fail','--location','--silent','--show-error','--connect-timeout','15',
            '--max-time','300','--retry','1','--output',$archive,$download.Url)
    }
    if ((Get-FileHash -LiteralPath $archive).Hash -ne $download.Hash) { throw "Dependency checksum failed: $($download.Name)" }
    $directory = Join-Path $downloadDirectory "extracted/$($download.Name)"
    $arguments = @('x', $archive, "-o$directory", '-y', '-bso0', '-bsp0')
    if ($download.Name -eq 'Visual.Enhancer.v15.0.zip') { $arguments += @('*nvngx_dlssnr.dll', '-r') }
    Invoke-VeCommand $sevenZip $arguments
    $extracted[$download.Name] = $directory
}
function Copy-Dependency([string]$Source, [string]$RelativeTarget) {
    if (-not (Test-Path -LiteralPath $Source -PathType Leaf)) { throw "Missing dependency input: $Source" }
    $target = Join-Path $destinationRoot $RelativeTarget
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $target) | Out-Null
    Copy-Item -LiteralPath $Source -Destination $target -Force
}
$ffmpegRoot = Join-Path $extracted['ffmpeg-9.0.2-full_build.7z'] 'ffmpeg-9.0.2-full_build'
foreach ($name in @('ffmpeg.exe', 'ffprobe.exe')) { Copy-Dependency (Join-Path $ffmpegRoot "bin/$name") "bin/ffmpeg/bin/$name" }
Copy-Dependency (Join-Path $ffmpegRoot 'LICENSE') 'bin/ffmpeg/LICENSE'
foreach ($name in @('avifenc.exe', 'avifdec.exe')) { Copy-Dependency (Join-Path $extracted['windows-artifacts.zip'] $name) "bin/avif/$name" }
$exiv2Root = Join-Path $extracted['exiv2-0.28.9-2022msvc-AMD64.zip'] 'exiv2-0.28.9-2022msvc-AMD64'
foreach ($file in Get-ChildItem -LiteralPath (Join-Path $exiv2Root 'bin') -File | Where-Object { $_.Name -eq 'exiv2.exe' -or $_.Extension -eq '.dll' }) {
    Copy-Dependency $file.FullName "bin/exiv2/$($file.Name)"
}
Copy-Dependency (Join-Path $exiv2Root 'COPYING') 'bin/exiv2/COPYING'
# Prepare one coherent runtime; obsolete hashed wheel DLLs must never coexist
# with the selected libav libraries because the loader resolves by prefix.
$avTarget = [IO.Path]::GetFullPath((Join-Path $destinationRoot 'bin/runtime/av'))
$safePrefix = [IO.Path]::GetFullPath($destinationRoot).TrimEnd('\','/') + [IO.Path]::DirectorySeparatorChar
if (-not $avTarget.StartsWith($safePrefix,[StringComparison]::OrdinalIgnoreCase)) { throw 'AV dependency target is outside the runtime.' }
if (Test-Path -LiteralPath $avTarget) {
    if ((Get-Item -LiteralPath $avTarget).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'AV dependency target cannot be a link.' }
}
& (Join-Path $PSScriptRoot 'prepare-media-runtime.ps1') -DownloadRoot $downloadDirectory -Destination $destinationRoot
$currentAvNames = @('avutil-60.dll','avcodec-62.dll','avformat-62.dll','swscale-9.dll','swresample-6.dll')
foreach ($file in Get-ChildItem -LiteralPath $avTarget -File -Filter '*.dll') {
    if ($file.Name -notin $currentAvNames) { Remove-Item -LiteralPath $file.FullName -Force }
}
$heifRoot = $extracted['pillow_heif-1.8.1-cp312-cp312-win_amd64.whl']
foreach ($file in Get-ChildItem -LiteralPath $heifRoot -Recurse -File -Filter '*.dll') { Copy-Dependency $file.FullName "bin/runtime/image-codecs/$($file.Name)" }
Copy-Dependency (Join-Path $heifRoot 'pillow_heif-1.8.1.dist-info/licenses/LICENSES_bundled.txt') 'bin/licenses/HEIF-NATIVE-DEPENDENCIES.txt'
Copy-Dependency (Join-Path $heifRoot 'pillow_heif-1.8.1.dist-info/licenses/LICENSE.txt') 'bin/licenses/PILLOW-HEIF-LICENSE.txt'
$nrModel = @(Get-ChildItem -LiteralPath $extracted['Visual.Enhancer.v15.0.zip'] -Recurse -File -Filter 'nvngx_dlssnr.dll')
if ($nrModel.Count -ne 1 -or (Get-FileHash -LiteralPath $nrModel[0].FullName).Hash -ne '6EB209E764F39872625DEBD6ABAF45E2BB6322F6F270F781F70C059AE30B3927') { throw 'Unexpected NVIDIA Neural Rendering model.' }
Copy-Dependency $nrModel[0].FullName 'bin/runtime/dlssnr/nvngx_dlssnr.dll'
Copy-Dependency (Join-Path $externalRoot 'DLSS/lib/Windows_x86_64/rel/nvngx_dlss.dll') 'bin/runtime/dlsssr/nvngx_dlss.dll'
Copy-Dependency (Join-Path $externalRoot 'DLSS/lib/Windows_x86_64/rel/nvngx_dlssg.dll') 'bin/runtime/dlssg/nvngx_dlssg.dll'
Copy-Dependency (Join-Path $externalRoot 'streamline-sdk-v2.14.16/bin/x64/nvngx_dlssd.dll') 'bin/runtime/dlssrr/nvngx_dlssd.dll'
Copy-Dependency (Join-Path $externalRoot 'streamline-sdk-v2.14.16/bin/x64/nvngx_dlss.license.txt') 'bin/runtime/dlssrr/LICENSE-NVIDIA-DLSS.txt'
foreach ($feature in @('dlssnr', 'dlsssr', 'dlssg')) { Copy-Dependency (Join-Path $externalRoot 'DLSS/LICENSE.txt') "bin/runtime/$feature/LICENSE-NVIDIA-DLSS.txt" }
Copy-Dependency (Join-Path $externalRoot 'RTX_Video_SDK_v1.1.0/NVIDIA_RTX_Video_SDK_License.pdf') 'bin/runtime/rtx_video/NVIDIA_RTX_Video_SDK_License.pdf'

# Retain licensing records directly from the selected Qt kit and upstream tags.
$licenseDirectory = Join-Path $destinationRoot 'bin/licenses'
New-Item -ItemType Directory -Force -Path (Join-Path $licenseDirectory 'qt') | Out-Null
foreach ($module in @('qtbase', 'qtdeclarative', 'qtimageformats', 'qtmultimedia', 'qtshadertools', 'qtsvg')) {
    $inventory = Get-ChildItem -LiteralPath (Join-Path $resolvedQt 'sbom') -File -Filter "$module-*.spdx.json" | Select-Object -First 1
    if (-not $inventory) { throw "Missing Qt license inventory: $module" }
    Copy-Dependency $inventory.FullName "bin/licenses/qt/$($inventory.Name)"
    $sbom = Get-Content -LiteralPath $inventory.FullName -Raw | ConvertFrom-Json
    foreach ($license in $sbom.hasExtractedLicensingInfos) {
        [IO.File]::WriteAllText((Join-Path $licenseDirectory "qt/$($license.licenseId).txt"), $license.extractedText, [Text.UTF8Encoding]::new($false))
    }
}
foreach ($license in @('GPL-2.0-only', 'GPL-3.0-only', 'LGPL-2.1-or-later', 'LGPL-3.0-only', 'Qt-GPL-exception-1.0')) {
    $name = $license.Replace('-only', '').Replace('-or-later', '') + '.txt'
    Invoke-VeCommand 'curl.exe' @('--fail','--location','--silent','--show-error','--connect-timeout','15','--max-time','30',
        '--output',(Join-Path $licenseDirectory $name),"https://raw.githubusercontent.com/qt/qtbase/v6.12.0/LICENSES/$license.txt")
}
Invoke-VeCommand 'curl.exe' @('--fail','--location','--silent','--show-error','--connect-timeout','15','--max-time','30',
    '--output',(Join-Path $destinationRoot 'bin/avif/LICENSE'),'https://raw.githubusercontent.com/AOMediaCodec/libavif/v1.4.2/LICENSE')
Invoke-VeCommand 'curl.exe' @('--fail','--location','--silent','--show-error','--connect-timeout','15','--max-time','30',
    '--output',(Join-Path $licenseDirectory 'Vulkan-Loader-LICENSE.txt'),'https://raw.githubusercontent.com/KhronosGroup/Vulkan-Loader/v1.4.321/LICENSE.txt')
$licenseSource = Join-Path $projectRoot 'LICENSE.txt'
if (-not (Test-Path -LiteralPath $licenseSource -PathType Leaf)) {
    $licenseSource = Join-Path $projectRoot 'LICENSE'
}
Copy-Dependency $licenseSource 'LICENSE.txt'
Copy-Dependency (Join-Path $projectRoot 'THIRD-PARTY-NOTICES') 'THIRD-PARTY-NOTICES.txt'
Copy-Dependency (Join-Path $PSScriptRoot 'QT-RELINKING.txt') 'bin/licenses/QT-RELINKING.txt'
$downloads | ConvertTo-Json -Depth 3 | Set-Content -LiteralPath (Join-Path $destinationRoot 'dependency-sources.json') -Encoding UTF8
Write-Host "Runtime dependencies prepared: $destinationRoot"
