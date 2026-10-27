"""Tests for the `keep-native` command's wiring, with Steam and the terminal faked.

The fake picker presses a scripted list of keys through the real picker
state machine, so the tests choose games the way a user would. The fake
Steam client serves fixed games and applies the tool changes asked of it.
"""

import argparse
import json
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

import pytest

from steamshare.__main__ import build_parser
from steamshare.adapters.steam_cdp import SteamDebuggerUnavailableError
from steamshare.adapters.steam_client_api import OwnedGames
from steamshare.cli.keep_native import EXIT_FAILURE, EXIT_OK, System, keep_native
from steamshare.core.checkbox_picker import Key, PickerKey, PickerState, handle_key
from steamshare.core.live_proton_mapping import CompatToolChange, CompatToolSetting, OwnedGame
from steamshare.core.systemd_units import WatchServiceConfig, render_watch_service

TOOL = "proton_experimental"
LIBRARY = PurePosixPath("/mnt/steamlib/SteamLibrary")

UNMAPPED_NATIVE = CompatToolSetting("", 0)
STEAMSHARE_MAPPING = CompatToolSetting(TOOL, 250)
VALVE_CHOICE = CompatToolSetting("steamlinuxruntime_sniper", 100)


def owned(
    appid: str,
    name: str,
    tool: CompatToolSetting = UNMAPPED_NATIVE,
    install_folder: PurePosixPath | None = None,
) -> OwnedGame:
    return OwnedGame(
        appid,
        name,
        installed=install_folder is not None,
        install_folder=install_folder,
        compat_tool=tool,
    )


# Listed in this order: BIGFOOT, LIMBO, Portal.
LIMBO_MAPPED = owned("48000", "LIMBO", STEAMSHARE_MAPPING)
BIGFOOT = owned("509980", "BIGFOOT")
PORTAL_ELSEWHERE = owned("400", "Portal", install_folder=PurePosixPath("/home/u/Games/Portal"))
COUNTER_STRIKE = owned("730", "Counter-Strike 2", VALVE_CHOICE)


class FakeSteam:
    """A Steam client whose tool changes take effect, unless told otherwise."""

    def __init__(self, games: list[OwnedGame]) -> None:
        self.games = {game.appid: game for game in games}
        self.changes: list[tuple[str, str]] = []
        self.ignores_changes = False

    def owned_games(self, appids: frozenset[str] | None = None) -> OwnedGames:
        wanted = [game for game in self.games.values() if appids is None or game.appid in appids]
        return OwnedGames(games=tuple(wanted), without_details=())

    def specify_compat_tools(
        self, changes: Sequence[CompatToolChange]
    ) -> dict[str, CompatToolSetting | None]:
        reported = {}
        for change in changes:
            self.changes.append((change.appid, change.tool))
            game = self.games[change.appid]
            if not self.ignores_changes:
                tool = STEAMSHARE_MAPPING if change.tool else UNMAPPED_NATIVE
                game = OwnedGame(game.appid, game.name, game.installed, game.install_folder, tool)
                self.games[change.appid] = game
            reported[change.appid] = game.compat_tool
        return reported


