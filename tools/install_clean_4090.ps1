[CmdletBinding()]
param(
    [string]$SourceRoot = "",
    [string]$InstallDir = "",
    [switch]$Launch,
    [switch]$KeepDownload
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$RuntimeTag = "v7.0"
$RuntimeAsset = "DLSS.5.Visual.Enhancer.v7.0.zip"
$RuntimeUrl = "https://github.com/Merserk/dlss5-visual-enhancer/releases/download/v7.0/DLSS.5.Visual.Enhancer.v7.0.zip"
$RuntimeSha256 = "995a3c8ec73cac1dce8b329279b2ec66f4b9f52048b7d119dcb46c6b7e0914cb"

function Write-Step([string]$Message) {
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Resolve-FullPath([string]$PathValue) {
    $expanded = [Environment]::ExpandEnvironmentVariables($PathValue)
    if ([IO.Path]::IsPathRooted($expanded)) {
        return [IO.Path]::GetFullPath($expanded)
    }
    return [IO.Path]::GetFullPath((Join-Path (Get-Location).Path $expanded))
}

function Copy-TreeContents([string]$From, [string]$To) {
    if (-not (Test-Path -LiteralPath $From -PathType Container)) {
        return
    }
    New-Item -ItemType Directory -Path $To -Force | Out-Null
    $robocopy = Get-Command robocopy.exe -ErrorAction Stop
    & $robocopy.Source $From $To /E /COPY:DAT /DCOPY:DAT /R:2 /W:1 /NFL /NDL /NJH /NJS /NP
    $code = $LASTEXITCODE
    # Robocopy uses 0-7 for successful/no-change/copy-with-extra-info states.
    if ($code -gt 7) {
        throw "Robocopy failed while copying '$From' to '$To' (exit $code)."
    }
}

function Test-PortableLayout([string]$Root) {
    $required = @(
        "app.py",
        "start.bat",
        "start_4090.bat",
        "tools\rtx4090_profile.py",
        "bin\python-3.13.15-embed-amd64\python.exe",
        "bin\ffmpeg\bin\ffmpeg.exe",
        "bin\ffmpeg\bin\ffprobe.exe",
        "bin\mpv\mpv.exe",
        "bin\yt-dlp\yt-dlp.exe",
        "bin\runtime\host\nvngx.dll",
        "bin\runtime\host\dxgi.dll",
        "bin\runtime\dlss\nvngx_dlss.dll",
        "bin\runtime\dlssnr\renodx-dlss5.addon64",
        "bin\runtime\dlssnr\nvngx_dlssnr.dll",
        "bin\runtime\dlssg\dlssg-worker.exe",
        "bin\runtime\dlssg\nvngx_dlssg.dll",
        "bin\runtime\rtx_video\rtx-video-worker.exe",
        "bin\runtime\rtx_video\nvngx_vsr.dll",
        "bin\runtime\rtx_video\nvngx_truehdr.dll"
    )
    $missing = @()
    foreach ($relative in $required) {
        if (-not (Test-Path -LiteralPath (Join-Path $Root $relative) -PathType Leaf)) {
            $missing += $relative
        }
    }
    if ($missing.Count -gt 0) {
        throw "Portable installation is incomplete. Missing:`n - $($missing -join "`n - ")"
    }
}

if ($env:OS -ne "Windows_NT") {
    throw "This installer supports native Windows only."
}
if (-not [Environment]::Is64BitOperatingSystem) {
    throw "A 64-bit Windows installation is required."
}

if ([string]::IsNullOrWhiteSpace($SourceRoot)) {
    $SourceRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
}
$SourceRoot = Resolve-FullPath $SourceRoot
if (-not (Test-Path -LiteralPath (Join-Path $SourceRoot "app.py") -PathType Leaf)) {
    throw "SourceRoot does not look like the DLSS5 repository: $SourceRoot"
}

$sourceParent = Split-Path -Parent $SourceRoot
if ([string]::IsNullOrWhiteSpace($InstallDir)) {
    # Sibling directory keeps the 500+ MB portable runtime out of the Git repo and
    # avoids using C:\Temp when the repo is on another drive.
    $InstallDir = Join-Path $sourceParent "DLSS5_4090_PORTABLE"
}
$InstallDir = Resolve-FullPath $InstallDir

$installVolumeRoot = [IO.Path]::GetPathRoot($InstallDir)
if ($InstallDir.TrimEnd('\') -ieq $installVolumeRoot.TrimEnd('\')) {
    throw "InstallDir cannot be the root of a drive. Choose a dedicated application folder."
}
$sourcePrefix = $SourceRoot.TrimEnd('\') + '\'
$installPrefix = $InstallDir.TrimEnd('\') + '\'
if ($InstallDir -ieq $SourceRoot -or $installPrefix.StartsWith($sourcePrefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw "InstallDir must be outside the source repository so the clean overlay cannot recurse into itself."
}

$installParent = Split-Path -Parent $InstallDir
New-Item -ItemType Directory -Path $installParent -Force | Out-Null
$work = Join-Path $installParent (".dlss5-install-" + [Guid]::NewGuid().ToString("N"))
$zipPath = Join-Path $work $RuntimeAsset
$extractPath = Join-Path $work "release"
$stagePath = Join-Path $work "stage"
$backupPath = $null
New-Item -ItemType Directory -Path $work -Force | Out-Null

try {
    Write-Step "Downloading the verified $RuntimeTag portable runtime"
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest -UseBasicParsing -Uri $RuntimeUrl -OutFile $zipPath

    Write-Step "Verifying the official release SHA-256"
    $actualHash = (Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actualHash -ne $RuntimeSha256) {
        throw "Runtime ZIP SHA-256 mismatch. Expected $RuntimeSha256 but received $actualHash. Nothing was installed."
    }

    Write-Step "Extracting the portable release"
    New-Item -ItemType Directory -Path $extractPath -Force | Out-Null
    Expand-Archive -LiteralPath $zipPath -DestinationPath $extractPath -Force

    $candidates = @(
        Get-ChildItem -LiteralPath $extractPath -Filter "app.py" -File -Recurse |
            Where-Object {
                Test-Path -LiteralPath (Join-Path $_.Directory.FullName "start.bat") -PathType Leaf
            }
    )
    if ($candidates.Count -ne 1) {
        throw "Could not identify exactly one portable application root in the verified release."
    }
    $portableRoot = $candidates[0].Directory.FullName

    Write-Step "Building a clean staged install and overlaying this repository"
    New-Item -ItemType Directory -Path $stagePath -Force | Out-Null
    Copy-TreeContents $portableRoot $stagePath

    # Overlay only application/source artifacts. Runtime binaries always come
    # from the verified portable release rather than an arbitrary local bin/.
    $fileItems = @(
        "app.py",
        "start.bat",
        "start_4090.bat",
        "install_clean_4090.bat",
        "README.md",
        "LICENSE",
        ".gitignore"
    )
    foreach ($name in $fileItems) {
        $source = Join-Path $SourceRoot $name
        if (Test-Path -LiteralPath $source -PathType Leaf) {
            Copy-Item -LiteralPath $source -Destination (Join-Path $stagePath $name) -Force
        }
    }
    foreach ($name in @("src", "tools", "docs", "tests")) {
        Copy-TreeContents (Join-Path $SourceRoot $name) (Join-Path $stagePath $name)
    }

    Test-PortableLayout $stagePath

    Write-Step "Installing to $InstallDir"
    if (Test-Path -LiteralPath $InstallDir) {
        $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
        $backupPath = "$InstallDir.backup-$stamp"
        if (Test-Path -LiteralPath $backupPath) {
            throw "Backup destination already exists: $backupPath"
        }
        Write-Host "Existing install is being preserved as: $backupPath" -ForegroundColor Yellow
        Move-Item -LiteralPath $InstallDir -Destination $backupPath
    }

    try {
        Move-Item -LiteralPath $stagePath -Destination $InstallDir
    }
    catch {
        if ($backupPath -and (Test-Path -LiteralPath $backupPath) -and -not (Test-Path -LiteralPath $InstallDir)) {
            Move-Item -LiteralPath $backupPath -Destination $InstallDir
        }
        throw
    }

    Test-PortableLayout $InstallDir

    $sourceCommit = "unknown"
    try {
        $git = Get-Command git.exe -ErrorAction Stop
        $sourceCommit = (& $git.Source -C $SourceRoot rev-parse HEAD 2>$null).Trim()
        if ([string]::IsNullOrWhiteSpace($sourceCommit)) { $sourceCommit = "unknown" }
    }
    catch {}

    $manifest = [ordered]@{
        schema = 1
        installed_utc = [DateTime]::UtcNow.ToString("o")
        source_root = $SourceRoot
        source_commit = $sourceCommit
        runtime_repository = "Merserk/dlss5-visual-enhancer"
        runtime_tag = $RuntimeTag
        runtime_asset = $RuntimeAsset
        runtime_sha256 = $RuntimeSha256
        install_dir = $InstallDir
        previous_install_backup = $backupPath
    }
    $manifest | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $InstallDir "install_manifest_4090.json") -Encoding UTF8

    Write-Step "Applying the RTX 4090 balanced-performance profile"
    $python = Join-Path $InstallDir "bin\python-3.13.15-embed-amd64\python.exe"
    $profile = Join-Path $InstallDir "tools\rtx4090_profile.py"
    & $python $profile --apply --best-settings
    if ($LASTEXITCODE -ne 0) {
        throw "The portable files installed successfully, but the RTX 4090 profile could not be applied. The install was left intact for diagnosis."
    }

    Write-Host ""
    Write-Host "Clean RTX 4090 installation completed." -ForegroundColor Green
    Write-Host "Install: $InstallDir"
    if ($backupPath) {
        Write-Host "Previous install backup: $backupPath"
    }
    Write-Host "Launch with: $(Join-Path $InstallDir 'start_4090.bat')"

    if ($Launch) {
        Write-Step "Launching DLSS 5 Visual Enhancer"
        Start-Process -FilePath (Join-Path $InstallDir "start_4090.bat") -WorkingDirectory $InstallDir
    }
}
finally {
    if (Test-Path -LiteralPath $work) {
        if ($KeepDownload -and (Test-Path -LiteralPath $zipPath)) {
            $savedZip = Join-Path $installParent $RuntimeAsset
            Copy-Item -LiteralPath $zipPath -Destination $savedZip -Force
            Write-Host "Verified runtime ZIP retained at: $savedZip"
        }
        Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue
    }
}
