"""Read settings back from a unit file steamshare generated.

`uninstall` finds the units by their header, and then needs a few facts only
the files still hold: where each mount is (`Where=`), what a bind mount puts
there (`What=`), the mount's type (`Type=`), and whether the unit has an
[Install] section, which decides between disabling and only stopping it.

Only the syntax `core.systemd_units` writes is read: `[Section]` headers,
`Key=value` lines, and comments. Line continuations and drop-ins are not,
since steamshare writes neither.
"""

from pathlib import PurePosixPath

# `man systemd.syntax`: lines starting with either are comments.
COMMENT_PREFIXES = ("#", ";")
SECTION_START = "["
SECTION_END = "]"
ASSIGNMENT = "="

# systemd expands `%` specifiers in settings such as What= and Where=, so
# `systemd_units` doubles a literal `%` in a path; reading it back halves it.
ESCAPED_PERCENT = "%%"
PERCENT = "%"


def read_setting(content: str, section: str, key: str) -> str | None:
    """Return the value of `key` in `section`, or None when it is not set.

    When a key is assigned twice, the last value is returned: systemd uses
    the last one for a setting that takes a single value.
    """
    value = None
    for line_section, line_key, line_value in _assignments(content):
        if line_section == section and line_key == key:
            value = line_value
    return value


def read_path_setting(content: str, section: str, key: str) -> PurePosixPath | None:
    """Return a path setting such as Where=, as the path it names."""
    value = read_setting(content, section, key)
    if value is None:
        return None
    return PurePosixPath(value.replace(ESCAPED_PERCENT, PERCENT))


def has_section(content: str, section: str) -> bool:
    return any(_section_name(line) == section for line in _meaningful_lines(content))


def _assignments(content: str) -> list[tuple[str | None, str, str]]:
    """Return every `Key=value` line as (section, key, value), in file order."""
    assignments = []
    section = None
    for line in _meaningful_lines(content):
        name = _section_name(line)
        if name is not None:
            section = name
            continue
        key, separator, value = line.partition(ASSIGNMENT)
        if separator:
            assignments.append((section, key.strip(), value.strip()))
    return assignments


def _meaningful_lines(content: str) -> list[str]:
    stripped = (line.strip() for line in content.splitlines())
    return [line for line in stripped if line and not line.startswith(COMMENT_PREFIXES)]


def _section_name(line: str) -> str | None:
    if line.startswith(SECTION_START) and line.endswith(SECTION_END):
        return line[1:-1]
    return None
