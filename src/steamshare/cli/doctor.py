"""The `doctor` command: check that the shared library is safe to use.

This module only wires adapters to the checks in `core.checks`, prints the
results, and picks the exit code. The decisions live in `core`.
"""

import argparse
from collections.abc import Iterable
from pathlib import Path, PurePosixPath

from steamshare.adapters.library_walker import walk_library
from steamshare.adapters.mountinfo import read_mount_table
from steamshare.core.checks import (
    check_library_folder,
    check_library_mount,
    check_linux_only_directories,
    check_unexpected_symlinks,
    check_windows_names,
)
from steamshare.core.doctor import CheckResult, Status
from steamshare.core.library_layout import STEAMAPPS_DIRECTORY
from steamshare.core.mounts import find_mount_containing

STATUS_MARKERS = {
    Status.OK: "[ OK ]",
    Status.WARNING: "[WARN]",
    Status.FAILURE: "[FAIL]",
}

# Details and hints line up under the title, after the marker.
INDENT = " " * (len(STATUS_MARKERS[Status.OK]) + 1)
HINT_LABEL = "Next step: "

EXIT_OK = 0
EXIT_FAILURE = 1


def add_parser(subcommands: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    parser = subcommands.add_parser(
        "doctor", help="check that the shared library is mounted and safe for Windows"
    )
    parser.add_argument(
        "--library",
        type=Path,
        required=True,
        help="the Steam library folder (the one that contains steamapps)",
    )
    parser.set_defaults(run=run)


def run(arguments: argparse.Namespace) -> int:
    results = run_checks(arguments.library.resolve())
    for result in results:
        print(format_result(result))
    return exit_code_for(results)


def run_checks(library: Path) -> list[CheckResult]:
    mounts = read_mount_table()
    library_path = PurePosixPath(library)
    results = [
        check_library_mount(find_mount_containing(mounts, library_path)),
        check_linux_only_directories(mounts, library_path / STEAMAPPS_DIRECTORY),
        check_library_folder(library_path, library.is_dir()),
    ]
    # There is nothing to walk when the folder is missing; that failure is
    # already reported by check_library_folder.
    if library.is_dir():
        # A list, because two checks read the same walk.
        entries = list(walk_library(library))
        results.append(check_windows_names(entries))
        results.append(check_unexpected_symlinks(entries))
    return results


def exit_code_for(results: Iterable[CheckResult]) -> int:
    """Return 1 if any check failed. Warnings alone do not fail the command."""
    if any(result.status is Status.FAILURE for result in results):
        return EXIT_FAILURE
    return EXIT_OK


def format_result(result: CheckResult) -> str:
    lines = [f"{STATUS_MARKERS[result.status]} {result.title}"]
    lines.extend(f"{INDENT}{detail}" for detail in result.details)
    if result.hint is not None:
        first_line, *other_lines = result.hint.splitlines()
        lines.append(f"{INDENT}{HINT_LABEL}{first_line}")
        hint_indent = INDENT + " " * len(HINT_LABEL)
        lines.extend(f"{hint_indent}{line}" for line in other_lines)
    return "\n".join(lines)
