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
- this repository and the files on the NTFS partition. A one-line install
  (`steamshare.release_setup`) has no repository but a program folder, such
  as /opt/steamshare; the notes name it and the command that deletes it.
  Uninstall runs from that folder, so it never deletes it itself: the user
  does, once the run is over.

`--purge` goes further, for users who want everything gone: it also
deletes the ext4 Proton prefixes and shader caches, and Steam's debugger
flag, and clears in Steam the Proton choices steamshare made for games that
are not installed (`steam_settings_cleanup`). It still keeps the choices of
installed games, since clearing those would re-download them as Linux
builds. Its order and why are in `core.uninstall_plan`.

It also removes the `steamshare` command install linked onto PATH, in
/usr/local/bin or the user's ~/.local/bin, but only a symlink to a steamshare
launcher (`adapters.command_link`): a file of the same name is the user's.

The user's service manager runs only while the user is logged in. When it
cannot be reached, the watch service's file is still removed, the system
part still runs, and the report says what is left to do. Every effect on
the machine goes through `System`, so the wiring is tested without root.
"""

import argparse
import dataclasses
import os
import shlex
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from steamshare.adapters.command_link import (
    SYSTEM_COMMAND_DIRECTORY,
    USER_COMMAND_DIRECTORY_IN_HOME,
    find_command_links,
)
from steamshare.adapters.console import Console, terminal_console
from steamshare.adapters.folder_contents import folder_size, remove_tree, subfolder_names
from steamshare.adapters.generated_units import find_generated_unit, find_generated_units
from steamshare.adapters.invoking_user import InvokingUser, resolve_invoking_user
from steamshare.adapters.launcher_location import REPOSITORY_ROOT
from steamshare.adapters.release_install import is_release_install
from steamshare.adapters.state_file import state_file_path
from steamshare.adapters.steam_cdp import remote_debugging_flag_path
from steamshare.adapters.steam_client_api import connect_to_steam_client
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
from steamshare.cli.questions import ask_typed_word, ask_yes_no
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
from steamshare.cli.steam_settings_cleanup import (
    KEEP_STEAM_SETTINGS_FLAG,
    PURGE_FLAG,
    ConnectToSteam,
    check_steam_mappings,
    clear_steam_mappings,
)
from steamshare.cli.uninstall_summary import (
    cleared_choices,
    cleared_choices_item,
    kept_choices,
    kept_program_item,
    summary_lines,
)
from steamshare.cli.watch_service_setup import (
    FLAG_FILE_NAME_IN_REPORT,
    USER_UNIT_DIRECTORY_IN_HOME,
)
from steamshare.core.library_layout import PROTON_PREFIXES_DIRECTORY, count_proton_prefixes
from steamshare.core.live_proton_mapping import OwnedGame
from steamshare.core.mapping_cleanup import MappingCleanup
from steamshare.core.ntfs_partitions import format_size
from steamshare.core.systemd_units import WATCH_SERVICE
from steamshare.core.uninstall_plan import (
    ClearProtonMappings,
    DisableUnit,
    FolderSize,
    InstalledUnit,
    ReloadManager,
    RemoveFile,
    RemoveFolderIfEmpty,
    RemoveTree,
    ServiceManager,
    StopUnit,
    UncheckedMappings,
    UninstallAction,
    UninstallFacts,
    UninstallPlan,
    bind_mount_sources,
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
PURGE_SECTION_TITLE = "Purge"
COMMAND_SECTION_TITLE = "Command"
KEPT_SECTION_TITLE = "Kept"
DONE_HEADLINE = "Done. What was kept, and what you can do with it:"
DRY_RUN_HEADLINE = "Nothing was changed. After a real uninstall:"

# The status words of the report, as (after a real run, in a dry run).
DISABLED_WORDS = ("disabled", "would disable")
STOPPED_WORDS = ("stopped", "would stop")
REMOVED_WORDS = ("removed", "would remove")
RELOADED_WORDS = ("reloaded", "would reload")
CLEARED_WORDS = ("cleared", "would clear")
KEPT_WORD = "kept"
ACTION_WORDS = (DISABLED_WORDS, STOPPED_WORDS, REMOVED_WORDS, RELOADED_WORDS, CLEARED_WORDS)
# The status-word column is as wide as the longest word it can show.
DONE_WORD_WIDTH = max(
    len(word) for word in [*(done for done, _ in ACTION_WORDS), KEPT_WORD, SKIPPED_WORD]
)
PLANNED_WORD_WIDTH = max(
    len(word) for word in [*(planned for _, planned in ACTION_WORDS), KEPT_WORD]
)

# A purge deletes saves that cannot be recovered, so it asks for this word
# typed out, not a y that a reflexive Enter or habit can give.
PURGE_CONFIRMATION_WORD = "purge"
PURGE_QUESTION = f'Type "{PURGE_CONFIRMATION_WORD}" to delete these permanently:'

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
RELEASE_FOLDER_NOTE = (
    "steamshare's program files stay in {folder}, and the files on the NTFS partition were "
    "not touched. To delete them:  {command}"
)
INSTALLED_CHOICES_NOTE = (
    "The Proton choices of installed games stay in Steam: clearing them would make Steam "
    "re-download those games as their Linux build. To change one game, use its "
    "Properties {arrow} Compatibility."
)
UNCONFIRMED_CLEARS_NOTE = (
    "Steam did not confirm clearing the Proton choice of {games}. Clear it by hand in the "
    "game's Properties {arrow} Compatibility."
)
UNCHECKED_CHOICES_NOTE = (
    "Steam is not running, so the {count} games steamshare mapped were not checked. A real "
    f"{PURGE_FLAG} needs Steam open, or {KEEP_STEAM_SETTINGS_FLAG} to leave those choices in "
    "Steam."
)
PURGE_REMAINS_REPOSITORY = (
    "What remains is this repository and the games on the shared NTFS partition. Delete the "
    "repository folder whenever you like."
)
PURGE_REMAINS_RELEASE_FOLDER = (
    "What remains is steamshare's program files in {folder} and the games on the shared NTFS "
    "partition. {games_note} To delete the program files:  {command}"
)
PURGE_GAMES_NOTE = (
    "To delete the games too, format the partition from Windows (Disk Management); that "
    "deletes every game on it."
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
    # Opens the running Steam client, for a purge's Proton choices.
    connect_to_steam: ConnectToSteam
    # Where install links `steamshare` when it can (see `command_link_setup`).
    command_directory: Path
    # The folder of the running steamshare: a clone, or a one-line install's.
    program_folder: Path


def add_parser(subcommands: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    parser = subcommands.add_parser(
        "uninstall",
        help="remove the mounts and the watch service that install set up (run with sudo)",
        description="Finds what install set up, by the header of its unit files, shows what "
        "it removes and what it keeps, and asks before removing anything. It keeps the Proton "
        "prefixes, the Proton choices made in Steam and, unless told otherwise, Steam's "
        "debugger flag; --purge deletes those too. Running it again when nothing is left "
        "changes nothing.",
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
        PURGE_FLAG,
        action="store_true",
        help="also delete the Proton prefixes (with any saves in them) and shader caches, and "
        "Steam's debugger flag, and clear the Proton choices steamshare made for games that "
        "are not installed; Steam must be running (asks you to type 'purge')",
    )
    parser.add_argument(
        KEEP_STEAM_SETTINGS_FLAG,
        action="store_true",
        help=f"with {PURGE_FLAG}, leave the Proton choices in Steam, so Steam need not be running",
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
        connect_to_steam=connect_to_steam_client,
        command_directory=SYSTEM_COMMAND_DIRECTORY,
        program_folder=REPOSITORY_ROOT,
    )
    return uninstall(arguments, system)


def uninstall(arguments: argparse.Namespace, system: System) -> int:
    try:
        _check_can_run(arguments, system)
        facts = _discover(arguments, system)
        if nothing_installed(facts):
            print(NOTHING_TO_UNINSTALL)
            return EXIT_OK
        if arguments.purge:
            # Before anything changes: a purge that cannot reach Steam refuses here.
            facts = _discover_purge(facts, arguments, system)
        plan = build_uninstall_plan(facts)
        sections = _sections(plan, system)
        if arguments.dry_run:
            _show_plan(sections, facts, plan, system)
            notes = _closing_notes(
                facts, plan, system, manager_unreachable=False, unconfirmed_clears=()
            )
            _print_closing(notes, dry_run=True)
            return EXIT_OK
        # A real run lists every file once, as it removes it; before the
        # question, a few lines of counts are easier to read.
        for line in summary_lines(facts, plan, system.invoking_user.home):
            system.console.show(line)
        _confirm(system.console, assume_yes=arguments.yes, purge=arguments.purge)
        progress = _apply_sections(sections, system, _steam_root(arguments, system))
        notes = _closing_notes(
            facts,
            plan,
            system,
            manager_unreachable=progress.user_manager_error is not None,
            unconfirmed_clears=progress.unconfirmed_clears,
        )
        _print_closing(notes, dry_run=False)
    except CommandError as error:
        print(f"Error: {error}", file=sys.stderr)
        return EXIT_FAILURE
    return EXIT_OK


# --- Before anything else ----------------------------------------------------


def _check_can_run(arguments: argparse.Namespace, system: System) -> None:
    if arguments.keep_steam_settings and not arguments.purge:
        raise UninstallError(
            f"{KEEP_STEAM_SETTINGS_FLAG} only applies with {PURGE_FLAG}: without it, uninstall "
            f"leaves Steam's settings alone anyway. Add {PURGE_FLAG}, or leave the flag out."
        )
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
    given = [
        ("--yes", arguments.yes),
        (REMOVE_DEBUGGER_FLAG, arguments.remove_debugger_flag),
        (PURGE_FLAG, arguments.purge),
        (KEEP_STEAM_SETTINGS_FLAG, arguments.keep_steam_settings),
    ]
    parts = [flag for flag, is_given in given if is_given]
    if arguments.steam_root is not None:
        parts += ["--steam-root", str(arguments.steam_root)]
    command = shlex.join([*SUDO_UNINSTALL_COMMAND.split(), *parts])
    return (
        f"uninstall removes files from {SYSTEM_UNIT_DIRECTORY}, so it needs root. "
        f"Run:\n  {command}\n"
        "To see what it would remove first, add --dry-run (no root needed)."
    )


def _discover(arguments: argparse.Namespace, system: System) -> UninstallFacts:
    home = Path(system.invoking_user.home)
    flag = remote_debugging_flag_path(_steam_root(arguments, system))
    # Under sudo the environment is reset, so XDG_CONFIG_HOME is unset and the
    # state file is looked for in ~/.config, where watch puts it by default.
    state_file = state_file_path(system.environment, home)
    try:
        return UninstallFacts(
            system_units=tuple(find_generated_units(system.unit_directory)),
            watch_unit=find_generated_unit(home / USER_UNIT_DIRECTORY_IN_HOME / WATCH_SERVICE),
            state_file=state_file if state_file.is_file() else None,
            debugger_flag=flag if flag.is_file() else None,
            # A purge removes the flag along with everything else.
            remove_debugger_flag=arguments.remove_debugger_flag or arguments.purge,
            # Either folder: the system one may have become read-only, or
            # writable, since install chose.
            command_links=tuple(
                find_command_links(
                    [system.command_directory, home / USER_COMMAND_DIRECTORY_IN_HOME]
                )
            ),
            release_folder=(
                PurePosixPath(system.program_folder)
                if is_release_install(system.program_folder)
                else None
            ),
        )
    except OSError as error:
        raise _look_error(error) from error


def _discover_purge(
    facts: UninstallFacts, arguments: argparse.Namespace, system: System
) -> UninstallFacts:
    """Add what only a purge needs: the data's sizes, and what Steam says."""
    sources = [Path(source) for source in bind_mount_sources(facts.system_units)]
    try:
        on_disk = tuple(
            FolderSize(source, folder_size(source))
            for source in sources
            if source.is_dir() and not source.is_symlink()
        )
        prefix_folders = [
            name
            for source in sources
            if source.name == PROTON_PREFIXES_DIRECTORY
            for name in subfolder_names(source)
        ]
    except OSError as error:
        raise _look_error(error) from error
    mappings = None
    if not arguments.keep_steam_settings:
        mappings = check_steam_mappings(
            None if facts.state_file is None else Path(facts.state_file),
            _steam_root(arguments, system),
            system.connect_to_steam,
            dry_run=arguments.dry_run,
        )
    return dataclasses.replace(
        facts,
        purge=True,
        data_folders_on_disk=on_disk,
        proton_prefix_count=count_proton_prefixes(prefix_folders),
        mappings=mappings,
    )


