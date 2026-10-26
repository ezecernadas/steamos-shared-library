"""Rules for file names that Windows accepts.

Why this matters: Linux can write names on NTFS that Windows forbids, such as
the `c:` entries Proton creates in `dosdevices/`. Windows and `chkdsk` then
treat those files as corruption. `doctor` uses these rules to find such names
before Windows does.

Source: Microsoft, "Naming Files, Paths, and Namespaces"
https://learn.microsoft.com/windows/win32/fileio/naming-a-file

To add a rule, write a function with the `NameRule` signature and append it to
`NAME_RULES`. Nothing else needs to change.
"""

from collections.abc import Callable
from dataclasses import dataclass

# Characters Windows reserves in any path component.
RESERVED_CHARACTERS = frozenset('<>:"/\\|?*')

# Device names Windows reserves, with or without an extension ("NUL.txt" is
# still the NUL device). Superscript digits are included because Windows
# treats them as plain digits in COM and LPT names.
RESERVED_DEVICE_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"{port}{digit}" for port in ("COM", "LPT") for digit in "123456789¹²³"}
)

# Code points 0-31 are control characters, which Windows rejects in names.
LAST_CONTROL_CHARACTER = 31


@dataclass(frozen=True)
class NameViolation:
    """A file name Windows would reject, and the reason why."""

    name: str
    reason: str


# A rule returns a human-readable reason when the name breaks it, or None.
NameRule = Callable[[str], str | None]


def has_reserved_character(name: str) -> str | None:
    found = sorted(RESERVED_CHARACTERS.intersection(name))
    if found:
        return f"contains reserved character(s): {' '.join(found)}"
    return None


def has_control_character(name: str) -> str | None:
    if any(ord(char) <= LAST_CONTROL_CHARACTER for char in name):
        return "contains a control character"
    return None


def is_reserved_device_name(name: str) -> str | None:
    base_name = name.split(".", 1)[0].upper()
    if base_name in RESERVED_DEVICE_NAMES:
        return f"is a reserved device name: {base_name}"
    return None


def has_trailing_dot_or_space(name: str) -> str | None:
    # Windows silently strips a trailing dot or space, so the name it sees
    # differs from the name on disk and the file becomes unreachable.
    if name.endswith((".", " ")):
        return "ends with a dot or a space"
    return None


NAME_RULES: tuple[NameRule, ...] = (
    has_reserved_character,
    has_control_character,
    is_reserved_device_name,
    has_trailing_dot_or_space,
)


def find_name_violation(name: str) -> NameViolation | None:
    """Return the first rule the name breaks, or None if Windows accepts it."""
    for rule in NAME_RULES:
        reason = rule(name)
        if reason is not None:
            return NameViolation(name=name, reason=reason)
    return None
