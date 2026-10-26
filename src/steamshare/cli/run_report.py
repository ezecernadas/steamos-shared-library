"""How commands lay out what they report: sections, aligned status lines, short paths.

Why this exists: install touches a dozen files in three folders, and one
line per file with its full path and full backup path read as a wall of
text. So the report:

- groups lines into sections with a short header, separated by a blank
  line, and names the folder a section's files share once, in the header;
- starts each line with a status marker and a padded status word, so the
  subjects line up;
- shows the desktop user's home as `~` (the user who ran sudo, not root);
- sums up the backups once at the end, and ends with numbered next steps.

Everything here is pure: it builds strings and prints nothing, so the layout
is tested line by line. The commands (`install` with `watch_service_setup`,
and `uninstall`) do the printing.
"""

import codecs
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath

from steamshare.adapters.safe_writes import BACKUP_NAME_INFIX
from steamshare.adapters.unit_installer import DirectoryOutcome, FileAction, UnitFileOutcome

# --- Symbols -----------------------------------------------------------------


@dataclass(frozen=True)
class Symbols:
    """The non-letter characters the report uses, in one place so they can fall back to ASCII."""

    # Something done, or already as it should be.
    done: str
    # Something a dry run would do.
    planned: str
    # Something failed or skipped: the reader should look at it.
    attention: str
    # Between steps of a menu path, such as Settings > Storage.
    arrow: str


UNICODE_SYMBOLS = Symbols(done="✓", planned="·", attention="!", arrow="→")
ASCII_SYMBOLS = Symbols(done="+", planned="-", attention="!", arrow=">")

# https://no-color.org: a non-empty NO_COLOR asks command-line tools for plain
# output. TERM=dumb is how editors' shells and some CI logs say the same.
NO_COLOR_VARIABLE = "NO_COLOR"
TERM_VARIABLE = "TERM"
DUMB_TERMINAL = "dumb"


def choose_symbols(encoding: str | None, environment: Mapping[str, str]) -> Symbols:
    """Return the Unicode symbols, or ASCII ones where Unicode may not show or is not wanted.

    `encoding` is the output's, such as `sys.stdout.encoding`. On an output
    that is not UTF-8 (a C locale, an old log file), printing ✓ would fail
    or come out garbled.
    """
    wants_plain_output = bool(environment.get(NO_COLOR_VARIABLE)) or (
        environment.get(TERM_VARIABLE) == DUMB_TERMINAL
    )
    if wants_plain_output or not _is_utf8(encoding):
        return ASCII_SYMBOLS
    return UNICODE_SYMBOLS


def _is_utf8(encoding: str | None) -> bool:
    if encoding is None:
        return False
    try:
        # Normalizes the spellings: "UTF-8", "utf8", "utf_8".
        return codecs.lookup(encoding).name == "utf-8"
    except LookupError:
        return False


# --- Status words ------------------------------------------------------------

# How each file outcome reads after a real run, and in a dry run.
DONE_FILE_WORDS = {
    FileAction.UNCHANGED: "unchanged",
    FileAction.CREATED: "created",
    FileAction.UPDATED: "updated",
}
PLANNED_FILE_WORDS = {
    FileAction.UNCHANGED: "unchanged",
    FileAction.CREATED: "would create",
    FileAction.UPDATED: "would update",
}
STARTED_WORD = "started"
RUNNING_WORD = "running"
WOULD_RUN_WORD = "would run"
SKIPPED_WORD = "skipped"

# The status-word column is as wide as the longest word the run can print.
DONE_WORD_WIDTH = max(
    map(len, [*DONE_FILE_WORDS.values(), STARTED_WORD, RUNNING_WORD, SKIPPED_WORD])
)
PLANNED_WORD_WIDTH = max(map(len, [*PLANNED_FILE_WORDS.values(), WOULD_RUN_WORD, SKIPPED_WORD]))


# --- Small layout pieces ----------------------------------------------------

# Status lines and fields sit this far in, under their section header.
INDENT = "  "
# Between a column and the next one.
COLUMN_GAP = "  "


def shorten_home(path: PurePosixPath, home: PurePosixPath) -> str:
    """Return the path with `home` shown as `~`, or the whole path when it is not under it."""
    # A home of / would turn every path into ~/...; install refuses root anyway.
    if home == PurePosixPath("/") or not path.is_relative_to(home):
        return str(path)
    if path == home:
        return "~"
    return f"~/{path.relative_to(home)}"


def section_header(title: str, folder: str | None = None) -> str:
    if folder is None:
        return title
    return f"{title}{COLUMN_GAP}({folder})"


def status_line(marker: str, word: str, subject: str, *, width: int) -> str:
    return f"{INDENT}{marker} {word:<{width}}{COLUMN_GAP}{subject}"


def aligned_fields(fields: Sequence[tuple[str, str]]) -> list[str]:
    """Return one `label  value` line per field, with the values lined up."""
    width = max((len(label) for label, _ in fields), default=0)
    return [f"{INDENT}{label:<{width}}{COLUMN_GAP}{value}" for label, value in fields]


