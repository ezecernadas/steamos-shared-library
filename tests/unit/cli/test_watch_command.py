"""Tests for the `watch` command's wiring, with Steam, the clock, and signals faked.

The state file lives in a temporary folder. The fake Steam client serves a
fixed list of owned games and records every tool change asked of it, so the
tests run whole passes, and the wait-and-retry loop, without a real Steam.
"""

import argparse
import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

import pytest

from steamshare.adapters.steam_cdp import SteamDebuggerError, SteamDebuggerUnavailableError
from steamshare.adapters.steam_client_api import OwnedGames
from steamshare.cli.watch import (
    CHANGES_PER_BATCH,
    EXIT_FAILURE,
    EXIT_OK,
    FIRST_RETRY_SECONDS,
    LONGEST_RETRY_SECONDS,
    System,
    next_retry_delay,
    watch,
)
from steamshare.core.live_proton_mapping import CompatToolChange, CompatToolSetting, OwnedGame
from steamshare.core.watch_pass_scope import GameOverview, LibrarySnapshot

TOOL = "proton_experimental"
LIBRARY = PurePosixPath("/mnt/steamlib/SteamLibrary")
INTERVAL = 300.0
FULL_INTERVAL = 3600.0
STEAM_SESSION = 1793032431065.8

UNMAPPED_NATIVE = CompatToolSetting("", 0)
MAPPED = CompatToolSetting(TOOL, 250)


def owned(appid: str, name: str, tool: CompatToolSetting = UNMAPPED_NATIVE) -> OwnedGame:
    return OwnedGame(appid, name, installed=False, install_folder=None, compat_tool=tool)


LIMBO = owned("48000", "LIMBO")
BIGFOOT = owned("509980", "BIGFOOT")
VALVE_CHOICE = CompatToolSetting("steamlinuxruntime_sniper", 100)
COUNTER_STRIKE = owned("730", "Counter-Strike 2", VALVE_CHOICE)


class FakeSteam:
    """A Steam client whose tool changes take effect unless told otherwise."""

    def __init__(self, games: list[OwnedGame]) -> None:
        self.games = games
        self.session = STEAM_SESSION
        # Which games each details read asked for; None means every game.
        self.details_reads: list[frozenset[str] | None] = []
        # Each batch of changes asked of Steam, as (appid, tool) pairs.
        self.batches: list[list[tuple[str, str]]] = []
        # What Steam reports after a change; by default, the requested tool.
        self.reported_after: CompatToolSetting | None = None
        # Games whose details do not arrive after a change.
        self.silent_after_change: set[str] = set()
        # Runs while Steam applies a batch, as another command saving the state would.
        self.during_change: Callable[[], None] = lambda: None

    @property
    def changes(self) -> list[tuple[str, str]]:
        return [change for batch in self.batches for change in batch]

    def library_snapshot(self) -> LibrarySnapshot:
        overviews = frozenset(GameOverview(game.appid, game.installed) for game in self.games)
        return LibrarySnapshot(client_session=self.session, games=overviews)

    def owned_games(self, appids: frozenset[str] | None = None) -> OwnedGames:
        self.details_reads.append(appids)
        wanted = [game for game in self.games if appids is None or game.appid in appids]
        return OwnedGames(games=tuple(wanted), without_details=())

    def specify_compat_tools(
        self, changes: list[CompatToolChange]
    ) -> dict[str, CompatToolSetting | None]:
        self.batches.append([(change.appid, change.tool) for change in changes])
        self.during_change()
        return {change.appid: self._reported(change) for change in changes}

    def _reported(self, change: CompatToolChange) -> CompatToolSetting | None:
        if change.appid in self.silent_after_change:
            return None
        if self.reported_after is not None:
            return self.reported_after
        return MAPPED if change.tool else UNMAPPED_NATIVE


