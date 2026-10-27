"""Show a `core.checkbox_picker` full screen in the terminal, with the standard `curses`.

This is only the drawing and the reading of keys: what a key does, what is
scrolled into view and what each line says are decided in
`core.checkbox_picker`, where they are tested. The caller checks that the
terminal is interactive before calling `run_picker`.
"""

import contextlib
import curses

from steamshare.core.checkbox_picker import (
    HEADER_ROWS,
    Key,
    PickerKey,
    PickerOutcome,
    PickerState,
    footer_line,
    handle_key,
    help_line,
    list_height,
    page_rows,
    resize,
    row_text,
)

# Milliseconds curses waits after Esc to tell the Esc key from the start of
# an escape sequence (an arrow key sends Esc [ A). Its default is a full
# second, which makes Esc feel broken; terminals send a sequence at once.
ESCAPE_DELAY_MILLISECONDS = 25

ESCAPE_CHARACTER = "\x1b"

# What `get_wch` returns: a string for a typed character, a number for a
# function key. Enter and Backspace arrive either way, depending on the terminal.
CURSES_KEYS: dict[int | str, PickerKey] = {
    curses.KEY_UP: PickerKey.UP,
    curses.KEY_DOWN: PickerKey.DOWN,
    curses.KEY_PPAGE: PickerKey.PAGE_UP,
    curses.KEY_NPAGE: PickerKey.PAGE_DOWN,
    curses.KEY_HOME: PickerKey.HOME,
    curses.KEY_END: PickerKey.END,
    curses.KEY_ENTER: PickerKey.ENTER,
    "\n": PickerKey.ENTER,
    "\r": PickerKey.ENTER,
    ESCAPE_CHARACTER: PickerKey.ESCAPE,
    curses.KEY_BACKSPACE: PickerKey.BACKSPACE,
    "\x7f": PickerKey.BACKSPACE,
    "\b": PickerKey.BACKSPACE,
}


def translate_key(code: int | str) -> Key | None:
    """Return the picker's key for what curses read, or None for a key it does not use."""
    if code in CURSES_KEYS:
        return CURSES_KEYS[code]
    if isinstance(code, str):
        return code
    return None


def run_picker(state: PickerState, title: str) -> PickerState:
    """Let the user pick until they confirm or cancel; return the last state.

    The terminal is restored on the way out, also after Ctrl+C, which then
    reaches the caller as `KeyboardInterrupt`.
    """
    return curses.wrapper(_pick, state, title)


def _pick(screen: "curses.window", state: PickerState, title: str) -> PickerState:
    curses.set_escdelay(ESCAPE_DELAY_MILLISECONDS)
    _hide_cursor()
    state = resize(state, page_height=list_height(screen_height=screen.getmaxyx()[0]))
    while state.outcome is PickerOutcome.CHOOSING:
        _draw(screen, state, title)
        code = screen.get_wch()
        if code == curses.KEY_RESIZE:
            state = resize(state, page_height=list_height(screen_height=screen.getmaxyx()[0]))
            continue
        key = translate_key(code)
        if key is not None:
            state = handle_key(state, key)
    return state


def _hide_cursor() -> None:
    # The highlighted row shows where the user is; some terminals cannot hide it.
    with contextlib.suppress(curses.error):
        curses.curs_set(0)


def _draw(screen: "curses.window", state: PickerState, title: str) -> None:
    screen.erase()
    height, width = screen.getmaxyx()
    _write(screen, 0, title, width, height, curses.A_BOLD)
    _write(screen, 1, help_line(state), width, height, curses.A_DIM)
    for row, (item, is_cursor) in enumerate(page_rows(state), start=HEADER_ROWS):
        _write(screen, row, row_text(state, item), width, height, _row_style(is_cursor))
    _write(screen, height - 1, footer_line(state), width, height, curses.A_DIM)
    screen.refresh()


def _row_style(is_cursor: bool) -> int:
    return curses.A_REVERSE if is_cursor else curses.A_NORMAL


def _write(
    screen: "curses.window", row: int, text: str, width: int, height: int, style: int
) -> None:
    # Cut to the width, one column short: curses fails when a write fills the
    # bottom-right cell. Rows past the bottom of a tiny terminal are skipped.
    if 0 <= row < height and width > 1:
        screen.addnstr(row, 0, text, width - 1, style)
