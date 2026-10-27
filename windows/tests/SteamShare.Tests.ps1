# Pester 5 tests for windows/SteamShare.psm1.
#
# Every external effect in the module (registry, powercfg, fsutil, Get-Volume,
# the elevation test, file reads) lives in a small wrapper function. These
# tests mock those wrappers, never the real cmdlets, so they run on Linux pwsh
# as well as on Windows PowerShell 5.1.

BeforeAll {
    # Join-Path takes one child only in Windows PowerShell 5.1.
    Import-Module (Join-Path (Split-Path $PSScriptRoot -Parent) 'SteamShare.psm1') -Force
}

Describe 'Format-CheckResult' {
    It 'prints the marker and the title on one line' {
        $result = New-CheckResult -Title 'Hibernation is off' -Status 'OK'

        Format-CheckResult $result | Should -Be '[ OK ] Hibernation is off'
    }

    It 'indents details under the title' {
        $result = New-CheckResult -Title 'Library folder exists' -Status 'FAIL' -Details @('first', 'second')

        Format-CheckResult $result | Should -Be ((
            '[FAIL] Library folder exists',
            '       first',
            '       second') -join [Environment]::NewLine)
    }

    It 'prefixes the hint with "Next step: " and aligns its extra lines' {
        $result = New-CheckResult -Title 'Volume is clean' -Status 'WARN' -Hint "line one`nline two"

        Format-CheckResult $result | Should -Be ((
            '[WARN] Volume is clean',
            '       Next step: line one',
            '                  line two') -join [Environment]::NewLine)
    }
}

Describe 'Get-SetupExitCode' {
    It 'returns 0 when nothing failed, even with warnings' {
        $results = @(
            New-CheckResult -Title 'a' -Status 'OK'
            New-CheckResult -Title 'b' -Status 'WARN'
        )

        Get-SetupExitCode $results | Should -Be 0
    }

    It 'returns 1 when any check failed' {
        $results = @(
            New-CheckResult -Title 'a' -Status 'OK'
            New-CheckResult -Title 'b' -Status 'FAIL'
        )

        Get-SetupExitCode $results | Should -Be 1
    }
}

Describe 'Disable-Hibernation' {
    BeforeEach {
        Mock -ModuleName SteamShare Test-IsElevated { $true }
        Mock -ModuleName SteamShare Invoke-PowercfgHibernateOff {
            [pscustomobject]@{ ExitCode = 0; Output = @() }
        }
    }

    It 'changes nothing, and needs no admin, when hibernation is already off' {
        Mock -ModuleName SteamShare Get-RegistryValue { 0 } -ParameterFilter { $Name -eq 'HibernateEnabled' }

        $result = Disable-Hibernation -ElevatedCommand 'elevated command'

        $result.Status | Should -Be 'OK'
        Should -Invoke -ModuleName SteamShare Invoke-PowercfgHibernateOff -Times 0
        Should -Invoke -ModuleName SteamShare Test-IsElevated -Times 0
    }

    It 'turns hibernation off with powercfg when it is on' {
        Mock -ModuleName SteamShare Get-RegistryValue { 1 } -ParameterFilter { $Name -eq 'HibernateEnabled' }

        $result = Disable-Hibernation -ElevatedCommand 'elevated command'

        $result.Status | Should -Be 'OK'
        Should -Invoke -ModuleName SteamShare Invoke-PowercfgHibernateOff -Times 1 -Exactly
    }

    It 'treats a missing registry value as on, since that cannot prove it is off' {
        Mock -ModuleName SteamShare Get-RegistryValue { $null } -ParameterFilter { $Name -eq 'HibernateEnabled' }

        Disable-Hibernation -ElevatedCommand 'elevated command' | Out-Null

        Should -Invoke -ModuleName SteamShare Invoke-PowercfgHibernateOff -Times 1 -Exactly
    }

    It 'applies nothing, and needs no admin, with -WhatIf' {
        Mock -ModuleName SteamShare Get-RegistryValue { 1 } -ParameterFilter { $Name -eq 'HibernateEnabled' }

        $result = Disable-Hibernation -ElevatedCommand 'elevated command' -WhatIf

        $result.Status | Should -Be 'WARN'
        Should -Invoke -ModuleName SteamShare Invoke-PowercfgHibernateOff -Times 0
        Should -Invoke -ModuleName SteamShare Test-IsElevated -Times 0
    }

    It 'fails with the elevated command line when a change is needed without admin' {
        Mock -ModuleName SteamShare Get-RegistryValue { 1 } -ParameterFilter { $Name -eq 'HibernateEnabled' }
        Mock -ModuleName SteamShare Test-IsElevated { $false }

        $result = Disable-Hibernation -ElevatedCommand 'elevated command'

        $result.Status | Should -Be 'FAIL'
        $result.Hint | Should -BeLike '*elevated command*'
        Should -Invoke -ModuleName SteamShare Invoke-PowercfgHibernateOff -Times 0
    }

    It 'fails with the powercfg output when powercfg fails' {
        Mock -ModuleName SteamShare Get-RegistryValue { 1 } -ParameterFilter { $Name -eq 'HibernateEnabled' }
        Mock -ModuleName SteamShare Invoke-PowercfgHibernateOff {
            [pscustomobject]@{ ExitCode = 1; Output = @('An unexpected error occurred.') }
        }

        $result = Disable-Hibernation -ElevatedCommand 'elevated command'

        $result.Status | Should -Be 'FAIL'
        $result.Details | Should -Contain 'An unexpected error occurred.'
    }
}