class Setup:
    def __init__(self, tmp_path: Path, games: list[OwnedGame]) -> None:
        self.state_path = tmp_path / "steamshare" / "state.json"
        self.watch_unit_path = tmp_path / "steamshare-watch.service"
        self.steam = FakeSteam(games)
        self.steam_reachable = True
        self.interactive = True
        self.keys: list[Key] = []
        # What the picker was shown, and runs while it is open.
        self.shown: PickerState | None = None
        self.while_picking = lambda: None

    def write_state(self, **lists: list[str]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(lists))

    def state(self) -> dict:
        return json.loads(self.state_path.read_text())

    def install_watch_service(self, library: PurePosixPath) -> None:
        unit = render_watch_service(
            WatchServiceConfig(
                launcher=PurePosixPath("/repository/steamshare"),
                library=library,
                tool=TOOL,
                steam_root=PurePosixPath("/home/u/.steam/steam"),
            )
        )
        self.watch_unit_path.write_text(unit.content)

    @contextmanager
    def connect(self) -> Iterator[FakeSteam]:
        if not self.steam_reachable:
            raise SteamDebuggerUnavailableError("Steam's debugger is not answering.")
        yield self.steam

    def pick(self, state: PickerState, title: str) -> PickerState:
        self.shown = state
        self.while_picking()
        for key in self.keys:
            state = handle_key(state, key)
        return state

    def run(self, *keys: Key, library: PurePosixPath | None = LIBRARY) -> int:
        self.keys = list(keys)
        arguments = argparse.Namespace(
            library=library, tool=TOOL, steam_root=Path("/home/u/.steam/steam")
        )
        system = System(
            state_path=self.state_path,
            watch_unit_path=self.watch_unit_path,
            connect_to_steam=self.connect,
            is_interactive=self.interactive,
            pick=self.pick,
        )
        return keep_native(arguments, system)


@pytest.fixture
def setup(tmp_path: Path) -> Setup:
    return Setup(tmp_path, [LIMBO_MAPPED, BIGFOOT, PORTAL_ELSEWHERE, COUNTER_STRIKE])


def shown_names(setup: Setup) -> list[str]:
    assert setup.shown is not None
    return [item.name for item in setup.shown.items]


# --- What the picker shows -------------------------------------------------------


def test_lists_the_native_games_by_name_with_the_kept_ones_ticked(setup: Setup) -> None:
    setup.write_state(keep_native=["400"], managed_mappings=["48000"])

    setup.run(PickerKey.ENTER)

    assert shown_names(setup) == ["BIGFOOT", "LIMBO", "Portal"]
    assert setup.shown is not None
    assert setup.shown.checked == {"400"}
    assert setup.shown.items[2].note == "installed"


