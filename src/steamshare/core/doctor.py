"""The result model shared by every `doctor` check.

Each check in `core.checks` returns one `CheckResult`. The CLI only prints
these results and turns them into an exit code, so a new check needs no
change anywhere else.
"""

from dataclasses import dataclass
from enum import Enum


class Status(Enum):
    OK = "ok"
    # Works today, but something the user should know about or clean up.
    WARNING = "warning"
    # The shared library is unsafe to use until this is fixed.
    FAILURE = "failure"


@dataclass(frozen=True)
class CheckResult:
    """What one check found, and what the user should do about it."""

    title: str
    status: Status
    details: tuple[str, ...] = ()
    # The next step for the user (CONTRIBUTING.md, rule 7). None when there
    # is nothing to do.
    hint: str | None = None
