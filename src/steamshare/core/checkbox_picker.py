"""A full-screen checkbox list, as a state machine: a key in, the next state out.

Why this exists: `keep-native` lets the user tick games in a terminal list
that can be longer than the screen and can be filtered by typing. Drawing
it needs curses, which cannot run in a unit test, so everything that decides
something (where the cursor is, what is scrolled into view, what the filter
shows, what is ticked, when the user is done) lives here, pure, and
`adapters.curses_picker` only draws the state and reads keys.

The list has two modes:

- Browsing: Up/Down (or j/k), PgUp/PgDn, Home/End move; Space ticks; Enter
  confirms; q cancels; Esc clears the filter, or cancels when there is none.
  `/`, or any other letter, starts typing a filter.
- Typing a filter: letters, space and digits go into the filter, so a name
  such as "Portal 2" can be typed whole; Backspace edits it; Enter goes back
  to browsing with the filter kept; Esc clears it. The arrows still move.

A name starting with j, k or q needs the `/` first, since those letters are
commands while browsing.

The keys each mode understands are in `BROWSING_KEYS`, `TYPING_KEYS` and
`MOVING_KEYS`; to add one, write a function from state to state and add it.
"""

from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import Enum


class PickerKey(Enum):
    """The keys that are not plain characters; the adapter maps curses codes to these."""

    UP = "up"
    DOWN = "down"
    PAGE_UP = "page up"
    PAGE_DOWN = "page down"
    HOME = "home"
    END = "end"
    ENTER = "enter"
    ESCAPE = "escape"
    BACKSPACE = "backspace"


# A key press: a named key, or one typed character.
Key = PickerKey | str


class PickerOutcome(Enum):
    CHOOSING = "choosing"
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class PickerItem:
    key: str  # what the caller identifies the item by, such as an appid
    name: str  # shown, and matched by the filter
    note: str = ""  # shown in parentheses after the name


@dataclass(frozen=True)
class PickerState:
    items: tuple[PickerItem, ...]
    checked: frozenset[str]
    # How many rows of the list fit on the screen.
    page_height: int
    filter_text: str = ""
    typing_filter: bool = False
    # Positions in `visible_items()`: the highlighted row, and the top row shown.
    cursor: int = 0
    scroll: int = 0
    outcome: PickerOutcome = PickerOutcome.CHOOSING

    def visible_items(self) -> tuple[PickerItem, ...]:
        """Return the items whose name contains the filter, ignoring case."""
        wanted = self.filter_text.casefold()
        return tuple(item for item in self.items if wanted in item.name.casefold())

    def current_item(self) -> PickerItem | None:
        visible = self.visible_items()
        return visible[self.cursor] if visible else None

    def is_checked(self, item: PickerItem) -> bool:
        return item.key in self.checked


def new_picker(
    items: tuple[PickerItem, ...], *, checked: frozenset[str], page_height: int
) -> PickerState:
    return PickerState(items=items, checked=checked, page_height=page_height)


def handle_key(state: PickerState, key: Key) -> PickerState:
    """Return the state after the key; keys the mode does not use change nothing."""
    if key in MOVING_KEYS:
        return MOVING_KEYS[key](state)
    if state.typing_filter:
        return _handle_typing_key(state, key)
    return _handle_browsing_key(state, key)


def _handle_typing_key(state: PickerState, key: Key) -> PickerState:
    if key in TYPING_KEYS:
        return TYPING_KEYS[key](state)
    if isinstance(key, str) and key.isprintable():
        return _set_filter(state, state.filter_text + key)
    return state


def _handle_browsing_key(state: PickerState, key: Key) -> PickerState:
    if key in BROWSING_KEYS:
        return BROWSING_KEYS[key](state)
    if isinstance(key, str) and key.isprintable() and not key.isspace():
        return _set_filter(replace(state, typing_filter=True), state.filter_text + key)
    return state


def resize(state: PickerState, *, page_height: int) -> PickerState:
    """Return the state for a screen of another height, the cursor still in view."""
    return _scrolled_to_cursor(replace(state, page_height=page_height))


# --- Moving --------------------------------------------------------------------


def _move(state: PickerState, rows: int) -> PickerState:
    last = max(len(state.visible_items()) - 1, 0)
    cursor = min(max(state.cursor + rows, 0), last)
    return _scrolled_to_cursor(replace(state, cursor=cursor))


def _scrolled_to_cursor(state: PickerState) -> PickerState:
    scroll = min(state.scroll, state.cursor)
    scroll = max(scroll, state.cursor - state.page_height + 1)
    return replace(state, scroll=scroll)


def _move_up(state: PickerState) -> PickerState:
    return _move(state, -1)


