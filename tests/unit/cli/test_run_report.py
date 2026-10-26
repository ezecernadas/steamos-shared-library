"""Tests for how `install` lays out its report: symbols, sections, status lines, paths."""

from pathlib import Path, PurePosixPath

import pytest

from steamshare.adapters.unit_installer import DirectoryOutcome, FileAction, UnitFileOutcome
from steamshare.cli.run_report import (
    ASCII_SYMBOLS,
    UNICODE_SYMBOLS,
    ClosingNotes,
    RunReport,
    aligned_fields,
    backup_note,
    choose_symbols,
    combine_notes,
    format_closing,
    section_header,
    shorten_home,
    status_line,
)

HOME = PurePosixPath("/home/player")
UNIT_DIRECTORY = Path("/etc/systemd/system")


def report(*, dry_run: bool = False) -> RunReport:
    return RunReport(symbols=UNICODE_SYMBOLS, home=HOME, dry_run=dry_run)


# --- Symbols -----------------------------------------------------------------


@pytest.mark.parametrize("encoding", ["utf-8", "UTF-8", "utf8"])
def test_a_utf8_output_gets_the_unicode_symbols(encoding: str) -> None:
    assert choose_symbols(encoding, {"TERM": "xterm-256color"}) == UNICODE_SYMBOLS


@pytest.mark.parametrize("encoding", ["ascii", "latin-1", None, "no-such-codec"])
def test_an_output_that_cannot_show_unicode_gets_ascii(encoding: str | None) -> None:
    assert choose_symbols(encoding, {}) == ASCII_SYMBOLS


@pytest.mark.parametrize("environment", [{"NO_COLOR": "1"}, {"TERM": "dumb"}])
def test_a_request_for_plain_output_gets_ascii(environment: dict[str, str]) -> None:
    assert choose_symbols("utf-8", environment) == ASCII_SYMBOLS


def test_an_empty_no_color_does_not_count_as_set() -> None:
    # https://no-color.org: only a non-empty value asks for plain output.
    assert choose_symbols("utf-8", {"NO_COLOR": ""}) == UNICODE_SYMBOLS


def test_ascii_symbols_are_ascii_and_every_marker_is_one_character() -> None:
    for symbols in (ASCII_SYMBOLS, UNICODE_SYMBOLS):
        assert {len(symbols.done), len(symbols.planned), len(symbols.attention)} == {1}
    assert all(
        text.isascii()
        for text in (
            ASCII_SYMBOLS.done,
            ASCII_SYMBOLS.planned,
            ASCII_SYMBOLS.attention,
            ASCII_SYMBOLS.arrow,
        )
    )


# --- Small layout pieces ----------------------------------------------------


def test_shorten_home_replaces_the_home_with_a_tilde() -> None:
    assert shorten_home(HOME / ".config" / "x.service", HOME) == "~/.config/x.service"
    assert shorten_home(HOME, HOME) == "~"


def test_shorten_home_leaves_other_paths_and_lookalike_homes_alone() -> None:
    assert shorten_home(PurePosixPath("/etc/systemd/system"), HOME) == "/etc/systemd/system"
    assert shorten_home(PurePosixPath("/home/player2/x"), HOME) == "/home/player2/x"


def test_section_header_names_a_shared_folder_once() -> None:
    assert section_header("Summary") == "Summary"
    assert section_header("Mounts", "/etc/systemd/system") == "Mounts  (/etc/systemd/system)"


def test_status_lines_pad_the_word_so_subjects_line_up() -> None:
    assert status_line("✓", "updated", "a.mount", width=9) == "  ✓ updated    a.mount"
    assert status_line("✓", "unchanged", "b.mount", width=9) == "  ✓ unchanged  b.mount"


def test_aligned_fields_line_up_the_values() -> None:
    assert aligned_fields([("Partition", "/dev/sda1"), ("Library", "/mnt/x")]) == [
        "  Partition  /dev/sda1",
        "  Library    /mnt/x",
    ]


def test_backup_note_names_the_timestamp_once() -> None:
    backups = [
        UNIT_DIRECTORY / "a.mount.bak-20261026-183131",
        UNIT_DIRECTORY / "b.mount.bak-20261026-183131",
    ]

    assert backup_note(backups) == (
        "Replaced files were backed up next to themselves (*.bak-20261026-183131)."
    )


def test_backup_note_lists_each_distinct_timestamp() -> None:
    backups = [Path("/a.bak-20261026-183131"), Path("/b.bak-20261026-183133")]

    assert "(*.bak-20261026-183131, *.bak-20261026-183133)" in str(backup_note(backups))


def test_no_backups_means_no_backup_note() -> None:
    assert backup_note([]) is None


