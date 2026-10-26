"""Tests for the terminal console that `install` asks its questions through."""

import io

from steamshare.adapters.console import terminal_console


class FakeTerminal(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_a_terminal_on_stdin_makes_the_console_interactive() -> None:
    assert terminal_console(stdin=FakeTerminal()).is_interactive


def test_a_pipe_or_file_on_stdin_is_not_interactive() -> None:
    assert not terminal_console(stdin=io.StringIO("2\n")).is_interactive
