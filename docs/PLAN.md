# Project plan

## Goal

A dual-boot PC running Windows and SteamOS 3.8 (reference hardware: RX 6700 XT, Ryzen 5 5600XT) uses **a single Steam library on NTFS**. Games installed from either OS run on both, with no second download.

## Root cause

- Linux NTFS drivers support symlinks, and `ntfs3` emulates permissions through mount options. Neither one is the blocker.
- Proton prefixes (`compatdata`) contain names that are invalid on Windows (`dosdevices/c:`). Windows and `chkdsk` treat those names as corruption.
- Valve does not support NTFS for Proton, and SteamOS does not automount NTFS.
- Windows Fast Startup leaves NTFS dirty, so Linux mounts it read-only.

## Architecture

| Component | Responsibility |
|---|---|
| NTFS mount unit | Mounts the library partition at boot. It is a `systemd` unit in `/etc/systemd/system/` (survives SteamOS A/B updates) using `ntfs3` with `uid=1000,gid=1000,umask=022,windows_names,nofail`. |
| Bind-mount units | Mount `~/.local/share/steam-shared/{compatdata,shadercache}` (ext4) over `SteamLibrary/steamapps/{compatdata,shadercache}`. They have `Requires=`/`After=` on the NTFS mount. |
| `force-proton` | A Python tool built on the `vdf` library. It writes `CompatToolMapping` in `config.vdf` for every app in the shared library that ships a native Linux build. It refuses to run while Steam is open, and backs up first. |
| `doctor` | Health check: the mount is `ntfs3` (not `fuseblk`), the volume is neither dirty nor read-only, the bind mounts are active, and there are no Windows-invalid names or stray symlinks on NTFS. |
| `install.sh` / `uninstall.sh` | Idempotent setup and teardown; they detect the partition by UUID. |
| `windows/setup.ps1` | Disables Fast Startup and hibernation, and checks the library path. |

## Phases

Each phase ends with passing tests.

0. **Manual spike** with one game. Confirm that the bind mount and `windows_names` work, and that Windows and SteamOS open the same install without re-downloading. This is the go/no-go gate.
1. NTFS mount unit and a minimal `doctor`.
2. Bind mounts for `compatdata` and `shadercache`.
3. `force-proton`, with pytest over `config.vdf` and `appmanifest_*.acf` fixtures.
4. Installer and uninstaller. Check that the setup survives a SteamOS update.
5. Windows setup script.
6. CI on GitHub Actions:
   - lint with `shellcheck` and `ruff`;
   - `pytest`;
   - an integration test that creates an NTFS image (`mkntfs` on a loop device), mounts it with `ntfs3`, and checks `windows_names` and the bind mounts.

## Testing

Strict TDD.

- **Unit:** `bats` for shell, `pytest` for Python.
- **Integration:** a loop-device NTFS image in CI.
- **End-to-end:** done manually on the real dual-boot PC.

## Known risks

- Steam rewrites `config.vdf` on exit, so `force-proton` must run with Steam closed.
- A SteamOS update could reset `/etc`. Phase 4 verifies this.
- Some games keep saves inside the install folder; those saves are shared across the two OSes.
- Steam may occasionally re-validate a game after an OS switch.
- The SteamOS 3.8 installer wipes the target disk. Install it on a separate SSD with the Windows disk disconnected.
