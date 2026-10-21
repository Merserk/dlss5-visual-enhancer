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
    [string]$OutputDirectory
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Toolchain.ps1')
$projectRoot = Split-Path -Parent $PSScriptRoot
$workspaceRoot = Split-Path -Parent $projectRoot
$resolvedQt = Resolve-VeQtRoot $QtRoot
$resolvedBuild = Resolve-VePath $BuildDir $projectRoot
if (-not $Destination) { $Destination = Join-Path $workspaceRoot 'Visual Enhancer' }
$resolvedDestination = Resolve-VePath $Destination $projectRoot
if ($resolvedDestination -eq $projectRoot -or $resolvedDestination -eq $workspaceRoot) {
    throw 'Deploy to the Visual Enhancer folder or another dedicated application folder.'
}
$appDirectory = $resolvedDestination
$runtimeSource = if ($RuntimeSource) { Resolve-VePath $RuntimeSource $projectRoot }
    elseif (Test-Path -LiteralPath (Join-Path $workspaceRoot 'native (dev)/external/VisualEnhancerRuntime/bin/runtime')) {
        Join-Path $workspaceRoot 'native (dev)/external/VisualEnhancerRuntime'
    } elseif (Test-Path -LiteralPath (Join-Path $workspaceRoot 'Visual Enhancer/bin/runtime')) {
        Join-Path $workspaceRoot 'Visual Enhancer'
    } else { Join-Path $workspaceRoot 'Visual Enhancer/app' }