def test_with_no_native_game_there_is_nothing_to_pick(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    setup = Setup(tmp_path, [COUNTER_STRIKE])

    assert setup.run() == EXIT_OK
    assert setup.shown is None
    assert "No native Linux games" in capsys.readouterr().out


# --- Applying the choice ---------------------------------------------------------


def test_keeping_a_mapped_game_native_clears_steamshares_mapping_now(
    setup: Setup, capsys: pytest.CaptureFixture[str]
) -> None:
    setup.write_state(managed_mappings=["48000"])

    exit_code = setup.run(PickerKey.DOWN, " ", PickerKey.ENTER)

    assert exit_code == EXIT_OK
    assert setup.steam.changes == [("48000", "")]
    assert setup.state() == {"keep_native": ["48000"], "managed_mappings": []}
    output = capsys.readouterr().out
    # One line per game, with what Steam did.
    assert output.count("48000  LIMBO") == 1
    assert "unmapped" in output
    assert "Windows can no longer run" in output
    assert "separate Linux library" in output


def test_keeping_an_unmapped_game_native_only_saves_it(setup: Setup) -> None:
    exit_code = setup.run(" ", PickerKey.ENTER)

    assert exit_code == EXIT_OK
    assert setup.steam.changes == []
    assert setup.state()["keep_native"] == ["509980"]


def test_unticking_a_game_gives_it_back_to_proton_now(
    setup: Setup, capsys: pytest.CaptureFixture[str]
) -> None:
    setup.write_state(keep_native=["509980"])

    exit_code = setup.run(" ", PickerKey.ENTER)

    assert exit_code == EXIT_OK
    assert setup.steam.changes == [("509980", TOOL)]
    assert setup.state() == {"keep_native": [], "managed_mappings": ["509980"]}
    output = capsys.readouterr().out
    assert output.count("509980  BIGFOOT") == 1
    assert "mapped" in output


def test_a_game_given_back_but_installed_elsewhere_is_left_as_it_is(
    setup: Setup, capsys: pytest.CaptureFixture[str]
) -> None:
    setup.write_state(keep_native=["400"])

    exit_code = setup.run(PickerKey.END, " ", PickerKey.ENTER)

    assert exit_code == EXIT_OK
    assert setup.steam.changes == []
    assert setup.state()["keep_native"] == []
    assert "skipped (installed elsewhere)" in capsys.readouterr().out


def test_cancelling_changes_nothing(setup: Setup, capsys: pytest.CaptureFixture[str]) -> None:
    setup.write_state(managed_mappings=["48000"])
    before = setup.state_path.read_text()

    exit_code = setup.run(" ", "q")

    assert exit_code == EXIT_OK
    assert setup.state_path.read_text() == before
    assert setup.steam.changes == []
    assert "nothing was changed" in capsys.readouterr().out


def test_confirming_the_same_ticks_changes_nothing(
    setup: Setup, capsys: pytest.CaptureFixture[str]
) -> None:
    setup.write_state(keep_native=["400"])
    before = setup.state_path.read_text()

    assert setup.run(PickerKey.ENTER) == EXIT_OK
    assert setup.state_path.read_text() == before
    assert "Nothing to change" in capsys.readouterr().out


def test_keeps_what_watch_saved_while_the_picker_was_open(setup: Setup) -> None:
    setup.while_picking = lambda: setup.write_state(managed_mappings=["620"])

    setup.run(" ", PickerKey.ENTER)

    assert setup.state() == {"keep_native": ["509980"], "managed_mappings": ["620"]}


def test_a_change_steam_did_not_take_fails_but_the_choice_stays_saved(
    setup: Setup, capsys: pytest.CaptureFixture[str]
) -> None:
    setup.write_state(managed_mappings=["48000"])
    setup.steam.ignores_changes = True

    exit_code = setup.run(PickerKey.DOWN, " ", PickerKey.ENTER)

    assert exit_code == EXIT_FAILURE
    # watch clears the mapping on its next pass, since the game is kept native.
    assert setup.state() == {"keep_native": ["48000"], "managed_mappings": ["48000"]}
    assert "48000 LIMBO" in capsys.readouterr().err


# --- The library ----------------------------------------------------------------


def test_the_library_defaults_to_the_one_the_watch_service_uses(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    other_library = PurePosixPath("/mnt/games/Steam")
    in_other_library = owned(
        "509980", "BIGFOOT", install_folder=other_library / "steamapps/common/BIGFOOT"
    )
    setup = Setup(tmp_path, [in_other_library])
    setup.install_watch_service(other_library)
    setup.write_state(keep_native=["509980"])

    setup.run(" ", PickerKey.ENTER, library=None)

    # Installed in the shared library, so giving it back maps it.
    assert setup.steam.changes == [("509980", TOOL)]


def test_without_a_watch_service_the_library_must_be_given(
    setup: Setup, capsys: pytest.CaptureFixture[str]
) -> None:
    assert setup.run(library=None) == EXIT_FAILURE
    assert "--library" in capsys.readouterr().err


# --- When it cannot run ---------------------------------------------------------


def test_refuses_without_a_terminal_and_points_to_force_proton(
    setup: Setup, capsys: pytest.CaptureFixture[str]
) -> None:
    setup.interactive = False

    assert setup.run() == EXIT_FAILURE
    assert "force-proton" in capsys.readouterr().err
    assert setup.shown is None


def test_fails_with_the_reason_when_steam_is_not_reachable(
    setup: Setup, capsys: pytest.CaptureFixture[str]
) -> None:
    setup.steam_reachable = False

    assert setup.run() == EXIT_FAILURE
    error = capsys.readouterr().err
    assert "debugger is not answering" in error
    assert "Steam" in error


def test_the_command_is_registered_with_its_options() -> None:
    arguments = build_parser().parse_args(["keep-native"])

    assert arguments.library is None
    assert arguments.tool == TOOL
