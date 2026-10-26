# Contributing

These rules apply to all code in this repository.

## 1. No third-party runtime dependencies

SteamOS has a read-only root filesystem. Packages installed with `pacman` or a system-wide `pip install` either fail or are wiped by the next OS update. At runtime, the tools may only use:

- Python 3.11+ standard library
- `bash`
- `systemd` and core utilities (`mount`, `modprobe`, `findmnt`)

Development and CI tools (`pytest`, `ruff`, `shellcheck`, `bats`) are allowed, but never imported or called at runtime. VDF files are parsed by our own small parser, not the `vdf` package.

## 2. Logic lives in Python; shell is a thin wrapper

Bash is only for what cannot reasonably be done elsewhere: calling `modprobe`, `mount` or `systemctl`, and bootstrapping the installer. A shell script must not contain decisions that need tests. If it grows a branch worth testing, move that logic to Python.

## 3. Light hexagonal architecture

```
src/steamshare/
  core/       pure logic: VDF parsing, Windows-name rules, which apps to force to Proton
  adapters/   I/O: filesystem, mounts, systemd, Steam process detection
  cli/        argument parsing and wiring; no business logic
```

- `core` imports nothing from `adapters` or `cli`, and does no I/O.
- Adapters implement small interfaces (`typing.Protocol`) defined next to the core code that uses them.
- Unit tests for `core` never touch the disk, the network, or real processes.

## 4. Strict TDD

1. Write a failing test.
2. Write the minimum code to make it pass.
3. Refactor with the tests green.

No new behavior without a test. A bug fix starts with a test that reproduces the bug.

- `tests/unit/`: pytest, fast, no I/O.
- `tests/integration/`: real NTFS images on loop devices, root required; runs in CI.
- Shell scripts: `bats`.

## 5. Safe by default

These tools write to Steam's config, to `/etc`, and to a disk Windows also uses.

- **Back up before writing.** Every write to a Steam or system file makes a timestamped backup first.
- **Idempotent.** Running a command twice leaves the system in the same state as running it once.
- **`--dry-run`.** Every command that changes state supports it, and it prints exactly what would change.
- **Steam must be closed.** Any command that edits Steam files refuses to run while Steam is open.
- **No deleting without confirmation.** Nothing is deleted unless the user explicitly confirms it.

## 6. Style

**Python:**
- Type hints on all public functions.
- Formatted and linted with `ruff` (`ruff format`, `ruff check`).
- Prefer `pathlib`, dataclasses and plain functions over class hierarchies.

**Bash:**
- Start with `set -euo pipefail`.
- Pass `shellcheck` with no warnings.
- Quote every expansion.

**Language:** code, comments, docs, and commit messages are in English.

## 7. Errors say what to do next

Every user-facing error states the cause and the next step. For example:

> `ntfs3` is blacklisted by `/etc/modprobe.d/zorin-blacklist-ntfs3.conf`. Run `sudo modprobe ntfs3`, or re-run the installer, which loads it at boot.

Never print a bare traceback for an expected failure.

## 8. Commits

- Use [Conventional Commits](https://www.conventionalcommits.org/): `feat:`, `fix:`, `docs:`, `test:`, `refactor:`, `chore:`.
- Keep commits small, one logical change each. Tests go in the same commit as the code they cover.
- No AI attribution or `Co-Authored-By` trailers.