$binaryDirectory = Join-Path $resolvedBuild $Configuration
if (-not $MediaRuntimeSource) {
    $MediaRuntimeSource = if (Test-Path -LiteralPath (Join-Path $runtimeSource 'bin/runtime/av/source-manifest.json')) { $runtimeSource }
        else { Join-Path $workspaceRoot 'native (dev)/external/VisualEnhancerMediaRuntime' }
}
$mediaSource = Resolve-VePath $MediaRuntimeSource $projectRoot
$mediaAv = Join-Path $mediaSource 'bin/runtime/av'
$mediaManifestPath = Join-Path $mediaAv 'source-manifest.json'
if (-not (Test-Path -LiteralPath $mediaManifestPath -PathType Leaf)) { throw 'Prepare the pinned DirectX media runtime with tools/prepare-media-runtime.ps1.' }
$mediaManifest = Get-Content -LiteralPath $mediaManifestPath -Raw | ConvertFrom-Json
if ($mediaManifest.version -ne '8.1.2') { throw 'Unexpected DirectX media runtime version.' }
$mediaLibraries = @('avutil-60.dll','avcodec-62.dll','avformat-62.dll','swscale-9.dll','swresample-6.dll')
foreach ($name in $mediaLibraries) {
    $record = @($mediaManifest.libraries | Where-Object name -EQ $name)
    if ($record.Count -ne 1 -or (Get-FileHash -LiteralPath (Join-Path $mediaAv $name)).Hash -ne $record[0].sha256) { throw "Media runtime checksum mismatch: $name" }
}
$deployment = Join-Path $resolvedQt 'bin/windeployqt.exe'
foreach ($name in @('Visual Enhancer.exe', 'VE_CLI.exe')) {
    $binary = Join-Path $binaryDirectory $name
    if (-not (Test-Path -LiteralPath $binary)) { throw "Build $Configuration first: $binary is missing." }
}
if (-not (Test-Path -LiteralPath (Join-Path $runtimeSource 'bin/runtime'))) {
    throw "Required media tools and native engines are missing from $runtimeSource/bin. Prepare dependencies with tools/prepare-runtime.ps1 or pass -RuntimeSource."
}
foreach ($required in @('bin/ffmpeg/bin/ffmpeg.exe', 'bin/ffmpeg/bin/ffprobe.exe', 'bin/avif/avifenc.exe',
    'bin/exiv2/exiv2.exe', 'bin/runtime/dlssnr/nvngx_dlssnr.dll', 'bin/runtime/dlsssr/nvngx_dlss.dll',
    'bin/runtime/dlssg/nvngx_dlssg.dll', 'bin/runtime/dlssrr/nvngx_dlssd.dll')) {
    if (-not (Test-Path -LiteralPath (Join-Path $runtimeSource $required) -PathType Leaf)) {
        throw "Required runtime dependency is missing: $runtimeSource/$required"
    }
}
$nativeModules = @{
    neural_rendering = @('dlssnr', 'neuroframe_engine_neural_rendering.dll')
    super_resolution = @('dlsssr', 'neuroframe_engine_super_resolution.dll')
    ray_reconstruction = @('dlssrr', 'neuroframe_engine_ray_reconstruction.dll')
    frame_interpolation = @('dlssg', 'neuroframe_engine_frame_interpolation.dll')
    rtx_video = @('rtx_video', 'neuroframe_engine_upscaling.dll')
    video_effects = @('vfx', 'neuroframe_engine_video_effects.dll')
    reshade = @('reshade', 'neuroframe_engine_reshade.dll')
}
if (-not $NativeBuildRoot) {
    $localNativeRoot = Join-Path $workspaceRoot 'native (dev)/modules'
    $missingLocalEngines = @($nativeModules.Keys | Where-Object {
        -not (Test-Path -LiteralPath (Join-Path $localNativeRoot "$_/build/$($nativeModules[$_][1])") -PathType Leaf)
    })
    if (-not $missingLocalEngines.Count) { $NativeBuildRoot = $localNativeRoot }
}
if ($NativeBuildRoot) {
    $resolvedNativeBuildRoot = Resolve-VePath $NativeBuildRoot $projectRoot
    foreach ($feature in $nativeModules.Keys) {
        $binary = Join-Path $resolvedNativeBuildRoot "$feature/build/$($nativeModules[$feature][1])"
        if (-not (Test-Path -LiteralPath $binary -PathType Leaf)) { throw "Rebuild native engine first: $binary" }
    }
}
# Copy directory contents explicitly so runtime assets become ordinary files,
# including any source asset directories backed by junctions.
function Copy-VeTree([string]$Source, [string]$Target) {
    if ([IO.Path]::GetFullPath($Source) -eq [IO.Path]::GetFullPath($Target)) { return }
    # This dependency set is staged separately from its pinned manifest.
    if ([IO.Path]::GetFullPath($Source) -eq [IO.Path]::GetFullPath((Join-Path $runtimeSource 'bin/runtime/av'))) { return }
    New-Item -ItemType Directory -Path $Target -Force | Out-Null
    foreach ($item in Get-ChildItem -LiteralPath $Source -Force) {
        $targetItem = Join-Path $Target $item.Name
        if ($item.PSIsContainer) { Copy-VeTree $item.FullName $targetItem }
        else { Copy-Item -LiteralPath $item.FullName -Destination $targetItem -Force }
    }
}
if ((Test-Path -LiteralPath $appDirectory) -and @(Get-ChildItem -LiteralPath $appDirectory -Force).Count) {
    throw 'Package into a new empty directory; use deploy.ps1 for validated installation upgrades.'
}
New-Item -ItemType Directory -Path $appDirectory -Force | Out-Null
if ([IO.Path]::GetFullPath($runtimeSource) -ne [IO.Path]::GetFullPath($appDirectory)) {
    # Replacing a wheel can change every DLL filename. Remove superseded files
    # from these supplied dependency sets to avoid ambiguous libav/libheif loads.
    foreach ($relative in @('bin/runtime/av', 'bin/runtime/image-codecs')) {
        $sourceDirectory = Join-Path $runtimeSource $relative
        $targetDirectory = Join-Path $appDirectory $relative
        if (-not (Test-Path -LiteralPath $sourceDirectory -PathType Container)) { throw "Missing dependency set: $sourceDirectory" }
        if (Test-Path -LiteralPath $targetDirectory) {
            if ((Get-Item -LiteralPath $targetDirectory).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "Dependency target cannot be a link: $targetDirectory" }
            foreach ($file in Get-ChildItem -LiteralPath $targetDirectory -File) {
                if (-not (Test-Path -LiteralPath (Join-Path $sourceDirectory $file.Name) -PathType Leaf)) {
                    Remove-Item -LiteralPath $file.FullName -Force
                }
            }
        }
    }
}
Copy-VeTree (Join-Path $runtimeSource 'bin') (Join-Path $appDirectory 'bin')
$targetAv = Join-Path $appDirectory 'bin/runtime/av'
New-Item -ItemType Directory -Path $targetAv -Force | Out-Null
foreach ($name in $mediaLibraries + @('source-manifest.json')) { Copy-Item -LiteralPath (Join-Path $mediaAv $name) -Destination $targetAv -Force }
foreach ($name in @('FFMPEG-SHARED-LICENSE.txt','FFMPEG-SHARED-BUILD.txt')) { Copy-Item -LiteralPath (Join-Path $mediaSource "bin/licenses/$name") -Destination (Join-Path $appDirectory 'bin/licenses') -Force }
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'QT-RELINKING.txt') -Destination (Join-Path $appDirectory 'bin/licenses/QT-RELINKING.txt') -Force
$vfxModuleRoot = Join-Path $workspaceRoot 'native (dev)/modules/video_effects'
$vfxBinary = Join-Path $vfxModuleRoot 'build/neuroframe_engine_video_effects.dll'
if($NativeBuildRoot){$vfxBinary=Join-Path $resolvedNativeBuildRoot 'video_effects/build/neuroframe_engine_video_effects.dll'}
$vfxRuntime = Join-Path $appDirectory 'bin/runtime/vfx'
if(Test-Path -LiteralPath $vfxBinary){
    New-Item -ItemType Directory -Force -Path $vfxRuntime | Out-Null
    Copy-Item -LiteralPath $vfxBinary -Destination $vfxRuntime -Force
    & (Join-Path $vfxModuleRoot 'package_runtime.ps1') -Destination $vfxRuntime
    # NGX plugins shared by RTX Video and VFX must come from the same SDK release.
    $vfxSdk = Join-Path $workspaceRoot 'native (dev)/external/VFXSDK_windows_1.3.0.0/VideoFX'
    foreach ($plugin in @(@('nvvfxvideosuperres','nvngx_vsr.dll'),@('nvvfxtruehdr','nvngx_truehdr.dll'))) {
        Copy-Item -LiteralPath (Join-Path $vfxSdk "features/$($plugin[0])/bin/$($plugin[1])") -Destination (Join-Path $appDirectory "bin/runtime/rtx_video/$($plugin[1])") -Force
    }
}elseif(-not(Test-Path -LiteralPath (Join-Path $vfxRuntime 'neuroframe_engine_video_effects.dll'))){throw 'Build the NVIDIA VFX bridge before packaging the application.'}
$reshadeModuleRoot = Join-Path $workspaceRoot 'native (dev)/modules/reshade'
$reshadeBinary = Join-Path $reshadeModuleRoot 'build/neuroframe_engine_reshade.dll'
if ($NativeBuildRoot) { $reshadeBinary = Join-Path $resolvedNativeBuildRoot 'reshade/build/neuroframe_engine_reshade.dll' }
$reshadeRuntime = Join-Path $appDirectory 'bin/runtime/reshade'
if (Test-Path -LiteralPath $reshadeBinary) {
    & (Join-Path $reshadeModuleRoot 'package_runtime.ps1') -Destination $reshadeRuntime
} elseif (-not (Test-Path -LiteralPath (Join-Path $reshadeRuntime 'neuroframe_engine_reshade.dll'))) {
    throw 'Build the ReShade bridge before packaging the application.'
}
$fsrModuleRoot = Join-Path $workspaceRoot 'native (dev)/modules/fsr_frame_generation'
$fsrBridgePath = if ($NativeBuildRoot) { Join-Path $resolvedNativeBuildRoot 'fsr_frame_generation/build/Release/neuroframe_engine_fsr_frame_generation.dll' }
    else { Join-Path $fsrModuleRoot 'build/Release/neuroframe_engine_fsr_frame_generation.dll' }
