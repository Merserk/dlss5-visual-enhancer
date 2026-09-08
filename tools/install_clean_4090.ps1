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

function Normalize-PathArgument([string]$PathValue, [string]$Name) {
    if ($null -eq $PathValue) {
        return ""
    }
    $value = [Environment]::ExpandEnvironmentVariables([string]$PathValue).Trim()
    # A quoted cmd.exe argument that ends in a backslash can reach Windows
    # PowerShell with a literal quote still attached. Quotes are illegal in a
    # Windows path, so remove only quote characters at the outside of the value.
    $value = $value.Trim([char[]]@('"', "'"))
    if ($value.IndexOf([char]0) -ge 0) {
        throw "$Name contains a NUL character."
    }
    if ($value.IndexOf('"') -ge 0) {
        throw "$Name contains an unexpected quote character: $value"
    }
    return $value
}

function Resolve-FullPath([string]$PathValue, [string]$Name = "Path") {
    $expanded = Normalize-PathArgument $PathValue $Name
    if ([string]::IsNullOrWhiteSpace($expanded)) {
        throw "$Name is empty."
    }
    try {
        if ([IO.Path]::IsPathRooted($expanded)) {
            return [IO.Path]::GetFullPath($expanded)
        }
        return [IO.Path]::GetFullPath((Join-Path (Get-Location).Path $expanded))
    }
    catch [System.ArgumentException] {
        throw "$Name is not a valid Windows path: '$expanded'. $($_.Exception.Message)"
    }
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
        "bin\runtime\dlssg\nvngx_dlssg.dll",
        "bin\runtime\dlssg\dlssg-worker.exe",
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
    return $missing
}

try {
    if (-not [Environment]::Is64BitOperatingSystem) {
        throw "A 64-bit Windows installation is required."
    }

    if ([string]::IsNullOrWhiteSpace($SourceRoot)) {
        $SourceRoot = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
    }
    $SourceRoot = Resolve-FullPath $SourceRoot "SourceRoot"
    if (-not (Test-Path -LiteralPath $SourceRoot -PathType Container)) {
        throw "SourceRoot does not exist: $SourceRoot"
    }

    if ([string]::IsNullOrWhiteSpace($InstallDir)) {
        $sourceParent = Split-Path -Parent $SourceRoot
        $InstallDir = Join-Path $sourceParent "DLSS5_4090_PORTABLE"
    }
    $InstallDir = Resolve-FullPath $InstallDir "InstallDir"

    if ([string]::Equals($SourceRoot.TrimEnd('\'), $InstallDir.TrimEnd('\'), [StringComparison]::OrdinalIgnoreCase)) {
        throw "InstallDir must be different from SourceRoot. Choose another folder."
    }

    $sourceFiles = @("app.py", "start.bat", "start_4090.bat", "src", "tools", "docs", "LICENSE", "README.md")
    foreach ($requiredSource in $sourceFiles) {
        $candidate = Join-Path $SourceRoot $requiredSource
        if (-not (Test-Path -LiteralPath $candidate)) {
            throw "Source overlay is incomplete; missing: $candidate"
        }
    }

    Write-Step "Preparing clean installation"
    $stagingRoot = Join-Path ([IO.Path]::GetTempPath()) ("dlss5-4090-install-" + [Guid]::NewGuid().ToString("N"))
    $downloadPath = Join-Path $stagingRoot $RuntimeAsset
    $extractRoot = Join-Path $stagingRoot "runtime"
    New-Item -ItemType Directory -Path $stagingRoot -Force | Out-Null

    try {
        Write-Step "Downloading verified $RuntimeTag portable runtime"
        Invoke-WebRequest -UseBasicParsing -Uri $RuntimeUrl -OutFile $downloadPath

        Write-Step "Verifying SHA-256"
        $actualHash = (Get-FileHash -LiteralPath $downloadPath -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actualHash -ne $RuntimeSha256) {
            throw "Runtime archive hash mismatch. Expected $RuntimeSha256 but received $actualHash."
        }

        Write-Step "Extracting portable runtime"
        Expand-Archive -LiteralPath $downloadPath -DestinationPath $extractRoot -Force
        $runtimeRoot = $extractRoot
        $children = @(Get-ChildItem -LiteralPath $extractRoot -Force)
        if ($children.Count -eq 1 -and $children[0].PSIsContainer) {
            $runtimeRoot = $children[0].FullName
        }
        if (-not (Test-Path -LiteralPath (Join-Path $runtimeRoot "app.py") -PathType Leaf)) {
            $candidate = Get-ChildItem -LiteralPath $extractRoot -Filter app.py -File -Recurse | Select-Object -First 1
            if ($null -eq $candidate) {
                throw "Downloaded archive does not contain app.py."
            }
            $runtimeRoot = Split-Path -Parent $candidate.FullName
        }

        Write-Step "Building clean RTX 4090 installation"
        $buildRoot = Join-Path $stagingRoot "build"
        New-Item -ItemType Directory -Path $buildRoot -Force | Out-Null
        Copy-TreeContents $runtimeRoot $buildRoot
        foreach ($item in $sourceFiles) {
            $from = Join-Path $SourceRoot $item
            $to = Join-Path $buildRoot $item
            if (Test-Path -LiteralPath $from -PathType Container) {
                Copy-TreeContents $from $to
            }
            elseif (Test-Path -LiteralPath $from -PathType Leaf) {
                $parent = Split-Path -Parent $to
                New-Item -ItemType Directory -Path $parent -Force | Out-Null
                Copy-Item -LiteralPath $from -Destination $to -Force
            }
        }

        $missing = @(Test-PortableLayout $buildRoot)
        if ($missing.Count -gt 0) {
            throw "Built portable installation is missing required runtime files:`n - $($missing -join "`n - ")"
        }

        Write-Step "Installing to $InstallDir"
        $backupPath = $null
        if (Test-Path -LiteralPath $InstallDir) {
            $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
            $backupPath = "$InstallDir.backup-$stamp"
            if (Test-Path -LiteralPath $backupPath) {
                throw "Backup path already exists: $backupPath"
            }
            Move-Item -LiteralPath $InstallDir -Destination $backupPath
            Write-Host "Existing installation moved to: $backupPath"
        }
        try {
            Move-Item -LiteralPath $buildRoot -Destination $InstallDir
        }
        catch {
            if ($backupPath -and -not (Test-Path -LiteralPath $InstallDir) -and (Test-Path -LiteralPath $backupPath)) {
                Move-Item -LiteralPath $backupPath -Destination $InstallDir
            }
            throw
        }

        Write-Step "Applying RTX 4090 best profile"
        $profilePython = Join-Path $InstallDir "bin\python-3.13.15-embed-amd64\python.exe"
        $profileTool = Join-Path $InstallDir "tools\rtx4090_profile.py"
        & $profilePython $profileTool --apply
        if ($LASTEXITCODE -ne 0) {
            throw "RTX 4090 profile application failed with exit code $LASTEXITCODE."
        }

        Write-Step "Installation complete"
        Write-Host "Installed: $InstallDir" -ForegroundColor Green
        if ($backupPath) {
            Write-Host "Previous install backup: $backupPath"
        }

        if ($Launch) {
            Write-Step "Launching RTX 4090 profile"
            $launcher = Join-Path $InstallDir "start_4090.bat"
            Start-Process -FilePath $launcher -WorkingDirectory $InstallDir
        }
    }
    finally {
        if (-not $KeepDownload -and (Test-Path -LiteralPath $stagingRoot)) {
            Remove-Item -LiteralPath $stagingRoot -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
}
catch {
    Write-Error $_
    exit 1
}