def backup_note(backups: Iterable[PurePosixPath]) -> str | None:
    """Return the one line that says where the backups went, or None without backups.

    A backup sits next to its file, named `<name>.bak-<timestamp>`, so the
    suffix is all the reader needs to find every one.
    """
    suffixes = sorted({"*" + BACKUP_NAME_INFIX + _backup_timestamp(backup) for backup in backups})
    if not suffixes:
        return None
    return f"Replaced files were backed up next to themselves ({', '.join(suffixes)})."


PLANNED_BACKUP_NOTE = (
    f"Replaced files would be backed up next to themselves first "
    f"(as <name>{BACKUP_NAME_INFIX}<timestamp>)."
)


def _backup_timestamp(backup: PurePosixPath) -> str:
    return backup.name.rpartition(BACKUP_NAME_INFIX)[2]


# --- Status lines of a run ---------------------------------------------------


@dataclass(frozen=True)
class RunReport:
    """The status lines of one run: its symbols, whose home is `~`, and real or dry."""

    symbols: Symbols
    # The desktop user's home, not root's: install runs under sudo.
    home: PurePosixPath
    dry_run: bool
    # How wide the status-word column is after a real run and in a dry run.
    # The defaults fit install's words; a command with other words passes
    # the length of its longest ones.
    done_word_width: int = DONE_WORD_WIDTH
    planned_word_width: int = PLANNED_WORD_WIDTH

    def section(self, title: str, folder: PurePosixPath | None = None) -> str:
        return section_header(title, None if folder is None else shorten_home(folder, self.home))

    def path(self, path: PurePosixPath, folder: PurePosixPath | None = None) -> str:
        """Return the file's name when it is in the section's folder, else its whole path."""
        if folder is not None and path.parent == folder:
            return path.name
        return shorten_home(path, self.home)

    def outcome(
        self, outcome: UnitFileOutcome | DirectoryOutcome, folder: PurePosixPath | None = None
    ) -> str:
        return self.file_action(outcome.action, self.path(outcome.path, folder))

    def file_action(self, action: FileAction, subject: str) -> str:
        # In a dry run, "unchanged" is already true, so it is marked done.
        if self.dry_run and action is not FileAction.UNCHANGED:
            return self.planned(PLANNED_FILE_WORDS[action], subject)
        words = PLANNED_FILE_WORDS if self.dry_run else DONE_FILE_WORDS
        return self.done(words[action], subject)

    def done(self, word: str, subject: str) -> str:
        return status_line(self.symbols.done, word, subject, width=self._word_width)

    def planned(self, word: str, subject: str) -> str:
        return status_line(self.symbols.planned, word, subject, width=self._word_width)

    def skipped(self, reason: str) -> str:
        return status_line(self.symbols.attention, SKIPPED_WORD, reason, width=self._word_width)

    @property
    def _word_width(self) -> int:
        return self.planned_word_width if self.dry_run else self.done_word_width


# --- Closing -----------------------------------------------------------------


@dataclass(frozen=True)
class ClosingNotes:
    """What a part of the run leaves for the end of the report."""

    # Every unit file written or planned, for the backup line.
    unit_outcomes: tuple[UnitFileOutcome, ...] = ()
    # What the user does next, in order.
    next_steps: tuple[str, ...] = ()
    # Commands worth knowing, shown after the steps as (label, command).
    tips: tuple[tuple[str, str], ...] = ()


def combine_notes(*notes: ClosingNotes) -> ClosingNotes:
    return ClosingNotes(
        unit_outcomes=tuple(outcome for note in notes for outcome in note.unit_outcomes),
        next_steps=tuple(step for note in notes for step in note.next_steps),
        tips=tuple(tip for note in notes for tip in note.tips),
    )


INSTALL_DONE_HEADLINE = "Done. Next steps:"
INSTALL_DRY_RUN_HEADLINE = "Nothing was changed. After a real install, the next steps are:"


def format_closing(
    notes: ClosingNotes,
    *,
    dry_run: bool,
    done_headline: str = INSTALL_DONE_HEADLINE,
    dry_run_headline: str = INSTALL_DRY_RUN_HEADLINE,
) -> list[str]:
    """Return the end of the report: where backups went, then the numbered next steps."""
    note = _planned_backup_note(notes) if dry_run else _done_backup_note(notes)
    lines = [] if note is None else [note, ""]
    lines.append(dry_run_headline if dry_run else done_headline)
    lines += [f"{INDENT}{number}. {step}" for number, step in enumerate(notes.next_steps, 1)]
    lines += [f"{INDENT}{label}:{COLUMN_GAP}{command}" for label, command in notes.tips]
    return lines


def _done_backup_note(notes: ClosingNotes) -> str | None:
    return backup_note(outcome.backup for outcome in notes.unit_outcomes if outcome.backup)


def _planned_backup_note(notes: ClosingNotes) -> str | None:
    replaces_a_file = any(o.action is FileAction.UPDATED for o in notes.unit_outcomes)
    return PLANNED_BACKUP_NOTE if replaces_a_file else None
