# steamshare

[![CI](https://github.com/ezecernadas/steamshare/actions/workflows/ci.yml/badge.svg)](https://github.com/ezecernadas/steamshare/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-3776AB?logo=python&logoColor=white)](pyproject.toml)
[![Platforms](https://img.shields.io/badge/platforms-Linux%20%7C%20Windows-1b2838?logo=steam&logoColor=white)](#requirements)
[![License: GPL-3.0-or-later](https://img.shields.io/badge/license-GPL--3.0--or--later-blue)](LICENSE)

Share one NTFS Steam library between Windows and Linux on a dual-boot PC

A dual-boot PC normally needs every game installed twice: once for Windows, once for Linux. steamshare lets both systems use **one Steam library on one NTFS partition**. Install a game from either side and it runs on both, with no second download.

**Highlights**

- 🎮 **One copy of every game.** Windows runs it natively; Linux runs the same files through Proton.
- 🛡️ **NTFS stays clean for Windows.** The things that usually corrupt a shared NTFS library are kept off it.
- 🖱️ **Nothing to remember.** Click *Install* in Steam on Linux and the Windows build lands in the shared library, even for games that also have a Linux build.
- 🩺 **Checks itself.** `doctor` verifies the whole setup, and every error says what to do next.
- 📦 **No dependencies.** It needs only the `python3` and systemd that modern Linux distros already ship, plus the PowerShell built into Windows.

## Contents

- [Why](#why)
- [How it works](#how-it-works)
- [Requirements](#requirements)
- [Install](#install)
- [Usage](#usage)
- [Safety and uninstalling](#safety-and-uninstalling)
- [Troubleshooting](#troubleshooting)
- [FAQ](#faq)
- [Status and roadmap](#status-and-roadmap)
- [AI assistance](#ai-assistance)
- [Contributing](#contributing)
- [License](#license)

## Why

The usual advice is "don't put a Steam library on NTFS for Linux". But NTFS itself is not the problem. Linux reads and writes it fine with the in-kernel `ntfs3` driver, symlinks included, and permissions can be emulated at mount time.

Three things break a shared library in practice:

1. **Proton's prefixes.** `steamapps/compatdata` holds a small Windows install per game, with names such as `dosdevices/c:`. Linux can write that name to NTFS, but Windows cannot, and `chkdsk` reports the volume as corrupted.
2. **Native Linux builds.** For a game that ships a Linux version, Linux Steam downloads it over the Windows files. Windows can no longer start the game, and each system keeps re-downloading it.
3. **Windows Fast Startup and hibernation.** Both leave the NTFS volume marked dirty, so Linux mounts it read-only, or not at all.

steamshare fixes all three, and nothing more. The game files stay exactly as Steam installs them.

## How it works

On Linux, `install` writes systemd units that run at every boot:

| Piece | What it does | Why |
|---|---|---|
| NTFS mount | Mounts the partition with `ntfs3` and `windows_names` | `windows_names` makes Linux refuse any name Windows would reject, so the volume cannot be corrupted that way |
| Bind mounts | Put `compatdata` and `shadercache` on ext4, over the library folders | Proton's prefixes never touch NTFS, and Windows sees ordinary empty folders, not broken symlinks |
| `ntfs3` loader | Runs `modprobe ntfs3` before the mount | Some distros (Zorin, for one) blacklist the module, and systemd's own module loading honors that blacklist |
| `watch` service | While Steam runs, forces Proton for every game that has a native Linux build | So Linux Steam picks the Windows build, through the same per-game setting as *Properties → Compatibility* |

**Live mapping.** `watch` talks to Steam through its local debugger, the same mechanism Decky Loader uses. Steam keeps its configuration in memory and overwrites edits made while it runs, so changing the file is not enough. `watch` never overrides a choice you made, a choice Valve made for a specific title (Counter-Strike 2, Baldur's Gate 3, and others), or a game already installed natively elsewhere.

On Windows, `setup.ps1` turns off hibernation and Fast Startup, then checks the library: the folder, its NTFS volume, the dirty bit, and that Steam knows it.

The design and the experiments behind it are in [docs/PLAN.md](docs/PLAN.md).

## Requirements

| | Needed | Notes |
|---|---|---|
| **Linux** | `python3` 3.11+, systemd 248+, the `ntfs3` kernel module (kernel 5.15+), util-linux 2.37+ | Any current distro, Ubuntu-based or not. Ubuntu 22.04 is too old: it ships Python 3.10 |
| **Windows** | Windows PowerShell 5.1 | Built into Windows 10 and 11 |
| **Disk** | One NTFS partition for the library, outside the Windows system drive | It can be created by shrinking the Windows partition in *Disk Management* |
| **Steam** | Run at least once on each system | |

### Tested on

| System | Status |
|---|---|
| Zorin OS 18 (Ubuntu 24.04 base) + Windows | ✅ Tested end to end |
| SteamOS 3.8 | ⏳ Planned |
| Other distros that meet the requirements | Expected to work, not yet tested. Reports are welcome |

## Install

### 1. On Windows

From an elevated PowerShell (*Run as administrator*), in the repository folder:

```powershell
powershell -ExecutionPolicy Bypass -File windows\setup.ps1
```

It asks you to confirm the shared library it found, or to pick one when there are several. Then shut Windows down fully: use *Shut down*, not *Restart*, so the volume is left clean.

### 2. On Linux

Download the latest release from the [Releases page](https://github.com/ezecernadas/steamshare/releases) and extract it, or clone the repository to follow the latest changes:

```bash
git clone https://github.com/ezecernadas/steamshare
cd steamshare
sudo ./steamshare install
```

`install` lists the NTFS partitions and what each one holds, and asks which one is the shared library:

```text
NTFS partitions
  1) /dev/nvme0n1p3  179 GB            Windows system partition (not recommended)
  2) /dev/nvme0n1p4   96 GB  SteamLib  Steam library in SteamLibrary/
  3) /dev/nvme0n1p5  930 MB            Windows recovery partition (not recommended)

Which partition holds the shared library? [2]:
```

To see what each partition holds, it mounts it read-only for a moment, which writes nothing. Before changing anything it shows a summary to confirm.

### 3. In Steam on Linux

1. Restart Steam once, so it turns on the local debugger that `watch` uses.
2. Go to *Settings → Storage*, pick the shared library, and choose *Make default*.

From then on, a click on *Install* puts the game in the shared library and fetches its Windows build.

## Usage

Everything runs through `./steamshare` in the repository folder:

| Command | What it does |
|---|---|
| `sudo ./steamshare install` | Sets up the mounts and the watch service. Safe to run again: it changes only what is out of date, and backs up anything it replaces. |
| `sudo ./steamshare uninstall` | Removes what `install` set up and keeps your data; see [Safety and uninstalling](#safety-and-uninstalling). |
| `sudo ./steamshare uninstall --purge` | Also deletes the Proton prefixes (and any saves in them) and the Proton choices steamshare made in Steam. Needs Steam open. |
| `./steamshare doctor --library <path>` | Checks the whole setup and says what to fix. |
| `./steamshare force-proton --library <path> --keep-native <appid>` | Lets a game use its native Linux build. Windows can no longer run it from the shared library, so such games are better installed in a separate Linux library. |
| `./steamshare watch --library <path> --once --dry-run` | Shows what the watch service would change right now. |

A healthy setup looks like this:

```text
$ ./steamshare doctor --library /mnt/steamlib/SteamLibrary
[ OK ] Library partition is mounted with ntfs3 and windows_names
[ OK ] compatdata and shadercache are bind-mounted from ext4
[ OK ] Library folder exists
[ OK ] No Windows-invalid names on NTFS
[WARN] No unexpected symlinks on NTFS
       steamapps/common/Steam.dll
```

The `Steam.dll` warning is expected. Linux Steam creates that link itself, and Windows Steam ignores it.

Useful `install` options:

- `--dry-run` shows the plan and changes nothing. It needs no sudo. Add `--show-units` to print the generated systemd files.
- `--no-watch` installs only the mounts.
- `--yes`, `--partition-uuid`, `--library-folder` and `--mount-point` (default `/mnt/steamlib`) answer the questions in advance, for scripts.
- `--steam-root` points at a Steam installed elsewhere, such as the Flatpak.

The Windows script takes `-Library <path>`, `-Yes`, and `-WhatIf` (a dry run).

## Safety and uninstalling

- **Nothing is written without confirmation,** and `--dry-run` / `-WhatIf` show every change first.
- **Every replaced file is backed up** next to itself as `*.bak-<timestamp>`.
- **Steam's debugger listens on `127.0.0.1` only.** No other machine can reach it, but any program on your PC can, as with Decky Loader. Delete `~/.steam/steam/.cef-enable-remote-debugging` and restart Steam to turn it off.
- **Game files are never modified.** steamshare only changes how they are mounted and which build Steam downloads.

To undo the setup:

```bash
./steamshare uninstall --dry-run   # what it would remove and keep; needs no sudo
sudo ./steamshare uninstall
```

It finds what `install` set up by the header of the generated files, never by name, shows the plan, and asks before removing anything (the default answer is no; `--yes` skips the question).

- **Removed:** the watch service, the mount units and the `ntfs3` loader (stopped first, the bind mounts before the NTFS mount), the backups `install` made of them, steamshare's state file, and the mount point folder if it is empty.
- **Kept:** the Proton prefixes and shader caches in `~/.local/share/steam-shared/` (some games keep their saves there), the Proton choices `watch` made in Steam (undoing them would make Steam re-download native games as their Linux build; change one in the game's *Properties → Compatibility*), this repository, and the files on the NTFS partition.
- **Steam's debugger flag** is kept too, since other tools such as Decky Loader may use it. Add `--remove-debugger-flag` to delete it.

To remove everything, saves included, add `--purge`:

| `--purge` also removes | How |
|---|---|
| The Proton prefixes and shader caches in `~/.local/share/steam-shared/`, with any saves inside them | Deleted only after the bind mounts that showed them are stopped; links inside a prefix (such as `dosdevices/z:`, which points at `/`) are removed, never followed |
| Steam's debugger flag | As `--remove-debugger-flag` does |
| The Proton choices steamshare made for games that are **not** installed | Cleared in the running Steam, right after the watch service stops (so it cannot set them again) and before steamshare's state is deleted |

The Proton choices of installed games stay: clearing them would make Steam re-download those games as their Linux build. What remains after a purge is this repository and the games on the NTFS partition.

- **Steam must be open,** since its settings live in the running client. A purge checks this before changing anything. Add `--keep-steam-settings` to purge without Steam and leave every Proton choice in place.
- **It asks you to type `purge`,** not just `y`: the saves in the prefixes cannot be recovered. `--yes` skips the question.
- `./steamshare uninstall --purge --dry-run` lists every file with its size, and how many Proton choices it would clear.

## Troubleshooting

Start with `./steamshare doctor --library <path>`. Every failure it reports includes the next step. The most common ones:

| Symptom | Cause and fix |
|---|---|
| The library is read-only, or not mounted, on Linux | Windows left the volume dirty. On Windows, run `windows\setup.ps1`, then shut down fully. If it persists, run `chkdsk <drive>: /f` on Windows. |
| `unknown filesystem type 'ntfs3'` | Your distro blacklists the module. `install` loads it at boot; for a manual mount, run `sudo modprobe ntfs3` first. |
| Installing a game on Linux downloaded the Linux build | Steam was not restarted after `install`, so `watch` could not reach it. Check with `journalctl --user -u steamshare-watch.service`. |

## FAQ

**Why not exFAT?**
It has no symlinks, which Proton needs, and Steam does not support it as a library on Linux.

**Why not ext4 or Btrfs with a Windows driver?**
Third-party Windows drivers such as WinBtrfs and Ext2Fsd have been unreliable under heavy game I/O. Windows handles NTFS natively and reliably, so Linux is the side that adapts.

**Why bind mounts instead of the usual `compatdata` symlink?**
A symlink is written onto NTFS, where Windows sees a broken link to a Linux path. A bind mount exists only on the Linux side, and Windows sees an ordinary folder.

**What about games with anti-cheat?**
`watch` does not touch titles where Valve chose the compatibility tool itself, such as Counter-Strike 2. Other anti-cheat games behave as they would under Proton anywhere.

**Does it work with the Steam Flatpak?**
Pass `--steam-root ~/.var/app/com.valvesoftware.Steam/.local/share/Steam` to `install`. This is untested.

## Status and roadmap

It works today on a Zorin OS + Windows dual boot. In testing, a native Linux game was installed with one click and only its Windows depot was downloaded. CI runs the unit tests on Python 3.11 to 3.13, integration tests on a real `ntfs3` mount, and the Windows script under PowerShell 5.1 and 7.

- [x] Proof of concept on real hardware
- [x] Boot-time mounts, `doctor`, `force-proton`, live `watch`
- [x] Windows setup script
- [x] Continuous integration
- [ ] Validation on SteamOS: read-only root, A/B updates, Game Mode
- [ ] Validation on more distros (Fedora, Arch, Bazzite)
- [x] `uninstall` command
- [ ] Linux-only tools (Proton, Steam Linux Runtime) kept on an ext4 library

Changes between versions are listed in [CHANGELOG.md](CHANGELOG.md).

## AI assistance

Most of the code and documentation in this project was written with AI assistance: Claude, by Anthropic, through Claude Code. The author directed the design, made the decisions, and tested the tool on real hardware. Every change runs through the CI described above, which runs the unit tests on Python 3.11 to 3.13, integration tests on a real `ntfs3` mount, and the Windows script's tests.

## Contributing

Issues and pull requests are welcome. Read [CONTRIBUTING.md](CONTRIBUTING.md) first. It covers the design rules: code that is easy to read, no runtime dependencies, safe by default, and test-driven development.

## License

[GPL-3.0-or-later](LICENSE) © ezecernadas

### Prior art

- [Proton wiki: Using a NTFS disk with Linux and Windows](https://github.com/ValveSoftware/Proton/wiki/Using-a-NTFS-disk-with-Linux-and-Windows) describes the manual `compatdata` symlink approach.
- [scawp/Steam-Deck.Mount-External-Drive](https://github.com/scawp/Steam-Deck.Mount-External-Drive) adds NTFS automounting on SteamOS.
- [Decky Loader](https://github.com/SteamDeckHomebrew/decky-loader) pioneered talking to Steam's client through its debugger.