Describe 'Disable-FastStartup' {
    BeforeEach {
        Mock -ModuleName SteamShare Test-IsElevated { $true }
        Mock -ModuleName SteamShare Set-RegistryDword { }
    }

    It 'changes nothing, and needs no admin, when Fast Startup is already off' {
        Mock -ModuleName SteamShare Get-RegistryValue { 0 } -ParameterFilter { $Name -eq 'HiberbootEnabled' }

        $result = Disable-FastStartup -ElevatedCommand 'elevated command'

        $result.Status | Should -Be 'OK'
        Should -Invoke -ModuleName SteamShare Set-RegistryDword -Times 0
        Should -Invoke -ModuleName SteamShare Test-IsElevated -Times 0
    }

    It 'sets HiberbootEnabled to 0 when Fast Startup is on' {
        Mock -ModuleName SteamShare Get-RegistryValue { 1 } -ParameterFilter { $Name -eq 'HiberbootEnabled' }

        $result = Disable-FastStartup -ElevatedCommand 'elevated command'

        $result.Status | Should -Be 'OK'
        Should -Invoke -ModuleName SteamShare Set-RegistryDword -Times 1 -Exactly -ParameterFilter {
            $Path -eq 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Power' -and
            $Name -eq 'HiberbootEnabled' -and
            $Value -eq 0
        }
    }

    It 'treats a missing registry value as on, since Windows enables Fast Startup by default' {
        Mock -ModuleName SteamShare Get-RegistryValue { $null } -ParameterFilter { $Name -eq 'HiberbootEnabled' }

        Disable-FastStartup -ElevatedCommand 'elevated command' | Out-Null

        Should -Invoke -ModuleName SteamShare Set-RegistryDword -Times 1 -Exactly
    }

    It 'applies nothing, and needs no admin, with -WhatIf' {
        Mock -ModuleName SteamShare Get-RegistryValue { 1 } -ParameterFilter { $Name -eq 'HiberbootEnabled' }

        $result = Disable-FastStartup -ElevatedCommand 'elevated command' -WhatIf

        $result.Status | Should -Be 'WARN'
        Should -Invoke -ModuleName SteamShare Set-RegistryDword -Times 0
        Should -Invoke -ModuleName SteamShare Test-IsElevated -Times 0
    }

    It 'fails with the elevated command line when a change is needed without admin' {
        Mock -ModuleName SteamShare Get-RegistryValue { 1 } -ParameterFilter { $Name -eq 'HiberbootEnabled' }
        Mock -ModuleName SteamShare Test-IsElevated { $false }

        $result = Disable-FastStartup -ElevatedCommand 'elevated command'

        $result.Status | Should -Be 'FAIL'
        $result.Hint | Should -BeLike '*elevated command*'
        Should -Invoke -ModuleName SteamShare Set-RegistryDword -Times 0
    }
}

Describe 'Test-LibraryFolder' {
    It 'passes when the folder exists and contains steamapps' {
        Mock -ModuleName SteamShare Test-IsFolder { $true }

        (Test-LibraryFolder -Library 'E:\Games\SteamLibrary').Status | Should -Be 'OK'
    }

    It 'fails when the folder does not exist' {
        Mock -ModuleName SteamShare Test-IsFolder { $false }

        $result = Test-LibraryFolder -Library 'E:\Games\SteamLibrary'

        $result.Status | Should -Be 'FAIL'
        $result.Hint | Should -Not -BeNullOrEmpty
    }

    It 'fails when the folder has no steamapps, since it is not a library' {
        Mock -ModuleName SteamShare Test-IsFolder { $true } -ParameterFilter { $Path -eq 'E:\Games\SteamLibrary' }
        Mock -ModuleName SteamShare Test-IsFolder { $false } -ParameterFilter { $Path -eq 'E:\Games\SteamLibrary\steamapps' }

        $result = Test-LibraryFolder -Library 'E:\Games\SteamLibrary'

        $result.Status | Should -Be 'FAIL'
        $result.Details -join ' ' | Should -BeLike '*steamapps*'
    }
}

