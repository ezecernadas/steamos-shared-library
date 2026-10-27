"""Tests for turning what curses reads into the picker's keys.

Drawing needs a real terminal and is not tested here; every decision it
draws is in `core.checkbox_picker`.
"""

import curses

import pytest

from steamshare.adapters.curses_picker import translate_key
from steamshare.core.checkbox_picker import PickerKey


@pytest.mark.parametrize(
    ("code", "key"),
    [
        (curses.KEY_UP, PickerKey.UP),
        (curses.KEY_DOWN, PickerKey.DOWN),
        (curses.KEY_PPAGE, PickerKey.PAGE_UP),
        (curses.KEY_NPAGE, PickerKey.PAGE_DOWN),
        (curses.KEY_HOME, PickerKey.HOME),
        (curses.KEY_END, PickerKey.END),
        (curses.KEY_ENTER, PickerKey.ENTER),
        ("\n", PickerKey.ENTER),
        ("\r", PickerKey.ENTER),
        ("\x1b", PickerKey.ESCAPE),
        (curses.KEY_BACKSPACE, PickerKey.BACKSPACE),
        ("\x7f", PickerKey.BACKSPACE),
        ("\b", PickerKey.BACKSPACE),
    ],
)
def test_special_keys_become_picker_keys(code: int | str, key: PickerKey) -> None:
    assert translate_key(code) is key


def test_a_typed_character_stays_a_character() -> None:
    assert translate_key("é") == "é"


def test_other_function_keys_are_ignored() -> None:
    assert translate_key(curses.KEY_F1) is None
