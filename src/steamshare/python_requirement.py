"""The oldest Python steamshare runs on, and the check the `steamshare` launcher runs first.

The launcher at the repository root runs this file by path, with whatever
`python3` it finds, before it imports anything else from steamshare. A Python
that is too old then gets a message saying what to do, instead of a
SyntaxError from code it cannot read.

That is why this file is written so any Python 3 can read it: no f-strings,
and annotations in quotes, which Python never evaluates here. It imports
nothing but the standard library.

This constant is the one source of the minimum. `pyproject.toml` repeats it
in `requires-python` for packaging tools, and a unit test keeps the two equal.
"""

import sys
from collections.abc import Sequence

# CONTRIBUTING.md rule 1 promises the Python 3.11+ standard library; CI's
# matrix starts at this version.
MINIMUM_PYTHON_VERSION = (3, 11)

EXIT_OK = 0
EXIT_TOO_OLD = 1


def unsupported_python_message(version_info: "Sequence[object]") -> "str | None":
    """Return why this Python cannot run steamshare, or None when it can.

    `version_info` is shaped like `sys.version_info`: major, minor, micro, ...
    """
    if tuple(version_info[:2]) >= MINIMUM_PYTHON_VERSION:
        return None
    return (
        "steamshare needs Python "
        + _dotted(MINIMUM_PYTHON_VERSION)
        + " or newer, but the python3 found on PATH ("
        + sys.executable
        + ") is "
        + _dotted(version_info[:3])
        + ". Install a newer Python 3 from your distribution, or put a newer python3 "
        "first on PATH, then run steamshare again."
    )


def _dotted(version: "Sequence[object]") -> str:
    return ".".join(str(part) for part in version)


def main() -> int:
    message = unsupported_python_message(sys.version_info)
    if message is None:
        return EXIT_OK
    sys.stderr.write(message + "\n")
    return EXIT_TOO_OLD


if __name__ == "__main__":
    sys.exit(main())
