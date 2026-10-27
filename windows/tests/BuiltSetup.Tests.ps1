# Pester 5 tests for the single-file setup.ps1 each release attaches, for
# `irm <url> | iex`. release/build_assets.py builds it from windows/setup.ps1
# and SteamShare.psm1; build it first:
#
#   python3 release/build_assets.py
#
# SteamShare.Tests.ps1 covers the logic with mocks. These tests run the
# built file for real, as users do, so the ones that run it need Windows (the
# registry, CIM, fsutil). They pass -WhatIf, or set $WhatIfPreference under
# iex, which takes no parameters: nothing is changed, so no administrator
# rights are needed.

BeforeAll {
    # Join-Path takes one child only in Windows PowerShell 5.1.
    $repository = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
    $script:builtSetup = Join-Path (Join-Path $repository 'dist') 'setup.ps1'
    if (-not (Test-Path -LiteralPath $script:builtSetup)) {
        throw "$script:builtSetup is missing. Build it first: python3 release/build_assets.py"
    }
    # The PowerShell running these tests: powershell.exe under 5.1, pwsh under 7.
    $script:powershell = (Get-Process -Id $PID).Path

    function Invoke-BuiltSetupInChild {
        # A child process, so an `exit` in the script cannot end the test run,
        # and its input is redirected, so it never waits for an answer.
        param([Parameter(Mandatory)] [string] $Command)
        $output = $null | & $script:powershell -NoProfile -NonInteractive -Command $Command 2>&1
        [pscustomobject]@{ ExitCode = $LASTEXITCODE; Output = ($output | Out-String) }
    }
}

Describe 'The single-file setup.ps1' {
    It 'parses without errors' {
        $errors = $null
        [void][Management.Automation.Language.Parser]::ParseFile($script:builtSetup, [ref]$null, [ref]$errors)

        $errors | Should -BeNullOrEmpty
    }

    It 'embeds the module instead of importing a file' {
        $text = Get-Content -Raw -LiteralPath $script:builtSetup

        $text | Should -Not -BeLike "*Join-Path `$PSScriptRoot 'SteamShare.psm1'*"
        $text | Should -BeLike '*New-Module -Name SteamShare -ScriptBlock {*'
    }
}

# [Environment]::OSVersion, since $IsWindows does not exist in 5.1.
Describe 'Running the single-file setup.ps1' -Skip:([Environment]::OSVersion.Platform -ne 'Win32NT') {
    It 'runs through iex without closing the session or leaving strict mode behind' {
        $command = @"
`$WhatIfPreference = `$true
Get-Content -Raw -LiteralPath '$script:builtSetup' | Invoke-Expression
`$WhatIfPreference = `$false
'session still open'
try { `$notDefinedAnywhere | Out-Null; 'strict mode not left behind' } catch { 'strict mode left behind' }
"@

        $run = Invoke-BuiltSetupInChild -Command $command

        $run.Output | Should -BeLike '*Hibernation*'
        $run.Output | Should -BeLike '*Fast Startup*'
        $run.Output | Should -BeLike '*session still open*'
        $run.Output | Should -BeLike '*strict mode not left behind*'
    }

    It 'still takes parameters and sets the exit code when run as a file' {
        # A folder that does not exist, so its check fails and the exit code is 1.
        $library = Join-Path $TestDrive 'MissingLibrary'
        $command = "& '$script:builtSetup' -Library '$library' -Yes -WhatIf; exit `$LASTEXITCODE"

        $run = Invoke-BuiltSetupInChild -Command $command

        $run.Output | Should -BeLike '*Library folder exists*'
        $run.ExitCode | Should -Be 1
    }
}
