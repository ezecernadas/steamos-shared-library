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
run all show the same plan, laid out by `run_report` in a "Watch service"
section. What the user does next (restart Steam, make the library the
default) goes back to `install` as `ClosingNotes`, for the end of the report.
Every effect on the machine arrives as a parameter, so the tests run without
root.

Security note: Steam's debugger listens on 127.0.0.1 only, so nothing outside
this machine can reach it. Any program running on this machine, though, can
use it to drive Steam as the user: read the library, change settings, start
games. Decky Loader makes the same trade-off. Deleting the flag file and
restarting Steam turns the debugger off again.
"""

import shlex
import subprocess
from dataclasses import dataclass
from datetime import datetime
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
from steamshare.cli.run_report import (
    RUNNING_WORD,
    WOULD_RUN_WORD,
    ClosingNotes,
    RunReport,
)
from steamshare.cli.shared import SUDO_INSTALL_COMMAND
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

# The next steps after setting the watch part up; {arrow} is the report's
# menu arrow, so an ASCII-only output gets one it can show.
RESTART_STEAM_STEP = (
    "Restart Steam once (Steam {arrow} Exit, then start it again) so it turns its debugger "
    "on. Until then, watch waits for it."
)
DEFAULT_LIBRARY_STEP = (
    "In Steam: Settings {arrow} Storage {arrow} the shared library {arrow} Make default, "
    "so a click on Install puts games there and fetches their Windows build."
)
FOLLOW_WATCH_TIP = ("Follow watch", f"journalctl --user -u {WATCH_SERVICE} -f")

SECTION_TITLE = "Watch service"
FLAG_FILE_NAME_IN_REPORT = "Steam debugger flag"


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


def watch_summary_field(watch: WatchService) -> tuple[str, str]:
    """Return the summary's (label, value) pair for the watch part."""
    if isinstance(watch, WatchServiceSkipped):
        return SECTION_TITLE, f"skipped ({watch.reason})"
    return SECTION_TITLE, f"for user {watch.user.name}"


def preview_watch_service(watch: WatchService, report: RunReport) -> ClosingNotes:
    """Print what the watch part would change, changing nothing."""
    if isinstance(watch, WatchServiceSkipped):
        return _report_skipped(watch, report)
    print(f"--- {report.path(watch.unit_directory / watch.unit.name)}")
    print(watch.unit.content)
    print(report.section(SECTION_TITLE, watch.unit_directory))
    flag_is_new = _turn_on_debugger(watch, change_owner=_no_owner_change_in_a_dry_run, dry_run=True)
    print(_flag_file_line(watch, flag_is_new, report))
    [outcome] = _install_unit(
        watch, change_owner=_no_owner_change_in_a_dry_run, now=None, dry_run=True
    )
    print(report.outcome(outcome, watch.unit_directory))
    for command in _plan_systemctl_commands(watch, outcome):
        print(report.planned(WOULD_RUN_WORD, shlex.join(command)))
    print()
    return _closing_notes(outcome, flag_is_new, report)


def apply_watch_service(
    watch: WatchService,
    *,
    change_owner: ChangeOwner,
    run_command: CommandRunner,
    report: RunReport,
    now: datetime,
) -> ClosingNotes:
    """Set up the watch part. The mounts are installed by then; errors say so.

    `now` names the backup of a replaced unit, the same as install's own.
    """
    if isinstance(watch, WatchServiceSkipped):
        return _report_skipped(watch, report)
    print(report.section(SECTION_TITLE, watch.unit_directory))
    try:
        flag_is_new = _turn_on_debugger(watch, change_owner=change_owner, dry_run=False)
        [outcome] = _install_unit(watch, change_owner=change_owner, now=now, dry_run=False)
    except OSError as error:
        raise InstallError(
            f"The mounts are set up, but writing {error.filename} for the watch service "
            f"failed: {error.strerror}. Fix the cause and run `{SUDO_INSTALL_COMMAND}` again, "
            f"or add {NO_WATCH_FLAG} to skip the watch service."
        ) from error
    print(_flag_file_line(watch, flag_is_new, report))
    print(report.outcome(outcome, watch.unit_directory))

    try:
        run_commands(_plan_systemctl_commands(watch, outcome), run_command)
    except (subprocess.CalledProcessError, FileNotFoundError) as error:
        raise InstallError(_service_manager_error(watch, error)) from error
    print(report.done(RUNNING_WORD, f"for {watch.user.name}"))
    print()
    return _closing_notes(outcome, flag_is_new, report)


def _flag_file_line(watch: WatchServicePlan, flag_is_new: bool, report: RunReport) -> str:
    action = FileAction.CREATED if flag_is_new else FileAction.UNCHANGED
    flag_file = report.path(remote_debugging_flag_path(watch.steam_root))
    return report.file_action(action, f"{FLAG_FILE_NAME_IN_REPORT} ({flag_file})")


def _closing_notes(outcome: UnitFileOutcome, flag_is_new: bool, report: RunReport) -> ClosingNotes:
    arrow = report.symbols.arrow
    # Steam reads the flag file only when it starts.
    restart = [RESTART_STEAM_STEP.format(arrow=arrow)] if flag_is_new else []
    return ClosingNotes(
        unit_outcomes=(outcome,),
        next_steps=(*restart, DEFAULT_LIBRARY_STEP.format(arrow=arrow)),
        tips=(FOLLOW_WATCH_TIP,),
    )


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
    watch: WatchServicePlan, *, change_owner: ChangeOwner, now: datetime | None, dry_run: bool
) -> list[UnitFileOutcome]:
    return install_owned_unit_files(
        [watch.unit],
        watch.unit_directory,
        watch.user.user_id,
        watch.user.group_id,
        change_owner=change_owner,
        now=now,
        dry_run=dry_run,
    )


def _report_skipped(watch: WatchServiceSkipped, report: RunReport) -> ClosingNotes:
    print(report.section(SECTION_TITLE))
    print(report.skipped(watch.reason))
    print()
    next_steps = () if watch.next_step is None else (watch.next_step,)
    return ClosingNotes(next_steps=next_steps)


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