class Setup:
    def __init__(self, tmp_path: Path, games: list[OwnedGame]) -> None:
        self.state_path = tmp_path / "steamshare" / "state.json"
        self.steam = FakeSteam(games)
        # Each connection attempt takes the next outcome; past the end, Steam is up.
        self.connection_outcomes: list[Exception | None] = []
        self.waits: list[float] = []
        # A fake clock that only moves while waiting.
        self.now = 0.0
        # How many waits end normally; the next one sees a stop request.
        self.waits_before_stop = 0
        self.stop_after_changes: int | None = None

    def write_state(self, **lists: list[str]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(lists))

    def state(self) -> dict:
        return json.loads(self.state_path.read_text())

    @contextmanager
    def connect(self) -> Iterator[FakeSteam]:
        if self.connection_outcomes:
            outcome = self.connection_outcomes.pop(0)
            if outcome is not None:
                raise outcome
        yield self.steam

    def wait(self, seconds: float) -> bool:
        self.waits.append(seconds)
        self.now += seconds
        return len(self.waits) > self.waits_before_stop

    def stop_requested(self) -> bool:
        return (
            self.stop_after_changes is not None
            and len(self.steam.changes) >= self.stop_after_changes
        )

    def run(self, **overrides: object) -> int:
        values: dict[str, object] = {
            "library": LIBRARY,
            "tool": TOOL,
            "interval": INTERVAL,
            "full_interval": FULL_INTERVAL,
            "once": True,
            "dry_run": False,
            "verbose": False,
            "steam_root": Path("/home/u/.steam/steam"),
        }
        values.update(overrides)
        system = System(
            state_path=self.state_path,
            connect_to_steam=self.connect,
            wait=self.wait,
            stop_requested=self.stop_requested,
            clock=lambda: self.now,
        )
        return watch(argparse.Namespace(**values), system)


@pytest.fixture
def setup(tmp_path: Path) -> Setup:
    return Setup(tmp_path, [LIMBO, COUNTER_STRIKE])


# --- One pass ------------------------------------------------------------------


