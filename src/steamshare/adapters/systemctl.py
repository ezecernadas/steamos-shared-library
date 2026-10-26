"""Tell systemd about new or removed unit files, and start, restart or stop them.

Commands run without a shell, so no path or unit name is ever interpreted
by one. The runner is a parameter so tests can record the commands instead.
Planning a command list is kept apart from running it, so a dry run prints
exactly what a real run would do.
"""

import subprocess
from collections.abc import Callable, Iterable, Sequence

# The system manager, for units in /etc/systemd/system (needs root).
SYSTEM_SYSTEMCTL = ("systemctl",)

# Runs one command and raises if it fails.
CommandRunner = Callable[[Sequence[str]], None]


def user_systemctl(user_name: str) -> tuple[str, ...]:
    """Return the systemctl that talks to one user's own service manager.

    `install` runs as root, and plain `systemctl --user` would reach root's
    manager, not the desktop user's. `--machine=<user>@` (an empty container
    name means the local host) connects to that user's manager instead, the
    one that reads ~/.config/systemd/user. It needs systemd 248 or newer
    (systemd NEWS, "CHANGES WITH 248": `systemctl --user -M lennart@ start
    quux`), and the user's manager must be running, which it is while the
    user is logged in.
    """
    return ("systemctl", "--user", f"--machine={user_name}@")


def run_command(command: Sequence[str]) -> None:
    """Run a command, raising `subprocess.CalledProcessError` if it fails."""
    subprocess.run(command, check=True)


def plan_enable_commands(
    unit_names: Iterable[str], *, systemctl: Sequence[str] = SYSTEM_SYSTEMCTL
) -> list[list[str]]:
    """Return the commands that start the units now and at every boot."""
    return [
        # Without the reload, systemd keeps using the unit files it read last.
        daemon_reload_command(systemctl=systemctl),
        [*systemctl, "enable", "--now", *unit_names],
    ]


def daemon_reload_command(*, systemctl: Sequence[str] = SYSTEM_SYSTEMCTL) -> list[str]:
    """Return the command that makes systemd reread its unit files, new or removed."""
    return [*systemctl, "daemon-reload"]


def disable_now_command(
    unit_name: str, *, systemctl: Sequence[str] = SYSTEM_SYSTEMCTL
) -> list[str]:
    """Return the command that stops a unit now and unlinks it from boot."""
    return [*systemctl, "disable", "--now", unit_name]


def stop_command(unit_name: str, *, systemctl: Sequence[str] = SYSTEM_SYSTEMCTL) -> list[str]:
    """Return the command that stops a unit that was never enabled."""
    return [*systemctl, "stop", unit_name]


def plan_restart_commands(
    unit_names: Iterable[str], *, systemctl: Sequence[str]
) -> list[list[str]]:
    """Return the command that restarts units whose unit file just changed.

    `enable --now` starts a stopped unit but leaves a running one alone, and
    a running service keeps the command line systemd read when it started.
    Only a restart makes it run the new one.
    """
    names = list(unit_names)
    if not names:
        return []
    return [[*systemctl, "restart", *names]]


def run_commands(commands: Iterable[Sequence[str]], run: CommandRunner = run_command) -> None:
    """Run each command in order, stopping at the first that fails."""
    for command in commands:
        run(command)


def enable_units_now(
    unit_names: Iterable[str],
    run: CommandRunner = run_command,
    *,
    systemctl: Sequence[str] = SYSTEM_SYSTEMCTL,
) -> None:
    """Make systemd reread unit files, then start the units now and at every boot."""
    run_commands(plan_enable_commands(unit_names, systemctl=systemctl), run)