def _steam_root(arguments: argparse.Namespace, system: System) -> Path:
    # The default is in the desktop user's home, not in root's.
    return arguments.steam_root or default_steam_root(Path(system.invoking_user.home))


def _look_error(error: OSError) -> UninstallError:
    return UninstallError(
        f"Could not look in {error.filename}: {error.strerror}. Fix the cause, "
        f"then run `{SUDO_UNINSTALL_COMMAND}` again."
    )


def _confirm(console: Console, *, assume_yes: bool, purge: bool) -> None:
    if assume_yes:
        return
    if not console.is_interactive:
        raise UninstallError(
            "uninstall asks before removing anything, but input is not a terminal. "
            "Add --yes to remove without asking, or --dry-run to only see the plan."
        )
    if purge:
        # Stronger than y/N: the saves inside the Proton prefixes cannot be
        # recovered once deleted.
        confirmed = ask_typed_word(console, PURGE_QUESTION, PURGE_CONFIRMATION_WORD)
    else:
        # No is the default, unlike install's: uninstall deletes files, so only
        # an explicit yes goes ahead.
        confirmed = ask_yes_no(console, "Proceed?", default=False)
    if not confirmed:
        raise UninstallError("Cancelled; nothing was changed.")


# --- Sections ----------------------------------------------------------------


