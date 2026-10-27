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
| NTFS mount unit | Mounts the library partition at boot. It is a `systemd` unit in `/etc/systemd/system/` (survives SteamOS A/B updates) using `ntfs3` with `uid=1000,gid=1000,umask=022,windows_names,nofail`. Some distros blacklist `ntfs3` (Zorin ships `/etc/modprobe.d/zorin-blacklist-ntfs3.conf`), so a oneshot `steamshare-ntfs3.service` runs `modprobe ntfs3` by name before the mount, without touching the distro blacklist. `/etc/modules-load.d` does not work here: `systemd-modules-load` honors the blacklist ("Module 'ntfs3' is deny-listed (by kmod)"), and so does the kernel's alias autoload on `mount -t ntfs3`. |
| Bind-mount units | Mount `~/.local/share/steam-shared/{compatdata,shadercache}` (ext4) over `SteamLibrary/steamapps/{compatdata,shadercache}`. They have `Requires=`/`After=` on the NTFS mount. |
| `force-proton` | A Python tool with its own small VDF parser (no third-party packages; see [CONTRIBUTING.md](../CONTRIBUTING.md)). It writes `CompatToolMapping` in `config.vdf` for every app in the shared library that ships a native Linux build. It refuses to run while Steam is open, and backs up first. It must run **before** the library is added to Linux Steam (or before Steam next starts); otherwise Steam queues the Linux depot. |
| `watch` | `force-proton`, live: a user service that, while Steam runs, maps every native Linux game (not installed, or installed in the shared library) to Proton through Steam's own client API, over the Chrome DevTools Protocol on 127.0.0.1:8080, the same mechanism Decky Loader uses. Clicking "Install" then fetches the Windows build. Valve's per-title choices (priority 90-100) and user mappings (250) are left alone. `install` sets it up for the user who ran sudo: it creates `~/.steam/steam/.cef-enable-remote-debugging`, which turns the debugger on at Steam's next start, writes `~/.config/systemd/user/steamshare-watch.service` (both owned by the user), and enables it in the user's manager with `systemctl --user --machine=<user>@` (systemd 248+). `--no-watch` skips it. It shares the state file with `force-proton`. |
| `keep-native` | `force-proton --keep-native`, live and by name: with Steam open, a curses checkbox list of the native Linux games steamshare decides about (Valve's choices, user mappings and tools are not listed). It saves `keep_native`, then clears steamshare's mapping of newly kept games and maps unticked ones again, through the same client API as `watch`. Every command saves only the state entries it changed, under an `flock`, so `watch` cannot overwrite it. |
| `steamshare` launcher | A thin bash script at the repository root that runs `python3 -m steamshare` with `src/` on `PYTHONPATH`, after checking the Python version (`src/steamshare/python_requirement.py`). Users need no `uv`, virtual environment or packaging step, and the watch service's `ExecStart=` runs it by absolute path, so the repository must stay where install ran from. `install` links it as `steamshare` into `/usr/local/bin`, or `~/.local/bin` on a read-only root such as SteamOS's. |
| Linux tools library | Proton and Steam Linux Runtime are installed into the same library as the game unless told otherwise. A dedicated ext4 library keeps those tools (about 1.4 GB and 3,000+ symlinks) off NTFS. |
| `doctor` | Health check: the mount is `ntfs3` (not `fuseblk`), the volume is neither dirty nor read-only, the bind mounts are active, and there are no Windows-invalid names on NTFS. It also lists symlinks on NTFS outside the known Linux-tool folders (Linux Steam creates `steamapps/common/Steam.dll` as a symlink to `~/.steam/.../legacycompat`). |
| `install` / `uninstall` | `sudo ./steamshare install` and `sudo ./steamshare uninstall`, run through the `steamshare` launcher. Idempotent setup and teardown: install mounts the partition by UUID and sets up `watch`; uninstall finds the units by their generated header and removes them, keeping the ext4 Proton prefixes. Both take `--dry-run`. |
| `windows/setup.ps1` | Disables Fast Startup and hibernation, and checks the library: folder, NTFS, dirty bit, and registration in Windows Steam. Without `-Library`, it detects the library among the NTFS ones Windows Steam knows outside the system drive. |

## Phases

Each phase ends with passing tests.

0. **Manual spike** with one game. Confirm that the bind mount and `windows_names` work, and that Windows and SteamOS open the same install without re-downloading. This is the go/no-go gate. **Done: GO.** See [Phase 0 results](#phase-0-results).
1. NTFS mount unit and a minimal `doctor`. **Done:** `steamshare install` and `steamshare doctor`; verified across a reboot on Zorin.
2. Bind mounts for `compatdata` and `shadercache`. **Done** as part of `install`.
3. `force-proton`, with pytest over `config.vdf` and `appmanifest_*.acf` fixtures. **Done**, plus `watch` (set up by `install`) for live mapping (see [Live mapping spike](#live-mapping-spike-2026-10-26)).
4. Installer and uninstaller. Check that the setup survives a SteamOS update. **Uninstaller done:** `steamshare uninstall` finds steamshare's units by their generated header (never by name), disables the bind mounts before the NTFS mount, stops the `ntfs3` loader, and removes the unit files, their backups and the state file. It keeps the ext4 Proton prefixes, the Proton choices `watch` made in Steam (undoing them would re-download native games as their Linux build), and Steam's debugger flag unless `--remove-debugger-flag` is given. `--purge` deletes those too (Steam must be open; it clears only the Proton choices of games that are not installed), after a typed confirmation. The SteamOS update check is still open.
5. Windows setup script. **Done:** `windows/setup.ps1`, with Pester tests and PSScriptAnalyzer in CI under Windows PowerShell 5.1 and PowerShell 7.
6. **Done.** CI on GitHub Actions (`.github/workflows/ci.yml`). The runner's Azure kernel ships `ntfs3` only in `linux-modules-extra`, which the workflow installs when needed:
   - lint with `shellcheck` and `ruff`;
   - `pytest`;
   - an integration test that creates an NTFS image (`mkntfs` on a loop device), mounts it with `ntfs3`, and checks `windows_names` and the bind mounts.

## Phase 0 results

Run on 2026-10-26 on a Zorin OS 18 laptop (kernel 7.0, `ntfs3`) dual-booting Windows. The goal was to validate the mechanism, not the SteamOS specifics. The test game was HYPERFIGHT (appid 1005870), chosen deliberately because it ships a native Linux build.

**Setup:**
1. A dedicated 96 GB NTFS partition was created from Windows.
2. Windows Steam installed the game into `D:\SteamLibrary`.
3. In Linux, the partition was mounted with `ntfs3` and `windows_names`, and `compatdata` and `shadercache` were bind-mounted from ext4.
4. With Steam closed, `CompatToolMapping` was set to `proton_experimental` in `config.vdf` by hand. Only after that was the library folder added to Linux Steam.

**Results:**
- `windows_names` rejects `c:`, `aux`, and names with a trailing dot with `EINVAL`. Inside the bind-mounted `compatdata`, `c:` is accepted, because it lands on ext4.
- Linux Steam detected the game as installed, and the Windows depot (1005871) was kept with no re-download. The ~80 MB it downloaded was the shader pre-cache, which landed in the bind-mounted `shadercache`.
- The game ran under Proton. The prefix (`compatdata/1005870`, including `dosdevices/c:`) lives on ext4, and NTFS has no Windows-invalid names.
- Back on Windows, the game launched without validating or downloading, and `chkdsk D:` reported no problems.

**Findings that change the plan:**
- Zorin blacklists `ntfs3`, so the module has to be loaded explicitly.
- Proton Experimental and SteamLinuxRuntime_4 were installed into the shared NTFS library: about 1.4 GB and 3,071 symlinks. `chkdsk` accepted them, but they belong on ext4.
- Linux Steam created `steamapps/common/Steam.dll` as a symlink to a Linux path. It is harmless to Windows Steam so far, but `doctor` should report it.
- Linux Steam rewrites `appmanifest_*.acf`: it drops `LauncherPath` and changes `DownloadType` and `TargetBuildID`. Windows Steam accepted the result.

Not yet validated: SteamOS itself (automount, `/etc` persistence across updates), games with anti-cheat, and switching OS repeatedly over time.

## Live mapping spike (2026-10-26)

`force-proton` edits `config.vdf`, which only works while Steam is closed: Steam keeps the file in memory and writes it back. To make "click Install in Steam" fetch the Windows build with no extra step, Proton has to be forced live. This spike tested the mechanism Decky Loader uses.

**Results:**
- **Debugger:** an empty `~/.steam/steam/.cef-enable-remote-debugging` makes Steam (after a restart) expose the Chrome DevTools Protocol on `127.0.0.1:8080`, bound to localhost only. The `SharedJSContext` target runs Steam's own UI scripts.
- **Owned games:** `collectionStore.allAppsCollection.allApps` lists them (615 here). `SteamClient.Apps.RegisterForAppDetails` gives each game's `strCompatToolName`, `nCompatToolPriority` and `strInstallFolder`.
- **`vecPlatforms` cannot detect native builds:** it lists `linux` for 604 of 614 games. Native games are recognized by their default tool instead (`steamlinuxruntime*`, or none).
- **Priorities:** 250 is a user mapping (or one made with `SpecifyCompatTool`); 90-100 is Valve's per-title choice (CS2, Baldur's Gate 3), which must not be overridden; 75 is Steam's default; 0 is a native game without a runtime.
- **The test:** `SteamClient.Apps.SpecifyCompatTool(48000, "proton_experimental")`, run with Steam open, set LIMBO to priority 250. Installing it into the shared library then downloaded the Windows build (`limbo.exe`, PE32). An empty tool name clears the mapping.
- **Fragility:** Steam's update of 13 Oct 2026 removed the sibling `GetAvailableCompatTools`. `watch` checks every call and fails with a clear message.

**End-to-end check:**
- With `watch` running, a native game was installed with one click, without touching its properties, into the default library (the shared one). The game was Papers, Please, with separate depots: 239031 for Windows, 239033 for Linux.
- Only the Windows depot (239031) was installed, and every binary is PE32+.
- Of the 141 mapped games, 126 have separate per-OS depots. The rest, such as Your Only Move Is HUSTLE, ship one depot for both systems, so they install the same files either way.
- A first full pass from an empty state mapped 141 games in 6.5 s. Right after many `SpecifyCompatTool` calls in a row, Steam answers details slowly for about a minute. Games whose details time out are retried on the next pass.

This became `steamshare watch`, a user service. `force-proton` remains the offline fallback.

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
