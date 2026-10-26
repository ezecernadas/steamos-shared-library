"""The questions a command asks at the terminal: yes or no, a number, or a typed word.

Shared by `install` and `uninstall`, so both ask, re-ask and stop the same
way. What counts as a valid answer is decided in `core.prompt_answers`; this
module does the asking through a `Console`, which tests script.
"""

from steamshare.adapters.console import Console
from steamshare.cli.shared import CommandError
from steamshare.core.prompt_answers import parse_numbered_choice, parse_yes_no

# Shown after an accepted answer, so the question stands apart from what follows.
ANSWERED_SEPARATOR = ""


def ask_numbered(console: Console, question: str, option_count: int, default: int | None) -> int:
    """Ask for a number from 1 to `option_count`; return its 0-based position."""
    default_hint = f" [{default + 1}]" if default is not None else ""
    while True:
        answer = _ask(console, f"{question}{default_hint}: ")
        position = parse_numbered_choice(answer, option_count=option_count, default=default)
        if position is not None:
            console.show(ANSWERED_SEPARATOR)
            return position
        console.show(f"Please answer a number from 1 to {option_count}.")


def ask_yes_no(console: Console, question: str, *, default: bool) -> bool:
    """Ask a yes-or-no question; the capital letter in the hint marks the default."""
    options = "[Y/n]" if default else "[y/N]"
    while True:
        answer = parse_yes_no(_ask(console, f"{question} {options}: "), default=default)
        if answer is not None:
            console.show(ANSWERED_SEPARATOR)
            return answer
        console.show("Please answer y or n.")


def ask_typed_word(console: Console, question: str, word: str) -> bool:
    """Ask the user to type `word`; return whether they did.

    For a step that cannot be undone: a reflexive Enter or "y" does not go
    ahead, and anything but the word declines, without asking again.
    """
    answer = _ask(console, f"{question} ")
    console.show(ANSWERED_SEPARATOR)
    return answer.strip() == word


def _ask(console: Console, prompt: str) -> str:
    try:
        return console.ask(prompt)
    except (EOFError, KeyboardInterrupt) as error:
        # Ctrl+D, Ctrl+C, or input that ran out: an expected way to stop.
        raise CommandError(
            "Stopped at the prompt; nothing was changed. Run the command again to start over."
        ) from error