@dataclass(frozen=True)
class Section:
    title: str
    # The folder most of the section's files are in, named once in its header;
    # None when they are in several.
    folder: Path | None
    actions: tuple[UninstallAction, ...]


def _sections(plan: UninstallPlan, system: System) -> list[Section]:
    home = Path(system.invoking_user.home)
    sections = [
        Section(WATCH_SECTION_TITLE, home / USER_UNIT_DIRECTORY_IN_HOME, plan.watch_actions),
        Section(MOUNTS_SECTION_TITLE, system.unit_directory, plan.mount_actions),
        Section(PURGE_SECTION_TITLE, None, plan.purge_actions),
        Section(COMMAND_SECTION_TITLE, None, plan.command_actions),
    ]
    return [section for section in sections if section.actions]


def _show_plan(
    sections: Sequence[Section], facts: UninstallFacts, plan: UninstallPlan, system: System
) -> None:
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
    for subject in _kept_subjects(facts, plan, report):
        print(report.done(KEPT_WORD, subject))
    print()


def _kept_subjects(facts: UninstallFacts, plan: UninstallPlan, report: RunReport) -> list[str]:
    flag = plan.kept_debugger_flag
    return [
        *(report.path(folder) for folder in plan.kept_data_folders),
        *kept_choices(facts),
        *([] if flag is None else [f"{FLAG_FILE_NAME_IN_REPORT} ({report.path(flag)})"]),
        kept_program_item(facts, report.home),
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
    steam_root: Path
    # Why the user's service manager could not be reached, once it could not:
    # its later actions are skipped instead of failing the same way again.
    user_manager_error: Exception | None = None
    # The games whose Proton choice Steam did not confirm clearing.
    unconfirmed_clears: Sequence[OwnedGame] = ()


def _apply_sections(sections: Sequence[Section], system: System, steam_root: Path) -> Progress:
    progress = Progress(
        system=system,
        report=_report(system, dry_run=False),
        section=sections[0],
        steam_root=steam_root,
    )
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


def _remove_tree(action: RemoveTree, progress: Progress) -> str:
    try:
        remove_tree(Path(action.path))
    except FileNotFoundError:
        pass
    except OSError as error:
        raise _file_error(action.path, error) from error
    return progress.report.done(REMOVED_WORDS[0], progress.report.path(action.path))


def _clear_proton_mappings(action: ClearProtonMappings, progress: Progress) -> str:
    cleanup = action.mappings
    # A real run checks Steam before it starts and refuses without it, so
    # only a dry run's plan can hold unchecked mappings.
    assert isinstance(cleanup, MappingCleanup), "a real purge always checks Steam first"
    unconfirmed = clear_steam_mappings(
        cleanup, progress.steam_root, progress.system.connect_to_steam
    )
    progress.unconfirmed_clears = unconfirmed
    cleared_count = len(cleanup.to_clear) - len(unconfirmed)
    lines = [progress.report.done(CLEARED_WORDS[0], cleared_choices_item(cleared_count))]
    if unconfirmed:
        lines.append(
            progress.report.skipped(f"{len(unconfirmed)} games: Steam did not confirm the clear")
        )
    return "\n".join(lines)


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


def _tree_subject(action: RemoveTree, section: Section, report: RunReport, system: System) -> str:
    return f"{report.path(action.path)}  ({format_size(action.size_bytes)})"


def _clear_subject(
    action: ClearProtonMappings, section: Section, report: RunReport, system: System
) -> str:
    # The plan holds this action only when there is something to clear, so
    # there is exactly one item.
    [subject] = cleared_choices(action.mappings)
    return subject


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
    RemoveTree: ActionKind(REMOVED_WORDS[1], _tree_subject, _remove_tree),
    ClearProtonMappings: ActionKind(CLEARED_WORDS[1], _clear_subject, _clear_proton_mappings),
}


