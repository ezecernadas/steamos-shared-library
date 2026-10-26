"""The `uninstall` command: remove what `install` set up, and keep the user's data.

It needs no arguments, because it finds what steamshare installed by the
header every generated unit starts with (`adapters.generated_units`), never
by guessing names: a unit the user wrote is never touched. It looks in
/etc/systemd/system, and for the watch service in the unit folder of the
desktop user who ran sudo. `core.uninstall_plan` turns what it found into
ordered actions, and this module shows them, asks, and applies them.

A real run shows a short summary of counts (`uninstall_summary`), asks, and
then reports in install's style (`run_report`): a "Watch service" and a
"Mounts" section with one line per file, then notes on removing the kept
things by hand. A dry run shows those sections with "would" verbs instead,
plus a "Kept" section.

What it keeps, on purpose:

- the ext4 Proton prefixes and shader caches, since some games keep their
  saves in their prefix;
- the Proton choices `watch` made in Steam, since undoing them would make
  Steam re-download every native game in the shared library as its Linux
  build;
- Steam's debugger flag, unless `--remove-debugger-flag` is given, since
  other tools such as Decky Loader may rely on it;
- this repository and the files on the NTFS partition.

The user's service manager runs only while the user is logged in. When it
cannot be reached, the watch service's file is still removed, the system
part still runs, and the report says what is left to do. Every effect on
the machine goes through `System`, so the wiring is tested without root.
"""

import argparse
import os
import shlex
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from steamshare.adapters.console import Console, terminal_console
from steamshare.adapters.generated_units import find_generated_unit, find_generated_units
from steamshare.adapters.invoking_user import InvokingUser, resolve_invoking_user
from steamshare.adapters.state_file import state_file_path
from steamshare.adapters.steam_cdp import remote_debugging_flag_path
from steamshare.adapters.steam_config import default_steam_root
from steamshare.adapters.systemctl import (
    SYSTEM_SYSTEMCTL,
    CommandRunner,
    daemon_reload_command,
    disable_now_command,
    run_command,
    stop_command,
    user_systemctl,
)
from steamshare.adapters.unit_installer import SYSTEM_UNIT_DIRECTORY
from steamshare.cli.questions import ask_yes_no
from steamshare.cli.run_report import (
    SKIPPED_WORD,
    ClosingNotes,
    RunReport,
    Symbols,
    choose_symbols,
    format_closing,
    section_header,
)
from steamshare.cli.shared import ROOT_USER_ID, SUDO_UNINSTALL_COMMAND, CommandError
from steamshare.cli.uninstall_summary import REPOSITORY_ITEM, summary_lines
from steamshare.cli.watch_service_setup import (
    FLAG_FILE_NAME_IN_REPORT,
    USER_UNIT_DIRECTORY_IN_HOME,
)
from steamshare.core.systemd_units import WATCH_SERVICE
from steamshare.core.uninstall_plan import (
    DisableUnit,
    InstalledUnit,
    ReloadManager,
    RemoveFile,
    RemoveFolderIfEmpty,
    ServiceManager,
    StopUnit,
    UninstallAction,
    UninstallFacts,
    UninstallPlan,
    build_uninstall_plan,
    enablement_links,
    nothing_installed,
)

EXIT_OK = 0
EXIT_FAILURE = 1

REMOVE_DEBUGGER_FLAG = "--remove-debugger-flag"

NOTHING_TO_UNINSTALL = "Nothing to uninstall: no steamshare units were found."
DRY_RUN_HEADER = "Dry run: nothing is removed and systemctl is not run."
WATCH_SECTION_TITLE = "Watch service"
MOUNTS_SECTION_TITLE = "Mounts"
KEPT_SECTION_TITLE = "Kept"
DONE_HEADLINE = "Done. What was kept, and what you can do with it:"
DRY_RUN_HEADLINE = "Nothing was changed. After a real uninstall:"

# The status words of the report, as (after a real run, in a dry run).
DISABLED_WORDS = ("disabled", "would disable")
STOPPED_WORDS = ("stopped", "would stop")
REMOVED_WORDS = ("removed", "would remove")
RELOADED_WORDS = ("reloaded", "would reload")
KEPT_WORD = "kept"
ACTION_WORDS = (DISABLED_WORDS, STOPPED_WORDS, REMOVED_WORDS, RELOADED_WORDS)
# The status-word column is as wide as the longest word it can show.
DONE_WORD_WIDTH = max(
    len(word) for word in [*(done for done, _ in ACTION_WORDS), KEPT_WORD, SKIPPED_WORD]
)
PLANNED_WORD_WIDTH = max(
    len(word) for word in [*(planned for _, planned in ACTION_WORDS), KEPT_WORD]
)

