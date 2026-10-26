"""The terminal that `install` shows its choices on and reads answers from.

The functions are fields, so tests replace them with a scripted conversation.
"""

import functools
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import TextIO


@dataclass(frozen=True)
class Console:
    # Shows a prompt and returns the line typed, without its newline.
    # Raises EOFError when input ends, as `input` does.
    ask: Callable[[str], str]
    show: Callable[[str], None]
    # False when input comes from a pipe or a file, as in scripts and CI:
    # nobody is there to answer, so a question must become an error.
    is_interactive: bool


def terminal_console(stdin: TextIO = sys.stdin) -> Console:
    # Flushed line by line: when output goes to a pipe or a log, a list shown
    # before an error must also come before it there, and errors go to the
    # unbuffered stderr.
    return Console(
        ask=input, show=functools.partial(print, flush=True), is_interactive=stdin.isatty()
    )
