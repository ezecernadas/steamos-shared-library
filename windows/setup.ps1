<#
.SYNOPSIS
Prepares Windows to share a Steam library with SteamOS on an NTFS partition.

.DESCRIPTION
Turns off hibernation and Fast Startup, which leave NTFS dirty so that Linux
mounts it read-only. Then checks that the library folder exists, sits on a
clean NTFS volume, and is registered in Windows Steam.

Changes need an elevated PowerShell; checks do not. Running it again changes
nothing that is already right. The output matches `steamshare doctor` on
Linux, and the exit code is 1 when any step failed.

This file only reads the parameters and prints; the logic lives in
SteamShare.psm1. Each release also ships a single-file copy, with the module
embedded, for `irm <url> | iex` (release/build_assets.py builds it). That
way takes no parameters, so -Library is chosen interactively.

.PARAMETER Library
The Steam library folder to share (the one that contains steamapps). When
omitted, it is detected among the libraries Windows Steam knows.

.PARAMETER Yes
Never ask a question, for scripts. A single detected library is used
without confirmation. When several libraries qualify, the script fails and
lists them; pass the one to use with -Library.

.EXAMPLE
irm https://github.com/ezecernadas/steamshare/releases/download/v<version>/setup.ps1 | iex

.EXAMPLE
powershell -ExecutionPolicy Bypass -File windows\setup.ps1

.EXAMPLE
powershell -ExecutionPolicy Bypass -File windows\setup.ps1 -Library E:\Games\SteamLibrary -WhatIf
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [string] $Library = '',
    [switch] $Yes
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

Import-Module (Join-Path $PSScriptRoot 'SteamShare.psm1') -Force

if ($Library) {
    # A relative path is resolved against the PowerShell location, which
    # [IO.Path]::GetFullPath would not follow. The folder need not exist yet;
    # the checks report that.
    $Library = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Library)
}

$results = Invoke-SteamShareSetup -Library $Library -ScriptPath $PSCommandPath -Yes:$Yes -WhatIf:$WhatIfPreference
foreach ($result in $results) {
    Format-CheckResult -Result $result
}
# $PSCommandPath is empty when the script runs through `irm | iex`, from no
# file. There `exit` would close the user's PowerShell window, and the
# results above already say what failed, so the exit code is only set when
# a file was run.
if ($PSCommandPath) {
    exit (Get-SetupExitCode -Results $results)
}
