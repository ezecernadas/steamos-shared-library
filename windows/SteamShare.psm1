# SteamShare: prepare Windows for a Steam library shared with SteamOS on NTFS.
#
# Linux mounts an NTFS volume read-only when Windows left it dirty, and both
# hibernation and Fast Startup leave it that way (docs/PLAN.md, "Known
# risks"). This module turns both off, then checks that the library itself is
# ready: the folder exists, it sits on a clean NTFS volume, and Windows Steam
# knows about it. Without -Library, it finds the library among the ones
# Windows Steam knows.
#
# It mirrors the Linux `steamshare doctor` (src/steamshare/cli/doctor.py):
# each step is one function that returns a check result, and a formatter
# prints them with the same markers and layout. windows/setup.ps1 is the thin
# entry point that runs the steps in order.
#
# Every external effect (registry, powercfg, fsutil, Get-Volume, CIM, the
# elevation test, file reads, prompts) is wrapped in a tiny function of its own, so the
# Pester tests mock the wrappers and also run on Linux.

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# --- Check results -----------------------------------------------------------

# The three outcomes, as in the Python CheckResult (src/steamshare/core/doctor.py).
$script:StatusOk = 'OK'
# Works today, but something the user should know about or clean up.
$script:StatusWarning = 'WARN'
# The shared library is unsafe to use until this is fixed.
$script:StatusFailure = 'FAIL'

# The same markers as the Linux doctor, so both sides read alike.
$script:StatusMarkers = @{
    $script:StatusOk      = '[ OK ]'
    $script:StatusWarning = '[WARN]'
    $script:StatusFailure = '[FAIL]'
}

# Details and hints line up under the title, after the marker.
$script:DetailIndent = ' ' * ($script:StatusMarkers[$script:StatusOk].Length + 1)
$script:HintLabel = 'Next step: '

$script:ExitOk = 0
$script:ExitFailure = 1

function New-CheckResult {
    <#
    .SYNOPSIS
    Builds what one step found, and what the user should do about it.
    #>
    # The rule targets functions that change system state; this one only
    # builds an object in memory.
    [Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSUseShouldProcessForStateChangingFunctions', '',
        Justification = 'Creates an in-memory object; changes no system state.')]
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)] [string] $Title,
        # Attributes cannot read variables, so these repeat the Status values above.
        [Parameter(Mandatory)] [ValidateSet('OK', 'WARN', 'FAIL')] [string] $Status,
        [string[]] $Details = @(),
        # The next step for the user (CONTRIBUTING.md, rule 7). Empty when
        # there is nothing to do.
        [string] $Hint = ''
    )
    [pscustomobject]@{
        PSTypeName = 'SteamShare.CheckResult'
        Title      = $Title
        Status     = $Status
        Details    = $Details
        Hint       = $Hint
    }
}

function Format-CheckResult {
    <#
    .SYNOPSIS
    Renders one result exactly like the Linux doctor does.
    #>
    [CmdletBinding()]
    [OutputType([string])]
    param([Parameter(Mandatory)] [psobject] $Result)

    $lines = New-Object System.Collections.Generic.List[string]
    $lines.Add("$($script:StatusMarkers[$Result.Status]) $($Result.Title)")
    foreach ($detail in $Result.Details) {
        $lines.Add("$script:DetailIndent$detail")
    }
    if ($Result.Hint) {
        $hintLines = @($Result.Hint -split "`r?`n")
        $lines.Add("$script:DetailIndent$script:HintLabel$($hintLines[0])")
        $hintIndent = $script:DetailIndent + (' ' * $script:HintLabel.Length)
        foreach ($line in ($hintLines | Select-Object -Skip 1)) {
            $lines.Add("$hintIndent$line")
        }
    }
    $lines -join [Environment]::NewLine
}

function Get-SetupExitCode {
    <#
    .SYNOPSIS
    Returns 1 if any step failed. Warnings alone do not fail the script.
    #>
    [CmdletBinding()]
    [OutputType([int])]
    param([Parameter(Mandatory)] [psobject[]] $Results)

    $failures = @($Results | Where-Object { $_.Status -eq $script:StatusFailure })
    if ($failures.Count -gt 0) {
        return $script:ExitFailure
    }
    $script:ExitOk
}

# --- External effects ----------------------------------------------------------
#
# Each wrapper does one thing to the outside world and holds no decisions, so
# the tests mock these and nothing else.

function Get-RegistryValue {
    <#
    .SYNOPSIS
    Returns a registry value, or $null when the key or the value is missing.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)] [string] $Path,
        [Parameter(Mandatory)] [string] $Name
    )
    $item = Get-ItemProperty -Path $Path -Name $Name -ErrorAction SilentlyContinue
    if ($null -eq $item) {
        return $null
    }
    $item.$Name
}

function Set-RegistryDword {
    <#
    .SYNOPSIS
    Writes a DWORD registry value, creating it if needed.
    #>
    # The caller asks ShouldProcess before calling this; asking again here
    # would print every -WhatIf message twice.
    [Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSUseShouldProcessForStateChangingFunctions', '',
        Justification = 'Only called after the calling change function has asked ShouldProcess.')]
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)] [string] $Path,
        [Parameter(Mandatory)] [string] $Name,
        [Parameter(Mandatory)] [int] $Value
    )
    New-ItemProperty -Path $Path -Name $Name -Value $Value -PropertyType DWord -Force | Out-Null
}