def test_maps_native_games_and_records_them_as_steamshares(
    setup: Setup, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = setup.run()

    assert exit_code == EXIT_OK
    assert setup.steam.changes == [("48000", TOOL)]
    assert setup.state()["managed_mappings"] == ["48000"]
    output = capsys.readouterr().out
    assert "2 games: 1 map, 1 skipped (Valve's choice)" in output
    assert "mapped" in output and "48000  LIMBO" in output


def test_a_dry_run_asks_steam_for_nothing_and_writes_nothing(
    setup: Setup, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = setup.run(dry_run=True)

    assert exit_code == EXIT_OK
    assert setup.steam.changes == []
    assert not setup.state_path.exists()
    output = capsys.readouterr().out
    assert "would map" in output and "48000  LIMBO" in output
    assert "Dry run" in output


def test_skipped_games_are_listed_only_when_verbose(
    setup: Setup, capsys: pytest.CaptureFixture[str]
) -> None:
    setup.run(dry_run=True)
    assert "Counter-Strike 2" not in capsys.readouterr().out

    setup.run(dry_run=True, verbose=True)
    assert "Counter-Strike 2" in capsys.readouterr().out


def test_a_change_steam_did_not_take_fails_the_pass_and_is_not_recorded(
    setup: Setup, capsys: pytest.CaptureFixture[str]
) -> None:
    # Steam silently falls back to its default for a tool it does not know.
    setup.steam.reported_after = UNMAPPED_NATIVE

    exit_code = setup.run(tool="proton_typo")

    assert exit_code == EXIT_FAILURE
    assert not setup.state_path.exists()
    error = capsys.readouterr().err
    assert "proton_typo" in error
    assert "--tool" in error


def test_removes_steamshares_mapping_from_a_game_kept_native(tmp_path: Path) -> None:
    setup = Setup(tmp_path, [owned("48000", "LIMBO", MAPPED)])
    setup.write_state(keep_native=["48000"], managed_mappings=["48000"])

    exit_code = setup.run()

    assert exit_code == EXIT_OK
    assert setup.steam.changes == [("48000", "")]
    assert setup.state() == {"keep_native": ["48000"], "managed_mappings": []}


def test_forgets_a_mapping_the_user_took_over(tmp_path: Path) -> None:
    setup = Setup(tmp_path, [owned("48000", "LIMBO", CompatToolSetting("GE-Proton9-20", 250))])
    setup.write_state(managed_mappings=["48000"])

    setup.run()

    assert setup.steam.changes == []
    assert setup.state()["managed_mappings"] == []


def test_a_pass_keeps_what_another_command_saved_while_it_ran(setup: Setup) -> None:
    # keep-native saves the state after the pass read it, before the pass saves.
    setup.steam.during_change = lambda: setup.write_state(keep_native=["620"])

    setup.run()

    assert setup.state() == {"keep_native": ["620"], "managed_mappings": ["48000"]}


def test_a_second_pass_changes_nothing(tmp_path: Path) -> None:
    setup = Setup(tmp_path, [owned("48000", "LIMBO", MAPPED)])
    setup.write_state(managed_mappings=["48000"])
    before = setup.state_path.read_text()

    assert setup.run() == EXIT_OK
    assert setup.steam.changes == []
    assert setup.state_path.read_text() == before


def test_once_fails_with_the_reason_when_steam_is_not_reachable(
    setup: Setup, capsys: pytest.CaptureFixture[str]
) -> None:
    setup.connection_outcomes = [SteamDebuggerUnavailableError("restart Steam once")]

    exit_code = setup.run()

    assert exit_code == EXIT_FAILURE
    assert "restart Steam once" in capsys.readouterr().err


def test_a_broken_state_file_stops_the_pass_with_a_hint(
    setup: Setup, capsys: pytest.CaptureFixture[str]
) -> None:
    setup.state_path.parent.mkdir(parents=True)
    setup.state_path.write_text("{not json")

    assert setup.run() == EXIT_FAILURE
    assert "move the file away" in capsys.readouterr().err


def many_native_games(count: int) -> list[OwnedGame]:
    return [owned(str(100000 + number), f"Game {number}") for number in range(count)]


def test_changes_are_asked_of_steam_in_batches(tmp_path: Path) -> None:
    setup = Setup(tmp_path, many_native_games(CHANGES_PER_BATCH + 1))

    assert setup.run() == EXIT_OK

    assert [len(batch) for batch in setup.steam.batches] == [CHANGES_PER_BATCH, 1]
    assert len(setup.state()["managed_mappings"]) == CHANGES_PER_BATCH + 1


def test_a_stop_request_ends_the_pass_between_batches_keeping_what_was_done(
    tmp_path: Path,
) -> None:
    setup = Setup(tmp_path, many_native_games(CHANGES_PER_BATCH + 1))
    setup.stop_after_changes = CHANGES_PER_BATCH

    setup.run()

    assert len(setup.steam.batches) == 1
    assert len(setup.state()["managed_mappings"]) == CHANGES_PER_BATCH


def test_only_the_changes_steam_confirmed_in_a_batch_are_recorded(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    setup = Setup(tmp_path, [LIMBO, BIGFOOT])
    setup.steam.silent_after_change = {"509980"}

    exit_code = setup.run()

    assert exit_code == EXIT_FAILURE
    assert setup.steam.batches == [[("48000", TOOL), ("509980", TOOL)]]
    assert setup.state()["managed_mappings"] == ["48000"]
    output = capsys.readouterr()
    assert "48000  LIMBO" in output.out
    assert "509980 BIGFOOT" in output.err and "did not report" in output.err


# --- The loop ------------------------------------------------------------------


def test_waits_for_steam_then_runs_a_pass_every_interval(
    setup: Setup, capsys: pytest.CaptureFixture[str]
) -> None:
    setup.connection_outcomes = [
        SteamDebuggerUnavailableError("Steam is closed"),
        SteamDebuggerUnavailableError("Steam is closed"),
    ]
    setup.waits_before_stop = 2

    exit_code = setup.run(once=False)

    assert exit_code == EXIT_OK
    assert setup.waits == [FIRST_RETRY_SECONDS, FIRST_RETRY_SECONDS * 2, INTERVAL]
    output = capsys.readouterr().out
    # The reason is logged once per outage, not once per retry.
    assert output.count("Steam is closed") == 1


def test_goes_back_to_waiting_when_steam_closes(setup: Setup) -> None:
    setup.connection_outcomes = [None, SteamDebuggerUnavailableError("closed")]
    setup.waits_before_stop = 1

    setup.run(once=False)

    assert setup.waits == [INTERVAL, FIRST_RETRY_SECONDS]


def test_any_other_failure_is_logged_and_retried_after_the_interval(
    setup: Setup, capsys: pytest.CaptureFixture[str]
) -> None:
    setup.connection_outcomes = [SteamDebuggerError("Steam did not answer")]

    setup.run(once=False)

    assert setup.waits == [INTERVAL]
    assert "Steam did not answer" in capsys.readouterr().err


def test_the_retry_delay_doubles_up_to_a_limit() -> None:
    assert next_retry_delay(None) == FIRST_RETRY_SECONDS
    assert next_retry_delay(FIRST_RETRY_SECONDS) == FIRST_RETRY_SECONDS * 2
    assert next_retry_delay(LONGEST_RETRY_SECONDS) == LONGEST_RETRY_SECONDS


# --- Passes after the first ----------------------------------------------------


def test_a_pass_over_an_unchanged_library_reads_no_details_and_prints_nothing(
    setup: Setup, capsys: pytest.CaptureFixture[str]
) -> None:
    setup.waits_before_stop = 1

    setup.run(once=False)

    assert setup.steam.details_reads == [None]
    output = capsys.readouterr().out
    assert output.count("games:") == 1


def test_a_new_game_is_the_only_one_read_and_mapped(
    setup: Setup, capsys: pytest.CaptureFixture[str]
) -> None:
    setup.waits_before_stop = 1
    original_wait = setup.wait

    def buy_bigfoot_while_waiting(seconds: float) -> bool:
        setup.steam.games = [LIMBO, COUNTER_STRIKE, BIGFOOT]
        return original_wait(seconds)

    setup.wait = buy_bigfoot_while_waiting  # type: ignore[method-assign]

    setup.run(once=False)

    assert setup.steam.details_reads == [None, frozenset({"509980"})]
    assert setup.steam.changes == [("48000", TOOL), ("509980", TOOL)]
    assert "1 changed games: 1 map" in capsys.readouterr().out


def test_every_game_is_read_again_once_the_full_interval_has_passed(tmp_path: Path) -> None:
    setup = Setup(tmp_path, [LIMBO])
    setup.waits_before_stop = int(FULL_INTERVAL // INTERVAL)

    setup.run(once=False)

    full_reads = [read for read in setup.steam.details_reads if read is None]
    assert len(full_reads) == 2


def test_every_game_is_read_again_after_steam_comes_back(setup: Setup) -> None:
    setup.connection_outcomes = [None, SteamDebuggerUnavailableError("closed")]
    setup.waits_before_stop = 2

    setup.run(once=False)

    assert setup.steam.details_reads == [None, None]


def test_every_game_is_read_again_after_steam_restarted(setup: Setup) -> None:
    setup.waits_before_stop = 1
    original_wait = setup.wait

    def restart_steam_while_waiting(seconds: float) -> bool:
        setup.steam.session += 1
        return original_wait(seconds)

    setup.wait = restart_steam_while_waiting  # type: ignore[method-assign]

    setup.run(once=False)

    assert setup.steam.details_reads == [None, None]


def test_a_change_steam_did_not_confirm_is_tried_again_next_pass(tmp_path: Path) -> None:
    setup = Setup(tmp_path, [LIMBO, BIGFOOT])
    setup.steam.silent_after_change = {"509980"}
    setup.waits_before_stop = 1

    setup.run(once=False)

    assert setup.steam.details_reads == [None, frozenset({"509980"})]
    assert setup.steam.changes == [("48000", TOOL), ("509980", TOOL), ("509980", TOOL)]


def test_a_dry_run_plans_the_same_changes_again_next_pass(setup: Setup) -> None:
    setup.waits_before_stop = 1

    setup.run(once=False, dry_run=True)

    assert setup.steam.details_reads == [None, frozenset({"48000"})]
    assert setup.steam.changes == []
