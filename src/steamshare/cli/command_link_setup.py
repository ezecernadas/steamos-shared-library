"""The command part of `install`: put `steamshare` on PATH, so it runs from any folder.

The first install runs as `sudo ./steamshare install`, from the repository;
afterwards the user types `steamshare`. That is a symlink to the launcher
(`adapters.command_link`):

- in /usr/local/bin, on every distribution's PATH, owned by root;
- or, where that folder is on a read-only filesystem, as SteamOS's root is,
  in the user's ~/.local/bin, which the folder and the link are given to.

Root cannot tell whether ~/.local/bin is on the user's PATH: sudo resets
PATH, and many distributions add that folder in the login profile only once
it exists, so a fresh one counts only from the next login. So that case
always ends with a next step saying what to do if the command is not found.

A file named `steamshare` that is not steamshare's link is never replaced;
the run reports it and goes on. Like the watch part, planning is separate
from applying, so the summary, the dry run and the real run show the same
plan, in a "Command" section laid out by `run_report`.
"""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from steamshare.adapters.command_link import (
    COMMAND_NAME,
    USER_COMMAND_DIRECTORY_IN_HOME,
    ExistingCommand,
    inspect_command_path,
    link_command,
)
from steamshare.adapters.invoking_user import InvokingUser
from steamshare.adapters.safe_writes import ChangeOwner
from steamshare.adapters.unit_installer import FileAction, create_owned_directories
from steamshare.cli.install_choices import InstallError
from steamshare.cli.run_report import ClosingNotes, RunReport
from steamshare.cli.shared import INSTALLED_COMMAND, LAUNCHER_COMMAND, SUDO_INSTALL_COMMAND

SECTION_TITLE = "Command"

# How each thing already there reads in the report; another file is skipped.
FILE_ACTIONS = {
    ExistingCommand.MISSING: FileAction.CREATED,
    ExistingCommand.CURRENT_LINK: FileAction.UNCHANGED,
    ExistingCommand.OLD_LINK: FileAction.UPDATED,
}

# What a real run writes a link for: nothing there yet, or our link to a
# checkout that moved. A current link is left as it is, another file alone.
NEEDS_LINKING = frozenset({ExistingCommand.MISSING, ExistingCommand.OLD_LINK})

USER_PATH_STEP = (
    f"`{INSTALLED_COMMAND}` is in {{folder}}. If your shell does not find it, that folder "
    "is not on your PATH yet: log out and back in (most distributions add it at login once "
    f'it exists), or add `export PATH="$HOME/{USER_COMMAND_DIRECTORY_IN_HOME}:$PATH"` to '
    "~/.bashrc and open a new terminal."
)
OTHER_FILE_STEP = (
    f"`{INSTALLED_COMMAND}` was not added: {{path}} is another program's file. Rename or "
    f"remove it, then run `{SUDO_INSTALL_COMMAND}` again; until then, run "
    f"{LAUNCHER_COMMAND} from the repository folder."
)


@dataclass(frozen=True)
class CommandLinkPlan:
    """Where the link goes, and what is there now."""

    path: Path
    launcher: Path
    existing: ExistingCommand
    # The user the folder and link are given to, when they are in the user's
    # home; None for the system folder, which stays root's.
    owner: InvokingUser | None


@dataclass(frozen=True)
class CommandLinkSkipped:
    reason: str


CommandLink = CommandLinkPlan | CommandLinkSkipped


def plan_command_link(
    *,
    user: InvokingUser,
    launcher: Path,
    system_directory: Path,
    is_read_only: Callable[[Path], bool],
) -> CommandLink:
    if not launcher.is_file():
        return CommandLinkSkipped(reason=f"the launcher {launcher} is missing")
    if system_directory.is_dir() and not is_read_only(system_directory):
        path, owner = system_directory / COMMAND_NAME, None
    else:
        path = Path(user.home) / USER_COMMAND_DIRECTORY_IN_HOME / COMMAND_NAME
        owner = user
    return CommandLinkPlan(
        path=path, launcher=launcher, existing=inspect_command_path(path, launcher), owner=owner
    )


def command_link_summary_field(link: CommandLink) -> tuple[str, str]:
    """Return the summary's (label, value) pair for the command part."""
    if isinstance(link, CommandLinkSkipped):
        return SECTION_TITLE, f"skipped ({link.reason})"
    return SECTION_TITLE, f"{INSTALLED_COMMAND}, linked at {link.path}"


def preview_command_link(link: CommandLink, report: RunReport) -> ClosingNotes:
    """Print what the command part would change, changing nothing."""
    if isinstance(link, CommandLinkSkipped):
        return _report_skipped(link, report)
    print(report.section(SECTION_TITLE, PurePosixPath(link.path.parent)))
    print(_status_line(link, report))
    print()
    return _closing_notes(link, report)


def apply_command_link(
    link: CommandLink,
    *,
    change_owner: ChangeOwner,
    change_link_owner: ChangeOwner,
    report: RunReport,
) -> ClosingNotes:
    """Create or update the link. The rest of install is done by then; errors say so.

    `change_link_owner` changes the link itself, not the launcher it points
    at (`os.lchown`).
    """
    if isinstance(link, CommandLinkSkipped):
        return _report_skipped(link, report)
    print(report.section(SECTION_TITLE, PurePosixPath(link.path.parent)))
    if link.existing in NEEDS_LINKING:
        try:
            _make_link(link, change_owner=change_owner, change_link_owner=change_link_owner)
        except OSError as error:
            raise InstallError(
                f"Everything else is set up, but linking {link.path} failed: "
                f"{error.strerror}. Fix the cause and run `{SUDO_INSTALL_COMMAND}` again; "
                f"until then, run {LAUNCHER_COMMAND} from the repository folder."
            ) from error
    print(_status_line(link, report))
    print()
    return _closing_notes(link, report)


def _make_link(
    link: CommandLinkPlan, *, change_owner: ChangeOwner, change_link_owner: ChangeOwner
) -> None:
    owner = link.owner
    if owner is not None:
        create_owned_directories(
            [link.path.parent], owner.user_id, owner.group_id, change_owner=change_owner
        )
    link_command(link.path, link.launcher)
    if owner is not None:
        change_link_owner(link.path, owner.user_id, owner.group_id)


def _status_line(link: CommandLinkPlan, report: RunReport) -> str:
    if link.existing is ExistingCommand.OTHER_FILE:
        return report.skipped(f"{COMMAND_NAME}: another program's file has that name")
    subject = f"{COMMAND_NAME} {report.symbols.arrow} {report.path(PurePosixPath(link.launcher))}"
    return report.file_action(FILE_ACTIONS[link.existing], subject)


def _closing_notes(link: CommandLinkPlan, report: RunReport) -> ClosingNotes:
    if link.existing is ExistingCommand.OTHER_FILE:
        return ClosingNotes(next_steps=(OTHER_FILE_STEP.format(path=link.path),))
    if link.owner is not None:
        folder = report.path(PurePosixPath(link.path.parent))
        return ClosingNotes(next_steps=(USER_PATH_STEP.format(folder=folder),))
    return ClosingNotes()


def _report_skipped(link: CommandLinkSkipped, report: RunReport) -> ClosingNotes:
    print(report.section(SECTION_TITLE))
    print(report.skipped(link.reason))
    print()
    return ClosingNotes()