function Invoke-ExternalProgram {
    <#
    .SYNOPSIS
    Runs a console program and returns its exit code and output lines.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)] [string] $FilePath,
        [string[]] $ArgumentList = @()
    )
    # Windows PowerShell 5.1 turns every stderr line of a native program into
    # an error record, which 'Stop' would throw on. The exit code is what
    # reports failure here, so stderr is only collected as text.
    $ErrorActionPreference = 'Continue'
    $output = @(& $FilePath @ArgumentList 2>&1 | ForEach-Object { "$_" })
    [pscustomobject]@{
        ExitCode = $LASTEXITCODE
        Output   = $output
    }
}

function Invoke-PowercfgHibernateOff {
    [CmdletBinding()]
    param()
    Invoke-ExternalProgram -FilePath 'powercfg.exe' -ArgumentList @('/hibernate', 'off')
}

function Test-IsElevated {
    <#
    .SYNOPSIS
    True when this session runs as administrator (an elevated prompt).
    #>
    [CmdletBinding()]
    [OutputType([bool])]
    param()
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal $identity
    $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Invoke-FsutilDirtyQuery {
    [CmdletBinding()]
    param([Parameter(Mandatory)] [string] $DriveLetter)
    Invoke-ExternalProgram -FilePath 'fsutil.exe' -ArgumentList @('dirty', 'query', "${DriveLetter}:")
}

function Get-CimVolumeDirtyBit {
    <#
    .SYNOPSIS
    Returns Win32_Volume.DirtyBitSet for a drive: $true, $false, or $null
    when there is no such volume or no value.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)] [string] $DriveLetter)
    # DirtyBitSet is a read-only boolean: "If true, the Chkdsk method is
    # automatically run by the system at the next restart", which is when
    # the dirty bit is set. It is not localized, unlike fsutil's text.
    # Microsoft's Win32_Volume docs do not say whether reading it needs
    # elevation, so the caller treats $null or an error as "no answer".
    # https://learn.microsoft.com/en-us/previous-versions/windows/desktop/legacy/aa394515(v=vs.85)
    $volume = Get-CimInstance -ClassName Win32_Volume -Filter "DriveLetter = '${DriveLetter}:'"
    if ($null -eq $volume) {
        return $null
    }
    $volume.DirtyBitSet
}

function Get-VolumeFileSystem {
    <#
    .SYNOPSIS
    Returns the file system name of a drive (such as NTFS), or $null when
    Windows has no volume with that letter.
    #>
    [CmdletBinding()]
    [OutputType([string])]
    param([Parameter(Mandatory)] [string] $DriveLetter)
    $volume = Get-Volume -DriveLetter $DriveLetter -ErrorAction SilentlyContinue
    if ($null -eq $volume) {
        return $null
    }
    # FileSystem is the plain string on MSFT_Volume ("NTFS", "exFAT"); the
    # newer FileSystemType is a number that only the formatter turns into text.
    [string]$volume.FileSystem
}

function Test-IsFolder {
    [CmdletBinding()]
    [OutputType([bool])]
    param([Parameter(Mandatory)] [string] $Path)
    Test-Path -LiteralPath $Path -PathType Container
}

function Get-TextFileContent {
    <#
    .SYNOPSIS
    Returns a text file's whole content, or $null when the file is missing.
    #>
    [CmdletBinding()]
    [OutputType([string])]
    param([Parameter(Mandatory)] [string] $Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return $null
    }
    # Steam writes its .vdf files as UTF-8.
    [IO.File]::ReadAllText($Path, [Text.Encoding]::UTF8)
}

function Get-SystemDriveLetter {
    <#
    .SYNOPSIS
    Returns the letter of the drive Windows runs from, such as C.
    #>
    [CmdletBinding()]
    [OutputType([string])]
    param()
    $env:SystemDrive.TrimEnd(':').ToUpperInvariant()
}

function Test-InteractiveSession {
    <#
    .SYNOPSIS
    True when someone can answer a question: the session is interactive and
    its input is a console, not a pipe or a file.
    #>
    [CmdletBinding()]
    [OutputType([bool])]
    param()
    [Environment]::UserInteractive -and -not [Console]::IsInputRedirected
}

function Read-UserAnswer {
    [CmdletBinding()]
    [OutputType([string])]
    param([Parameter(Mandatory)] [string] $Prompt)
    Read-Host -Prompt $Prompt
}

# --- Changes -------------------------------------------------------------------
#
# Each change runs only when needed, so running the script twice is the same
# as running it once. Both need administrator rights, but only when there is
# something to change, and -WhatIf never needs them.

# Hibernation state: HibernateEnabled = 0 under this key means off. Microsoft
# documents the value in the AllowHibernate note of the Power Policy CSP
# (https://learn.microsoft.com/en-us/windows/client-management/mdm/policy-csp-power).
# It is only read: the change goes through `powercfg /hibernate off`, the
# switch Microsoft documents in KB 920730.
$script:HibernationKeyPath = 'HKLM:\SYSTEM\CurrentControlSet\Control\Power'
$script:HibernationValueName = 'HibernateEnabled'