# What every uninstall keeps, whatever it found; see the module docstring.
KEPT_PROTON_CHOICES = "the Proton choices watch made in Steam"
# The same wording as the summary, so both name the kept item identically.
KEPT_REPOSITORY = REPOSITORY_ITEM

# The notes at the end, about what was kept; {arrow} is the report's arrow.
DATA_FOLDERS_NOTE = (
    "The Proton prefixes and shader caches stay in {folders}; some games keep their saves "
    "there. To delete them as well:  {command}"
)
PROTON_CHOICES_NOTE = (
    "The Proton choices watch made stay in Steam: undoing them would make Steam re-download "
    "every native game in the shared library as its Linux build. To change one game, use its "
    "Properties {arrow} Compatibility."
)
DEBUGGER_FLAG_KEPT_NOTE = (
    "Steam's debugger stays on, since other tools such as Decky Loader may use it. To turn "
    "it off, run uninstall again with {flag}, or delete {path}, then restart Steam."
)
DEBUGGER_FLAG_REMOVED_NOTE = (
    "Steam's debugger turns off the next time Steam starts: restart Steam once "
    "(Steam {arrow} Exit, then start it again)."
)
REPOSITORY_NOTE = (
    "This repository and the files on the NTFS partition were not touched; delete the "
    "repository folder whenever you like."
)


class UninstallError(CommandError):
    """An expected failure of `uninstall`, with a message that says what to do next."""


@dataclass(frozen=True)
class System:
    """Every effect `uninstall` has on the machine, replaceable in tests."""

    is_root: bool
    invoking_user: InvokingUser
    unit_directory: Path
    # For the state file's place, which follows XDG_CONFIG_HOME.
    environment: Mapping[str, str]
    # Runs every systemctl command, for the system and the user's manager.
    run_command: CommandRunner
    console: Console
    symbols: Symbols


