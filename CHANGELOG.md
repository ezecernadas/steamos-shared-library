# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html). Versions below 1.0.0 are pre-releases: they work on the systems listed as tested in the README, and their interface may still change.

## [Unreleased]

### Added

- An "AI assistance" section in the README that says how the project was built.
- A README FAQ entry on NTFS read performance, with `ntfs3` vs ext4 measurements.

## [0.1.0] - 2026-10-26

First pre-release. Tested end to end on Zorin OS 18 with Windows; not yet validated on SteamOS.

### Added

- `steamshare install`:
  - lists the NTFS partitions and what each one holds, and asks which one is the shared library;
  - writes systemd units that mount it at boot with `ntfs3` and `windows_names`, and that bind-mount `compatdata` and `shadercache` from ext4;
  - loads `ntfs3` even where the distro blacklists it;
  - enables the `watch` service for the user who ran it.
- `steamshare watch`: a user service that, while Steam runs, forces Proton for games that have a native Linux build, through Steam's local debugger. A click on *Install* then fetches the Windows build into the shared library. It never overrides a choice made by the user or by Valve for a title, and it never touches games installed natively elsewhere.
- `steamshare force-proton`: the same mapping done by editing `config.vdf` while Steam is closed, plus `--keep-native` to let a game use its Linux build.
- `steamshare doctor`: checks the mounts, the bind mounts, Windows-invalid names and stray symlinks on NTFS. Every failure names the next step.
- `steamshare uninstall`: removes everything `install` wrote and keeps the Proton prefixes, which hold some games' saves, and Steam's settings. `--purge` also deletes the prefixes, the debugger flag and the mappings of games that are not installed, after a typed confirmation.
- `windows/setup.ps1`: turns off hibernation and Fast Startup, then checks the library folder, its NTFS volume, the dirty bit and its registration in Windows Steam. It runs on the Windows PowerShell 5.1 that ships with Windows.
- The `./steamshare` launcher, so users need only the system's `python3`, with no packaging tools.
- CI on GitHub Actions:
  - unit tests on Python 3.11 to 3.13;
  - integration tests on a real `ntfs3` mount;
  - the Windows script under PowerShell 5.1 and 7;
  - shellcheck, ruff and PSScriptAnalyzer.

[Unreleased]: https://github.com/ezecernadas/steamshare/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/ezecernadas/steamshare/releases/tag/v0.1.0