# --- Status lines of a run ---------------------------------------------------


def test_a_real_run_marks_every_outcome_done_and_names_files_in_the_section_folder() -> None:
    outcome = UnitFileOutcome(UNIT_DIRECTORY / "a.mount", FileAction.UPDATED, Path("/x.bak-1"))

    assert report().outcome(outcome, UNIT_DIRECTORY) == "  ✓ updated    a.mount"


def test_a_path_outside_the_section_folder_is_shown_whole_with_a_tilde() -> None:
    outcome = DirectoryOutcome(Path(HOME / ".local/share/steam-shared"), FileAction.UNCHANGED)

    line = report().outcome(outcome, UNIT_DIRECTORY)

    assert line == "  ✓ unchanged  ~/.local/share/steam-shared"


def test_a_dry_run_marks_planned_changes_apart_from_what_is_already_done() -> None:
    dry_run = report(dry_run=True)
    planned = UnitFileOutcome(UNIT_DIRECTORY / "a.mount", FileAction.UPDATED)
    unchanged = UnitFileOutcome(UNIT_DIRECTORY / "b.mount", FileAction.UNCHANGED)

    assert dry_run.outcome(planned, UNIT_DIRECTORY) == "  · would update  a.mount"
    assert dry_run.outcome(unchanged, UNIT_DIRECTORY) == "  ✓ unchanged     b.mount"


def test_a_skipped_part_gets_the_attention_marker() -> None:
    assert report().skipped("--no-watch was given") == "  ! skipped    --no-watch was given"


def test_a_section_header_shortens_its_folder() -> None:
    assert report().section("Watch service", Path(HOME / ".config/systemd/user")) == (
        "Watch service  (~/.config/systemd/user)"
    )


# --- Closing -----------------------------------------------------------------


def test_closing_of_a_real_run_sums_up_backups_then_numbers_the_steps() -> None:
    notes = ClosingNotes(
        unit_outcomes=(
            UnitFileOutcome(
                UNIT_DIRECTORY / "a.mount",
                FileAction.UPDATED,
                UNIT_DIRECTORY / "a.mount.bak-20261026-183131",
            ),
        ),
        next_steps=("Make it default.", "Check it."),
        tips=(("Follow watch", "journalctl -f"),),
    )

    assert format_closing(notes, dry_run=False) == [
        "Replaced files were backed up next to themselves (*.bak-20261026-183131).",
        "",
        "Done. Next steps:",
        "  1. Make it default.",
        "  2. Check it.",
        "  Follow watch:  journalctl -f",
    ]


def test_closing_of_a_dry_run_says_nothing_changed_and_what_would_be_backed_up() -> None:
    notes = ClosingNotes(
        unit_outcomes=(UnitFileOutcome(UNIT_DIRECTORY / "a.mount", FileAction.UPDATED),),
        next_steps=("Check it.",),
    )

    lines = format_closing(notes, dry_run=True)

    assert lines[0].startswith("Replaced files would be backed up next to themselves")
    assert lines[2] == "Nothing was changed. After a real install, the next steps are:"
    assert lines[3] == "  1. Check it."


def test_closing_without_replaced_files_has_no_backup_line() -> None:
    notes = ClosingNotes(
        unit_outcomes=(UnitFileOutcome(UNIT_DIRECTORY / "a.mount", FileAction.UNCHANGED),),
        next_steps=("Check it.",),
    )

    assert format_closing(notes, dry_run=True)[0].startswith("Nothing was changed")
    assert format_closing(notes, dry_run=False)[0] == "Done. Next steps:"


def test_combined_notes_keep_the_order_of_their_parts() -> None:
    first = ClosingNotes(next_steps=("one",), tips=(("a", "x"),))
    second = ClosingNotes(next_steps=("two",))

    combined = combine_notes(first, second)

    assert combined.next_steps == ("one", "two")
    assert combined.tips == (("a", "x"),)


# --- Other commands ----------------------------------------------------------


def test_a_command_with_longer_status_words_sets_its_own_column_widths() -> None:
    wide = RunReport(
        symbols=UNICODE_SYMBOLS,
        home=HOME,
        dry_run=True,
        done_word_width=8,
        planned_word_width=13,
    )

    assert wide.planned("would disable", "a.mount") == "  · would disable  a.mount"
    assert wide.planned("would stop", "b.service") == "  · would stop     b.service"


def test_the_closing_headlines_can_be_a_commands_own() -> None:
    notes = ClosingNotes(next_steps=("Step.",))

    lines = format_closing(
        notes, dry_run=True, done_headline="All gone.", dry_run_headline="Nothing changed."
    )

    assert lines == ["Nothing changed.", "  1. Step."]
