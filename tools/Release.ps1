Set-StrictMode -Version Latest

function Get-VeOwnedBinaries([string]$PackageRoot) {
    foreach ($relative in @('Visual Enhancer.exe', 'VE_CLI.exe',
        'bin/runtime/dlssnr/neuroframe_engine_neural_rendering.dll',
        'bin/runtime/dlsssr/neuroframe_engine_super_resolution.dll',
        'bin/runtime/dlssrr/neuroframe_engine_ray_reconstruction.dll',
        'bin/runtime/dlssg/neuroframe_engine_frame_interpolation.dll',
        'bin/runtime/fsrfg/neuroframe_engine_fsr_frame_generation.dll',
        'bin/runtime/rtx_video/neuroframe_engine_upscaling.dll',
        'bin/runtime/vfx/neuroframe_engine_video_effects.dll',
        'bin/runtime/reshade/neuroframe_engine_reshade.dll',
        'bin/runtime/reshade/ve_reshade_runtime.dll')) {
        $path = Join-Path $PackageRoot $relative
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Missing application binary: $relative" }
        Get-Item -LiteralPath $path
    }
}

function Get-VeSignTool {
    $command = Get-Command signtool.exe -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    $kit = Join-Path ([Environment]::GetFolderPath('ProgramFilesX86')) 'Windows Kits/10/bin'
    if (Test-Path -LiteralPath $kit) {
        foreach ($version in (Get-ChildItem -LiteralPath $kit -Directory |
            Where-Object Name -Match '^\d+\.\d+\.\d+\.\d+$' |
            Sort-Object { [version]$_.Name } -Descending)) {
            $tool = Join-Path $version.FullName 'x64/signtool.exe'
            if (Test-Path -LiteralPath $tool -PathType Leaf) { return $tool }
        }
    }
    throw 'Install the Windows SDK signing tools to sign a release.'
}

function Get-VePeSecurity([string]$Path) {
    $stream = [IO.File]::OpenRead($Path)
    $reader = [IO.BinaryReader]::new($stream)
    try {
        if ($reader.ReadUInt16() -ne 0x5A4D) { throw "Not a Windows PE file: $Path" }
        $stream.Position = 0x3C
        $pe = $reader.ReadInt32()
        if ($pe -lt 64 -or $pe -gt ($stream.Length - 96)) { throw "Invalid PE header: $Path" }
        $stream.Position = $pe
        if ($reader.ReadUInt32() -ne 0x00004550) { throw "Invalid PE signature: $Path" }
        $machine = $reader.ReadUInt16()
        $stream.Position = $pe + 24
        $magic = $reader.ReadUInt16()
        $stream.Position = $pe + 24 + 70
        $characteristics = $reader.ReadUInt16()
        $stream.Position = $pe + 24 + 68
        $subsystem = $reader.ReadUInt16()
        [pscustomobject]@{
            x64 = ($machine -eq 0x8664 -and $magic -eq 0x20B)
            aslr = [bool]($characteristics -band 0x40)
            high_entropy_aslr = [bool]($characteristics -band 0x20)
            dep = [bool]($characteristics -band 0x100)
            control_flow_guard = [bool]($characteristics -band 0x4000)
            subsystem = $subsystem
        }
    } finally { $reader.Dispose() }
}

# Inspect exports as data. Never load an unverified engine into the audit process.
function Get-VePeExports([string]$Path) {
    $stream = [IO.File]::OpenRead($Path)
    $reader = [IO.BinaryReader]::new($stream)
    try {
        $stream.Position = 0x3C
        $pe = $reader.ReadUInt32()
        $stream.Position = $pe + 6
        $count = $reader.ReadUInt16()
        $stream.Position = $pe + 20
        $optionalSize = $reader.ReadUInt16()
        $stream.Position = $pe + 24
        if ($reader.ReadUInt16() -ne 0x20B) { throw "Expected a 64-bit native engine: $Path" }
        $stream.Position = $pe + 24 + 112
        $exportRva = $reader.ReadUInt32()
        $sections = for ($i = 0; $i -lt $count; ++$i) {
            $stream.Position = $pe + 24 + $optionalSize + $i * 40 + 8
            [pscustomobject]@{ size = $reader.ReadUInt32(); rva = $reader.ReadUInt32(); rawSize = $reader.ReadUInt32(); offset = $reader.ReadUInt32() }
        }
        function Convert-VeRva([uint32]$Rva) {
            foreach ($section in $sections) {
                if ($Rva -ge $section.rva -and $Rva -lt ($section.rva + [Math]::Max($section.size, $section.rawSize))) {
                    return [long]($section.offset + $Rva - $section.rva)
                }
            }
            throw "Invalid PE export address in $Path"
        }
        $stream.Position = (Convert-VeRva $exportRva) + 24
        $namesCount = $reader.ReadUInt32()
        if ($namesCount -gt 65536) { throw "Invalid export count in $Path" }
        $null = $reader.ReadUInt32()
        $namesOffset = Convert-VeRva $reader.ReadUInt32()
        for ($i = 0; $i -lt $namesCount; ++$i) {
            $stream.Position = $namesOffset + $i * 4
            $stream.Position = Convert-VeRva $reader.ReadUInt32()
            $name = [Text.StringBuilder]::new()
            while (($character = $reader.ReadByte()) -ne 0) {
                if ($name.Length -gt 1024) { throw "Invalid export name in $Path" }
                $null = $name.Append([char]$character)
            }
            $name.ToString()
        }
    } finally { $reader.Dispose() }
}
