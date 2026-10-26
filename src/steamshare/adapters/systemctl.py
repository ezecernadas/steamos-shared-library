"""Tell systemd about new unit files and start them.

Commands run without a shell, so no path or unit name is ever interpreted
by one. The runner is a parameter so tests can record the commands instead.
"""

import subprocess
from collections.abc import Callable, Iterable, Sequence

# The system manager, for units in /etc/systemd/system (needs root), and the
# calling user's own manager, for units in ~/.config/systemd/user (no root).
SYSTEM_SYSTEMCTL = ("systemctl",)
USER_SYSTEMCTL = ("systemctl", "--user")

# Runs one command and raises if it fails.
CommandRunner = Callable[[Sequence[str]], None]


def run_command(command: Sequence[str]) -> None:
    """Run a command, raising `subprocess.CalledProcessError` if it fails."""
    subprocess.run(command, check=True)


def plan_enable_commands(
    unit_names: Iterable[str], *, systemctl: Sequence[str] = SYSTEM_SYSTEMCTL
) -> list[list[str]]:
    """Return the commands that start the units now and at every boot.

    Kept separate so a dry run can print exactly what would run.
    """
    return [
        # Without the reload, systemd keeps using the unit files it read last.
        [*systemctl, "daemon-reload"],
        [*systemctl, "enable", "--now", *unit_names],
    ]


def enable_units_now(
    unit_names: Iterable[str],
    run: CommandRunner = run_command,
    *,
    systemctl: Sequence[str] = SYSTEM_SYSTEMCTL,
) -> None:
    """Make systemd reread unit files, then start the units now and at every boot."""
    for command in plan_enable_commands(unit_names, systemctl=systemctl):
        run(command)
