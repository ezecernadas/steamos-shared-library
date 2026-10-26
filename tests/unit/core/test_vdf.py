"""Tests for the text KeyValues (VDF) parser and serializer.

The fixtures mirror the shape of Steam's real `config.vdf` and
`appmanifest_*.acf` files, hand-written so no account data leaks in. The
serializer must reproduce Steam's own layout byte for byte, because
`force-proton` rewrites a file Steam owns and should change only the lines it
means to change.
"""

import pytest

from steamshare.core.vdf import VdfSyntaxError, parse_vdf, serialize_vdf

# A trimmed `config.vdf`, in exactly the layout Steam writes it.
STEAM_CONFIG = (
    '"InstallConfigStore"\n'
    "{\n"
    '\t"Software"\n'
    "\t{\n"
    '\t\t"Valve"\n'
    "\t\t{\n"
    '\t\t\t"Steam"\n'
    "\t\t\t{\n"
    '\t\t\t\t"CompatToolMapping"\n'
    "\t\t\t\t{\n"
    '\t\t\t\t\t"1005870"\n'
    "\t\t\t\t\t{\n"
    '\t\t\t\t\t\t"name"\t\t"proton_experimental"\n'
    '\t\t\t\t\t\t"config"\t\t""\n'
    '\t\t\t\t\t\t"priority"\t\t"250"\n'
    "\t\t\t\t\t}\n"
    "\t\t\t\t}\n"
    '\t\t\t\t"AutoUpdateWindowEnabled"\t\t"0"\n'
    '\t\t\t\t"ShaderCacheManager"\n'
    "\t\t\t\t{\n"
    '\t\t\t\t\t"CommittedDepotManifests"\n'
    "\t\t\t\t\t{\n"
    '\t\t\t\t\t\t"1005870"\t\t"1860407_fozpipelinesv6\\\\steam_pipeline_cache.foz"\n'
    "\t\t\t\t\t}\n"
    "\t\t\t\t}\n"
    "\t\t\t}\n"
    "\t\t}\n"
    "\t}\n"
    "}\n"
)

# A trimmed `appmanifest_*.acf`, as Windows Steam writes it.
APP_MANIFEST = (
    '"AppState"\n'
    "{\n"
    '\t"appid"\t\t"1005870"\n'
    '\t"LauncherPath"\t\t"C:\\\\Program Files (x86)\\\\Steam\\\\steam.exe"\n'
    '\t"name"\t\t"HYPERFIGHT"\n'
    '\t"installdir"\t\t"HYPERFIGHT"\n'
    "}\n"
)


# --- Parsing -----------------------------------------------------------------


def test_parses_nested_sections_into_ordered_dictionaries() -> None:
    document = parse_vdf(STEAM_CONFIG)

    steam = document["InstallConfigStore"]["Software"]["Valve"]["Steam"]
    assert list(steam) == ["CompatToolMapping", "AutoUpdateWindowEnabled", "ShaderCacheManager"]
    assert steam["CompatToolMapping"]["1005870"] == {
        "name": "proton_experimental",
        "config": "",
        "priority": "250",
    }


def test_unescapes_backslashes_in_values() -> None:
    manifest = parse_vdf(APP_MANIFEST)["AppState"]

    assert manifest["LauncherPath"] == "C:\\Program Files (x86)\\Steam\\steam.exe"


def test_unescapes_quotes_inside_a_quoted_string() -> None:
    assert parse_vdf('"title"\t\t"The \\"Best\\" Game"\n') == {"title": 'The "Best" Game'}


def test_ignores_comments_and_arbitrary_whitespace() -> None:
    text = '// written by hand\n"root"   {  "a"  "1" // trailing comment\n\n   "b" "2"   }\n'

    assert parse_vdf(text) == {"root": {"a": "1", "b": "2"}}


def test_a_comment_marker_inside_a_quoted_string_is_text() -> None:
    assert parse_vdf('"url"\t\t"https://example.com"\n') == {"url": "https://example.com"}


def test_parses_unquoted_tokens() -> None:
    assert parse_vdf("root { key value }") == {"root": {"key": "value"}}


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        ('"root"\n{\n\t"a"\t\t"1"\n', "missing a closing brace"),
        ('"root"\n{\n}\n}\n', "unexpected closing brace"),
        ('"root"\t\t"unterminated\n', "unterminated quoted string"),
        ('"root"\n', "has no value"),
        ('"bad"\t\t"\\q"\n', "unknown escape"),
    ],
)
def test_rejects_malformed_text_with_the_line_number(text: str, problem: str) -> None:
    with pytest.raises(VdfSyntaxError, match=problem) as raised:
        parse_vdf(text)

    assert "line" in str(raised.value)


# --- Serializing -------------------------------------------------------------


def test_serializes_in_steams_layout() -> None:
    document = {"AppState": {"appid": "1005870", "UserConfig": {"language": "english"}}}

    assert serialize_vdf(document) == (
        '"AppState"\n'
        "{\n"
        '\t"appid"\t\t"1005870"\n'
        '\t"UserConfig"\n'
        "\t{\n"
        '\t\t"language"\t\t"english"\n'
        "\t}\n"
        "}\n"
    )


def test_serializes_an_empty_section_with_both_braces() -> None:
    assert serialize_vdf({"CompatToolMapping": {}}) == '"CompatToolMapping"\n{\n}\n'


def test_escapes_backslashes_and_quotes() -> None:
    assert serialize_vdf({'say "hi"': "C:\\Games"}) == '"say \\"hi\\""\t\t"C:\\\\Games"\n'


@pytest.mark.parametrize("text", [STEAM_CONFIG, APP_MANIFEST])
def test_reproduces_a_steam_formatted_document_byte_for_byte(text: str) -> None:
    assert serialize_vdf(parse_vdf(text)) == text


def test_parsing_a_serialized_document_gives_the_same_document() -> None:
    document = {
        "root": {
            "plain": "value",
            "tricky": 'back\\slash and "quotes" // not a comment',
            "empty": "",
            "nested": {"deeper": {"leaf": "1"}, "blank": {}},
        }
    }

    assert parse_vdf(serialize_vdf(document)) == document
