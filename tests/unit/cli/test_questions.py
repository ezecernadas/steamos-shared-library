"""Tests for the questions commands ask at the terminal, with a scripted console."""

import pytest

from steamshare.adapters.console import Console
from steamshare.cli.questions import ask_numbered, ask_typed_word, ask_yes_no
from steamshare.cli.shared import CommandError


class ScriptedConsole:
    def __init__(self, answers: list[str]) -> None:
        self.answers = answers
        self.prompts: list[str] = []
        self.shown: list[str] = []

    def build(self) -> Console:
        return Console(ask=self._ask, show=self.shown.append, is_interactive=True)

    def _ask(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if not self.answers:
            raise EOFError
        return self.answers.pop(0)


@pytest.mark.parametrize(("default", "options"), [(True, "[Y/n]"), (False, "[y/N]")])
def test_an_empty_answer_takes_the_default_shown_in_capitals(default: bool, options: str) -> None:
    console = ScriptedConsole([""])

    assert ask_yes_no(console.build(), "Proceed?", default=default) is default
    assert console.prompts == [f"Proceed? {options}: "]


def test_an_unclear_answer_is_asked_again() -> None:
    console = ScriptedConsole(["maybe", "y"])

    assert ask_yes_no(console.build(), "Proceed?", default=False) is True
    assert "Please answer y or n." in console.shown


def test_a_number_out_of_range_is_asked_again() -> None:
    console = ScriptedConsole(["9", "2"])

    assert ask_numbered(console.build(), "Which?", 3, None) == 1
    assert "Please answer a number from 1 to 3." in console.shown


def test_input_ending_at_a_prompt_stops_and_says_nothing_changed() -> None:
    with pytest.raises(CommandError, match="nothing was changed"):
        ask_yes_no(ScriptedConsole([]).build(), "Proceed?", default=False)


def test_a_typed_confirmation_needs_the_exact_word() -> None:
    assert ask_typed_word(ScriptedConsole(["purge"]).build(), 'Type "purge":', "purge")
    assert ask_typed_word(ScriptedConsole([" purge "]).build(), 'Type "purge":', "purge")


def test_anything_else_typed_declines_without_asking_again() -> None:
    for answer in ["", "y", "yes", "PURGE", "purg"]:
        console = ScriptedConsole([answer, "purge"])
        assert not ask_typed_word(console.build(), 'Type "purge":', "purge")
        assert console.prompts == ['Type "purge": ']