# Fast Startup state (1 = on, 0 = off). Microsoft documents no other switch
# for it, and Group Policy can only enable it, so this value is the common way
# to turn it off (Microsoft Q&A 988290). Fast Startup is a partial hibernation:
# at shutdown Windows hibernates the kernel session, so its NTFS volumes are
# left dirty and Linux mounts them read-only. Turning hibernation off already
# disables it, but setting this value too makes the intent visible and keeps
# Fast Startup off if someone turns hibernation back on later.
$script:FastStartupKeyPath = 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Power'
$script:FastStartupValueName = 'HiberbootEnabled'

# The registry value both features use to mean "off".
$script:FeatureOffValue = 0

function Test-RegistryFeatureOff {
    <#
    .SYNOPSIS
    True only when the value says "off". A missing value proves nothing, and
    both features default to on, so it counts as on.
    #>
    [CmdletBinding()]
    [OutputType([bool])]
    param(
        [Parameter(Mandatory)] [string] $Path,
        [Parameter(Mandatory)] [string] $Name
    )
    $value = Get-RegistryValue -Path $Path -Name $Name
    ($null -ne $value) -and ($value -eq $script:FeatureOffValue)
}

function New-SkippedChangeResult {
    <#
    .SYNOPSIS
    The result of a change that was needed but not applied (-WhatIf, or the
    user answered No to -Confirm).
    #>
    [Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSUseShouldProcessForStateChangingFunctions', '',
        Justification = 'Creates an in-memory object; changes no system state.')]
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)] [string] $Title,
        [Parameter(Mandatory)] [string] $Change
    )
    New-CheckResult -Title $Title -Status $script:StatusWarning `
        -Details @("Not changed. Would run: $Change") `
        -Hint 'Run the script again without -WhatIf, from an elevated PowerShell.'
}

function New-NotElevatedResult {
    <#
    .SYNOPSIS
    The result of a change that is needed but cannot run without admin rights.
    #>
    [Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSUseShouldProcessForStateChangingFunctions', '',
        Justification = 'Creates an in-memory object; changes no system state.')]
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)] [string] $Title,
        [Parameter(Mandatory)] [string] $Change,
        [Parameter(Mandatory)] [string] $ElevatedCommand
    )
    New-CheckResult -Title $Title -Status $script:StatusFailure `
        -Details @("Needs administrator rights to run: $Change") `
        -Hint "Open PowerShell with 'Run as administrator' and run:`n$ElevatedCommand"
}

function Disable-Hibernation {
    <#
    .SYNOPSIS
    Turns hibernation off, which leaves NTFS volumes dirty.
    #>
    [CmdletBinding(SupportsShouldProcess)]
    param(
        # The full command to re-run this script elevated, for the hint.
        [Parameter(Mandatory)] [string] $ElevatedCommand
    )
    $title = 'Hibernation is off'
    $change = 'powercfg /hibernate off'
    if (Test-RegistryFeatureOff -Path $script:HibernationKeyPath -Name $script:HibernationValueName) {
        return New-CheckResult -Title $title -Status $script:StatusOk -Details @('Already off.')
    }
    if (-not $PSCmdlet.ShouldProcess('Hibernation', $change)) {
        return New-SkippedChangeResult -Title $title -Change $change
    }
    if (-not (Test-IsElevated)) {
        return New-NotElevatedResult -Title $title -Change $change -ElevatedCommand $ElevatedCommand
    }
    $run = Invoke-PowercfgHibernateOff
    if ($run.ExitCode -ne 0) {
        return New-CheckResult -Title $title -Status $script:StatusFailure `
            -Details (@("$change exited with code $($run.ExitCode):") + $run.Output) `
            -Hint "Run $change yourself from an elevated prompt and read its message."
    }
    New-CheckResult -Title $title -Status $script:StatusOk -Details @("Turned off with $change.")
}

function Disable-FastStartup {
    <#
    .SYNOPSIS
    Turns Fast Startup off, which leaves NTFS volumes dirty at every shutdown.
    #>
    [CmdletBinding(SupportsShouldProcess)]
    param(
        # The full command to re-run this script elevated, for the hint.
        [Parameter(Mandatory)] [string] $ElevatedCommand
    )
    $title = 'Fast Startup is off'
    $change = "set $script:FastStartupValueName = $script:FeatureOffValue in $script:FastStartupKeyPath"
    if (Test-RegistryFeatureOff -Path $script:FastStartupKeyPath -Name $script:FastStartupValueName) {
        return New-CheckResult -Title $title -Status $script:StatusOk -Details @('Already off.')
    }
    if (-not $PSCmdlet.ShouldProcess('Fast Startup', $change)) {
        return New-SkippedChangeResult -Title $title -Change $change
    }
    if (-not (Test-IsElevated)) {
        return New-NotElevatedResult -Title $title -Change $change -ElevatedCommand $ElevatedCommand
    }
    Set-RegistryDword -Path $script:FastStartupKeyPath -Name $script:FastStartupValueName -Value $script:FeatureOffValue
    New-CheckResult -Title $title -Status $script:StatusOk -Details @('Turned off.')
}