& (Join-Path $fsrModuleRoot 'package_runtime.ps1') -Destination (Join-Path $appDirectory 'bin/runtime/fsrfg') -BridgePath $fsrBridgePath
$nvencLicense = Join-Path $workspaceRoot 'native (dev)/external/VideoCodecHeaders-12.2/LICENSE-header.txt'
Copy-Item -LiteralPath $nvencLicense -Destination (Join-Path $appDirectory 'bin/licenses/NVENC-HEADERS-LICENSE.txt') -Force
if ($NativeBuildRoot) {
    foreach ($feature in $nativeModules.Keys) {
        $binary = Join-Path $resolvedNativeBuildRoot "$feature/build/$($nativeModules[$feature][1])"
        $target = Join-Path $appDirectory "bin/runtime/$($nativeModules[$feature][0])/$($nativeModules[$feature][1])"
        Copy-Item -LiteralPath $binary -Destination $target -Force
    }
}
foreach ($name in @('LICENSE.txt', 'THIRD-PARTY-NOTICES.txt')) {
    $source = Join-Path $runtimeSource $name
    if ($name -eq 'THIRD-PARTY-NOTICES.txt' -and (Test-Path -LiteralPath (Join-Path $projectRoot 'THIRD-PARTY-NOTICES') -PathType Leaf)) {
        $source = Join-Path $projectRoot 'THIRD-PARTY-NOTICES'
    } elseif ($name -eq 'LICENSE.txt' -and (Test-Path -LiteralPath (Join-Path $projectRoot 'LICENSE.txt') -PathType Leaf)) {
        $source = Join-Path $projectRoot 'LICENSE.txt'
    }
    $target = Join-Path $appDirectory $name
    if ($source -ne $target -and (Test-Path -LiteralPath $source)) {
        Copy-Item -LiteralPath $source -Destination $target -Force
    }
}
# The optional user guide may come from the source tree or the runtime bundle.
foreach ($guide in @((Join-Path $projectRoot 'FSR-FRAME-GENERATION.md'), (Join-Path $runtimeSource 'FSR-FRAME-GENERATION.md'))) {
    if (Test-Path -LiteralPath $guide -PathType Leaf) {
        Copy-Item -LiteralPath $guide -Destination (Join-Path $appDirectory 'FSR-FRAME-GENERATION.md') -Force
        break
    }
}
$resources = Join-Path $appDirectory 'resources'
$brandingDirectory = Join-Path $resources 'branding'
New-Item -ItemType Directory -Path $brandingDirectory -Force | Out-Null
$brandingSource = Join-Path $runtimeSource 'resources/branding/icon.png'
if (-not (Test-Path -LiteralPath $brandingSource -PathType Leaf)) {
    $brandingSource = Join-Path $workspaceRoot 'native (dev)/assets/icon.png'
}
if (-not (Test-Path -LiteralPath $brandingSource -PathType Leaf)) {
    throw 'The selected runtime source must include resources/branding/icon.png.'
}
Copy-Item -LiteralPath $brandingSource -Destination $brandingDirectory -Force
Copy-VeTree (Join-Path $projectRoot 'src/desktop') (Join-Path $resources 'desktop')
Copy-VeTree (Join-Path $projectRoot 'src/processing/shaders') (Join-Path $resources 'shaders')
foreach ($name in @('Visual Enhancer.exe', 'VE_CLI.exe')) {
    Copy-Item -LiteralPath (Join-Path $binaryDirectory $name) -Destination (Join-Path $resolvedDestination $name) -Force
}
$mode = if ($Configuration -eq 'Debug') { '--debug' } else { '--release' }
$visualStudio = Get-VeVisualStudioInstallation
$originalVcDirectory = [Environment]::GetEnvironmentVariable('VCINSTALLDIR')
try {
    $env:VCINSTALLDIR = (Join-Path $visualStudio.installationPath 'VC') + '\'
    # The Qt-bundled D3DCompiler_47 (6.3.9600) rejects the shader model 5.1 targets used by the ReShade runtime;
    # the in-box Windows 10+ compiler in System32 is used instead.
    $deploymentArguments = @($mode, '--compiler-runtime', '--no-system-d3d-compiler', '--skip-plugin-types', 'qmltooling', '--dir', $appDirectory,
        '--qmldir', (Join-Path $projectRoot 'src/desktop/qml'),
        (Join-Path $appDirectory 'Visual Enhancer.exe'), (Join-Path $appDirectory 'VE_CLI.exe'))
    Invoke-VeCommand $deployment $deploymentArguments
} finally { $env:VCINSTALLDIR = $originalVcDirectory }
$offscreenName = if ($Configuration -eq 'Debug') { 'qoffscreend.dll' } else { 'qoffscreen.dll' }
$offscreenSource = Join-Path $resolvedQt "plugins/platforms/$offscreenName"
if (-not (Test-Path -LiteralPath $offscreenSource)) { throw "Missing Qt offscreen plugin: $offscreenSource" }
$platformDirectory = Join-Path $appDirectory 'platforms'
New-Item -ItemType Directory -Path $platformDirectory -Force | Out-Null
# The CLI uses QGuiApplication with the offscreen platform for image processing.
Copy-Item -LiteralPath $offscreenSource -Destination $platformDirectory -Force
$redistRoot = Join-Path $visualStudio.installationPath 'VC/Redist/MSVC'
$redistVersion = Get-ChildItem -LiteralPath $redistRoot -Directory |
    Where-Object { $_.Name -match '^\d+\.\d+\.\d+$' } |
    Sort-Object { [version]$_.Name } -Descending | Select-Object -First 1
