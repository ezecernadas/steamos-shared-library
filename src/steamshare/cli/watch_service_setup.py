"""The watch part of `install`: run `steamshare watch` whenever the desktop user is logged in.

`install` runs as root, through sudo, but this part belongs to the desktop
user who runs Steam. It:

- turns on Steam's debugger: an empty flag file in the Steam root, read when
  Steam starts, given to the user;
- writes the `steamshare-watch.service` user unit rendered by
  `core.systemd_units` into the user's own unit folder, given to the user;
- enables and starts it in the user's own service manager, which root
  reaches with `systemctl --user --machine=<user>@` (see `adapters.systemctl`).

When Steam has never run, there is no Steam root to put the flag file in, so
this part is skipped and says how to finish it later; the mounts are still
installed. `--no-watch` skips it on purpose.

Planning is separate from applying, so the summary, the dry run and the real
run all show the same plan. Every effect on the machine arrives as a
parameter, so the tests run without root.

Security note: Steam's debugger listens on 127.0.0.1 only, so nothing outside
this machine can reach it. Any program running on this machine, though, can
use it to drive Steam as the user: read the library, change settings, start
games. Decky Loader makes the same trade-off. Deleting the flag file and
restarting Steam turns the debugger off again.
"""

import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from steamshare.adapters.invoking_user import InvokingUser
from steamshare.adapters.safe_writes import ChangeOwner
from steamshare.adapters.steam_cdp import remote_debugging_flag_path, turn_on_remote_debugging
from steamshare.adapters.steam_config import default_steam_root
from steamshare.adapters.systemctl import (
    CommandRunner,
    plan_enable_commands,
    plan_restart_commands,
    run_commands,
    user_systemctl,
)
from steamshare.adapters.unit_installer import (
    FileAction,
    UnitFileOutcome,
    install_owned_unit_files,
)
from steamshare.cli.install_choices import InstallError
from steamshare.cli.shared import (
    DONE_ACTION_LABELS,
    PLANNED_ACTION_LABELS,
    SUDO_INSTALL_COMMAND,
    format_outcome,
)
from steamshare.core.systemd_units import (
    WATCH_SERVICE,
    UnitFile,
    WatchServiceConfig,
    render_watch_service,
)

# Where the user's systemd manager looks for the user's own units, relative to
# the home. systemd uses $XDG_CONFIG_HOME/systemd/user instead when the
# variable is set in the manager's own environment (`man systemd.unit`, "User
# Unit Search Path"). Root cannot read that environment reliably: sudo resets
# it, and a value set in a shell profile never reaches the manager anyway. So
# this is ~/.config, where the manager looks unless the user configured it
# otherwise through environment.d or PAM.
USER_UNIT_DIRECTORY_IN_HOME = Path(".config") / "systemd" / "user"

NO_WATCH_FLAG = "--no-watch"

RESTART_STEAM_NOTICE = (
    "Restart Steam once (Steam → Exit, then start it again) so it turns its debugger "
    "on. Until then, watch waits for it."
)
DEFAULT_LIBRARY_NOTICE = (
    "In Steam, make the shared library the default (Settings → Storage → Make default), "
    "so a click on Install puts games there and fetches their Windows build."
)


@dataclass(frozen=True)
class WatchServicePlan:
    """The watch part, ready to preview or apply."""

    user: InvokingUser
    steam_root: Path
    unit_directory: Path
    unit: UnitFile


@dataclass(frozen=True)
class WatchServiceSkipped:
    """The watch part is not set up this time, and why."""

    reason: str
    # What the user can do to set it up later; None when skipping was asked for.
    next_step: str | None = None


WatchService = WatchServicePlan | WatchServiceSkipped


def plan_watch_service(
    *,
    skip: bool,
    user: InvokingUser,
    library: PurePosixPath,
    tool: str,
    steam_root: Path | None,
    launcher: Path,
) -> WatchService:
    """Decide what the watch part does for this user.

    `steam_root` is None unless the user named one; the default is in the
    user's home, not in root's.
    """
    if skip:
        return WatchServiceSkipped(reason=f"{NO_WATCH_FLAG} was given")
    if not launcher.is_file():
        raise InstallError(
            f"The watch service runs steamshare through {launcher}, which is missing. "
            f"Run install from a clone of the repository (`{SUDO_INSTALL_COMMAND}`), "
            f"or add {NO_WATCH_FLAG} to install only the mounts."
        )
    home = Path(user.home)
    steam_root = steam_root if steam_root is not None else default_steam_root(home)
    if not steam_root.is_dir():
        return WatchServiceSkipped(
            reason=f"Steam has never run for {user.name}: {steam_root} does not exist",
            next_step=(
                f"To add it, run Steam once as {user.name}, then run `{SUDO_INSTALL_COMMAND}` "
                "again; it changes only what is missing. If Steam is installed elsewhere "
                "(such as the Flatpak), pass its folder with --steam-root."
            ),
        )
    unit = render_watch_service(
        WatchServiceConfig(
            launcher=PurePosixPath(launcher),
            library=library,
            tool=tool,
            steam_root=PurePosixPath(steam_root),
        )
    )
    return WatchServicePlan(
        user=user,
        steam_root=steam_root,
        unit_directory=home / USER_UNIT_DIRECTORY_IN_HOME,
        unit=unit,
    )