# --- Read-only checks ----------------------------------------------------------
#
# PowerShell has no "Check" verb, so the checks use Test-. Unlike a plain
# Test- function they return a check result, not a bool, because the user
# needs the details and the next step, not just yes or no.

# Every Steam library keeps its games and manifests here.
$script:SteamAppsDirectory = 'steamapps'

# Linux can share only a local NTFS partition: ntfs3 is the driver that
# enforces windows_names (docs/PLAN.md, "Architecture").
$script:RequiredFileSystem = 'NTFS'

# An example library path for hints. Any drive letter but the system one works.
$script:ExampleLibraryPath = 'E:\Games\SteamLibrary'

# A local path starts with its drive letter, as in E:\Games\SteamLibrary.
$script:DriveLetterPattern = '^(?<letter>[A-Za-z]):'

function Join-WindowsPath {
    <#
    .SYNOPSIS
    Joins a Windows path and a child name with a backslash. Join-Path would
    use "/" when the tests run on Linux.
    #>
    [CmdletBinding()]
    [OutputType([string])]
    param(
        [Parameter(Mandatory)] [string] $Path,
        [Parameter(Mandatory)] [string] $ChildPath
    )
    $Path.TrimEnd('\', '/') + '\' + $ChildPath
}

function Get-LibraryDriveLetter {
    <#
    .SYNOPSIS
    Returns the library's drive letter in upper case, or $null for a path
    without one (a network share).
    #>
    [CmdletBinding()]
    [OutputType([string])]
    param([Parameter(Mandatory)] [string] $Library)
    if ($Library -match $script:DriveLetterPattern) {
        return $Matches['letter'].ToUpperInvariant()
    }
    $null
}

function Test-LibraryFolder {
    <#
    .SYNOPSIS
    Checks that the library folder exists and is a Steam library.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)] [string] $Library)
    $title = 'Library folder exists'
    $hint = "Pass the folder that contains $script:SteamAppsDirectory, for example: -Library $script:ExampleLibraryPath"
    if (-not (Test-IsFolder -Path $Library)) {
        return New-CheckResult -Title $title -Status $script:StatusFailure `
            -Details @("$Library is not a folder.") -Hint $hint
    }
    if (-not (Test-IsFolder -Path (Join-WindowsPath -Path $Library -ChildPath $script:SteamAppsDirectory))) {
        return New-CheckResult -Title $title -Status $script:StatusFailure `
            -Details @("$Library has no $script:SteamAppsDirectory folder, so it is not a Steam library.") -Hint $hint
    }
    New-CheckResult -Title $title -Status $script:StatusOk -Details @($Library)
}

function Test-LibraryFileSystem {
    <#
    .SYNOPSIS
    Checks that the library sits on a local NTFS volume.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)] [string] $Library)
    $title = "Library volume is $script:RequiredFileSystem"
    $driveLetter = Get-LibraryDriveLetter -Library $Library
    if ($null -eq $driveLetter) {
        return New-CheckResult -Title $title -Status $script:StatusFailure `
            -Details @("$Library has no drive letter; Linux can share only a local partition.") `
            -Hint "Put the library on a local NTFS partition with a drive letter, such as $script:ExampleLibraryPath."
    }
    $fileSystem = Get-VolumeFileSystem -DriveLetter $driveLetter
    if (-not $fileSystem) {
        return New-CheckResult -Title $title -Status $script:StatusFailure `
            -Details @("Windows reports no volume for drive ${driveLetter}:.") `
            -Hint 'Check the drive letter in Disk Management (diskmgmt.msc).'
    }
    if ($fileSystem -ne $script:RequiredFileSystem) {
        return New-CheckResult -Title $title -Status $script:StatusFailure `
            -Details @("Drive ${driveLetter}: is $fileSystem.") `
            -Hint "Move the library to an $script:RequiredFileSystem partition; Linux shares it with the ntfs3 driver."
    }
    New-CheckResult -Title $title -Status $script:StatusOk -Details @("Drive ${driveLetter}: is $fileSystem.")
}

# The fsutil fallback, used only when Win32_Volume gives no answer.
#
# How `fsutil dirty query` answers. In English, Windows 10 and 11 print
# "Volume - <drive>: is NOT Dirty" or "Volume - <drive>: is Dirty";
# Microsoft's reference shows "Volume <drive>: is not dirty" or "Volume <drive>: is dirty"
# (https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/fsutil-dirty).
#
# The exit code cannot tell these apart. Microsoft documents no exit codes
# for fsutil; in practice it is 0 whenever the query ran, dirty or not, and
# non-zero when the query itself failed (most often, no admin rights: scripts
# widely use `fsutil dirty query` as an "am I elevated?" test for that
# reason). ReactOS's reimplementation does the same. So the exit code only
# says whether the query ran, and the text decides. The text is localized:
# only the English sentences are known, and any other answer is Unknown,
# which becomes a warning that shows the raw text, never an OK.
$script:DirtyBitDirty = 'Dirty'
$script:DirtyBitClean = 'Clean'
$script:DirtyBitUnknown = 'Unknown'
$script:CleanVolumePattern = '\bis not dirty\b'
$script:DirtyVolumePattern = '\bis dirty\b'