# --- Closing -----------------------------------------------------------------


def _closing_notes(
    facts: UninstallFacts,
    plan: UninstallPlan,
    system: System,
    *,
    manager_unreachable: bool,
    unconfirmed_clears: Sequence[OwnedGame],
) -> ClosingNotes:
    """Return the notes on what was kept, and what is left to do by hand."""
    report = _report(system, dry_run=False)
    arrow = system.symbols.arrow
    steps = []
    if manager_unreachable and facts.watch_unit is not None:
        steps.append(_unreachable_manager_step(facts.watch_unit, system, report))
    if unconfirmed_clears:
        games = ", ".join(f"{game.name} ({game.appid})" for game in unconfirmed_clears)
        steps.append(UNCONFIRMED_CLEARS_NOTE.format(games=games, arrow=arrow))
    if isinstance(facts.mappings, UncheckedMappings):
        steps.append(UNCHECKED_CHOICES_NOTE.format(count=facts.mappings.managed_count))
    if plan.kept_data_folders:
        steps.append(_data_folders_note(plan.kept_data_folders, report))
    steps += _choices_notes(facts, arrow)
    if plan.kept_debugger_flag is not None:
        steps.append(
            DEBUGGER_FLAG_KEPT_NOTE.format(
                flag=REMOVE_DEBUGGER_FLAG, path=report.path(plan.kept_debugger_flag)
            )
        )
    if plan.removed_debugger_flag is not None:
        steps.append(DEBUGGER_FLAG_REMOVED_NOTE.format(arrow=arrow))
    steps.append(_program_note(facts, system))
    return ClosingNotes(next_steps=tuple(steps))