if (-not $redistVersion) { throw 'The Visual C++ runtime directory was not found.' }
$architecturePath = if ($Configuration -eq 'Debug') { 'debug_nonredist/x64' } else { 'x64' }
$crtDirectory = Get-ChildItem -LiteralPath (Join-Path $redistVersion.FullName $architecturePath) -Directory |
    Where-Object { $_.Name -match '^Microsoft\.VC\d+\.(Debug)?CRT$' } | Select-Object -First 1
if (-not $crtDirectory) { throw 'The matching x64 Visual C++ runtime was not found.' }
Get-ChildItem -LiteralPath $crtDirectory.FullName -File -Filter '*.dll' | ForEach-Object {
    Copy-Item -LiteralPath $_.FullName -Destination $appDirectory -Force
}
# Runtime paths are embedded in the application executables.
$externalQtConfiguration = Join-Path $resolvedDestination 'qt.conf'
if (Test-Path -LiteralPath $externalQtConfiguration) { Remove-Item -LiteralPath $externalQtConfiguration -Force }
# Remove only obsolete application entry files after the replacement executables
# and their dependencies have been deployed successfully.
foreach ($name in @('Visual Enhancer.app.dll', 'VE_CLI.app.dll', 'Visual Enhancer.app.exe', 'VE_CLI.app.exe', 'qt.conf')) {
    $obsolete = Join-Path $appDirectory $name
    if (Test-Path -LiteralPath $obsolete) { Remove-Item -LiteralPath $obsolete -Force }
}
# The application fixes its Qt Quick style to Basic. Keep its runtime imports,
# translations and dialogs, but omit development metadata and unused styles.
# Validate each target before removing old files from an existing installation.
function Remove-VePackagedArtifact([string]$RelativePath) {
    $appRoot = [IO.Path]::GetFullPath($appDirectory).TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar
    $target = [IO.Path]::GetFullPath((Join-Path $appDirectory $RelativePath))
    if (-not $target.StartsWith($appRoot, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Packaging cleanup target is outside the package: $RelativePath"
    }
    if (Test-Path -LiteralPath $target) {
        $item = Get-Item -LiteralPath $target -Force
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw "Packaging cleanup cannot follow a linked directory: $RelativePath"
        }
        if ($item.PSIsContainer) {
            $links = @(Get-ChildItem -LiteralPath $target -Recurse -Force |
                Where-Object { $_.Attributes -band [IO.FileAttributes]::ReparsePoint })
            if ($links.Count) { throw "Packaging cleanup found a linked directory in $RelativePath" }
        }
        Remove-Item -LiteralPath $target -Recurse -Force
    }
}
foreach ($relative in @('release', 'qmltooling', 'vc_redist.x64.exe', 'qml/QtQuick/NativeStyle')) {
    Remove-VePackagedArtifact $relative
}
foreach ($style in @('Fusion', 'Imagine', 'Material', 'Universal', 'FluentWinUI3', 'Windows', 'Native')) {
    Remove-VePackagedArtifact "qml/QtQuick/Controls/$style"
    foreach ($suffix in @('.dll', 'd.dll', 'StyleImpl.dll', 'StyleImpld.dll')) {
        Remove-VePackagedArtifact "Qt6QuickControls2$style$suffix"
    }
}
foreach ($name in @('Qt6QuickNativeStyle.dll', 'Qt6QuickNativeStyled.dll')) {
    Remove-VePackagedArtifact $name
}
foreach ($directory in @('qml', 'resources/desktop/translations')) {
    Get-ChildItem -LiteralPath (Join-Path $appDirectory $directory) -Recurse -File |
        Where-Object { $_.Extension -in @('.qmltypes', '.ts') } | ForEach-Object {
            Remove-VePackagedArtifact ([IO.Path]::GetRelativePath($appDirectory, $_.FullName))
        }
}
foreach ($directory in @('outputs', 'logs')) {
    New-Item -ItemType Directory -Path (Join-Path $resolvedDestination $directory) -Force | Out-Null
}
if ($CertificateThumbprint) {
    & (Join-Path $PSScriptRoot 'sign-release.ps1') -PackageRoot $resolvedDestination `
        -CertificateThumbprint $CertificateThumbprint -TimestampServer $TimestampServer `
        -MachineCertificateStore:$MachineCertificateStore
    # Signing changes the application bridge's hash; vendor binaries stay pinned.
    $fsrManifestPath = Join-Path $appDirectory 'bin/runtime/fsrfg/runtime-manifest.json'
    $fsrManifest = Get-Content -LiteralPath $fsrManifestPath -Raw | ConvertFrom-Json
    foreach ($record in $fsrManifest.binaries) {
        if ($record.File -eq 'neuroframe_engine_fsr_frame_generation.dll') {
            $record.SHA256 = (Get-FileHash -LiteralPath (Join-Path $appDirectory "bin/runtime/fsrfg/$($record.File)")).Hash
        }
    }
    $fsrManifest | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $fsrManifestPath -Encoding utf8
}
& (Join-Path $PSScriptRoot 'audit-release.ps1') -PackageRoot $resolvedDestination `
    -RequireSignatures:($RequireSignatures -or [bool]$CertificateThumbprint) -OutputDirectory $OutputDirectory
Write-Host "Ready-to-run application: $resolvedDestination"