function ConvertFrom-FsutilDirtyOutput {
    <#
    .SYNOPSIS
    Reads the output of `fsutil dirty query` as Dirty, Clean or Unknown.
    #>
    [CmdletBinding()]
    [OutputType([string])]
    param([Parameter(Mandatory)] [AllowEmptyCollection()] [string[]] $Output)
    # -match ignores case, which covers both "NOT Dirty" and "not dirty".
    $text = $Output -join ' '
    if ($text -match $script:CleanVolumePattern) {
        return $script:DirtyBitClean
    }
    if ($text -match $script:DirtyVolumePattern) {
        return $script:DirtyBitDirty
    }
    $script:DirtyBitUnknown
}

$script:DirtyBitTitle = 'Library volume is clean'

function Get-ChkdskHint {
    [CmdletBinding()]
    [OutputType([string])]
    param([Parameter(Mandatory)] [string] $DriveLetter)
    "Run ``chkdsk ${DriveLetter}: /f`` from an elevated prompt, then shut Windows down fully."
}

function New-DirtyBitResult {
    <#
    .SYNOPSIS
    The result for a dirty bit that was read: dirty fails, clean passes.
    #>
    [Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSUseShouldProcessForStateChangingFunctions', '',
        Justification = 'Creates an in-memory object; changes no system state.')]
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)] [string] $DriveLetter,
        [Parameter(Mandatory)] [bool] $IsDirty,
        # Where the answer came from, so a surprising result can be checked.
        [Parameter(Mandatory)] [string] $Source
    )
    if ($IsDirty) {
        return New-CheckResult -Title $script:DirtyBitTitle -Status $script:StatusFailure `
            -Details @("Drive ${DriveLetter}: is dirty ($Source), so Linux would mount it read-only.") `
            -Hint (Get-ChkdskHint -DriveLetter $DriveLetter)
    }
    New-CheckResult -Title $script:DirtyBitTitle -Status $script:StatusOk `
        -Details @("Drive ${DriveLetter}: is not dirty ($Source).")
}

function Find-CimDirtyBit {
    <#
    .SYNOPSIS
    Returns the dirty bit from Win32_Volume, or $null when CIM gives no answer.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)] [string] $DriveLetter)
    # Any failure here (the WMI service stopped, access denied, no instance)
    # only means this source has no answer; fsutil is asked next.
    try {
        return Get-CimVolumeDirtyBit -DriveLetter $DriveLetter
    }
    catch {
        return $null
    }
}

function Test-DirtyBitWithFsutil {
    <#
    .SYNOPSIS
    The fallback: reads the dirty bit from `fsutil dirty query`, whose text is
    localized. An answer it cannot read is a warning, never an OK.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)] [string] $DriveLetter)
    $query = Invoke-FsutilDirtyQuery -DriveLetter $DriveLetter
    if ($query.ExitCode -ne 0) {
        return New-CheckResult -Title $script:DirtyBitTitle -Status $script:StatusWarning `
            -Details (@("fsutil dirty query ${DriveLetter}: failed with code $($query.ExitCode):") + $query.Output) `
            -Hint 'fsutil needs administrator rights. Run this script from an elevated PowerShell.'
    }
    $state = ConvertFrom-FsutilDirtyOutput -Output $query.Output
    if ($state -eq $script:DirtyBitUnknown) {
        return New-CheckResult -Title $script:DirtyBitTitle -Status $script:StatusWarning `
            -Details (@('Could not read the answer of fsutil dirty query:') + $query.Output) `
            -Hint "If the line above says the volume is dirty: $(Get-ChkdskHint -DriveLetter $DriveLetter)"
    }
    New-DirtyBitResult -DriveLetter $DriveLetter -IsDirty ($state -eq $script:DirtyBitDirty) -Source 'fsutil'
}

function Test-LibraryDirtyBit {
    <#
    .SYNOPSIS
    Checks that the library volume is clean. Linux mounts a dirty NTFS
    volume read-only, so Steam on SteamOS could not write to the library.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)] [string] $Library)
    $driveLetter = Get-LibraryDriveLetter -Library $Library
    # Win32_Volume answers with a boolean in any language; fsutil only when
    # CIM cannot answer.
    $isDirty = Find-CimDirtyBit -DriveLetter $driveLetter
    if ($null -ne $isDirty) {
        return New-DirtyBitResult -DriveLetter $driveLetter -IsDirty $isDirty -Source 'Win32_Volume'
    }
    Test-DirtyBitWithFsutil -DriveLetter $driveLetter
}

# --- Windows Steam ---------------------------------------------------------------

# Where Windows Steam is installed. The per-user SteamPath is what the Steam
# client itself writes, with forward slashes; the machine-wide InstallPath is
# written by the installer and is the fallback.
$script:SteamUserKeyPath = 'HKCU:\Software\Valve\Steam'
$script:SteamUserValueName = 'SteamPath'
$script:SteamMachineKeyPath = 'HKLM:\SOFTWARE\WOW6432Node\Valve\Steam'
$script:SteamMachineValueName = 'InstallPath'

# Steam lists every library it knows in this file, under its install folder.
$script:LibraryFoldersFile = 'libraryfolders.vdf'

# A `"path" "value"` pair in libraryfolders.vdf. This is not a general VDF
# parser (the Linux side has one, in src/steamshare/core): the library paths
# are the only thing read here, and Steam writes each one as a single quoted
# value on its own line. Inside the quotes VDF escapes with a backslash, so
# E:\Games is stored as E:\\Games.
$script:VdfPathPattern = '"path"\s+"(?<value>(?:[^"\\]|\\.)*)"'
$script:VdfEscapePattern = '\\(.)'

$script:AddDriveSteps = 'In Windows Steam, open Settings > Storage > Add Drive and pick the shared NTFS partition.'

function Get-SteamInstallPath {
    <#
    .SYNOPSIS
    Returns Windows Steam's install folder with backslashes, or $null when
    Steam is not installed.
    #>
    [CmdletBinding()]
    [OutputType([string])]
    param()
    $path = Get-RegistryValue -Path $script:SteamUserKeyPath -Name $script:SteamUserValueName
    if (-not $path) {
        $path = Get-RegistryValue -Path $script:SteamMachineKeyPath -Name $script:SteamMachineValueName
    }
    if (-not $path) {
        return $null
    }
    $path.Replace('/', '\')
}

function Get-VdfLibraryPath {
    <#
    .SYNOPSIS
    Returns the library paths listed in a libraryfolders.vdf, unescaped.
    #>
    [CmdletBinding()]
    [OutputType([string[]])]
    param([Parameter(Mandatory)] [AllowEmptyString()] [string] $Content)
    foreach ($match in [regex]::Matches($Content, $script:VdfPathPattern)) {
        $match.Groups['value'].Value -replace $script:VdfEscapePattern, '$1'
    }
}

function Get-RegisteredSteamLibrary {
    <#
    .SYNOPSIS
    Returns the libraries Windows Steam knows, or nothing when its
    libraryfolders.vdf is missing.
    #>
    [CmdletBinding()]
    [OutputType([string[]])]
    param([Parameter(Mandatory)] [string] $SteamPath)
    $steamApps = Join-WindowsPath -Path $SteamPath -ChildPath $script:SteamAppsDirectory
    $content = Get-TextFileContent -Path (Join-WindowsPath -Path $steamApps -ChildPath $script:LibraryFoldersFile)
    if ($null -eq $content) {
        return
    }
    Get-VdfLibraryPath -Content $content
}

function Test-SameLibraryPath {
    <#
    .SYNOPSIS
    True when two paths name the same folder. Windows paths ignore case, and
    Steam may store them with a trailing slash or with forward slashes.
    #>
    [CmdletBinding()]
    [OutputType([bool])]
    param(
        [Parameter(Mandatory)] [string] $Left,
        [Parameter(Mandatory)] [string] $Right
    )
    $normalizedLeft = $Left.Replace('/', '\').TrimEnd('\')
    $normalizedRight = $Right.Replace('/', '\').TrimEnd('\')
    [string]::Equals($normalizedLeft, $normalizedRight, [StringComparison]::OrdinalIgnoreCase)
}

function Test-LibraryRegisteredInSteam {
    <#
    .SYNOPSIS
    Checks that Windows Steam lists the library, so its games show up as
    installed on Windows too.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)] [string] $Library)
    $title = 'Library is registered in Windows Steam'
    $steamPath = Get-SteamInstallPath
    if ($null -eq $steamPath) {
        return New-CheckResult -Title $title -Status $script:StatusWarning `
            -Details @('Windows Steam is not installed.') `
            -Hint "Install Steam for Windows. $script:AddDriveSteps"
    }
    $registered = @(Get-RegisteredSteamLibrary -SteamPath $steamPath)
    $matching = @($registered | Where-Object { Test-SameLibraryPath -Left $_ -Right $Library })
    if ($matching.Count -eq 0) {
        return New-CheckResult -Title $title -Status $script:StatusWarning `
            -Details @("Windows Steam does not list $Library.") -Hint $script:AddDriveSteps
    }
    New-CheckResult -Title $title -Status $script:StatusOk -Details @($Library)
}