def _move_down(state: PickerState) -> PickerState:
    return _move(state, 1)


def _page_up(state: PickerState) -> PickerState:
    return _move(state, -state.page_height)


def _page_down(state: PickerState) -> PickerState:
    return _move(state, state.page_height)


def _go_to_first(state: PickerState) -> PickerState:
    return _move(state, -len(state.items))


def _go_to_last(state: PickerState) -> PickerState:
    return _move(state, len(state.items))


# --- Ticking, filtering and ending -----------------------------------------------


def _toggle_current(state: PickerState) -> PickerState:
    item = state.current_item()
    if item is None:
        return state
    return replace(state, checked=state.checked ^ {item.key})


def _set_filter(state: PickerState, text: str) -> PickerState:
    # The list the cursor pointed into has changed, so it starts at the top.
    return replace(state, filter_text=text, cursor=0, scroll=0)


def _start_typing(state: PickerState) -> PickerState:
    return replace(state, typing_filter=True)


def _stop_typing(state: PickerState) -> PickerState:
    return replace(state, typing_filter=False)


def _delete_last_filter_character(state: PickerState) -> PickerState:
    return _set_filter(state, state.filter_text[:-1])


def _clear_filter(state: PickerState) -> PickerState:
    return _set_filter(replace(state, typing_filter=False), "")


def _clear_filter_or_cancel(state: PickerState) -> PickerState:
    if state.filter_text:
        return _clear_filter(state)
    return _cancel(state)


def _confirm(state: PickerState) -> PickerState:
    return replace(state, outcome=PickerOutcome.CONFIRMED)


def _cancel(state: PickerState) -> PickerState:
    return replace(state, outcome=PickerOutcome.CANCELLED)


# --- The keys each mode understands ------------------------------------------------

KeyHandler = Callable[[PickerState], PickerState]

# In both modes.
MOVING_KEYS: dict[Key, KeyHandler] = {
    PickerKey.UP: _move_up,
    PickerKey.DOWN: _move_down,
    PickerKey.PAGE_UP: _page_up,
    PickerKey.PAGE_DOWN: _page_down,
    PickerKey.HOME: _go_to_first,
    PickerKey.END: _go_to_last,
}

BROWSING_KEYS: dict[Key, KeyHandler] = {
    "k": _move_up,
    "j": _move_down,
    " ": _toggle_current,
    "/": _start_typing,
    "q": _cancel,
    PickerKey.ENTER: _confirm,
    PickerKey.ESCAPE: _clear_filter_or_cancel,
    PickerKey.BACKSPACE: _delete_last_filter_character,
}

TYPING_KEYS: dict[Key, KeyHandler] = {
    PickerKey.ENTER: _stop_typing,
    PickerKey.ESCAPE: _clear_filter,
    PickerKey.BACKSPACE: _delete_last_filter_character,
}


# --- What is drawn ---------------------------------------------------------------

# Rows above the list (title, help) and below it (counts).
HEADER_ROWS = 2
FOOTER_ROWS = 1

BROWSING_HELP = (
    "Up/Down move  Space tick  / filter  Enter save  q cancel  (j/k move too; "
    "typing a name filters)"
)
TYPING_HELP = "Type to filter  Backspace edit  Enter done typing  Esc clear filter"

# Shown after the filter while it is being typed, where the text goes next.
TYPING_MARK = "_"
FOOTER_SEPARATOR = " | "


def list_height(*, screen_height: int) -> int:
    # At least one row, so a tiny terminal still shows the current item.
    return max(screen_height - HEADER_ROWS - FOOTER_ROWS, 1)


def page_rows(state: PickerState) -> list[tuple[PickerItem, bool]]:
    """Return the items on screen, top first, each with whether the cursor is on it."""
    visible = state.visible_items()
    end = state.scroll + state.page_height
    return [
        (item, position == state.cursor)
        for position, item in enumerate(visible[state.scroll : end], start=state.scroll)
    ]


def row_text(state: PickerState, item: PickerItem) -> str:
    tick = "[x]" if state.is_checked(item) else "[ ]"
    note = f"  ({item.note})" if item.note else ""
    return f"{tick} {item.name}{note}"


def help_line(state: PickerState) -> str:
    return TYPING_HELP if state.typing_filter else BROWSING_HELP


def footer_line(state: PickerState) -> str:
    parts = [
        f"{len(state.checked)} checked",
        f"{len(state.visible_items())} of {len(state.items)} shown",
    ]
    if state.filter_text or state.typing_filter:
        mark = TYPING_MARK if state.typing_filter else ""
        parts.append(f"filter: {state.filter_text}{mark}")
    return FOOTER_SEPARATOR.join(parts)
