"""Tests for the checkbox picker's state machine: keys in, new state out.

The terminal is not involved: `adapters.curses_picker` only draws the state
and turns key codes into `PickerKey`s or typed characters.
"""

import pytest

from steamshare.core.checkbox_picker import (
    PickerItem,
    PickerKey,
    PickerOutcome,
    PickerState,
    footer_line,
    handle_key,
    help_line,
    list_height,
    new_picker,
    page_rows,
    resize,
    row_text,
)

ITEMS = (
    PickerItem("1", "BIGFOOT"),
    PickerItem("2", "Half-Life", note="installed"),
    PickerItem("3", "LIMBO"),
    PickerItem("4", "Portal"),
    PickerItem("5", "Portal 2"),
)


def picker(page_height: int = 10, checked: set[str] = frozenset()) -> PickerState:
    return new_picker(ITEMS, checked=frozenset(checked), page_height=page_height)


def press(state: PickerState, *keys: PickerKey | str) -> PickerState:
    for key in keys:
        state = handle_key(state, key)
    return state


def visible_names(state: PickerState) -> list[str]:
    return [item.name for item in state.visible_items()]


def current_name(state: PickerState) -> str | None:
    item = state.current_item()
    return None if item is None else item.name


# --- Moving ------------------------------------------------------------------------


def test_starts_on_the_first_item_with_nothing_decided() -> None:
    state = picker()

    assert current_name(state) == "BIGFOOT"
    assert state.outcome is PickerOutcome.CHOOSING


@pytest.mark.parametrize("down", [PickerKey.DOWN, "j"])
def test_down_moves_to_the_next_item(down: PickerKey | str) -> None:
    assert current_name(press(picker(), down)) == "Half-Life"


@pytest.mark.parametrize("up", [PickerKey.UP, "k"])
def test_up_moves_to_the_previous_item(up: PickerKey | str) -> None:
    assert current_name(press(picker(), PickerKey.DOWN, PickerKey.DOWN, up)) == "Half-Life"


def test_the_cursor_stops_at_both_ends() -> None:
    assert current_name(press(picker(), PickerKey.UP)) == "BIGFOOT"
    assert current_name(press(picker(), *[PickerKey.DOWN] * 9)) == "Portal 2"


def test_page_keys_move_by_one_screen() -> None:
    state = press(picker(page_height=2), PickerKey.PAGE_DOWN)
    assert current_name(state) == "LIMBO"

    assert current_name(press(state, PickerKey.PAGE_UP)) == "BIGFOOT"


def test_home_and_end_jump_to_the_first_and_last_item() -> None:
    state = press(picker(), PickerKey.END)
    assert current_name(state) == "Portal 2"

    assert current_name(press(state, PickerKey.HOME)) == "BIGFOOT"


# --- Scrolling ---------------------------------------------------------------------


def test_the_list_scrolls_to_keep_the_cursor_on_screen() -> None:
    state = press(picker(page_height=2), PickerKey.DOWN, PickerKey.DOWN)

    assert [item.name for item, _ in page_rows(state)] == ["Half-Life", "LIMBO"]

    state = press(state, PickerKey.UP, PickerKey.UP)
    assert [item.name for item, _ in page_rows(state)] == ["BIGFOOT", "Half-Life"]


def test_page_rows_mark_the_cursor_row() -> None:
    state = press(picker(page_height=3), PickerKey.DOWN)

    assert [is_cursor for _, is_cursor in page_rows(state)] == [False, True, False]


def test_shrinking_the_screen_keeps_the_cursor_visible() -> None:
    state = press(picker(page_height=10), PickerKey.END)

    state = resize(state, page_height=2)

    assert [item.name for item, _ in page_rows(state)] == ["Portal", "Portal 2"]


def test_the_list_height_leaves_room_for_the_header_and_footer() -> None:
    assert list_height(screen_height=24) == 21
    assert list_height(screen_height=2) == 1


# --- Ticking -----------------------------------------------------------------------


def test_space_toggles_the_current_item() -> None:
    state = press(picker(), PickerKey.DOWN, " ")
    assert state.checked == {"2"}

    assert press(state, " ").checked == frozenset()


def test_items_start_checked_as_given() -> None:
    assert picker(checked={"3"}).is_checked(ITEMS[2])


# --- Filtering ---------------------------------------------------------------------


def test_slash_then_typing_filters_by_name_ignoring_case() -> None:
    state = press(picker(), "/", "p", "O", "r")

    assert visible_names(state) == ["Portal", "Portal 2"]
    assert current_name(state) == "Portal"


def test_typing_a_letter_that_is_not_a_command_starts_filtering() -> None:
    state = press(picker(), "l", "i")

    assert visible_names(state) == ["Half-Life", "LIMBO"]


def test_while_typing_command_letters_and_space_are_part_of_the_filter() -> None:
    state = press(picker(), "/", "p", "o", "r", "t", "a", "l", " ", "2")

    assert visible_names(state) == ["Portal 2"]
    assert state.checked == frozenset()


def test_backspace_edits_the_filter() -> None:
    state = press(picker(), "/", "l", "x", PickerKey.BACKSPACE)

    assert state.filter_text == "l"


def test_enter_ends_typing_and_keeps_the_filter_so_space_ticks_again() -> None:
    state = press(picker(), "/", "l", "i", "m", PickerKey.ENTER, " ")

    assert state.outcome is PickerOutcome.CHOOSING
    assert visible_names(state) == ["LIMBO"]
    assert state.checked == {"3"}


def test_escape_clears_the_filter() -> None:
    state = press(picker(), "/", "l", "i", "m", PickerKey.ESCAPE)

    assert state.filter_text == ""
    assert len(visible_names(state)) == len(ITEMS)
    assert state.outcome is PickerOutcome.CHOOSING


def test_escape_clears_a_kept_filter_before_it_cancels() -> None:
    state = press(picker(), "/", "l", PickerKey.ENTER, PickerKey.ESCAPE)

    assert state.filter_text == ""
    assert state.outcome is PickerOutcome.CHOOSING


def test_ticks_survive_filtering() -> None:
    state = press(picker(), " ", "/", "l", "i", "m", PickerKey.ENTER, " ", PickerKey.ESCAPE)

    assert state.checked == {"1", "3"}


def test_a_filter_matching_nothing_has_no_current_item_and_space_does_nothing() -> None:
    state = press(picker(), "/", "z", "z", PickerKey.ENTER, " ", PickerKey.DOWN)

    assert state.current_item() is None
    assert state.checked == frozenset()


# --- Ending ------------------------------------------------------------------------


def test_enter_confirms() -> None:
    assert press(picker(), PickerKey.ENTER).outcome is PickerOutcome.CONFIRMED


@pytest.mark.parametrize("cancel", ["q", PickerKey.ESCAPE])
def test_q_or_escape_cancels(cancel: PickerKey | str) -> None:
    assert press(picker(), cancel).outcome is PickerOutcome.CANCELLED


# --- What is drawn -----------------------------------------------------------------


def test_a_row_shows_the_tick_the_name_and_the_note() -> None:
    state = picker(checked={"2"})

    assert row_text(state, ITEMS[1]) == "[x] Half-Life  (installed)"
    assert row_text(state, ITEMS[0]) == "[ ] BIGFOOT"


def test_the_help_line_follows_the_mode() -> None:
    assert "Space" in help_line(picker())
    assert "Esc" in help_line(press(picker(), "/"))


def test_the_footer_counts_the_ticked_and_shown_items() -> None:
    state = press(picker(checked={"1"}), "/", "p", "o", "r")

    assert footer_line(state) == "1 checked | 2 of 5 shown | filter: por_"