# --- Library detection ------------------------------------------------------------
#
# With no -Library, the shared library is found among the ones Windows Steam
# knows: it must be on NTFS, and it cannot be on the system drive, since
# Windows itself lives on that partition and Linux shares a separate one.

$script:DetectionTitle = 'Shared library detected'
$script:LibraryParameterHint = 'Pass the library with -Library <path>.'

function Find-SharedLibraryCandidate {
    <#
    .SYNOPSIS
    Returns the libraries that could be the shared one. Pure: the volume facts
    come in as a drive letter to file system table.
    #>
    [CmdletBinding()]
    [OutputType([string[]])]
    param(
        [Parameter(Mandatory)] [AllowEmptyCollection()] [string[]] $LibraryPaths,
        [Parameter(Mandatory)] [hashtable] $FileSystemByDrive,
        [Parameter(Mandatory)] [string] $SystemDriveLetter
    )
    foreach ($path in $LibraryPaths) {
        $driveLetter = Get-LibraryDriveLetter -Library $path
        if ($null -eq $driveLetter) {
            continue
        }
        if ($driveLetter -eq $SystemDriveLetter) {
            continue
        }
        # Hashtable keys ignore case, so a lower-case letter in Steam's file
        # still finds its volume.
        if ($FileSystemByDrive[$driveLetter] -ne $script:RequiredFileSystem) {
            continue
        }
        $path
    }
}