def add_parser(subcommands: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    parser = subcommands.add_parser(
        "uninstall",
        help="remove the mounts and the watch service that install set up (run with sudo)",
        description="Finds what install set up, by the header of its unit files, shows what "
        "it removes and what it keeps, and asks before removing anything. It keeps the Proton "
        "prefixes, the Proton choices made in Steam and, unless told otherwise, Steam's "
        "debugger flag. Running it again when nothing is left changes nothing.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print what would be removed and kept, without changing anything (no sudo needed)",
    )
    parser.add_argument(
        "--yes", action="store_true", help="do not ask for confirmation before removing"
    )
    parser.add_argument(
        REMOVE_DEBUGGER_FLAG,
        action="store_true",
        help="also delete Steam's debugger flag file, which turns the debugger off at "
        "Steam's next start (kept by default: other tools, such as Decky Loader, may use it)",
    )
    parser.add_argument(
        "--steam-root",
        type=Path,
        help="the Steam install folder, where the debugger flag is (default: ~/.steam/steam "
        "of the user who ran sudo)",
    )
    parser.set_defaults(run=run)


def run(arguments: argparse.Namespace) -> int:
    system = System(
        is_root=os.geteuid() == ROOT_USER_ID,
        invoking_user=resolve_invoking_user(),
        unit_directory=SYSTEM_UNIT_DIRECTORY,
        environment=os.environ,
        run_command=run_command,
        console=terminal_console(),
        symbols=choose_symbols(sys.stdout.encoding, os.environ),
    )
    return uninstall(arguments, system)


def uninstall(arguments: argparse.Namespace, system: System) -> int:
    try:
        _check_can_run(arguments, system)
        facts = _discover(arguments, system)
        if nothing_installed(facts):
            print(NOTHING_TO_UNINSTALL)
            return EXIT_OK
        plan = build_uninstall_plan(facts)
        sections = _sections(plan, system)
        if arguments.dry_run:
            _show_plan(sections, plan, system)
            notes = _closing_notes(facts, plan, system, manager_unreachable=False)
            _print_closing(notes, dry_run=True)
            return EXIT_OK
        # A real run lists every file once, as it removes it; before the
        # question, a few lines of counts are easier to read.
        for line in summary_lines(facts, plan, system.invoking_user.home):
            system.console.show(line)
        _confirm(system.console, assume_yes=arguments.yes)
        progress = _apply_sections(sections, system)
        notes = _closing_notes(
            facts, plan, system, manager_unreachable=progress.user_manager_error is not None
        )
        _print_closing(notes, dry_run=False)
    except CommandError as error:
        print(f"Error: {error}", file=sys.stderr)
        return EXIT_FAILURE
    return EXIT_OK


# --- Before anything else ----------------------------------------------------


def _check_can_run(arguments: argparse.Namespace, system: System) -> None:
    if not arguments.dry_run and not system.is_root:
        raise UninstallError(_needs_root_message(arguments))
    if system.invoking_user.user_id == ROOT_USER_ID:
        raise UninstallError(
            "uninstall removes the watch service and state of the desktop user who ran sudo, "
            "and root is not that user. Run it with sudo from the desktop user account that "
            "runs Steam."
        )


def _needs_root_message(arguments: argparse.Namespace) -> str:
    # Repeats only the flags the user gave.
    given = [("--yes", arguments.yes), (REMOVE_DEBUGGER_FLAG, arguments.remove_debugger_flag)]
    parts = [flag for flag, is_given in given if is_given]
    if arguments.steam_root is not None:
        parts += ["--steam-root", str(arguments.steam_root)]
    command = shlex.join([*SUDO_UNINSTALL_COMMAND.split(), *parts])
    return (
        f"uninstall removes files from {SYSTEM_UNIT_DIRECTORY}, so it needs root. "
        f"From the repository root, run:\n  {command}\n"
        "To see what it would remove first, add --dry-run (no root needed)."
    )


def _discover(arguments: argparse.Namespace, system: System) -> UninstallFacts:
    home = Path(system.invoking_user.home)
    steam_root = arguments.steam_root or default_steam_root(home)
    flag = remote_debugging_flag_path(steam_root)
    # Under sudo the environment is reset, so XDG_CONFIG_HOME is unset and the
    # state file is looked for in ~/.config, where watch puts it by default.
    state_file = state_file_path(system.environment, home)
    try:
        return UninstallFacts(
            system_units=tuple(find_generated_units(system.unit_directory)),
            watch_unit=find_generated_unit(home / USER_UNIT_DIRECTORY_IN_HOME / WATCH_SERVICE),
            state_file=state_file if state_file.is_file() else None,
            debugger_flag=flag if flag.is_file() else None,
            remove_debugger_flag=arguments.remove_debugger_flag,
        )
    except OSError as error:
        raise UninstallError(
            f"Could not look in {error.filename}: {error.strerror}. Fix the cause, "
            f"then run `{SUDO_UNINSTALL_COMMAND}` again."
        ) from error


def _confirm(console: Console, *, assume_yes: bool) -> None:
    if assume_yes:
        return
    if not console.is_interactive:
        raise UninstallError(
            "uninstall asks before removing anything, but input is not a terminal. "
            "Add --yes to remove without asking, or --dry-run to only see the plan."
        )
    # No is the default, unlike install's: uninstall deletes files, so only an
    # explicit yes goes ahead.
    if not ask_yes_no(console, "Proceed?", default=False):
        raise UninstallError("Cancelled; nothing was changed.")


# --- Sections ----------------------------------------------------------------


@dataclass(frozen=True)
class Section:
    title: str
    # The folder most of the section's files are in, named once in its header.
    folder: Path
    actions: tuple[UninstallAction, ...]


def _sections(plan: UninstallPlan, system: System) -> list[Section]:
    home = Path(system.invoking_user.home)
    sections = [
        Section(WATCH_SECTION_TITLE, home / USER_UNIT_DIRECTORY_IN_HOME, plan.watch_actions),
        Section(MOUNTS_SECTION_TITLE, system.unit_directory, plan.mount_actions),
    ]
    return [section for section in sections if section.actions]


def _show_plan(sections: Sequence[Section], plan: UninstallPlan, system: System) -> None:
    """Print every action a dry run would take, and what it keeps."""
    report = _report(system, dry_run=True)
    print(DRY_RUN_HEADER)
    print()
    for section in sections:
        print(report.section(section.title, section.folder))
        for action in section.actions:
            kind = ACTION_KINDS[type(action)]
            subject = kind.planned_subject(action, section, report, system)
            print(report.planned(kind.planned_word, subject))
        print()
    print(section_header(KEPT_SECTION_TITLE))
    for subject in _kept_subjects(plan, report):
        print(report.done(KEPT_WORD, subject))
    print()


def _kept_subjects(plan: UninstallPlan, report: RunReport) -> list[str]:
    flag = plan.kept_debugger_flag
    return [
        *(report.path(folder) for folder in plan.kept_data_folders),
        KEPT_PROTON_CHOICES,
        *([] if flag is None else [f"{FLAG_FILE_NAME_IN_REPORT} ({report.path(flag)})"]),
        KEPT_REPOSITORY,
    ]


def _report(system: System, *, dry_run: bool) -> RunReport:
    return RunReport(
        symbols=system.symbols,
        home=system.invoking_user.home,
        dry_run=dry_run,
        done_word_width=DONE_WORD_WIDTH,
        planned_word_width=PLANNED_WORD_WIDTH,
    )


# --- Applying ----------------------------------------------------------------


@dataclass
class Progress:
    """What a real run carries from one action to the next."""

    system: System
    report: RunReport
    section: Section
    # Why the user's service manager could not be reached, once it could not:
    # its later actions are skipped instead of failing the same way again.
    user_manager_error: Exception | None = None


def _apply_sections(sections: Sequence[Section], system: System) -> Progress:
    progress = Progress(system=system, report=_report(system, dry_run=False), section=sections[0])
    for section in sections:
        progress.section = section
        print(progress.report.section(section.title, section.folder))
        for action in section.actions:
            line = ACTION_KINDS[type(action)].apply(action, progress)
            if line is not None:
                print(line)
        print()
    return progress


def _disable_unit(action: DisableUnit, progress: Progress) -> str:
    command = disable_now_command(action.unit_name, systemctl=_systemctl(action, progress))
    return _run_systemctl(action, command, DISABLED_WORDS[0], action.unit_name, progress)


def _stop_unit(action: StopUnit, progress: Progress) -> str:
    command = stop_command(action.unit_name, systemctl=_systemctl(action, progress))
    return _run_systemctl(action, command, STOPPED_WORDS[0], action.unit_name, progress)


def _reload_manager(action: ReloadManager, progress: Progress) -> str:
    command = daemon_reload_command(systemctl=_systemctl(action, progress))
    subject = _manager_label(action.manager, progress.system)
    return _run_systemctl(action, command, RELOADED_WORDS[0], subject, progress)


def _remove_file(action: RemoveFile, progress: Progress) -> str:
    try:
        Path(action.path).unlink(missing_ok=True)
    except OSError as error:
        raise _file_error(action.path, error) from error
    subject = progress.report.path(action.path, progress.section.folder)
    return progress.report.done(REMOVED_WORDS[0], subject)


def _remove_folder_if_empty(action: RemoveFolderIfEmpty, progress: Progress) -> str | None:
    path = Path(action.path)
    if not path.is_dir():
        return None
    if any(path.iterdir()):
        return progress.report.skipped(f"{progress.report.path(path)}: not empty, so it stays")
    try:
        # rmdir, never a recursive delete: it fails rather than remove content.
        path.rmdir()
    except OSError as error:
        raise _file_error(action.path, error) from error
    return progress.report.done(REMOVED_WORDS[0], progress.report.path(path))


ManagedAction = DisableUnit | StopUnit | ReloadManager


def _run_systemctl(
    action: ManagedAction,
    command: list[str],
    done_word: str,
    subject: str,
    progress: Progress,
) -> str:
    is_user = action.manager is ServiceManager.USER
    if is_user and progress.user_manager_error is not None:
        return progress.report.skipped(f"{subject}: {_unreachable(progress.system)}")
    try:
        progress.system.run_command(command)
    except (subprocess.CalledProcessError, FileNotFoundError) as error:
        if not is_user:
            raise _system_command_error(command, error) from error
        # The user is probably not logged in. The system part does not need
        # their manager, so the run goes on; the closing says what is left.
        progress.user_manager_error = error
        return progress.report.skipped(f"{subject}: {_unreachable(progress.system)}")
    return progress.report.done(done_word, subject)


def _systemctl(action: ManagedAction, progress: Progress) -> Sequence[str]:
    if action.manager is ServiceManager.USER:
        return user_systemctl(progress.system.invoking_user.name)
    return SYSTEM_SYSTEMCTL


def _unreachable(system: System) -> str:
    return f"{system.invoking_user.name}'s service manager is not reachable"


def _system_command_error(command: Sequence[str], error: Exception) -> UninstallError:
    return UninstallError(
        f"`{shlex.join(command)}` failed ({error}), so uninstall stopped there. A mount "
        "cannot stop while something uses it: close Steam and every program using the "
        f"library, then run `{SUDO_UNINSTALL_COMMAND}` again; it continues with what is left."
    )


def _file_error(path: PurePosixPath, error: OSError) -> UninstallError:
    return UninstallError(
        f"Could not remove {path}: {error.strerror}. Fix the cause, then run "
        f"`{SUDO_UNINSTALL_COMMAND}` again; it continues with what is left."
    )


# --- How each action is shown and applied ------------------------------------


@dataclass(frozen=True)
class ActionKind:
    """How one kind of action reads in the plan, and how a real run applies it."""

    planned_word: str
    # The subject of the action's line in the plan.
    planned_subject: Callable[[Any, Section, RunReport, System], str]
    # Applies the action and returns its report line; None prints nothing.
    apply: Callable[[Any, Progress], str | None]


def _unit_subject(
    action: DisableUnit | StopUnit, section: Section, report: RunReport, system: System
) -> str:
    return action.unit_name


def _reload_subject(
    action: ReloadManager, section: Section, report: RunReport, system: System
) -> str:
    return _manager_label(action.manager, system)


def _file_subject(action: RemoveFile, section: Section, report: RunReport, system: System) -> str:
    return report.path(action.path, section.folder)


def _folder_subject(
    action: RemoveFolderIfEmpty, section: Section, report: RunReport, system: System
) -> str:
    # Whether it is empty is known only once its mount is gone, so the plan
    # says it depends.
    return f"{report.path(action.path)}  (if empty)"


def _manager_label(manager: ServiceManager, system: System) -> str:
    if manager is ServiceManager.USER:
        return f"systemd for {system.invoking_user.name}"
    return "systemd"


ACTION_KINDS: dict[type, ActionKind] = {
    DisableUnit: ActionKind(DISABLED_WORDS[1], _unit_subject, _disable_unit),
    StopUnit: ActionKind(STOPPED_WORDS[1], _unit_subject, _stop_unit),
    ReloadManager: ActionKind(RELOADED_WORDS[1], _reload_subject, _reload_manager),
    RemoveFile: ActionKind(REMOVED_WORDS[1], _file_subject, _remove_file),
    RemoveFolderIfEmpty: ActionKind(REMOVED_WORDS[1], _folder_subject, _remove_folder_if_empty),
}


# --- Closing -----------------------------------------------------------------


def _closing_notes(
    facts: UninstallFacts, plan: UninstallPlan, system: System, *, manager_unreachable: bool
) -> ClosingNotes:
    """Return the notes on what was kept, and what is left to do by hand."""
    report = _report(system, dry_run=False)
    arrow = system.symbols.arrow
    steps = []
    if manager_unreachable and facts.watch_unit is not None:
        steps.append(_unreachable_manager_step(facts.watch_unit, system, report))
    if plan.kept_data_folders:
        steps.append(_data_folders_note(plan.kept_data_folders, report))
    steps.append(PROTON_CHOICES_NOTE.format(arrow=arrow))
    if plan.kept_debugger_flag is not None:
        steps.append(
            DEBUGGER_FLAG_KEPT_NOTE.format(
                flag=REMOVE_DEBUGGER_FLAG, path=report.path(plan.kept_debugger_flag)
            )
        )
    if plan.removed_debugger_flag is not None:
        steps.append(DEBUGGER_FLAG_REMOVED_NOTE.format(arrow=arrow))
    steps.append(REPOSITORY_NOTE)
    return ClosingNotes(next_steps=tuple(steps))


def _data_folders_note(folders: Sequence[PurePosixPath], report: RunReport) -> str:
    # The command names the full paths: `~` would not expand once quoted.
    return DATA_FOLDERS_NOTE.format(
        folders=" and ".join(report.path(folder) for folder in folders),
        command=shlex.join(["rm", "-rf", *(str(folder) for folder in folders)]),
    )


def _unreachable_manager_step(unit: InstalledUnit, system: System, report: RunReport) -> str:
    name = system.invoking_user.name
    links = shlex.join(str(link) for link in enablement_links(unit))
    return (
        f"{name}'s service manager could not be reached ({name} is not logged in, or systemd "
        f"is older than 248), so {unit.name} was not stopped. Its file is removed, so it will "
        f"not start again. As {name}: run `systemctl --user stop {unit.name}` if it is still "
        f"running, and `rm {links}` to remove the link that started it at login."
    )


def _print_closing(notes: ClosingNotes, *, dry_run: bool) -> None:
    lines = format_closing(
        notes, dry_run=dry_run, done_headline=DONE_HEADLINE, dry_run_headline=DRY_RUN_HEADLINE
    )
    for line in lines:
        print(line)