def _program_note(facts: UninstallFacts, system: System) -> str:
    """Return the note on steamshare's own files, and how to delete them."""
    folder = facts.release_folder
    if folder is None:
        if facts.purge:
            return f"{PURGE_REMAINS_REPOSITORY} {PURGE_GAMES_NOTE}"
        return REPOSITORY_NOTE
    # A folder in the user's home is theirs (release_setup gives it to them).
    in_home = folder.is_relative_to(system.invoking_user.home)
    command = shlex.join([*([] if in_home else ["sudo"]), "rm", "-rf", str(folder)])
    if facts.purge:
        # The command comes last, so it can be copied without the sentence after it.
        return PURGE_REMAINS_RELEASE_FOLDER.format(
            folder=folder, games_note=PURGE_GAMES_NOTE, command=command
        )
    return RELEASE_FOLDER_NOTE.format(folder=folder, command=command)


def _choices_notes(facts: UninstallFacts, arrow: str) -> list[str]:
    """Return the note on the Proton choices left in Steam, if any are."""
    if not facts.purge or facts.mappings is None:
        return [PROTON_CHOICES_NOTE.format(arrow=arrow)]
    if kept_choices(facts):
        return [INSTALLED_CHOICES_NOTE.format(arrow=arrow)]
    return []


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