Describe 'Get-LibraryDriveLetter' {
    It 'returns the upper-case drive letter of <Library>' -ForEach @(
        @{ Library = 'E:\Games\SteamLibrary' }
        @{ Library = 'e:\Games\SteamLibrary' }
        @{ Library = 'E:/Games/SteamLibrary' }
        @{ Library = 'E:\' }
    ) {
        Get-LibraryDriveLetter -Library $Library | Should -BeExactly 'E'
    }

    It 'returns nothing for a network path, which has no drive letter' {
        Get-LibraryDriveLetter -Library '\\server\games\SteamLibrary' | Should -BeNullOrEmpty
    }
}

Describe 'Test-LibraryFileSystem' {
    It 'passes when the volume is NTFS' {
        Mock -ModuleName SteamShare Get-VolumeFileSystem { 'NTFS' } -ParameterFilter { $DriveLetter -eq 'E' }

        (Test-LibraryFileSystem -Library 'E:\Games\SteamLibrary').Status | Should -Be 'OK'
    }

    It 'fails when the volume is not NTFS' {
        Mock -ModuleName SteamShare Get-VolumeFileSystem { 'exFAT' }

        $result = Test-LibraryFileSystem -Library 'E:\Games\SteamLibrary'

        $result.Status | Should -Be 'FAIL'
        $result.Details -join ' ' | Should -BeLike '*exFAT*'
    }

    It 'fails when Windows reports no volume for the drive letter' {
        Mock -ModuleName SteamShare Get-VolumeFileSystem { $null }

        (Test-LibraryFileSystem -Library 'E:\Games\SteamLibrary').Status | Should -Be 'FAIL'
    }

    It 'fails without asking Windows when the library has no drive letter' {
        Mock -ModuleName SteamShare Get-VolumeFileSystem { 'NTFS' }

        $result = Test-LibraryFileSystem -Library '\\server\games\SteamLibrary'

        $result.Status | Should -Be 'FAIL'
        Should -Invoke -ModuleName SteamShare Get-VolumeFileSystem -Times 0
    }
}

Describe 'ConvertFrom-FsutilDirtyOutput' {
    # Windows 10 and 11 print "Volume - <drive>: is NOT Dirty"; Microsoft's docs
    # show "Volume <drive>: is not dirty". Both are covered. The unknown
    # sentence below stands in for any translation, not a real one.
    It 'reads <Output> as <Expected>' -ForEach @(
        @{ Output = 'Volume - E: is NOT Dirty'; Expected = 'Clean' }
        @{ Output = 'Volume E: is not dirty'; Expected = 'Clean' }
        @{ Output = 'Volume - E: is Dirty'; Expected = 'Dirty' }
        @{ Output = 'Volume E: is dirty'; Expected = 'Dirty' }
    ) {
        ConvertFrom-FsutilDirtyOutput -Output @($Output) | Should -Be $Expected
    }

    It 'reads localized text it does not know as Unknown, never as Clean' {
        ConvertFrom-FsutilDirtyOutput -Output @('Volumen - E: texto traducido desconocido') | Should -Be 'Unknown'
    }

    It 'reads empty output as Unknown' {
        ConvertFrom-FsutilDirtyOutput -Output @() | Should -Be 'Unknown'
    }
}

Describe 'Test-LibraryDirtyBit' {
    BeforeEach {
        # Most tests below are about the fsutil fallback, so CIM has no answer.
        Mock -ModuleName SteamShare Get-CimVolumeDirtyBit { $null }
    }

    It 'fails with the chkdsk command when Win32_Volume says the volume is dirty' {
        Mock -ModuleName SteamShare Get-CimVolumeDirtyBit { $true } -ParameterFilter { $DriveLetter -eq 'E' }
        Mock -ModuleName SteamShare Invoke-FsutilDirtyQuery { }

        $result = Test-LibraryDirtyBit -Library 'E:\Games\SteamLibrary'

        $result.Status | Should -Be 'FAIL'
        $result.Hint | Should -BeLike '*chkdsk E: /f*'
        Should -Invoke -ModuleName SteamShare Invoke-FsutilDirtyQuery -Times 0
    }

    It 'passes without fsutil when Win32_Volume says the volume is clean' {
        Mock -ModuleName SteamShare Get-CimVolumeDirtyBit { $false }
        Mock -ModuleName SteamShare Invoke-FsutilDirtyQuery { }

        (Test-LibraryDirtyBit -Library 'E:\Games\SteamLibrary').Status | Should -Be 'OK'
        Should -Invoke -ModuleName SteamShare Invoke-FsutilDirtyQuery -Times 0
    }

    It 'falls back to fsutil when Win32_Volume has no value' {
        Mock -ModuleName SteamShare Invoke-FsutilDirtyQuery {
            [pscustomobject]@{ ExitCode = 0; Output = @('Volume - E: is Dirty') }
        }

        (Test-LibraryDirtyBit -Library 'E:\Games\SteamLibrary').Status | Should -Be 'FAIL'
        Should -Invoke -ModuleName SteamShare Invoke-FsutilDirtyQuery -Times 1 -Exactly
    }

    It 'falls back to fsutil when CIM is unavailable' {
        Mock -ModuleName SteamShare Get-CimVolumeDirtyBit { throw 'The WS-Management service is not running.' }
        Mock -ModuleName SteamShare Invoke-FsutilDirtyQuery {
            [pscustomobject]@{ ExitCode = 0; Output = @('Volume - E: is NOT Dirty') }
        }

        (Test-LibraryDirtyBit -Library 'E:\Games\SteamLibrary').Status | Should -Be 'OK'
        Should -Invoke -ModuleName SteamShare Invoke-FsutilDirtyQuery -Times 1 -Exactly
    }

    It 'passes when the volume is clean' {
        Mock -ModuleName SteamShare Invoke-FsutilDirtyQuery {
            [pscustomobject]@{ ExitCode = 0; Output = @('Volume - E: is NOT Dirty') }
        } -ParameterFilter { $DriveLetter -eq 'E' }

        (Test-LibraryDirtyBit -Library 'E:\Games\SteamLibrary').Status | Should -Be 'OK'
    }

    It 'fails with the chkdsk command when the volume is dirty' {
        Mock -ModuleName SteamShare Invoke-FsutilDirtyQuery {
            [pscustomobject]@{ ExitCode = 0; Output = @('Volume - E: is Dirty') }
        }

        $result = Test-LibraryDirtyBit -Library 'E:\Games\SteamLibrary'

        $result.Status | Should -Be 'FAIL'
        $result.Hint | Should -BeLike '*chkdsk E: /f*'
    }

    It 'warns and shows the raw output when it cannot read the answer' {
        Mock -ModuleName SteamShare Invoke-FsutilDirtyQuery {
            [pscustomobject]@{ ExitCode = 0; Output = @('Volumen - E: texto traducido desconocido') }
        }

        $result = Test-LibraryDirtyBit -Library 'E:\Games\SteamLibrary'

        $result.Status | Should -Be 'WARN'
        $result.Details | Should -Contain 'Volumen - E: texto traducido desconocido'
    }

    It 'warns when fsutil itself fails, for example without admin rights' {
        Mock -ModuleName SteamShare Invoke-FsutilDirtyQuery {
            [pscustomobject]@{ ExitCode = 1; Output = @('The FSUTIL utility requires that you have administrative privileges.') }
        }

        $result = Test-LibraryDirtyBit -Library 'E:\Games\SteamLibrary'

        $result.Status | Should -Be 'WARN'
        $result.Hint | Should -BeLike '*elevated*'
    }
}

Describe 'Get-VdfLibraryPath' {
    It 'returns every "path" value, with escaped backslashes unescaped' {
        $content = @'
"libraryfolders"
{
	"0"
	{
		"path"		"C:\\Program Files (x86)\\Steam"
		"label"		""
		"apps"
		{
			"228980"		"433371614"
		}
	}
	"1"
	{
		"path"		"E:\\Games\\SteamLibrary"
		"label"		"Shared"
	}
}
'@

        Get-VdfLibraryPath -Content $content | Should -Be @(
            'C:\Program Files (x86)\Steam'
            'E:\Games\SteamLibrary'
        )
    }

    It 'returns nothing when there is no "path" entry' {
        Get-VdfLibraryPath -Content '"libraryfolders" { }' | Should -BeNullOrEmpty
    }
}

Describe 'Test-SameLibraryPath' {
    It 'treats <Left> and <Right> as the same folder' -ForEach @(
        @{ Left = 'E:\Games\SteamLibrary'; Right = 'e:\games\steamlibrary' }
        @{ Left = 'E:\Games\SteamLibrary\'; Right = 'E:\Games\SteamLibrary' }
        @{ Left = 'E:/Games/SteamLibrary/'; Right = 'E:\Games\SteamLibrary' }
    ) {
        Test-SameLibraryPath -Left $Left -Right $Right | Should -BeTrue
    }

    It 'tells different folders apart' {
        Test-SameLibraryPath -Left 'E:\Games\SteamLibrary' -Right 'E:\Games\SteamLibrary2' | Should -BeFalse
    }
}

Describe 'Get-SteamInstallPath' {
    It 'reads the per-user SteamPath, which uses forward slashes' {
        Mock -ModuleName SteamShare Get-RegistryValue { 'c:/program files (x86)/steam' } -ParameterFilter { $Name -eq 'SteamPath' }
        Mock -ModuleName SteamShare Get-RegistryValue { $null } -ParameterFilter { $Name -eq 'InstallPath' }

        Get-SteamInstallPath | Should -Be 'c:\program files (x86)\steam'
    }

    It 'falls back to the machine-wide InstallPath' {
        Mock -ModuleName SteamShare Get-RegistryValue { $null } -ParameterFilter { $Name -eq 'SteamPath' }
        Mock -ModuleName SteamShare Get-RegistryValue { 'C:\Program Files (x86)\Steam' } -ParameterFilter { $Name -eq 'InstallPath' }

        Get-SteamInstallPath | Should -Be 'C:\Program Files (x86)\Steam'
    }

    It 'returns nothing when Steam is not installed' {
        Mock -ModuleName SteamShare Get-RegistryValue { $null }

        Get-SteamInstallPath | Should -BeNullOrEmpty
    }
}

Describe 'Test-LibraryRegisteredInSteam' {
    BeforeEach {
        Mock -ModuleName SteamShare Get-SteamInstallPath { 'C:\Program Files (x86)\Steam' }
        Mock -ModuleName SteamShare Get-TextFileContent {
            '"libraryfolders" { "1" { "path" "e:\\games\\steamlibrary\\" } }'
        } -ParameterFilter { $Path -eq 'C:\Program Files (x86)\Steam\steamapps\libraryfolders.vdf' }
    }

    It 'passes when Steam lists the library, whatever the case or trailing slash' {
        (Test-LibraryRegisteredInSteam -Library 'E:\Games\SteamLibrary').Status | Should -Be 'OK'
    }

    It 'warns with the Add Drive steps when Steam does not list the library' {
        $result = Test-LibraryRegisteredInSteam -Library 'F:\OtherLibrary'

        $result.Status | Should -Be 'WARN'
        $result.Hint | Should -BeLike '*Add Drive*'
    }

    It 'warns when Windows Steam is not installed' {
        Mock -ModuleName SteamShare Get-SteamInstallPath { $null }

        (Test-LibraryRegisteredInSteam -Library 'E:\Games\SteamLibrary').Status | Should -Be 'WARN'
    }

    It 'warns when Steam has no libraryfolders.vdf' {
        # Same filter as BeforeEach: Pester tries filtered mocks before
        # unfiltered ones, so an unfiltered override would never run.
        Mock -ModuleName SteamShare Get-TextFileContent { $null } -ParameterFilter {
            $Path -eq 'C:\Program Files (x86)\Steam\steamapps\libraryfolders.vdf'
        }

        (Test-LibraryRegisteredInSteam -Library 'E:\Games\SteamLibrary').Status | Should -Be 'WARN'
    }
}

Describe 'Find-SharedLibraryCandidate' {
    BeforeAll {
        $script:fileSystems = @{ C = 'NTFS'; E = 'NTFS'; F = 'NTFS'; G = 'exFAT' }
    }

    It 'keeps an NTFS library outside the system drive' {
        Find-SharedLibraryCandidate -LibraryPaths @('E:\Games\SteamLibrary') `
            -FileSystemByDrive $script:fileSystems -SystemDriveLetter 'C' | Should -Be 'E:\Games\SteamLibrary'
    }

    It 'excludes the system drive, which cannot be the shared partition' {
        Find-SharedLibraryCandidate -LibraryPaths @('C:\Program Files (x86)\Steam') `
            -FileSystemByDrive $script:fileSystems -SystemDriveLetter 'C' | Should -BeNullOrEmpty
    }

    It 'compares the system drive letter without regard to case' {
        Find-SharedLibraryCandidate -LibraryPaths @('c:\SteamLibrary') `
            -FileSystemByDrive $script:fileSystems -SystemDriveLetter 'C' | Should -BeNullOrEmpty
    }

    It 'excludes a library that is not on NTFS' {
        Find-SharedLibraryCandidate -LibraryPaths @('G:\SteamLibrary') `
            -FileSystemByDrive $script:fileSystems -SystemDriveLetter 'C' | Should -BeNullOrEmpty
    }

    It 'excludes a library on a drive with no volume facts, or with no drive letter' {
        Find-SharedLibraryCandidate -LibraryPaths @('H:\SteamLibrary', '\\server\games') `
            -FileSystemByDrive $script:fileSystems -SystemDriveLetter 'C' | Should -BeNullOrEmpty
    }

    It 'returns every candidate when there are several' {
        Find-SharedLibraryCandidate -LibraryPaths @('C:\Steam', 'E:\Games\SteamLibrary', 'F:\SteamLibrary') `
            -FileSystemByDrive $script:fileSystems -SystemDriveLetter 'C' | Should -Be @('E:\Games\SteamLibrary', 'F:\SteamLibrary')
    }
}

Describe 'ConvertFrom-YesNoAnswer' {
    It 'reads <Answer> as <Expected>' -ForEach @(
        @{ Answer = ''; Expected = $true }
        @{ Answer = 'y'; Expected = $true }
        @{ Answer = 'YES'; Expected = $true }
        @{ Answer = ' n '; Expected = $false }
        @{ Answer = 'No'; Expected = $false }
    ) {
        ConvertFrom-YesNoAnswer -Answer $Answer | Should -Be $Expected
    }

    It 'reads anything else as no answer, so the question is asked again' {
        ConvertFrom-YesNoAnswer -Answer 'maybe' | Should -BeNullOrEmpty
    }
}

Describe 'ConvertFrom-CandidateNumber' {
    It 'turns the number <Answer> into the index <Expected>' -ForEach @(
        @{ Answer = '1'; Expected = 0 }
        @{ Answer = ' 3 '; Expected = 2 }
    ) {
        ConvertFrom-CandidateNumber -Answer $Answer -CandidateCount 3 | Should -Be $Expected
    }

    It 'rejects <Answer>' -ForEach @(
        @{ Answer = '' }
        @{ Answer = '0' }
        @{ Answer = '4' }
        @{ Answer = 'two' }
        @{ Answer = '-1' }
    ) {
        ConvertFrom-CandidateNumber -Answer $Answer -CandidateCount 3 | Should -BeNullOrEmpty
    }
}

Describe 'Select-SharedLibrary' {
    BeforeEach {
        # Answers the mocked prompt gives, in order.
        $script:answers = New-Object System.Collections.Generic.Queue[string]
        Mock -ModuleName SteamShare Read-UserAnswer { $script:answers.Dequeue() }
    }

    It 'returns nothing when there is no candidate, without asking' {
        Select-SharedLibrary -Candidates @() -CanAsk $true | Should -BeNullOrEmpty
        Should -Invoke -ModuleName SteamShare Read-UserAnswer -Times 0
    }

    It 'uses the only candidate without asking when it cannot ask' {
        Select-SharedLibrary -Candidates @('E:\Games\SteamLibrary') -CanAsk $false | Should -Be 'E:\Games\SteamLibrary'
        Should -Invoke -ModuleName SteamShare Read-UserAnswer -Times 0
    }

    It 'uses the only candidate when the user presses Enter' {
        $script:answers.Enqueue('')

        Select-SharedLibrary -Candidates @('E:\Games\SteamLibrary') -CanAsk $true | Should -Be 'E:\Games\SteamLibrary'
        Should -Invoke -ModuleName SteamShare Read-UserAnswer -Times 1 -Exactly -ParameterFilter {
            $Prompt -like '*Use E:\Games\SteamLibrary*[Y/n]*'
        }
    }

    It 'returns nothing when the user declines the only candidate' {
        $script:answers.Enqueue('n')

        Select-SharedLibrary -Candidates @('E:\Games\SteamLibrary') -CanAsk $true | Should -BeNullOrEmpty
    }

    It 'asks again after an answer that is neither yes nor no' {
        'maybe', 'y' | ForEach-Object { $script:answers.Enqueue($_) }

        Select-SharedLibrary -Candidates @('E:\Games\SteamLibrary') -CanAsk $true | Should -Be 'E:\Games\SteamLibrary'
        Should -Invoke -ModuleName SteamShare Read-UserAnswer -Times 2 -Exactly
    }

    It 'returns nothing for several candidates when it cannot ask' {
        Select-SharedLibrary -Candidates @('E:\Games\SteamLibrary', 'F:\SteamLibrary') -CanAsk $false | Should -BeNullOrEmpty
        Should -Invoke -ModuleName SteamShare Read-UserAnswer -Times 0
    }

    It 'lists several candidates by number and returns the one chosen' {
        $script:answers.Enqueue('2')

        Select-SharedLibrary -Candidates @('E:\Games\SteamLibrary', 'F:\SteamLibrary') -CanAsk $true | Should -Be 'F:\SteamLibrary'
        Should -Invoke -ModuleName SteamShare Read-UserAnswer -ParameterFilter {
            $Prompt -like '*1) E:\Games\SteamLibrary*2) F:\SteamLibrary*Which one is the shared library?*'
        }
    }

    It 'asks again until the number is valid, since there is no default' {
        '', '7', 'x', '1' | ForEach-Object { $script:answers.Enqueue($_) }

        Select-SharedLibrary -Candidates @('E:\Games\SteamLibrary', 'F:\SteamLibrary') -CanAsk $true | Should -Be 'E:\Games\SteamLibrary'
        Should -Invoke -ModuleName SteamShare Read-UserAnswer -Times 4 -Exactly
    }
}

Describe 'New-LibraryDetectionResult' {
    It 'reports the library that was selected' {
        $result = New-LibraryDetectionResult -RegisteredLibraries @('C:\Steam', 'E:\Games\SteamLibrary') `
            -Candidates @('E:\Games\SteamLibrary') -Selected 'E:\Games\SteamLibrary'

        $result.Status | Should -Be 'OK'
        $result.Title | Should -Be 'Shared library detected'
        $result.Details | Should -Contain 'E:\Games\SteamLibrary'
    }

    It 'fails with the Add Drive steps and -Library when there is no candidate' {
        $result = New-LibraryDetectionResult -RegisteredLibraries @('C:\Steam') -Candidates @() -Selected ''

        $result.Status | Should -Be 'FAIL'
        $result.Details | Should -Contain 'C:\Steam'
        $result.Hint | Should -BeLike '*Add Drive*'
        $result.Hint | Should -BeLike '*-Library <path>*'
    }

    It 'fails and asks for -Library when the user declined the only candidate' {
        $result = New-LibraryDetectionResult -RegisteredLibraries @('E:\Games\SteamLibrary') `
            -Candidates @('E:\Games\SteamLibrary') -Selected ''

        $result.Status | Should -Be 'FAIL'
        $result.Hint | Should -BeLike '*-Library <path>*'
    }

    It 'fails, lists them all, and asks for -Library when several were not chosen from' {
        $result = New-LibraryDetectionResult -RegisteredLibraries @('E:\Games\SteamLibrary', 'F:\SteamLibrary') `
            -Candidates @('E:\Games\SteamLibrary', 'F:\SteamLibrary') -Selected ''

        $result.Status | Should -Be 'FAIL'
        $result.Details | Should -Contain 'E:\Games\SteamLibrary'
        $result.Details | Should -Contain 'F:\SteamLibrary'
        $result.Hint | Should -BeLike '*-Library <path>*'
    }
}

Describe 'Get-ElevatedCommandLine' {
    It 'repeats the script and the library, quoted' {
        Get-ElevatedCommandLine -ScriptPath 'C:\Tools\steamshare\windows\setup.ps1' -Library 'E:\Games\SteamLibrary' |
            Should -Be 'powershell -ExecutionPolicy Bypass -File "C:\Tools\steamshare\windows\setup.ps1" -Library "E:\Games\SteamLibrary"'
    }

    It 'leaves -Library out when the library was auto-detected' {
        Get-ElevatedCommandLine -ScriptPath 'C:\Tools\steamshare\windows\setup.ps1' -Library '' |
            Should -Be 'powershell -ExecutionPolicy Bypass -File "C:\Tools\steamshare\windows\setup.ps1"'
    }

    It 'asks for the same one-line command again when there is no script file' {
        # Run through `irm <url> | iex`, the script has no path to repeat.
        Get-ElevatedCommandLine -ScriptPath '' -Library '' |
            Should -Be 'the same "irm <url> | iex" line you just ran'
    }
}

Describe 'Invoke-SteamShareSetup' {
    BeforeEach {
        # A healthy machine: everything already off and the library ready.
        Mock -ModuleName SteamShare Get-RegistryValue { 0 }
        Mock -ModuleName SteamShare Test-IsElevated { $true }
        Mock -ModuleName SteamShare Invoke-PowercfgHibernateOff { [pscustomobject]@{ ExitCode = 0; Output = @() } }
        Mock -ModuleName SteamShare Set-RegistryDword { }
        Mock -ModuleName SteamShare Test-IsFolder { $true }
        Mock -ModuleName SteamShare Get-VolumeFileSystem { 'NTFS' }
        Mock -ModuleName SteamShare Get-SystemDriveLetter { 'C' }
        Mock -ModuleName SteamShare Test-InteractiveSession { $false }
        Mock -ModuleName SteamShare Read-UserAnswer { 'y' }
        Mock -ModuleName SteamShare Get-CimVolumeDirtyBit { $false }
        Mock -ModuleName SteamShare Invoke-FsutilDirtyQuery {
            [pscustomobject]@{ ExitCode = 0; Output = @('Volume - E: is NOT Dirty') }
        }
        Mock -ModuleName SteamShare Get-SteamInstallPath { 'C:\Program Files (x86)\Steam' }
        Mock -ModuleName SteamShare Get-TextFileContent {
            '"0" { "path" "C:\\Program Files (x86)\\Steam" } "1" { "path" "E:\\Games\\SteamLibrary" }'
        }
        $script:scriptPath = 'C:\Tools\steamshare\windows\setup.ps1'
    }

    It 'runs every step in order for a given library' {
        $results = Invoke-SteamShareSetup -Library 'E:\Games\SteamLibrary' -ScriptPath $script:scriptPath

        $results.Title | Should -Be @(
            'Hibernation is off'
            'Fast Startup is off'
            'Library folder exists'
            'Library volume is NTFS'
            'Library volume is clean'
            'Library is registered in Windows Steam'
        )
        $results.Status | Should -Not -Contain 'FAIL'
    }

    It 'passes -WhatIf down to the changes' {
        Mock -ModuleName SteamShare Get-RegistryValue { 1 } -ParameterFilter { $Name -in 'HibernateEnabled', 'HiberbootEnabled' }

        Invoke-SteamShareSetup -Library 'E:\Games\SteamLibrary' -ScriptPath $script:scriptPath -WhatIf | Out-Null

        Should -Invoke -ModuleName SteamShare Invoke-PowercfgHibernateOff -Times 0
        Should -Invoke -ModuleName SteamShare Set-RegistryDword -Times 0
    }

    It 'skips the dirty bit when the volume is not NTFS' {
        Mock -ModuleName SteamShare Get-VolumeFileSystem { 'exFAT' }

        $results = Invoke-SteamShareSetup -Library 'E:\Games\SteamLibrary' -ScriptPath $script:scriptPath

        $results.Title | Should -Not -Contain 'Library volume is clean'
        Should -Invoke -ModuleName SteamShare Get-CimVolumeDirtyBit -Times 0
        Should -Invoke -ModuleName SteamShare Invoke-FsutilDirtyQuery -Times 0
    }

    It 'detects the only candidate and checks it when -Library is omitted' {
        $results = Invoke-SteamShareSetup -ScriptPath $script:scriptPath

        $results[2].Title | Should -Be 'Shared library detected'
        $results[2].Details | Should -Contain 'E:\Games\SteamLibrary'
        Should -Invoke -ModuleName SteamShare Test-IsFolder -ParameterFilter { $Path -eq 'E:\Games\SteamLibrary' }
        $results.Status | Should -Not -Contain 'FAIL'
    }

    It 'runs without a script path, as under irm | iex' {
        $results = Invoke-SteamShareSetup -Library 'E:\Games\SteamLibrary' -ScriptPath ''

        $results.Status | Should -Not -Contain 'FAIL'
    }

    It 'names the one-line command in the hint when not elevated and run without a file' {
        Mock -ModuleName SteamShare Test-IsElevated { $false }
        Mock -ModuleName SteamShare Get-RegistryValue { 1 } -ParameterFilter { $Name -in 'HibernateEnabled', 'HiberbootEnabled' }

        $results = Invoke-SteamShareSetup -Library 'E:\Games\SteamLibrary' -ScriptPath ''

        $results[0].Status | Should -Be 'FAIL'
        $results[0].Hint | Should -BeLike '*irm <url> | iex*'
    }

    It 'stops after the detection when it finds no candidate' {
        Mock -ModuleName SteamShare Get-TextFileContent { '"0" { "path" "C:\\Program Files (x86)\\Steam" }' }

        $results = Invoke-SteamShareSetup -ScriptPath $script:scriptPath

        $results[-1].Title | Should -Be 'Shared library detected'
        $results[-1].Status | Should -Be 'FAIL'
        Should -Invoke -ModuleName SteamShare Test-IsFolder -Times 0
    }

    It 'asks to confirm the detected library in an interactive session' {
        Mock -ModuleName SteamShare Test-InteractiveSession { $true }

        Invoke-SteamShareSetup -ScriptPath $script:scriptPath | Out-Null

        Should -Invoke -ModuleName SteamShare Read-UserAnswer -Times 1 -Exactly
    }

    It 'never asks with -Yes, even in an interactive session' {
        Mock -ModuleName SteamShare Test-InteractiveSession { $true }

        $results = Invoke-SteamShareSetup -ScriptPath $script:scriptPath -Yes

        Should -Invoke -ModuleName SteamShare Read-UserAnswer -Times 0
        $results.Status | Should -Not -Contain 'FAIL'
    }
}