def format_watch_summary(watch: WatchService) -> list[str]:
    """Return the summary lines for the watch part, shown before install asks to proceed."""
    if isinstance(watch, WatchServiceSkipped):
        return [f"  Watch service:  skipped ({watch.reason})"]
    return [
        f"  Watch service:  {watch.unit_directory / watch.unit.name} (user {watch.user.name})",
        f"  Steam debugger: {remote_debugging_flag_path(watch.steam_root)}",
    ]


def preview_watch_service(watch: WatchService) -> None:
    """Print what the watch part would change, changing nothing."""
    if isinstance(watch, WatchServiceSkipped):
        _print_skipped(watch)
        return
    print(f"--- {watch.unit_directory / watch.unit.name}")
    print(watch.unit.content)
    print("Planned changes (watch service):")
    flag_is_new = _turn_on_debugger(watch, change_owner=_no_owner_change_in_a_dry_run, dry_run=True)
    print(f"  {'would create' if flag_is_new else 'unchanged'}: {_flag_file(watch)}")
    [outcome] = _install_unit(watch, change_owner=_no_owner_change_in_a_dry_run, dry_run=True)
    print(format_outcome(outcome, PLANNED_ACTION_LABELS))
    for command in _plan_systemctl_commands(watch, outcome):
        print(f"  would run: {shlex.join(command)}")


def apply_watch_service(
    watch: WatchService, *, change_owner: ChangeOwner, run_command: CommandRunner
) -> None:
    """Set up the watch part. The mounts are installed by then; errors say so."""
    if isinstance(watch, WatchServiceSkipped):
        _print_skipped(watch)
        return
    try:
        flag_is_new = _turn_on_debugger(watch, change_owner=change_owner, dry_run=False)
        [outcome] = _install_unit(watch, change_owner=change_owner, dry_run=False)
    except OSError as error:
        raise InstallError(
            f"The mounts are set up, but writing {error.filename} for the watch service "
            f"failed: {error.strerror}. Fix the cause and run `{SUDO_INSTALL_COMMAND}` again, "
            f"or add {NO_WATCH_FLAG} to skip the watch service."
        ) from error
    print(f"  {'created' if flag_is_new else 'unchanged'}: {_flag_file(watch)}")
    print(format_outcome(outcome, DONE_ACTION_LABELS))

    try:
        run_commands(_plan_systemctl_commands(watch, outcome), run_command)
    except (subprocess.CalledProcessError, FileNotFoundError) as error:
        raise InstallError(_service_manager_error(watch, error)) from error
    print(f"Enabled and running for {watch.user.name}: {watch.unit.name}")

    if flag_is_new:
        print(RESTART_STEAM_NOTICE)
    print(DEFAULT_LIBRARY_NOTICE)
    print(f"Follow what watch does with `journalctl --user -u {WATCH_SERVICE} -f`.")


def _plan_systemctl_commands(watch: WatchServicePlan, outcome: UnitFileOutcome) -> list[list[str]]:
    systemctl = user_systemctl(watch.user.name)
    changed = [watch.unit.name] if outcome.action is FileAction.UPDATED else []
    return [
        *plan_enable_commands([watch.unit.name], systemctl=systemctl),
        # A new unit is started by `enable --now`; an updated one may already
        # be running its old command line, such as one from before the
        # launcher existed, so it is restarted.
        *plan_restart_commands(changed, systemctl=systemctl),
    ]


def _turn_on_debugger(watch: WatchServicePlan, *, change_owner: ChangeOwner, dry_run: bool) -> bool:
    return turn_on_remote_debugging(
        watch.steam_root,
        user_id=watch.user.user_id,
        group_id=watch.user.group_id,
        change_owner=change_owner,
        dry_run=dry_run,
    )


def _install_unit(
    watch: WatchServicePlan, *, change_owner: ChangeOwner, dry_run: bool
) -> list[UnitFileOutcome]:
    return install_owned_unit_files(
        [watch.unit],
        watch.unit_directory,
        watch.user.user_id,
        watch.user.group_id,
        change_owner=change_owner,
        dry_run=dry_run,
    )


def _flag_file(watch: WatchServicePlan) -> Path:
    return remote_debugging_flag_path(watch.steam_root)


def _print_skipped(watch: WatchServiceSkipped) -> None:
    print(f"Watch service skipped: {watch.reason}.")
    if watch.next_step is not None:
        print(watch.next_step)


def _service_manager_error(watch: WatchServicePlan, error: Exception) -> str:
    name = watch.user.name
    return (
        f"The mounts are set up and the watch service was written, but starting it in "
        f"{name}'s service manager failed ({error}). That manager runs only while {name} "
        f"is logged in, and reaching it from root needs systemd 248 or newer. Log in as "
        f"{name}, then run `{SUDO_INSTALL_COMMAND}` again. To see why, run "
        f"`systemctl --user status {WATCH_SERVICE}` as {name}. "
        f"To skip the watch service, add {NO_WATCH_FLAG}."
    )


def _no_owner_change_in_a_dry_run(path: Path, user_id: int, group_id: int) -> None:
    # A dry run changes no owner; this stands in where an owner change is required.
    raise AssertionError(f"a dry run tried to change the owner of {path}")
