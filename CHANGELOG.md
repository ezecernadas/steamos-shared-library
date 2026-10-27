# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html). Versions below 1.0.0 are pre-releases: they work on the systems listed as tested in the README, and their interface may still change.

## [Unreleased]

## [0.2.0] - 2026-10-27

One-command installs and a picker for games that keep their Linux build. Still a pre-release: tested on Zorin OS 18 with Windows, not yet on SteamOS.

### Added

- An "AI assistance" section in the README that says how the project was built.
- A README FAQ entry on NTFS read performance, with `ntfs3` vs ext4 measurements.
- A project logo (`assets/logo.svg`) and a social preview image for link previews.
- A README note that `watch` fetches the Windows build of native games in any library, and how to opt a game out.
- `steamshare keep-native`: with Steam open, a full-screen checkbox list of the native Linux games steamshare decides about, to choose which ones keep their Linux build. It applies the choice at once: it clears steamshare's Proton mapping of a game newly kept native, and maps an unticked game to Proton again. Its library defaults to the one the watch service uses.
- `install` puts a `steamshare` command on `PATH`: a link to the launcher in `/usr/local/bin`, or in the user's `~/.local/bin` when the root filesystem is read-only (SteamOS). It never replaces a file that is not its own link, and `uninstall` removes the link.
- One-command installs, served from the assets of each GitHub release:
  - Windows: `irm <release>/setup.ps1 | iex` runs a single-file `setup.ps1` with the module embedded. It takes no parameters, so the library is chosen interactively; run that way, it never closes the PowerShell window and leaves no settings behind in it.
  - Linux: `curl -fsSL <release>/install.sh | sudo bash` downloads the release into `/opt/steamshare` (or `~/.local/share/steamshare` on a read-only root, as on SteamOS) and runs `install` from there. Install options go after `bash -s --`; running it again updates. It only replaces a folder an earlier one-line install placed.
- `uninstall` names the program folder of a one-line install and the command that deletes it; it never deletes the code it runs from.
- `release/build_assets.py` builds those assets, and a release workflow publishes them as a pre-release when a `vX.Y.Z` tag is pushed, with the CHANGELOG section as notes.

### Fixed

- `watch` could undo a `keep_native` change saved while one of its passes ran, by writing back the whole state it read at the start of the pass. Every command now saves only the appids it changed, on top of the file as it is, under a lock.

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

[Unreleased]: https://github.com/ezecernadas/steamshare/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/ezecernadas/steamshare/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/ezecernadas/steamshare/releases/tag/v0.1.0
