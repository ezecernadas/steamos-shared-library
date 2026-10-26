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

The manual proof of concept passed: one install of a game ran on both Windows and Linux (Proton) with no re-download and a clean `chkdsk`. Tooling is not written yet. See [docs/PLAN.md](docs/PLAN.md).

## Prior art

- [Proton wiki: Using a NTFS disk with Linux and Windows](https://github.com/ValveSoftware/Proton/wiki/Using-a-NTFS-disk-with-Linux-and-Windows) describes a manual `compatdata` symlink.
- [scawp/Steam-Deck.Mount-External-Drive](https://github.com/scawp/Steam-Deck.Mount-External-Drive) adds NTFS automounting on SteamOS.
