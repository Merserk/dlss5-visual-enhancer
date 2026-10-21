[CmdletBinding()]
param(
    [string]$DownloadRoot = (Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'VisualEnhancerBuildCache'),
    [string]$ArchivePath,
    [string]$Destination,
    [switch]$UpdateHeaders
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Toolchain.ps1')
$workspaceRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
if (-not $Destination) { $Destination = Join-Path $workspaceRoot 'native (dev)/external/VisualEnhancerMediaRuntime' }
$destinationRoot = Resolve-VePath $Destination $workspaceRoot
$cache = Resolve-VePath $DownloadRoot $workspaceRoot
$name = 'ffmpeg-8.1.2-full_build-shared.7z'
$url = "https://github.com/GyanD/codexffmpeg/releases/download/8.1.2/$name"
$hash = 'cba748035c21ce1431d0823c7a3a711f38616f89f87a265dceddf9b7f6749d2d'
New-Item -ItemType Directory -Path $cache -Force | Out-Null
if (-not $ArchivePath) {
    $ArchivePath = Join-Path $cache $name
    if (-not (Test-Path -LiteralPath $ArchivePath) -or (Get-FileHash -LiteralPath $ArchivePath).Hash -ne $hash) {
        Invoke-VeCommand 'curl.exe' @('--fail','--location','--silent','--show-error','--connect-timeout','15','--max-time','240','--output',$ArchivePath,$url)
    }
}
if ((Get-FileHash -LiteralPath $ArchivePath).Hash -ne $hash) { throw 'FFmpeg shared runtime checksum mismatch.' }
$sevenZip = Join-Path ([Environment]::GetFolderPath('ProgramFiles')) '7-Zip/7z.exe'
if (-not (Test-Path -LiteralPath $sevenZip)) { $sevenZip = (Get-Command 7z.exe -ErrorAction Stop).Source }
$extract = Join-Path $cache 'ffmpeg-shared-8.1.2'
Invoke-VeCommand $sevenZip @('x',$ArchivePath,"-o$extract",'-y','-bso0','-bsp0')
$upstream = Join-Path $extract 'ffmpeg-8.1.2-full_build-shared'
$libraries = Join-Path $destinationRoot 'bin/runtime/av'
$licenses = Join-Path $destinationRoot 'bin/licenses'
New-Item -ItemType Directory -Path $libraries,$licenses -Force | Out-Null
$records = @()
foreach ($file in @('avutil-60.dll','avcodec-62.dll','avformat-62.dll','swscale-9.dll','swresample-6.dll')) {
    $source = Join-Path $upstream "bin/$file"
    Copy-Item -LiteralPath $source -Destination (Join-Path $libraries $file) -Force
    $records += [ordered]@{name=$file;sha256=(Get-FileHash -LiteralPath $source).Hash.ToLowerInvariant()}
}
Copy-Item -LiteralPath (Join-Path $upstream 'LICENSE') -Destination (Join-Path $licenses 'FFMPEG-SHARED-LICENSE.txt') -Force
Copy-Item -LiteralPath (Join-Path $upstream 'README.txt') -Destination (Join-Path $licenses 'FFMPEG-SHARED-BUILD.txt') -Force
[ordered]@{version='8.1.2';source=$url;archive_sha256=$hash;upstream='https://github.com/FFmpeg/FFmpeg/tree/n8.1.2';libraries=$records} |
    ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $libraries 'source-manifest.json') -Encoding utf8
if ($UpdateHeaders) {
    $headers = Join-Path (Split-Path -Parent $PSScriptRoot) 'third_party/ffmpeg'
    foreach ($directory in Get-ChildItem -LiteralPath (Join-Path $upstream 'include') -Directory) {
        $target = Join-Path $headers $directory.Name
        New-Item -ItemType Directory -Path $target -Force | Out-Null
        foreach ($header in Get-ChildItem -LiteralPath $directory.FullName -File -Filter '*.h') { Copy-Item -LiteralPath $header.FullName -Destination $target -Force }
    }
    Copy-Item -LiteralPath (Join-Path $libraries 'source-manifest.json') -Destination (Join-Path $headers 'source-manifest.json') -Force
}
Write-Host "Prepared FFmpeg 8.1.2 with DirectX decode, AMD AMF and Intel QSV: $destinationRoot"