function Get-DriveFileSystemTable {
    <#
    .SYNOPSIS
    Returns a drive letter to file system table for the drives of these paths.
    #>
    [CmdletBinding()]
    [OutputType([hashtable])]
    param([Parameter(Mandatory)] [AllowEmptyCollection()] [string[]] $LibraryPaths)
    $table = @{}
    foreach ($path in $LibraryPaths) {
        $driveLetter = Get-LibraryDriveLetter -Library $path
        if (($null -ne $driveLetter) -and -not $table.ContainsKey($driveLetter)) {
            $table[$driveLetter] = Get-VolumeFileSystem -DriveLetter $driveLetter
        }
    }
    $table
}

# Accepted answers to a yes/no question. Enter alone means yes.
$script:YesAnswers = @('', 'y', 'yes')
$script:NoAnswers = @('n', 'no')

function ConvertFrom-YesNoAnswer {
    <#
    .SYNOPSIS
    Returns $true for yes, $false for no, and $null for anything else.
    #>
    [CmdletBinding()]
    [OutputType([bool])]
    param([Parameter(Mandatory)] [AllowEmptyString()] [string] $Answer)
    # -contains ignores case.
    $trimmed = $Answer.Trim()
    if ($script:YesAnswers -contains $trimmed) {
        return $true
    }
    if ($script:NoAnswers -contains $trimmed) {
        return $false
    }
    $null
}

function ConvertFrom-CandidateNumber {
    <#
    .SYNOPSIS
    Turns the number the user typed (1-based) into a list index, or $null
    when it is not one of the listed numbers.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)] [AllowEmptyString()] [string] $Answer,
        [Parameter(Mandatory)] [int] $CandidateCount
    )
    $number = 0
    if (-not [int]::TryParse($Answer.Trim(), [ref]$number)) {
        return $null
    }
    if (($number -lt 1) -or ($number -gt $CandidateCount)) {
        return $null
    }
    $number - 1
}

function Confirm-LibraryCandidate {
    <#
    .SYNOPSIS
    Asks whether to use the only candidate, until the answer is yes or no.
    #>
    [CmdletBinding()]
    [OutputType([bool])]
    param([Parameter(Mandatory)] [string] $Candidate)
    while ($true) {
        $answer = ConvertFrom-YesNoAnswer -Answer (Read-UserAnswer -Prompt "Use $Candidate as the shared library? [Y/n]")
        if ($null -ne $answer) {
            return $answer
        }
    }
}

function Read-LibraryChoice {
    <#
    .SYNOPSIS
    Lists the candidates by number and asks for one, until the answer is
    valid. There is no default: picking the wrong partition is worse than
    asking again.
    #>
    [CmdletBinding()]
    [OutputType([string])]
    param([Parameter(Mandatory)] [string[]] $Candidates)
    $lines = for ($index = 0; $index -lt $Candidates.Count; $index++) {
        "$($index + 1)) $($Candidates[$index])"
    }
    $prompt = (@($lines) + "Which one is the shared library? [1-$($Candidates.Count)]") -join [Environment]::NewLine
    while ($true) {
        $index = ConvertFrom-CandidateNumber -Answer (Read-UserAnswer -Prompt $prompt) -CandidateCount $Candidates.Count
        if ($null -ne $index) {
            return $Candidates[$index]
        }
    }
}

function Select-SharedLibrary {
    <#
    .SYNOPSIS
    Picks the shared library among the candidates, asking the user when it
    can. Returns $null when none was picked.
    #>
    [CmdletBinding()]
    [OutputType([string])]
    param(
        [Parameter(Mandatory)] [AllowEmptyCollection()] [string[]] $Candidates,
        # False in scripts (-Yes, or no console to answer from): then a single
        # candidate is used as is, and several are left for -Library.
        [Parameter(Mandatory)] [bool] $CanAsk
    )
    if ($Candidates.Count -eq 0) {
        return $null
    }
    if ($Candidates.Count -eq 1) {
        if ((-not $CanAsk) -or (Confirm-LibraryCandidate -Candidate $Candidates[0])) {
            return $Candidates[0]
        }
        return $null
    }
    if (-not $CanAsk) {
        return $null
    }
    Read-LibraryChoice -Candidates $Candidates
}

