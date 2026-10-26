"""Read and write Valve's text KeyValues format (VDF).

Why this exists: Steam keeps its settings in `config.vdf` and describes each
installed app in an `appmanifest_<appid>.acf`, both in this format.
`force-proton` reads the manifests and edits `config.vdf`. CONTRIBUTING.md
rule 1 forbids the `vdf` package at runtime, so this is our own small
implementation.

A document is a dictionary that keeps Steam's key order. Each key maps to a
string or to a nested section:

    "AppState"
    {
        "appid"		"1005870"
    }

parses to `{"AppState": {"appid": "1005870"}}`.

`serialize_vdf` writes Steam's own layout (tabs for indentation, two tabs
between key and value, braces on their own lines). So rewriting a file Steam
wrote changes only the entries that were actually modified, and the backup
diffs cleanly against the new file.
"""

from dataclasses import dataclass
from enum import Enum

# A section maps keys to strings or to nested sections, in file order.
KeyValues = dict[str, "str | KeyValues"]

OPEN_BRACE = "{"
CLOSE_BRACE = "}"
QUOTE = '"'
ESCAPE = "\\"
COMMENT_MARKER = "//"

# The escapes Steam writes in quoted strings. `config.vdf` escapes the
# backslashes in Windows paths, such as `pipelines\\steam_pipeline_cache.foz`.
# Anything else is rejected rather than guessed, so a misread value is never
# written back into a file Steam owns.
ESCAPE_SEQUENCES = {ESCAPE: ESCAPE, QUOTE: QUOTE}

# Steam's layout: one tab per nesting level, two tabs between key and value.
INDENT = "\t"
KEY_VALUE_SEPARATOR = "\t\t"


class VdfSyntaxError(ValueError):
    """The text is not valid KeyValues; the message names the line."""


class TokenKind(Enum):
    STRING = "string"
    OPEN = "open brace"
    CLOSE = "close brace"


@dataclass(frozen=True)
class Token:
    kind: TokenKind
    text: str
    line: int


# --- Parsing -----------------------------------------------------------------


def parse_vdf(text: str) -> KeyValues:
    """Parse KeyValues text into nested dictionaries."""
    tokens = _tokenize(text)
    document, position = _parse_section(tokens, 0)
    if position < len(tokens):
        raise VdfSyntaxError(f"unexpected closing brace on line {tokens[position].line}")
    return document


def _parse_section(tokens: list[Token], position: int) -> tuple[KeyValues, int]:
    """Read key/value pairs until a closing brace or the end of the tokens.

    Returns the section and the position of the token that ended it, so the
    caller can tell a closed section from a truncated file.
    """
    section: KeyValues = {}
    while position < len(tokens) and tokens[position].kind is not TokenKind.CLOSE:
        key = tokens[position]
        if key.kind is not TokenKind.STRING:
            raise VdfSyntaxError(f"expected a key on line {key.line}, found {key.kind.value}")
        if position + 1 >= len(tokens):
            raise VdfSyntaxError(f"key {key.text!r} on line {key.line} has no value")
        value = tokens[position + 1]
        if value.kind is TokenKind.STRING:
            section[key.text] = value.text
            position += 2
        elif value.kind is TokenKind.OPEN:
            section[key.text], position = _parse_nested_section(tokens, position + 2, value)
        else:
            raise VdfSyntaxError(f"key {key.text!r} on line {key.line} has no value")
    return section, position


def _parse_nested_section(
    tokens: list[Token], position: int, open_brace: Token
) -> tuple[KeyValues, int]:
    section, position = _parse_section(tokens, position)
    if position >= len(tokens):
        raise VdfSyntaxError(
            f"the section opened on line {open_brace.line} is missing a closing brace"
        )
    return section, position + 1  # skip the closing brace


def _tokenize(text: str) -> list[Token]:
    tokens = []
    position = 0
    line = 1
    while position < len(text):
        character = text[position]
        if character == "\n":
            line += 1
            position += 1
        elif character.isspace():
            position += 1
        elif text.startswith(COMMENT_MARKER, position):
            position = _end_of_line(text, position)
        elif character == OPEN_BRACE:
            tokens.append(Token(TokenKind.OPEN, character, line))
            position += 1
        elif character == CLOSE_BRACE:
            tokens.append(Token(TokenKind.CLOSE, character, line))
            position += 1
        elif character == QUOTE:
            value, position = _read_quoted_string(text, position, line)
            tokens.append(Token(TokenKind.STRING, value, line))
            line += value.count("\n")
        else:
            value, position = _read_unquoted_string(text, position)
            tokens.append(Token(TokenKind.STRING, value, line))
    return tokens


def _end_of_line(text: str, position: int) -> int:
    newline = text.find("\n", position)
    return len(text) if newline == -1 else newline


def _read_quoted_string(text: str, opening_quote: int, line: int) -> tuple[str, int]:
    """Return the unescaped string and the position just after its closing quote."""
    characters = []
    position = opening_quote + 1
    while position < len(text):
        character = text[position]
        if character == QUOTE:
            return "".join(characters), position + 1
        if character == ESCAPE:
            escaped = text[position + 1 : position + 2]
            if escaped not in ESCAPE_SEQUENCES:
                raise VdfSyntaxError(f"unknown escape {ESCAPE}{escaped} on line {line}")
            characters.append(ESCAPE_SEQUENCES[escaped])
            position += 2
        else:
            characters.append(character)
            position += 1
    raise VdfSyntaxError(f"unterminated quoted string starting on line {line}")


def _read_unquoted_string(text: str, start: int) -> tuple[str, int]:
    # Steam never writes these, but hand-edited files sometimes have them. An
    # unquoted token ends at whitespace, a brace, or a quote.
    position = start
    while position < len(text) and not _ends_unquoted_string(text[position]):
        position += 1
    return text[start:position], position


def _ends_unquoted_string(character: str) -> bool:
    return character.isspace() or character in (OPEN_BRACE, CLOSE_BRACE, QUOTE)


# --- Serializing -------------------------------------------------------------


def serialize_vdf(document: KeyValues) -> str:
    """Write a document in Steam's own layout."""
    return "".join(_serialize_section(document, depth=0))


def _serialize_section(section: KeyValues, depth: int) -> list[str]:
    indent = INDENT * depth
    lines = []
    for key, value in section.items():
        if isinstance(value, str):
            lines.append(f"{indent}{_quote(key)}{KEY_VALUE_SEPARATOR}{_quote(value)}\n")
        else:
            lines.append(f"{indent}{_quote(key)}\n")
            lines.append(f"{indent}{OPEN_BRACE}\n")
            lines.extend(_serialize_section(value, depth + 1))
            lines.append(f"{indent}{CLOSE_BRACE}\n")
    return lines


def _quote(text: str) -> str:
    # The backslash goes first, so the backslashes added for quotes are not
    # escaped a second time.
    escaped = text.replace(ESCAPE, ESCAPE * 2).replace(QUOTE, ESCAPE + QUOTE)
    return f"{QUOTE}{escaped}{QUOTE}"
