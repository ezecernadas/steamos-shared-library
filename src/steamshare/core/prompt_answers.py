"""Read the answers a user types at a prompt.

Kept apart from the prompts themselves, which do I/O, so the rules for what
counts as a valid answer are tested without a terminal. Every parser returns
None for an answer it does not accept, and the caller asks again.
"""

YES_ANSWERS = frozenset({"y", "yes"})
NO_ANSWERS = frozenset({"n", "no"})


def parse_numbered_choice(answer: str, *, option_count: int, default: int | None) -> int | None:
    """Return the 0-based position the user picked from a list numbered from 1.

    An empty answer takes `default`, which is a 0-based position too.
    """
    text = answer.strip()
    if not text:
        return default
    if not text.isdecimal():
        return None
    number = int(text)
    if not 1 <= number <= option_count:
        return None
    return number - 1


def parse_yes_no(answer: str, *, default: bool) -> bool | None:
    text = answer.strip().lower()
    if not text:
        return default
    if text in YES_ANSWERS:
        return True
    if text in NO_ANSWERS:
        return False
    return None