function New-LibraryDetectionResult {
    <#
    .SYNOPSIS
    Reports which library was picked, or why none was.
    #>
    [Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSUseShouldProcessForStateChangingFunctions', '',
        Justification = 'Creates an in-memory object; changes no system state.')]
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)] [AllowEmptyCollection()] [string[]] $RegisteredLibraries,
        [Parameter(Mandatory)] [AllowEmptyCollection()] [string[]] $Candidates,
        [Parameter(Mandatory)] [AllowEmptyString()] [string] $Selected
    )
    if ($Selected) {
        return New-CheckResult -Title $script:DetectionTitle -Status $script:StatusOk -Details @($Selected)
    }
    if ($Candidates.Count -eq 0) {
        $knownLibraries = @($RegisteredLibraries)
        if ($knownLibraries.Count -eq 0) {
            $knownLibraries = @('(none, or Windows Steam is not installed)')
        }
        return New-CheckResult -Title $script:DetectionTitle -Status $script:StatusFailure `
            -Details (@('No library Windows Steam knows is on NTFS outside the system drive. Known libraries:') + $knownLibraries) `
            -Hint "Create a library on the shared NTFS partition: $script:AddDriveSteps`nOr: $script:LibraryParameterHint"
    }
    if ($Candidates.Count -eq 1) {
        return New-CheckResult -Title $script:DetectionTitle -Status $script:StatusFailure `
            -Details @("Not confirmed: $($Candidates[0])") -Hint $script:LibraryParameterHint
    }
    New-CheckResult -Title $script:DetectionTitle -Status $script:StatusFailure `
        -Details (@('Several libraries could be the shared one:') + $Candidates) `
        -Hint "Pick one: $script:LibraryParameterHint"
}

function Find-SharedLibrary {
    <#
    .SYNOPSIS
    Gathers the facts for the detection, then picks the library. Returns the
    detection result and the picked library ($null when none).
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)] [bool] $CanAsk)
    $registered = @()
    $steamPath = Get-SteamInstallPath
    if ($null -ne $steamPath) {
        $registered = @(Get-RegisteredSteamLibrary -SteamPath $steamPath)
    }
    $candidates = @(Find-SharedLibraryCandidate -LibraryPaths $registered `
            -FileSystemByDrive (Get-DriveFileSystemTable -LibraryPaths $registered) `
            -SystemDriveLetter (Get-SystemDriveLetter))
    $selected = Select-SharedLibrary -Candidates $candidates -CanAsk $CanAsk
    [pscustomobject]@{
        Library = $selected
        Result  = New-LibraryDetectionResult -RegisteredLibraries $registered -Candidates $candidates -Selected ([string]$selected)
    }
}

# --- Setup ---------------------------------------------------------------------

# Run through `irm <url> | iex` there is no script file to name, and no
# -Library either: the one-line command takes no parameters.
$script:OneLineCommandAgain = 'the same "irm <url> | iex" line you just ran'

function Get-ElevatedCommandLine {
    <#
    .SYNOPSIS
    The exact command that re-runs this setup from an elevated prompt.
    #>
    [CmdletBinding()]
    [OutputType([string])]
    param(
        # Empty when the setup runs through `irm | iex`, from no file.
        [Parameter(Mandatory)] [AllowEmptyString()] [string] $ScriptPath,
        [Parameter(Mandatory)] [AllowEmptyString()] [string] $Library
    )
    if (-not $ScriptPath) {
        return $script:OneLineCommandAgain
    }
    # -ExecutionPolicy Bypass because Windows blocks unsigned scripts by default.
    $command = "powershell -ExecutionPolicy Bypass -File `"$ScriptPath`""
    if ($Library) {
        $command += " -Library `"$Library`""
    }
    $command
}

function Invoke-SteamShareSetup {
    <#
    .SYNOPSIS
    Runs every step in order and returns their results.
    #>
    [CmdletBinding(SupportsShouldProcess)]
    param(
        # Empty to detect the library among the ones Windows Steam knows.
        [string] $Library = '',
        # Empty when the setup runs through `irm | iex`, from no file.
        [Parameter(Mandatory)] [AllowEmptyString()] [string] $ScriptPath,
        # Never ask a question, for scripts.
        [switch] $Yes
    )
    $results = New-Object System.Collections.Generic.List[psobject]
    $elevatedCommand = Get-ElevatedCommandLine -ScriptPath $ScriptPath -Library $Library

    # Preference variables do not cross into a module's functions, so
    # -WhatIf is passed on explicitly.
    $results.Add((Disable-Hibernation -ElevatedCommand $elevatedCommand -WhatIf:$WhatIfPreference))
    $results.Add((Disable-FastStartup -ElevatedCommand $elevatedCommand -WhatIf:$WhatIfPreference))

    if (-not $Library) {
        $canAsk = (-not $Yes) -and (Test-InteractiveSession)
        $detection = Find-SharedLibrary -CanAsk $canAsk
        $results.Add($detection.Result)
        if (-not $detection.Library) {
            # Nothing to check; the detection result says what to do.
            return $results.ToArray()
        }
        $Library = $detection.Library
    }

    $results.Add((Test-LibraryFolder -Library $Library))
    $fileSystem = Test-LibraryFileSystem -Library $Library
    $results.Add($fileSystem)
    # The dirty bit only matters on NTFS, and fsutil needs a drive letter.
    if ($fileSystem.Status -eq $script:StatusOk) {
        $results.Add((Test-LibraryDirtyBit -Library $Library))
    }
    $results.Add((Test-LibraryRegisteredInSteam -Library $Library))
    $results.ToArray()
}
