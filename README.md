# steamos-shared-library

Share **one Steam library on an NTFS partition** between Windows and SteamOS on a dual-boot PC, so every game is installed only once.

## The problem

NTFS is not what breaks Steam on Linux. Linux NTFS drivers handle symlinks, and `ntfs3` emulates POSIX permissions through `uid`/`gid`/`umask`. The real issue is **Proton's prefixes** (`steamapps/compatdata`). They contain names that are invalid on Windows, such as `dosdevices/c:`, which leave the volume looking corrupted to Windows and `chkdsk`. Game files themselves are fine.

## The approach

- Mount the library with the in-kernel `ntfs3` driver and `windows_names`, so Linux can never write a Windows-invalid name.
- Keep `compatdata` and `shadercache` on an ext4 partition and **bind-mount** them over the library folders. NTFS never sees a Proton prefix, and Windows never sees a dangling symlink.
- Force Proton for games that ship a native Linux build, so both OSes use the same Windows depot and Steam does not re-download.
- Disable Windows Fast Startup and hibernation so the volume is never left dirty.

## Status

Working on a Zorin OS + Windows dual boot: `install` mounts the library at boot, `watch` forces Proton live so a click on Install fetches the Windows build, `doctor` checks the setup, and `windows/setup.ps1` prepares Windows. Not yet tested on SteamOS itself (Phase 4). See [docs/PLAN.md](docs/PLAN.md).

## Linux setup

From the repository folder, after `uv sync`:

```bash
sudo .venv/bin/python -m steamshare install
.venv/bin/python -m steamshare install-watch
```

- **`install`** lists the NTFS partitions and what each one holds (Windows, recovery, a Steam library). To see that, it mounts each one read-only for a moment, which writes nothing. It asks which partition is the shared library, then writes systemd units that mount it at every boot with `ntfs3` and `windows_names`, and that bind-mount `compatdata` and `shadercache` from ext4.
  - `--dry-run` shows the plan.
  - `--partition-uuid`, `--library-folder`, `--mount-point` (default `/mnt/steamlib`) and `--yes` skip the questions, for scripts.
- **`install-watch`** (no sudo) enables Steam's local debugger and a user service. With Steam open, the service forces Proton on games that ship a native Linux build, so installing one into the shared library fetches its Windows build.
  - Set the shared library as the default in Steam (Settings → Storage → Make default) and a click on Install is all it takes.
  - `force-proton --keep-native <appid>` opts a game out.
- **`doctor --library /mnt/steamlib/SteamLibrary`** checks the whole setup.

## Windows setup

`windows/setup.ps1` turns off hibernation and Fast Startup, which leave NTFS dirty so that Linux mounts it read-only, then checks the library: the folder, its NTFS volume, the dirty bit, and its registration in Windows Steam. It needs nothing beyond the Windows PowerShell that ships with Windows.

From an elevated PowerShell (Run as administrator), in the repository folder:

```powershell
powershell -ExecutionPolicy Bypass -File windows\setup.ps1
```

- Without `-Library`, the script looks for the shared library among the ones Windows Steam knows: an NTFS library outside the system drive. It asks you to confirm the one it found, or to pick one when there are several.
- `-Library <path>` names the library directly, for example `-Library E:\Games\SteamLibrary`. It is needed only when several libraries qualify and nobody can answer the question (in a script).
- `-Yes` never asks a question, for scripts: a single detected library is used as is, and several make the script fail and list them, so you pass one with `-Library`.
- `-WhatIf` shows what would change and changes nothing; it does not need an elevated prompt.

Running it again is safe: it only changes what is not already right. The exit code is 1 when a step failed.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## Prior art

- [Proton wiki: Using a NTFS disk with Linux and Windows](https://github.com/ValveSoftware/Proton/wiki/Using-a-NTFS-disk-with-Linux-and-Windows) describes a manual `compatdata` symlink.
- [scawp/Steam-Deck.Mount-External-Drive](https://github.com/scawp/Steam-Deck.Mount-External-Drive) adds NTFS automounting on SteamOS.
